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
        self.config = {"alerts_enabled": True, "poll_seconds": 300}
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

    def snapshot(self, intensity="MEDIUM", minute=0, distance=10, tracking=True):
        return {
            "gerado_em_utc": (self.now + timedelta(minutes=minute)).isoformat(),
            "evento_local_observado": False,
            "escola": {"rain_rate": 0, "stale": False},
            "radar": {"stale": False, "operacional": True, "frame_id": 1000 + minute*10,
                      "data_frame": (self.now + timedelta(minutes=minute)).isoformat()},
            "alerta_preventivo": {
                "nivel": "VERMELHO", "distance_km": distance,
                "front_pixels_low": 100 if intensity == "LOW" else 0,
                "front_pixels_medium": 100 if intensity == "MEDIUM" else 0,
                "front_pixels_high": 100 if intensity == "HIGH" else 0,
                "front_pixels_very_high": 100 if intensity == "VERY_HIGH" else 0,
                "track_id": 27, "tracking_valid": tracking,
                "approaching": tracking, "trajectory_compatible": tracking,
                "closest_approach_km": 13 if tracking else None,
                "trajectory_frames_used": 5 if tracking else 1,
                "trajectory_method": "linear_xy_6_pixel_runs",
                "trajectory_duration_minutes": 20, "trajectory_confidence": "ALTA" if tracking else "BAIXA",
                "projected_impact_min_distance_km": 3, "projected_impact": tracking, "projected_impact_horizon_minutes": 120, "projected_impact_eta_minutes": 25 if tracking else None,
                "clutter": False, "eta_border_minutes": 25,
                "eta_border_quality": "BOA",
            },
        }

    def process(self, snapshot=None, minute=0, config=None):
        return self.service.processar_alerta_publico(
            snapshot or self.snapshot(minute=minute), config or self.config,
            now=self.now + timedelta(minutes=minute))

    def confirmed_medium(self, minute=0, distance=10, config=None, intensity="MEDIUM"):
        first = self.snapshot(intensity, minute, distance)
        first["radar"].update(frame_id=1000+minute*10,
                              data_frame=(self.now+timedelta(minutes=minute-5)).isoformat())
        previous = self.process(first, minute, config)
        second = self.snapshot(intensity, minute, distance)
        second["radar"]["frame_id"] = 1001+minute*10
        result = self.process(second, minute, config)
        result["enfileirados"] += previous["enfileirados"]
        return result

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
        result = self.confirmed_medium()
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
            snap = self.snapshot(intensity, minute=i, tracking=True)
            snap["radar"]["frame_id"] = 70 + i
            snap["alerta_preventivo"]["cluster_id"] = i
            identities.append(self.process(snap, minute=i)["state"]["episode_id"])
        self.assertEqual(len(set(identities)), 1)
        self.assertEqual([r["nivel"] for r in self.rows("alertas_eventos")], [1, 2, 3])
        self.assertEqual([r["prioridade"] for r in self.rows("alertas_fila")], [50, 50, 80, 80, 100, 100])
        self.assertEqual([r["evento_id"].split(":")[-1] for r in self.rows("alertas_eventos")],
                         ["informativo", "atencao", "alerta"])

    def test_rearm_cooldown_novo_episodio(self):
        old = self.confirmed_medium()["state"]["episode_id"]
        self.process(self.snapshot("LOW", minute=1), minute=1)
        result = self.process(self.snapshot("LOW", minute=60), minute=60)
        self.assertTrue(result["state"]["active"])
        self.assertEqual(result["state"]["episode_id"], old)
        self.assertEqual(result["reason"], "rearm_pending")
        result = self.process(self.snapshot("LOW", minute=61), minute=61)
        self.assertEqual(result["reason"], "rearmed")
        self.assertFalse(result["state"]["active"])
        self.assertIsNone(result["state"]["episode_id"])
        self.assertEqual(result["state"]["last_enqueued_at"], self.now.isoformat())
        result = self.process(minute=62)
        self.assertEqual(result["reason"], "cooldown")
        new = result["state"]["episode_id"]
        self.assertNotEqual(old, new)
        self.assertEqual(self.process(minute=179)["reason"], "cooldown")
        self.assertEqual(len(self.rows("alertas_eventos")), 1)
        snap = self.snapshot(minute=180)
        result = self.process(snap, minute=180)
        self.assertEqual(result["enfileirados"], 2)
        self.assertEqual(result["state"]["episode_id"], new)

    def test_histerese_preserva_episodio_em_todas_as_distancias(self):
        for i, (intensity, near, far) in enumerate((("MEDIUM", 24, 101), ("HIGH", 34, 151),
                                                   ("VERY_HIGH", 19, 151))):
            with self.subTest(intensity=intensity):
                start = i * 200
                first = self.confirmed_medium(minute=start, distance=near, intensity=intensity)
                episode = first["state"]["episode_id"]
                self.assertEqual(first["enfileirados"], 2)
                for minute, distance in ((1, near + 6), (61, near + 11),
                                         (62, far), (122, far), (181, far)):
                    minute += start
                    snap = self.snapshot(intensity, minute, distance)
                    snap["alerta_preventivo"].update(cluster_id=minute, track_id=minute)
                    result = self.process(snap, minute)
                    self.assertEqual(result["enfileirados"], 0)
                    self.assertEqual(result["state"]["episode_id"], episode)
                    self.assertTrue(result["state"]["active"])
                    self.assertIsNone(result["state"]["clear_since"])
                    if distance == far:
                        self.assertEqual(result["reason"], "outside_proximity_range")
                back = self.process(self.snapshot(intensity, start + 182, near), start + 182)
                self.assertEqual(back["state"]["episode_id"], episode)
                self.assertEqual(back["reason"], "same_or_lower_severity")
                self.assertEqual(back["enfileirados"], 0)
        self.assertEqual([r["nivel"] for r in self.rows("alertas_eventos")], [1, 2, 3])

    def test_bloqueios_interrompem_ausencia_sem_encerrar_episodio(self):
        old = self.confirmed_medium()["state"]["episode_id"]
        changes = [
            ("radar", "stale", True, "radar_stale"),
            ("radar", "operacional", False, "radar_unavailable"),
            ("radar", "frame_id", None, "invalid_frame"),
            ("alerta_preventivo", "front_pixels_medium", -1, "inconsistent_data"),
            ("alerta_preventivo", "clutter", True, "clutter"),
            ("alerta_preventivo", "tracking_valid", False, "tracking_insufficient_for_early_warning"),
            ("alerta_preventivo", "approaching", False, "not_approaching"),
            ("alerta_preventivo", "trajectory_compatible", False, "trajectory_incompatible"),
            ("alerta_preventivo", "distance_km", 101, "outside_proximity_range"),
        ]
        for i, (section, key, value, reason) in enumerate(changes):
            with self.subTest(reason=reason):
                start = 1 + i * 200
                self.process(minute=start - 1)
                self.process(self.snapshot("LOW", start), start)
                for minute in (start + 60, start + 120):
                    snap = self.snapshot(minute=minute, distance=40, tracking=True)
                    snap[section][key] = value
                    result = self.process(snap, minute)
                    self.assertEqual(result["reason"], reason)
                    self.assertTrue(result["state"]["active"])
                    self.assertEqual(result["state"]["episode_id"], old)
                    self.assertIsNone(result["state"]["clear_since"])
                result = self.process(self.snapshot("LOW", start + 121), start + 121)
                self.assertEqual(result["reason"], "rearm_pending")
                self.assertEqual(result["state"]["episode_id"], old)
        self.assertEqual(len(self.rows("alertas_eventos")), 1)

    def test_pixels_insuficientes_exigem_ausencia_persistente(self):
        old = self.confirmed_medium()["state"]["episode_id"]
        for minute in (1, 60, 61):
            snap = self.snapshot(minute=minute)
            snap["alerta_preventivo"]["front_pixels_medium"] = 0
            result = self.process(snap, minute)
            if minute < 61:
                self.assertEqual(result["reason"], "rearm_pending")
                self.assertEqual(result["state"]["episode_id"], old)
            else:
                self.assertEqual(result["reason"], "rearmed")
                self.assertEqual(result["state"]["last_enqueued_at"], self.now.isoformat())

    def test_interrupcoes_nao_confirmam_ausencia(self):
        old = self.confirmed_medium()["state"]["episode_id"]
        for change in ["stale", "unavailable", "clutter", "tracking", "snapshot"]:
            with self.subTest(change=change):
                self.process(self.snapshot("LOW", minute=1), minute=1)
                snap = self.snapshot(minute=31, distance=40)
                if change == "stale": snap["radar"]["stale"] = True
                if change == "unavailable": snap["radar"]["operacional"] = False
                if change == "clutter": snap["alerta_preventivo"]["clutter"] = True
                if change == "tracking": snap["alerta_preventivo"]["tracking_valid"] = False
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
        for intensity, distance, tracking in [("MEDIUM", 20, True),
                                               ("HIGH", 30, True), ("VERY_HIGH", 20, True)]:
            with self.subTest(intensity=intensity, tracking=tracking):
                # Rearm e cooldown entre episódios, sem apagar qualquer histórico.
                minute = len(self.rows("alertas_eventos")) * 200
                if minute:
                    self.process(self.snapshot("LOW", minute=minute-61), minute=minute-61)
                    self.process(self.snapshot("LOW", minute=minute-1), minute=minute-1)
                snap = self.snapshot(intensity, minute, distance, tracking)
                snap["escola"].update(rain_rate=2, stale=True)
                snap["radar"]["data_frame"] = (self.now+timedelta(minutes=minute-5)).isoformat()
                self.process(snap, minute)
                snap["radar"].update(frame_id=snap["radar"]["frame_id"]+1,
                                     data_frame=(self.now+timedelta(minutes=minute)).isoformat())
                self.assertEqual(self.process(snap, minute)["enfileirados"], 2)

    def test_chuva_local_suprime_escalonamento_ate_rearm(self):
        self.confirmed_medium()
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
                    snap["alerta_preventivo"].update(alert_level=level, projected_impact_eta_minutes=eta, trajectory_confidence="ALTA" if quality in {"BOA", "MODERADA"} else "BAIXA")
                    msg = montar_mensagem_preventiva(snap)
                    self.assertEqual(msg.count("Distrito de São José"), 1)
                    self.assertEqual(msg.count("https://meteo.eesjv.com.br"), 1)
                    self.assertEqual("se aproximando" in msg, tracking)
                    self.assertEqual(msg.count("Estimativa de chegada:"), int(tracking and eta == 25 and quality in {"BOA", "MODERADA"}))
                    for term in ("eco", "refletividade", "cluster", "tracking", "célula", "maxcappi", "frame", "ee são josé", "escola estadual são josé", "vai chover"):
                        self.assertNotIn(term, msg.lower())
        self.confirmed_medium()
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
        self.assertEqual(self.confirmed_medium()["enfileirados"], 2)
        self.assertIs(self.service.salvar_estado, original)

    def test_rollback_erro_segundo_destinatario(self):
        conn = self.db.get_db()
        conn.execute("""CREATE TRIGGER falha_teste BEFORE INSERT ON alertas_fila
                        WHEN NEW.usuario_id = 6 BEGIN SELECT RAISE(ABORT, 'test'); END""")
        conn.commit()
        conn.close()
        with self.assertRaises(Exception): self.confirmed_medium()
        for table in ("alertas_eventos", "alertas_fila"):
            self.assertEqual(self.rows(table), [])
        self.assertEqual(json.loads(self.rows("health_check_estado")[0]["mensagem"])
                         ["pending_tracking_count"], 1)

    def test_estado_corrompido_falha_fechado(self):
        self.confirmed_medium()
        conn = self.db.get_db()
        conn.execute("UPDATE health_check_estado SET mensagem=? WHERE chave=?",
                     (json.dumps({"active": False}), self.service.ESTADO_CHAVE))
        conn.commit()
        conn.close()
        with self.assertRaises(ValueError): self.process(self.snapshot("HIGH"))
        self.assertEqual(len(self.rows("alertas_eventos")), 1)
        self.assertEqual(self.service.obter_status_alerta_publico(self.config)["last_result"], "status_unavailable")

    def test_concorrencia(self):
        self.process()
        original = self.snapshot(minute=5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.process(original, minute=5), range(2)))
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
        self.assertEqual(result["public_enqueued"], 0)
        self.assertEqual(result["test_alert"]["reason"], "public_alerts_enabled")

    def test_config_defaults(self):
        from config import nowcasting_config
        with mock.patch.dict(os.environ, {}, clear=True):
            config = nowcasting_config()
        self.assertFalse(config["alerts_enabled"])
        self.assertEqual(config["alert_cooldown_minutes"], 180)
        self.assertEqual(config["alert_rearm_minutes"], 60)
        self.assertEqual(config["public_track_intercept_km"], 15)
        self.assertEqual(config["public_very_high_near_km"], 20)
        self.assertEqual(config["public_impact_radius_km"], 5)
        self.assertEqual(config["public_trajectory_min_frames"], 4)
        self.assertEqual(config["public_projection_minutes"], 120)
        for invalid in ("0", "-1", "nan", "inf", "invalid"):
            with self.subTest(invalid=invalid), mock.patch.dict(
                    os.environ, {"NOWCASTING_PUBLIC_VERY_HIGH_NEAR_KM": invalid}):
                self.assertEqual(nowcasting_config()["public_very_high_near_km"], 20)
            with self.subTest(impact=invalid), mock.patch.dict(
                    os.environ, {"NOWCASTING_PUBLIC_IMPACT_RADIUS_KM": invalid}):
                self.assertEqual(nowcasting_config()["public_impact_radius_km"], 5)

    def test_todas_intensidades_exigem_impacto_mesmo_muito_perto(self):
        for intensity in ("MEDIUM", "HIGH", "VERY_HIGH"):
            for distance in (2, 10, 20, 40, 90):
                for field, value, reason in (
                    ("tracking_valid", False, "tracking_insufficient_for_early_warning"),
                    ("approaching", False, "not_approaching"),
                    ("trajectory_compatible", False, "trajectory_incompatible"),
                    ("trajectory_frames_used", 3, "public_trajectory_low_confidence"),
                    ("trajectory_confidence", "BAIXA", "public_trajectory_low_confidence"),
                    ("projected_impact", False, "public_projected_impact_absent"),
                    ("projected_impact_eta_minutes", None, "projected_impact_eta_invalid"),
                ):
                    with self.subTest(intensity=intensity, distance=distance, field=field):
                        snap = self.snapshot(intensity, distance=distance)
                        snap["alerta_preventivo"][field] = value
                        result = self.process(snap)
                        self.assertEqual(result["reason"], reason)
                        self.assertEqual(result["enfileirados"], 0)
        self.assertEqual(self.rows("alertas_eventos"), [])
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_medium_antecipado_a_100_km_exige_impacto_em_dois_frames(self):
        first = self.snapshot("MEDIUM", distance=100)
        self.assertEqual(self.process(first)["reason"], "awaiting_projected_impact_confirmation")
        second = self.snapshot("MEDIUM", minute=5, distance=90)
        second["radar"]["frame_id"] = 1084
        result = self.process(second, 5)
        self.assertEqual(result["enfileirados"], 2)
        self.assertEqual(result["state"]["highest_enqueued_severity"], 1)
        self.assertIn("Estimativa de chegada: 25 min.", self.rows("alertas_fila")[0]["mensagem"])

    def test_high_very_high_antecipados_a_150_km(self):
        for i, intensity in enumerate(("HIGH", "VERY_HIGH")):
            minute = i * 200
            if minute:
                self.process(self.snapshot("LOW", minute=minute-61), minute-61)
                self.process(self.snapshot("LOW", minute=minute-1), minute-1)
            result = self.confirmed_medium(minute=minute, distance=150, intensity=intensity)
            self.assertEqual(result["enfileirados"], 2)
        self.assertEqual([row["nivel"] for row in self.rows("alertas_eventos")], [2, 3])

    def test_borda_intercepta_com_centro_passando_ao_lado(self):
        snap = self.snapshot("HIGH", distance=80)
        snap["alerta_preventivo"].update(closest_approach_km=35, projected_impact=True)
        self.assertEqual(self.process(snap)["enfileirados"], 0)
        snap = self.snapshot("HIGH", minute=5, distance=75)
        snap["alerta_preventivo"].update(closest_approach_km=35, projected_impact=True)
        self.assertEqual(self.process(snap, 5)["enfileirados"], 2)

    def test_impacto_perdido_reinicia_confirmacao(self):
        self.process(self.snapshot("HIGH", distance=60))
        lost = self.snapshot("HIGH", minute=5, distance=55)
        lost["alerta_preventivo"]["projected_impact"] = False
        self.assertEqual(self.process(lost, 5)["state"]["pending_tracking_count"], 0)
        restart = self.snapshot("HIGH", minute=10, distance=50)
        self.assertEqual(self.process(restart, 10)["state"]["pending_tracking_count"], 1)
        second = self.snapshot("HIGH", minute=15, distance=45)
        self.assertEqual(self.process(second, 15)["enfileirados"], 2)

    def test_confirmacao_repetida_invertida_novo_track_e_gap(self):
        first = self.snapshot("HIGH", distance=60)
        self.process(first)
        repeat = self.snapshot("HIGH", minute=5, distance=55)
        repeat["radar"]["frame_id"] = first["radar"]["frame_id"]
        self.assertEqual(self.process(repeat, 5)["state"]["pending_tracking_count"], 1)
        inverted = self.snapshot("HIGH", minute=5, distance=55)
        inverted["radar"].update(frame_id=69, data_frame=(self.now-timedelta(minutes=1)).isoformat())
        self.assertEqual(self.process(inverted, 5)["state"]["pending_tracking_count"], 1)
        other = self.snapshot("HIGH", minute=10, distance=50)
        other["alerta_preventivo"]["track_id"] = 99
        self.assertEqual(self.process(other, 10)["state"]["pending_tracking_count"], 1)
        gap = self.snapshot("HIGH", minute=30, distance=45)
        gap["alerta_preventivo"]["track_id"] = 99
        self.assertEqual(self.process(gap, 30)["state"]["pending_tracking_count"], 1)
        second = self.snapshot("HIGH", minute=35, distance=40)
        second["alerta_preventivo"]["track_id"] = 99
        self.assertEqual(self.process(second, 35)["enfileirados"], 2)

    def test_id_novo_com_mesmo_timestamp_nao_confirma(self):
        first = self.snapshot("HIGH")
        self.process(first)
        duplicate = self.snapshot("HIGH")
        duplicate["radar"]["frame_id"] += 25
        self.assertEqual(self.process(duplicate)["enfileirados"], 0)
        self.assertEqual(self.rows("alertas_eventos"), [])

    def test_timestamp_real_obrigatorio_e_recente(self):
        for value, reason in (
            (None, "invalid_frame_timestamp"),
            ("invalid", "invalid_frame_timestamp"),
            ((self.now-timedelta(minutes=16)).isoformat(), "radar_frame_stale"),
            ((self.now+timedelta(minutes=2)).isoformat(), "radar_frame_stale"),
        ):
            with self.subTest(value=value):
                snap = self.snapshot("VERY_HIGH", distance=2)
                snap["radar"]["data_frame"] = value
                self.assertEqual(self.process(snap)["reason"], reason)
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_eta_invalido_ou_fora_do_horizonte_nao_envia(self):
        for eta in (None, True, -1, float("nan"), float("inf"), 121):
            with self.subTest(eta=eta):
                snap = self.snapshot("HIGH", distance=2)
                snap["alerta_preventivo"]["projected_impact_eta_minutes"] = eta
                self.assertEqual(self.process(snap)["reason"], "projected_impact_eta_invalid")
        for eta in (0, 120):
            snap = self.snapshot("HIGH", distance=2)
            snap["alerta_preventivo"]["projected_impact_eta_minutes"] = eta
            self.assertEqual(self.process(snap)["reason"], "awaiting_projected_impact_confirmation")
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_estado_antigo_exige_nova_confirmacao_com_timestamp(self):
        self.process()
        conn = self.db.get_db()
        row = conn.execute("SELECT mensagem FROM health_check_estado WHERE chave=?",
                           (self.service.ESTADO_CHAVE,)).fetchone()
        old = json.loads(row["mensagem"])
        old.pop("pending_tracking_observed_at")
        conn.execute("UPDATE health_check_estado SET mensagem=? WHERE chave=?",
                     (json.dumps(old), self.service.ESTADO_CHAVE))
        conn.commit()
        conn.close()
        next_frame = self.snapshot(minute=5)
        self.assertEqual(self.process(next_frame, 5)["state"]["pending_tracking_count"], 1)
        second = self.snapshot(minute=10)
        self.assertEqual(self.process(second, 10)["enfileirados"], 2)

    def test_escalonamento_high_confirmado_preserva_episodio_e_ignora_cooldown(self):
        inicial = self.confirmed_medium()
        episodio = inicial["state"]["episode_id"]
        primeiro = self.process(self.snapshot("HIGH", minute=5, distance=120), 5)
        self.assertEqual(primeiro["enfileirados"], 2)
        self.assertEqual(primeiro["state"]["episode_id"], episodio)
        self.assertEqual(primeiro["state"]["highest_enqueued_severity"], 2)
        self.assertEqual(len(self.rows("alertas_eventos")), 2)
        self.assertEqual(self.process(self.snapshot("HIGH", minute=10, distance=115), 10)["enfileirados"], 0)

    def test_estado_antigo_sem_campos_de_confirmacao_pede_duas_novas_imagens(self):
        self.process()
        conn = self.db.get_db()
        row = conn.execute("SELECT mensagem FROM health_check_estado WHERE chave=?",
                           (self.service.ESTADO_CHAVE,)).fetchone()
        antigo = json.loads(row["mensagem"])
        for campo in list(antigo):
            if campo.startswith("pending_"):
                antigo.pop(campo)
        conn.execute("UPDATE health_check_estado SET mensagem=? WHERE chave=?",
                     (json.dumps(antigo), self.service.ESTADO_CHAVE))
        conn.commit()
        conn.close()
        self.assertEqual(self.process(self.snapshot(minute=5), 5)["enfileirados"], 0)
        self.assertEqual(self.process(self.snapshot(minute=10), 10)["enfileirados"], 2)

    def test_snapshot_invalido_interrompe_rearm(self):
        self.confirmed_medium()
        self.process(self.snapshot("LOW", minute=1), minute=1)
        result = self.process({"radar": "invalid"}, minute=31)
        self.assertEqual(result["reason"], "invalid_snapshot")
        self.assertTrue(result["state"]["active"])
        self.assertIsNone(result["state"]["clear_since"])
        self.assertEqual(len(self.rows("alertas_eventos")), 1)

    def test_snapshot_antigo_ou_futuro_nao_enfileira(self):
        for minute in (-60, 5):
            snap = self.snapshot()
            snap["gerado_em_utc"] = (self.now+timedelta(minutes=minute)).isoformat()
            self.assertEqual(self.process(snap)["reason"], "snapshot_stale")
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
