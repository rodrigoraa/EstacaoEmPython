import importlib
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


class PublicAlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "ESTACAO_DB": str(Path(self.tmp.name) / "test.db"),
            "NOWCASTING_TEST_ALERTS_ENABLED": "false",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        import database
        from services import nowcasting_public_alerts
        self.db = importlib.reload(database)
        self.db.init_db()
        self.service = importlib.reload(nowcasting_public_alerts)
        self.now = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
        self.config = {"alerts_enabled": True, "alert_cooldown_minutes": 60,
                       "alert_rearm_minutes": 30, "poll_seconds": 300}
        conn = self.db.get_db()
        for i, (ativo, optin, status) in enumerate([
            (1, 1, "ativo"), (1, 0, "ativo"), (0, 1, "ativo"),
            (1, 1, "pendente"), (1, 0, "pendente"),
            (None, 1, None), (1, 1, "cancelado"),
        ]):
            conn.execute("INSERT INTO usuarios (nome, telefone, ativo, receber_whatsapp, status_cadastro) VALUES (?, ?, ?, ?, ?)",
                         (f"Pessoa {i}", f"6791234567{i}", ativo, optin, status))
        conn.commit()
        conn.close()

    def snapshot(self, intensity="MEDIUM", minute=0, distance=10, tracking=False):
        return {
            "gerado_em_utc": (self.now + timedelta(minutes=minute)).isoformat(),
            "evento_local_observado": False,
            "escola": {"rain_rate": 0, "stale": False},
            "radar": {"stale": False, "operacional": True, "frame_id": 70,
                      "data_frame": (self.now + timedelta(minutes=minute)).isoformat()},
            "alerta_preventivo": {
                "nivel": "VERMELHO", "distance_km": distance,
                "front_pixels_low": 100 if intensity == "LOW" else 0,
                "front_pixels_medium": 100 if intensity == "MEDIUM" else 0,
                "front_pixels_high": 100 if intensity == "HIGH" else 0,
                "front_pixels_very_high": 100 if intensity == "VERY_HIGH" else 0,
                "track_id": 27 if tracking else None, "tracking_valid": tracking,
                "approaching": tracking, "trajectory_compatible": tracking,
                "clutter": False, "eta_border_minutes": 25,
                "eta_border_quality": "BOA",
            },
        }

    def process(self, snapshot=None, minute=0, config=None):
        return self.service.processar_alerta_publico(
            snapshot or self.snapshot(minute=minute), config or self.config,
            now=self.now + timedelta(minutes=minute))

    def rows(self, table):
        conn = self.db.get_db()
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        finally:
            conn.close()

    def test_flag_optin_payload_idempotencia_restart(self):
        self.process(config={**self.config, "alerts_enabled": False})
        self.assertEqual(self.rows("alertas_eventos"), [])
        self.assertEqual(self.rows("alertas_fila"), [])
        self.assertEqual(self.rows("health_check_estado"), [])
        result = self.process()
        self.assertEqual(result["enfileirados"], 2)
        self.process()
        importlib.reload(self.service)
        self.process()
        self.assertEqual(len(self.rows("alertas_eventos")), 1)
        fila = self.rows("alertas_fila")
        self.assertEqual([r["usuario_id"] for r in fila], [1, 6])
        self.assertEqual([r["nome"] for r in fila], ["Pessoa 0", "Pessoa 5"])
        self.assertEqual(fila[0]["telefone"], "5567912345670")
        self.assertEqual(fila[0]["prioridade"], 50)
        event = self.rows("alertas_eventos")[0]
        self.assertEqual(event["tipo"], "nowcasting_radar")
        self.assertEqual(event["valor"], 10)
        self.assertEqual(event["unidade"], "km")
        self.assertEqual(event["fonte"], "redemet_jaraguari_nowcasting")
        self.assertEqual(event["ocorrido_em_local"], "2026-09-14T08:00:00-04:00")
        self.assertEqual(fila[0]["evento_id"], event["evento_id"])

    def test_escalonamento_identidade_prioridades(self):
        identities = []
        for i, intensity in enumerate(["MEDIUM", "MEDIUM", "HIGH", "HIGH", "VERY_HIGH", "HIGH", "MEDIUM"]):
            snap = self.snapshot(intensity, minute=i, tracking=i % 2 == 1)
            snap["alerta_preventivo"]["cluster_id"] = i
            identities.append(self.process(snap, minute=i)["state"]["episode_id"])
        self.assertEqual(len(set(identities)), 1)
        self.assertEqual([r["nivel"] for r in self.rows("alertas_eventos")], [1, 2, 3])
        self.assertEqual([r["prioridade"] for r in self.rows("alertas_fila")], [50, 50, 80, 80, 100, 100])
        self.assertEqual([r["evento_id"].split(":")[-1] for r in self.rows("alertas_eventos")],
                         ["informativo", "atencao", "alerta"])

    def test_rearm_cooldown_novo_episodio(self):
        old = self.process()["state"]["episode_id"]
        self.process(self.snapshot("LOW", minute=1), minute=1)
        result = self.process(self.snapshot("LOW", minute=31), minute=31)
        self.assertEqual(result["reason"], "rearmed")
        self.assertFalse(result["state"]["active"])
        self.assertIsNone(result["state"]["episode_id"])
        result = self.process(minute=32)
        self.assertEqual(result["reason"], "cooldown")
        new = result["state"]["episode_id"]
        self.assertNotEqual(old, new)
        result = self.process(minute=60)
        self.assertEqual(result["enfileirados"], 2)
        self.assertEqual(result["state"]["episode_id"], new)

    def test_interrupcoes_nao_confirmam_ausencia(self):
        old = self.process()["state"]["episode_id"]
        for change in ["stale", "unavailable", "clutter", "tracking", "snapshot"]:
            with self.subTest(change=change):
                self.process(self.snapshot("LOW", minute=1), minute=1)
                snap = self.snapshot(minute=31, distance=40)
                if change == "stale": snap["radar"]["stale"] = True
                if change == "unavailable": snap["radar"]["operacional"] = False
                if change == "clutter": snap["alerta_preventivo"]["clutter"] = True
                if change == "snapshot": snap["gerado_em_utc"] = "invalid"
                result = self.process(snap, minute=31)
                self.assertIsNone(result["state"]["clear_since"])
                self.assertEqual(result["state"]["episode_id"], old)

    def test_bloqueios_meteorologicos(self):
        mutations = [
            ("radar", "stale", True), ("radar", "operacional", False),
            ("radar", "timestamp_status", "suspect"), ("radar", "frame_id", None),
            ("alerta_preventivo", "clutter", True),
            ("alerta_preventivo", "front_pixels_medium", -1),
            ("alerta_preventivo", "front_pixels_medium", 1),
            ("alerta_preventivo", "distance_km", 110),
            ("alerta_preventivo", "tracking_valid", False),
            ("alerta_preventivo", "track_id", None),
            ("alerta_preventivo", "approaching", False),
            ("alerta_preventivo", "trajectory_compatible", False),
            ("escola", "rain_rate", 2),
        ]
        for section, key, value in mutations:
            with self.subTest(key=key):
                snap = self.snapshot(distance=40, tracking=True)
                snap[section][key] = value
                self.assertFalse(self.process(snap)["enfileirados"])
        self.assertEqual(self.process(self.snapshot("LOW"))["enfileirados"], 0)
        self.assertEqual(self.rows("alertas_eventos"), [])
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_rotas_e_leitura_stale(self):
        for intensity, distance, tracking in [("MEDIUM", 20, False), ("MEDIUM", 40, True),
                                               ("HIGH", 30, False), ("VERY_HIGH", 45, False)]:
            with self.subTest(intensity=intensity, tracking=tracking):
                # Rearm e cooldown entre episódios, sem apagar qualquer histórico.
                minute = len(self.rows("alertas_eventos")) * 100
                if minute:
                    self.process(self.snapshot("LOW", minute=minute-31), minute=minute-31)
                    self.process(self.snapshot("LOW", minute=minute-1), minute=minute-1)
                snap = self.snapshot(intensity, minute, distance, tracking)
                snap["escola"].update(rain_rate=2, stale=True)
                self.assertEqual(self.process(snap, minute)["enfileirados"], 2)

    def test_chuva_local_suprime_escalonamento_ate_rearm(self):
        self.process()
        snap = self.snapshot("HIGH", minute=1)
        snap["evento_local_observado"] = True
        self.process(snap, minute=1)
        self.assertEqual(self.process(self.snapshot("VERY_HIGH", minute=2), minute=2)["reason"], "local_event_observed")
        self.assertEqual(len(self.rows("alertas_eventos")), 1)

    def test_mensagens_tracking_eta_link(self):
        from services.nowcasting_alert_evaluation import montar_mensagem_preventiva
        for level in ("INFORMATIVO", "ATENCAO", "ALERTA"):
            for tracking in (False, True):
                for eta, quality in [(25, "BOA"), (25, "MODERADA"), (None, "BOA"),
                                     (float("nan"), "BOA"), (-1, "BOA"), (25, "RUIM")]:
                    snap = self.snapshot(tracking=tracking)
                    snap["alerta_preventivo"].update(alert_level=level, eta_border_minutes=eta, eta_border_quality=quality)
                    msg = montar_mensagem_preventiva(snap)
                    self.assertEqual(msg.count("Distrito de São José"), 1)
                    self.assertEqual(msg.count("https://meteo.eesjv.com.br"), 1)
                    self.assertEqual("se aproximando" in msg, tracking)
                    self.assertEqual(msg.count("Estimativa de chegada:"), int(tracking and eta == 25 and quality in {"BOA", "MODERADA"}))
                    for term in ("eco", "refletividade", "cluster", "tracking", "célula", "maxcappi", "frame", "ee são josé", "escola estadual são josé", "vai chover"):
                        self.assertNotIn(term, msg.lower())
        self.process()
        msg = self.rows("alertas_fila")[0]["mensagem"]
        self.assertTrue(msg.startswith("ATENÇÃO, Pessoa 0,\n"))
        self.assertEqual(msg.count("Distrito de São José"), 1)
        self.assertEqual(msg.count("https://meteo.eesjv.com.br"), 1)

    def test_rollback_fanout_e_estado(self):
        original = self.service.salvar_estado
        with mock.patch.object(self.service, "salvar_estado", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError): self.process()
        for table in ("alertas_eventos", "alertas_fila", "health_check_estado"):
            self.assertEqual(self.rows(table), [])
        self.assertEqual(self.process()["enfileirados"], 2)
        self.assertIs(self.service.salvar_estado, original)

    def test_rollback_erro_segundo_destinatario(self):
        conn = self.db.get_db()
        conn.execute("""CREATE TRIGGER falha_teste BEFORE INSERT ON alertas_fila
                        WHEN NEW.usuario_id = 6 BEGIN SELECT RAISE(ABORT, 'test'); END""")
        conn.commit()
        conn.close()
        with self.assertRaises(Exception): self.process()
        for table in ("alertas_eventos", "alertas_fila", "health_check_estado"):
            self.assertEqual(self.rows(table), [])

    def test_estado_corrompido_falha_fechado(self):
        self.process()
        conn = self.db.get_db()
        conn.execute("UPDATE health_check_estado SET mensagem=? WHERE chave=?",
                     (json.dumps({"active": False}), self.service.ESTADO_CHAVE))
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError): self.process(self.snapshot("HIGH"))
        self.assertEqual(len(self.rows("alertas_eventos")), 1)
        self.assertEqual(self.service.obter_status_alerta_publico(self.config)["last_result"], "status_unavailable")

    def test_concorrencia(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.process(), range(2)))
        self.assertEqual(sum(r["enfileirados"] for r in results), 2)
        self.assertEqual(len(self.rows("alertas_eventos")), 1)

    def test_ciclo_publico_suprime_admin_sem_http(self):
        from workers import nowcasting_updater as worker
        snap = self.snapshot()
        snap.update(estacoes_relevantes=[], status="NORMAL", nivel_evidencia="BAIXA")
        snap["alerta_preventivo"]["would_send"] = True
        with (mock.patch.object(worker, "carregar_entradas_nowcasting", return_value=({}, {}, {}, "test")),
              mock.patch.object(worker, "analisar_nowcasting", return_value=snap),
              mock.patch.object(worker, "salvar_snapshot", return_value=1),
              mock.patch.object(self.service, "agora_utc", return_value=self.now),
              mock.patch("services.whatsapp_service.enviar_whatsapp") as enviar,
              mock.patch("services.nowcasting_test_alerts.enviar_mensagem_admin") as admin):
            result = worker.executar_ciclo({**self.config, "enabled": True, "test_alerts_enabled": True})
        enviar.assert_not_called()
        admin.assert_not_called()
        self.assertEqual(result["public_enqueued"], 2)
        self.assertEqual(result["test_alert"]["reason"], "public_alerts_enabled")

    def test_config_defaults(self):
        from config import nowcasting_config
        with mock.patch.dict(os.environ, {}, clear=True):
            config = nowcasting_config()
        self.assertFalse(config["alerts_enabled"])
        self.assertEqual(config["alert_cooldown_minutes"], 60)
        self.assertEqual(config["alert_rearm_minutes"], 30)

    def test_snapshot_invalido_interrompe_rearm(self):
        self.process()
        self.process(self.snapshot("LOW", minute=1), minute=1)
        result = self.process({"radar": "invalid"}, minute=31)
        self.assertEqual(result["reason"], "invalid_snapshot")
        self.assertTrue(result["state"]["active"])
        self.assertIsNone(result["state"]["clear_since"])
        self.assertEqual(len(self.rows("alertas_eventos")), 1)

    def test_snapshot_antigo_ou_futuro_nao_enfileira(self):
        for minute in (-60, 5):
            result = self.process(self.snapshot(minute=minute))
            self.assertEqual(result["reason"], "snapshot_stale")
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_estado_json_invalido_nao_e_resetado(self):
        conn = self.db.get_db()
        conn.execute("INSERT INTO health_check_estado (chave, status, mensagem) VALUES (?, 'active', '{invalid')",
                     (self.service.ESTADO_CHAVE,))
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError): self.process()
        self.assertEqual(self.rows("alertas_eventos"), [])
        self.assertEqual(self.rows("health_check_estado")[0]["mensagem"], "{invalid")
