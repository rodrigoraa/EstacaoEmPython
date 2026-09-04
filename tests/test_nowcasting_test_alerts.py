import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ESTACAO = ROOT / "estacao"
sys.path.insert(0, str(ESTACAO))


class NowcastingTestAlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(
            {
                "ESTACAO_DB": str(Path(self.tmp.name) / "teste.db"),
                "SECRET_KEY": "teste",
                "NOWCASTING_ALERTS_ENABLED": "false",
                "NOWCASTING_TEST_ALERTS_ENABLED": "false",
            }
        )
        os.environ.pop("ADMIN_ALERT_PHONE", None)

        import database
        from services import nowcasting_test_alerts

        self.database = importlib.reload(database)
        self.database.init_db()
        self.service = importlib.reload(nowcasting_test_alerts)
        self.base = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()
        for key in (
            "ESTACAO_DB",
            "SECRET_KEY",
            "ADMIN_ALERT_PHONE",
            "NOWCASTING_ALERTS_ENABLED",
            "NOWCASTING_TEST_ALERTS_ENABLED",
            "NOWCASTING_TEST_ALERT_COOLDOWN_MINUTES",
            "NOWCASTING_TEST_ALERT_REARM_MINUTES",
        ):
            os.environ.pop(key, None)

    def config(self, *, enabled=True, cooldown=60, rearm=30):
        return {
            "test_alerts_enabled": enabled,
            "test_alert_cooldown_minutes": cooldown,
            "test_alert_rearm_minutes": rearm,
            "poll_seconds": 300,
        }

    def snapshot(
        self,
        *,
        level="VERMELHO",
        now=None,
        track_id=27,
        cluster_id=101,
        would_send=True,
        clutter=False,
        stale=False,
        operational=True,
        local_event=False,
        rain_rate=0,
        tracking=True,
    ):
        now = now or self.base
        return {
            "gerado_em_utc": now.isoformat(),
            "evento_local_observado": local_event,
            "escola": {"rain_rate": rain_rate},
            "radar": {
                "operacional": operational,
                "stale": stale,
                "frame_id": 70,
            },
            "alerta_preventivo": {
                "nivel": level,
                "would_send": would_send,
                "clutter": clutter,
                "low_confidence": clutter,
                "track_id": track_id,
                "cluster_id": cluster_id,
                "distance_km": 22.4,
                "tracking_valid": tracking,
                "approaching": True,
                "speed_kmh": 45,
                "eta_minutes": 30,
                "eta_border_minutes": 25,
                "regional_confirmation": True,
            },
        }

    def processar(self, snapshot=None, *, now=None, config=None, sender=None):
        return self.service.processar_alerta_teste_admin(
            snapshot or self.snapshot(now=now),
            config or self.config(),
            now=now or self.base,
            sender=sender,
        )

    def estado_persistido(self):
        conn = self.database.get_db()
        try:
            row = conn.execute(
                "SELECT * FROM health_check_estado WHERE chave=?",
                (self.service.ESTADO_CHAVE,),
            ).fetchone()
            return row, json.loads(row["mensagem"]) if row else None
        finally:
            conn.close()

    def assert_sem_fila_preventiva(self):
        conn = self.database.get_db()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0
            )
        finally:
            conn.close()

    def test_flag_desabilitada_nao_envia_vermelho(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        status = self.processar(config=self.config(enabled=False), sender=sender)
        sender.assert_not_called()
        self.assertFalse(status["enabled"])

    def test_flag_habilitada_sem_admin_phone_nao_envia_nem_quebra(self):
        sender = mock.Mock()
        status = self.processar(sender=sender)
        sender.assert_not_called()
        self.assertEqual(status["reason"], "admin_phone_missing")

    def test_configuracao_tem_defaults_e_flags_separadas(self):
        from config import nowcasting_config

        os.environ.pop("NOWCASTING_TEST_ALERTS_ENABLED", None)
        os.environ.pop("NOWCASTING_TEST_ALERT_COOLDOWN_MINUTES", None)
        os.environ.pop("NOWCASTING_TEST_ALERT_REARM_MINUTES", None)
        padrao = nowcasting_config()
        self.assertFalse(padrao["test_alerts_enabled"])
        self.assertEqual(padrao["test_alert_cooldown_minutes"], 60)
        self.assertEqual(padrao["test_alert_rearm_minutes"], 30)

        os.environ["NOWCASTING_ALERTS_ENABLED"] = "false"
        os.environ["NOWCASTING_TEST_ALERTS_ENABLED"] = "true"
        os.environ["NOWCASTING_TEST_ALERT_COOLDOWN_MINUTES"] = "75"
        os.environ["NOWCASTING_TEST_ALERT_REARM_MINUTES"] = "45"
        configurado = nowcasting_config()
        self.assertFalse(configurado["alerts_enabled"])
        self.assertTrue(configurado["test_alerts_enabled"])
        self.assertEqual(configurado["test_alert_cooldown_minutes"], 75)
        self.assertEqual(configurado["test_alert_rearm_minutes"], 45)

    def test_vermelho_elegivel_envia_uma_vez_ao_admin_sem_fila(self):
        telefone = "67999999999"
        os.environ["ADMIN_ALERT_PHONE"] = telefone
        sender = mock.Mock()
        status = self.processar(sender=sender)

        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0], telefone)
        self.assertTrue(status["sent_for_current_episode"])
        self.assertEqual(status["event_key"], "track:27")
        self.assert_sem_fila_preventiva()

        row, estado = self.estado_persistido()
        self.assertEqual(row["chave"], "nowcasting_test_alert")
        self.assertTrue(estado["active"])
        self.assertEqual(estado["last_track_id"], 27)
        self.assertEqual(estado["last_cluster_id"], 101)
        self.assertEqual(estado["last_distance_km"], 22.4)
        self.assertTrue(
            {
                "active", "event_key", "last_level", "last_track_id",
                "last_cluster_id", "last_distance_km", "last_sent_at",
                "last_seen_at", "clear_since", "last_result", "last_error",
            } <= set(estado)
        )
        self.assertNotIn(telefone, row["mensagem"])

    def test_niveis_nao_vermelhos_e_indisponivel_nao_enviam(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        for level in ("NORMAL", "AMARELO", "LARANJA", "INDISPONIVEL"):
            with self.subTest(level=level):
                sender = mock.Mock()
                self.processar(self.snapshot(level=level), sender=sender)
                sender.assert_not_called()

    def test_clutter_diagnostico_nao_envia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        status = self.processar(
            self.snapshot(level="AMARELO", would_send=False, clutter=True),
            sender=sender,
        )
        sender.assert_not_called()
        self.assertEqual(status["reason"], "level_not_red")

        vermelho_inconsistente = self.snapshot(clutter=False)
        vermelho_inconsistente["alerta_preventivo"]["clutter_index"] = 0.96
        status = self.processar(vermelho_inconsistente, sender=sender)
        sender.assert_not_called()
        self.assertEqual(status["reason"], "clutter")

    def test_vermelho_sem_would_send_ou_radar_operacional_nao_envia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        for snapshot in (
            self.snapshot(would_send=False),
            self.snapshot(operational=False),
        ):
            with self.subTest(snapshot=snapshot):
                sender = mock.Mock()
                self.processar(snapshot, sender=sender)
                sender.assert_not_called()

    def test_vermelho_stale_ou_snapshot_velho_nao_envia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        casos = (
            (self.snapshot(stale=True), self.base),
            (self.snapshot(now=self.base), self.base + timedelta(minutes=11)),
        )
        for snapshot, now in casos:
            with self.subTest(snapshot=snapshot["radar"], now=now):
                sender = mock.Mock()
                status = self.processar(snapshot, now=now, sender=sender)
                sender.assert_not_called()
                self.assertEqual(status["reason"], "snapshot_stale")

    def test_evento_local_ou_chuva_atual_suprime_todo_o_episodio(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        for campo in ("evento", "chuva"):
            with self.subTest(campo=campo):
                sender = mock.Mock()
                snapshot = self.snapshot(
                    local_event=campo == "evento", rain_rate=1.2 if campo == "chuva" else 0
                )
                status = self.processar(snapshot, sender=sender)
                sender.assert_not_called()
                self.assertEqual(status["reason"], "local_event_observed")
                _, estado = self.estado_persistido()
                self.assertTrue(estado["suppressed_for_current_episode"])

                seco = self.snapshot(now=self.base + timedelta(minutes=5))
                self.processar(seco, now=self.base + timedelta(minutes=5), sender=sender)
                sender.assert_not_called()
                conn = self.database.get_db()
                conn.execute(
                    "DELETE FROM health_check_estado WHERE chave=?",
                    (self.service.ESTADO_CHAVE,),
                )
                conn.commit()
                conn.close()

    def test_mesmo_episodio_e_reinicio_nao_reenviam(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        self.processar(
            self.snapshot(now=self.base + timedelta(minutes=5)),
            now=self.base + timedelta(minutes=5),
            sender=sender,
        )
        self.service = importlib.reload(self.service)
        self.processar(
            self.snapshot(now=self.base + timedelta(minutes=10)),
            now=self.base + timedelta(minutes=10),
            sender=sender,
        )
        self.assertEqual(sender.call_count, 1)

    def test_volta_antes_do_rearm_nao_envia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        saida = self.base + timedelta(minutes=10)
        self.processar(
            self.snapshot(level="LARANJA", now=saida), now=saida, sender=sender
        )
        retorno = self.base + timedelta(minutes=35)
        status = self.processar(
            self.snapshot(now=retorno), now=retorno, sender=sender
        )
        self.assertEqual(sender.call_count, 1)
        self.assertTrue(status["sent_for_current_episode"])

    def test_rearm_completo_e_cooldown_permite_novo_envio(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        saida = self.base + timedelta(minutes=10)
        self.processar(
            self.snapshot(level="LARANJA", now=saida), now=saida, sender=sender
        )
        rearmado = self.base + timedelta(minutes=41)
        status = self.processar(
            self.snapshot(level="NORMAL", now=rearmado),
            now=rearmado,
            sender=sender,
        )
        self.assertFalse(status["rearm_pending"])

        novo = self.base + timedelta(minutes=61)
        status = self.processar(
            self.snapshot(now=novo, track_id=88, cluster_id=202),
            now=novo,
            sender=sender,
        )
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(status["event_key"], "track:88")

    def test_rearm_sem_fim_do_cooldown_ainda_bloqueia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        saida = self.base + timedelta(minutes=1)
        self.processar(
            self.snapshot(level="NORMAL", now=saida), now=saida, sender=sender
        )
        rearmado = self.base + timedelta(minutes=32)
        self.processar(
            self.snapshot(level="NORMAL", now=rearmado), now=rearmado, sender=sender
        )
        novo = self.base + timedelta(minutes=40)
        status = self.processar(
            self.snapshot(now=novo, track_id=99), now=novo, sender=sender
        )
        self.assertEqual(sender.call_count, 1)
        self.assertTrue(status["cooldown_active"])

    def test_sem_track_troca_cluster_e_depois_track_nao_gera_spam(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(self.snapshot(track_id=None, cluster_id=1), sender=sender)
        cinco = self.base + timedelta(minutes=5)
        self.processar(
            self.snapshot(now=cinco, track_id=None, cluster_id=2),
            now=cinco,
            sender=sender,
        )
        dez = self.base + timedelta(minutes=10)
        status = self.processar(
            self.snapshot(now=dez, track_id=44, cluster_id=3),
            now=dez,
            sender=sender,
        )
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(status["event_key"], "untracked_red_episode")

    def test_track_estavel_envia_somente_uma_vez(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        for minutos in (0, 5, 10):
            now = self.base + timedelta(minutes=minutos)
            self.processar(
                self.snapshot(now=now, track_id=27), now=now, sender=sender
            )
        self.assertEqual(sender.call_count, 1)

    def test_falha_nao_derruba_worker_e_retry_respeita_cooldown(self):
        telefone = "67999999999"
        os.environ["ADMIN_ALERT_PHONE"] = telefone
        falhar = mock.Mock(side_effect=RuntimeError(f"falha para {telefone}"))
        with self.assertLogs(
            "services.nowcasting_test_alerts", level="ERROR"
        ) as logs:
            status = self.processar(sender=falhar)
        self.assertEqual(status["reason"], "send_failed")
        self.assertNotIn(telefone, "\n".join(logs.output))
        _, estado = self.estado_persistido()
        self.assertEqual(estado["last_error"], "RuntimeError")
        self.assertNotIn(telefone, json.dumps(estado))

        cedo = self.base + timedelta(minutes=5)
        self.processar(
            self.snapshot(now=cedo), now=cedo, sender=falhar
        )
        self.assertEqual(falhar.call_count, 1)

        ainda_cedo = self.base + timedelta(minutes=10)
        self.processar(
            self.snapshot(now=ainda_cedo), now=ainda_cedo, sender=falhar
        )
        self.assertEqual(falhar.call_count, 1)

        sucesso = mock.Mock()
        tarde = self.base + timedelta(minutes=61)
        status = self.processar(
            self.snapshot(now=tarde), now=tarde, sender=sucesso
        )
        sucesso.assert_called_once()
        self.assertTrue(status["sent_for_current_episode"])

    def test_mensagem_com_tracking_nomeia_velocidade_do_eco(self):
        mensagem = self.service.montar_mensagem_alerta_teste(self.snapshot())
        self.assertIn("🧪 ALERTA PREVENTIVO — TESTE", mensagem)
        self.assertIn("Movimento: aproximando", mensagem)
        self.assertIn("Velocidade estimada do eco: 45 km/h", mensagem)
        self.assertIn("ETA da trajetória: 30 min", mensagem)
        self.assertIn("ETA estimado da borda: 25 min", mensagem)
        self.assertIn("estações regionais", mensagem)
        self.assertNotIn("Velocidade do vento", mensagem)

    def test_mensagem_sem_tracking_nao_inventa_zero(self):
        mensagem = self.service.montar_mensagem_alerta_teste(
            self.snapshot(tracking=False)
        )
        self.assertIn("Movimento: dados insuficientes", mensagem)
        self.assertIn("Velocidade estimada do eco: dados insuficientes", mensagem)
        self.assertIn("ETA da trajetória: dados insuficientes", mensagem)
        self.assertNotIn("0 km/h", mensagem)
        self.assertNotIn("0 min", mensagem)

    def test_servico_admin_reutiliza_normalizacao_e_whatsapp_existentes(self):
        from services.admin_notification_service import enviar_mensagem_admin

        with mock.patch("services.whatsapp_service.enviar_whatsapp") as enviar:
            enviar_mensagem_admin("(67) 99999-9999", "teste")
        enviar.assert_called_once_with("5567999999999", "teste")


if __name__ == "__main__":
    unittest.main()
