import importlib
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


class ManualRadarAlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "ESTACAO_DB": str(Path(self.tmp.name) / "test.db"),
            "SECRET_KEY": "test", "ADMIN_ALERT_PHONE": "67999999999",
            "NOWCASTING_ENABLED": "true", "NOWCASTING_ALERTS_ENABLED": "false",
            "NOWCASTING_TEST_ALERTS_ENABLED": "true",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        import database
        self.db = importlib.reload(database)
        self.db.init_db()
        from services import nowcasting_manual_alerts, nowcasting_test_alerts
        from config import nowcasting_config
        self.manual = nowcasting_manual_alerts
        self.admin = nowcasting_test_alerts
        self.config = nowcasting_config()
        self.now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
        self.snapshot = {
            "gerado_em_utc": self.now.isoformat(),
            "escola": {"rain_rate": 0, "stale": False},
            "radar": {"operacional": True, "stale": False, "frame_id": 70,
                      "data_frame": self.now.isoformat()},
            "alerta_preventivo": {
                "nivel": "LARANJA", "distance_km": 40,
                "front_pixels_low": 0, "front_pixels_medium": 0,
                "front_pixels_high": 100, "front_pixels_very_high": 0,
                "track_id": 27, "tracking_valid": True, "approaching": True,
                "trajectory_compatible": True, "trajectory_frames_used": 5,
                "trajectory_duration_minutes": 20,
                "trajectory_method": "linear_xy_6_pixel_runs",
                "trajectory_confidence": "ALTA", "projected_impact": True,
                "projected_impact_eta_minutes": 30,
                "projected_impact_min_distance_km": 3,
                "projected_impact_horizon_minutes": 120, "clutter": False,
            },
        }
        conn = self.db.get_db()
        conn.executemany("""INSERT INTO usuarios
            (nome, telefone, ativo, receber_whatsapp, status_cadastro)
            VALUES (?, ?, ?, ?, ?)""", [
                ("Ana", "67999999991", 1, 1, "ativo"),
                ("Bia", "67999999992", 1, 1, "ativo"),
                ("Inativo", "67999999993", 0, 1, "ativo"),
                ("Sem assinatura", "67999999994", 1, 0, "ativo"),
                ("Pendente", "67999999995", 1, 1, "pendente"),
            ])
        conn.commit()
        conn.close()
        self.current = mock.patch("services.nowcasting_manual_alerts.obter_ultimo_snapshot",
                                  side_effect=lambda: self.snapshot)
        self.current.start()
        self.addCleanup(self.current.stop)

    def receive(self, sender=None):
        sender = sender or mock.Mock()
        self.admin.processar_alerta_teste_admin(self.snapshot, self.config,
                                              now=self.now, sender=sender)
        return sender

    def status(self, now=None):
        return self.manual.obter_status_alerta_manual(self.config, now=now or self.now)

    def send(self, alert_id=None, now=None):
        return self.manual.enfileirar_alerta_manual(
            alert_id or self.status()["alert"]["id"], self.config, now=now or self.now)

    def rows(self, table):
        conn = self.db.get_db_readonly()
        try:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        finally:
            conn.close()

    def test_only_successful_admin_delivery_creates_persisted_candidate(self):
        self.assertIsNone(self.status()["alert"])
        self.receive(mock.Mock(side_effect=RuntimeError("offline")))
        self.assertIsNone(self.status()["alert"])
        # O intervalo entre tentativas pertence ao fluxo administrativo existente.
        self.config["test_alert_cooldown_minutes"] = 0
        sender = self.receive()
        candidate = self.status()["alert"]
        self.assertEqual(candidate["mensagem"], sender.call_args.args[1])
        self.assertTrue(self.status()["can_send"])
        importlib.reload(self.manual)
        self.assertEqual(self.status()["alert"]["id"], candidate["id"])
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_manual_send_uses_exact_received_message_and_keeps_switch_off(self):
        sender = self.receive()
        self.snapshot["alerta_preventivo"]["projected_impact_eta_minutes"] = 20
        result = self.send()
        self.assertEqual(result["enfileirados"], 2)
        self.assertEqual({row["mensagem"] for row in self.rows("alertas_fila")},
                         {sender.call_args.args[1]})
        self.assertEqual(len(self.rows("alertas_eventos")), 1)
        from services.runtime_alert_controls import obter_controles
        self.assertFalse(obter_controles(self.config)["public"]["enabled"])
        self.assertFalse(self.status()["can_send"])
        self.assertEqual(self.status()["alert"]["enfileirados"], 2)

    def test_concurrent_clicks_enqueue_once(self):
        self.receive()
        alert_id = self.status()["alert"]["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.send(alert_id), range(2)))
        self.assertEqual(sum(item["enfileirados"] for item in results), 2)
        self.assertEqual(len(self.rows("alertas_eventos")), 1)
        self.assertEqual(len(self.rows("alertas_fila")), 2)

    def test_expiration_uses_original_observation_not_click_time(self):
        self.receive()
        alert_id = self.status()["alert"]["id"]
        later = self.now + timedelta(minutes=16)
        self.snapshot["radar"]["data_frame"] = later.isoformat()
        self.snapshot["gerado_em_utc"] = later.isoformat()
        self.assertEqual(self.send(alert_id, now=later)["reason"], "expired")
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_public_switch_and_changed_weather_block_manual_send(self):
        self.receive()
        from services.runtime_alert_controls import salvar_controle
        salvar_controle("public", True)
        self.assertEqual(self.send()["reason"], "public_alerts_enabled")
        salvar_controle("public", False)
        self.snapshot["alerta_preventivo"]["track_id"] = 99
        self.assertEqual(self.send()["reason"], "invalidated")
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_stale_id_cannot_approve_a_newer_received_alert(self):
        self.receive()
        old_id = self.status()["alert"]["id"]
        self.snapshot["alerta_preventivo"].update(front_pixels_high=0,
                                                front_pixels_very_high=100)
        self.receive()
        self.assertNotEqual(self.status()["alert"]["id"], old_id)
        self.assertEqual(self.send(old_id)["reason"], "not_found")
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_fanout_failure_rolls_back_event_and_all_recipients(self):
        self.receive()
        conn = self.db.get_db()
        conn.execute("""CREATE TRIGGER reject_bia BEFORE INSERT ON alertas_fila
            WHEN NEW.nome='Bia' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        conn.commit()
        conn.close()
        with self.assertRaises(Exception):
            self.send()
        self.assertEqual(self.rows("alertas_fila"), [])
        self.assertEqual(self.rows("alertas_eventos"), [])
        self.assertTrue(self.status()["can_send"])

    def test_failed_escalation_preserves_previous_received_message(self):
        self.receive()
        original = self.status()["alert"]
        self.snapshot["alerta_preventivo"].update(front_pixels_high=0,
                                                front_pixels_very_high=100)
        self.receive(mock.Mock(side_effect=RuntimeError("offline")))
        self.assertEqual(self.status()["alert"], original)

    def test_no_recipients_is_reported_without_backfilling_on_repeat(self):
        self.receive()
        conn = self.db.get_db()
        conn.execute("UPDATE usuarios SET receber_whatsapp=0")
        conn.commit()
        conn.close()
        self.assertEqual(self.send()["reason"], "no_recipients")
        self.assertEqual(self.send()["reason"], "duplicate")
        self.assertEqual(self.rows("alertas_fila"), [])

    def test_admin_confirmation_reaches_real_queue_service(self):
        self.receive()
        from app import create_app
        from security_helpers import autenticar_admin
        client = create_app({"TESTING": True, "RATELIMIT_ENABLED": False}).test_client()
        autenticar_admin(client, csrf_token="manual-csrf")
        candidate_id = self.status()["alert"]["id"]
        path = f"/admin/nowcasting/alertas/{candidate_id}/enviar"
        with mock.patch.object(self.manual, "agora_utc", return_value=self.now):
            response = client.post(path, data={"csrf_token": "manual-csrf"})
            self.assertEqual(response.status_code, 303)
            self.assertEqual(len(self.rows("alertas_fila")), 2)
            page = client.get(response.headers["Location"])
            self.assertIn("Alerta enfileirado para 2 destinatários".encode(), page.data)
            self.assertNotIn(b'data-open-dialog="manual-alert-confirm"', page.data)
            self.assertEqual(client.post(path, data={"csrf_token": "manual-csrf"}).status_code, 303)
        self.assertEqual(len(self.rows("alertas_fila")), 2)

    def test_worker_delivers_manual_while_public_off_without_changing_text(self):
        sender = self.receive()
        self.send()
        self.snapshot["alerta_preventivo"]["projected_impact_eta_minutes"] = 10
        from workers import whatsapp_sender
        with (mock.patch.object(whatsapp_sender, "agora_utc", return_value=self.now),
              mock.patch.object(whatsapp_sender, "enviar_whatsapp") as deliver):
            self.assertEqual(whatsapp_sender.processar_um_envio(), "enviado")
        self.assertEqual(deliver.call_args.args[1], sender.call_args.args[1])

    def test_worker_still_cancels_expired_and_unsubscribed_manual_delivery(self):
        self.receive()
        self.send()
        from workers import whatsapp_sender
        conn = self.db.get_db()
        conn.execute("UPDATE usuarios SET receber_whatsapp=0 WHERE nome='Ana'")
        conn.commit()
        conn.close()
        with (mock.patch.object(whatsapp_sender, "agora_utc", return_value=self.now),
              mock.patch.object(whatsapp_sender, "enviar_whatsapp") as deliver):
            self.assertEqual(whatsapp_sender.processar_um_envio(), "cancelado")
            self.assertEqual(whatsapp_sender.processar_um_envio(), "enviado")
        self.assertEqual(deliver.call_count, 1)

    def test_worker_rechecks_manual_expiration(self):
        self.receive()
        self.send()
        from workers import whatsapp_sender
        later = self.now + timedelta(minutes=16)
        with (mock.patch.object(whatsapp_sender, "agora_utc", return_value=later),
              mock.patch.object(whatsapp_sender, "enviar_whatsapp") as deliver):
            self.assertEqual(whatsapp_sender.processar_um_envio(), "cancelado")
        deliver.assert_not_called()

    def test_worker_rechecks_weather_and_manual_authorization(self):
        self.receive()
        self.send()
        self.snapshot["evento_local_observado"] = True
        from workers import whatsapp_sender
        with (mock.patch.object(whatsapp_sender, "agora_utc", return_value=self.now),
              mock.patch.object(whatsapp_sender, "enviar_whatsapp") as deliver):
            self.assertEqual(whatsapp_sender.processar_um_envio(), "cancelado")
            conn = self.db.get_db()
            conn.execute("DELETE FROM alertas_eventos")
            conn.commit()
            conn.close()
            self.snapshot["evento_local_observado"] = False
            self.assertEqual(whatsapp_sender.processar_um_envio(), "cancelado")
        deliver.assert_not_called()

    def test_worker_does_not_send_when_persisted_controls_are_unreadable(self):
        self.receive()
        self.send()
        conn = self.db.get_db()
        from services.runtime_alert_controls import STATE_KEY
        conn.execute("INSERT INTO estado_alertas (chave, valor_json) VALUES (?, ?)",
                     (STATE_KEY, "{invalid-json"))
        conn.commit()
        conn.close()
        self.assertEqual(self.status()["reason"], "unavailable")
        from workers import whatsapp_sender
        with (mock.patch.object(whatsapp_sender, "agora_utc", return_value=self.now),
              mock.patch.object(whatsapp_sender, "enviar_whatsapp") as deliver):
            self.assertEqual(whatsapp_sender.processar_um_envio(), "cancelado")
        deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
