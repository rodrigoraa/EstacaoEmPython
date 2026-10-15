import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

import database
from services.nowcasting_public_alerts import estado_padrao
from services.radar_service import RadarFrame, RadarServiceError
from workers import radar_alert_status


class RadarAlertStatusTest(unittest.TestCase):
    def setUp(self):
        self.host_environment = os.environ.copy()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "status.db"
        self.now = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        self.environment = {
            "ESTACAO_DB": str(self.path), "RADAR_ENABLED": "true",
            "NOWCASTING_ENABLED": "true", "NOWCASTING_ALERTS_ENABLED": "true",
            "NOWCASTING_TEST_ALERTS_ENABLED": "false",
            "REDEMET_API_KEY": "secret-redemet-do-not-print",
            "EVOLUTION_URL": "https://secret-host.invalid/private",
            "EVOLUTION_API_KEY": "secret-evolution-do-not-print",
            "EVOLUTION_INSTANCE": "secret-instance-do-not-print",
        }
        env = mock.patch.dict(os.environ, self.environment, clear=True)
        env.start()
        self.addCleanup(env.stop)
        db = mock.patch.object(database, "DATABASE", str(self.path))
        db.start()
        self.addCleanup(db.stop)

    def seed_current_data(self, *, age=0, state=None):
        database.init_db()
        timestamp = (self.now - timedelta(minutes=age)).isoformat()
        snapshot = {
            "gerado_em_utc": timestamp,
            "radar": {"operacional": True, "stale": False, "frame_id": 1,
                      "data_frame": timestamp, "timestamp_status": "utc_assumed"},
            "escola": {"rain_rate": 0, "stale": False},
            "alerta_preventivo": {"nivel": "NORMAL", "distance_km": 25,
                                 "front_pixels_low": 100, "front_pixels_medium": 0,
                                 "front_pixels_high": 0, "front_pixels_very_high": 0},
            "secret_extra": "snapshot-secret-do-not-print",
        }
        conn = database.get_db()
        try:
            conn.execute(
                "INSERT INTO radar_frames (id, radar_codigo, produto, data_frame, data_frame_utc, "
                "timestamp_status, path_remoto, lat_center, lon_center, lat_min, lat_max, lon_min, "
                "lon_max, status_processamento) VALUES (1, 'jr', 'maxcappi', ?, ?, 'utc_assumed', "
                "'https://secret.invalid/api-key', 0, 0, -1, 1, -1, 1, 'processado')",
                (timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO nowcasting_snapshots (calculado_em_utc, calculado_em_local, status, "
                "nivel_evidencia, indice_evidencia, estacoes_relevantes_json, evidencias_json, "
                "dados_escola_json, estado_json, input_fingerprint, versao_algoritmo) "
                "VALUES (?, ?, 'NORMAL', 'SEM_EVIDENCIA', 0, '[]', '[]', '{}', ?, 'test', '1.8')",
                (timestamp, timestamp, json.dumps(snapshot)),
            )
            if state is not None:
                conn.execute(
                    "INSERT INTO health_check_estado (chave, status, mensagem) VALUES "
                    "('nowcasting_public_alert', 'idle', ?)", (json.dumps(state),),
                )
            for event, status in (("nowcasting:episode:informativo", "pendente"),
                                  ("nowcasting:episode:alerta", "enviado"), ("chuva:30", "pendente")):
                conn.execute(
                    "INSERT INTO alertas_fila (telefone, nome, mensagem, evento_id, status) "
                    "VALUES ('5567999999999', 'Private User', 'Private message', ?, ?)", (event, status),
                )
            conn.commit()
        finally:
            conn.close()

    def assert_no_private_data(self, report):
        output = json.dumps(report)
        for secret in (*[self.environment[name] for name in radar_alert_status.CREDENTIALS],
                       "5567999999999", "Private User", "Private message", "snapshot-secret-do-not-print"):
            self.assertNotIn(secret, output)

    def test_missing_database_is_reported_without_creation_or_network(self):
        with mock.patch.object(database, "get_db", side_effect=AssertionError("write connection")), \
             mock.patch.object(database, "init_db", side_effect=AssertionError("migration")), \
             mock.patch("socket.socket", side_effect=AssertionError("network")):
            report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertFalse(self.path.exists())
        self.assertTrue(report["ready_to_start"])
        self.assertFalse(report["operational"])
        self.assertEqual(report["database"]["status"], "missing")
        self.assertTrue(report["public_alerts_after_schema_init"])
        self.assertEqual(report["flags"]["public_alerts"]["source"], "fail_safe")
        self.assert_no_private_data(report)

    def test_legacy_schema_is_not_migrated(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE legacy_data (value TEXT)")
            conn.execute("INSERT INTO legacy_data VALUES ('preserve')")
        before = self.path.read_bytes()
        report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(report["ready_to_start"])
        self.assertFalse(report["operational"])
        self.assertEqual(report["database"]["status"], "legacy")
        self.assertIn("radar_frames", report["database"]["missing_tables"])

    def test_current_data_without_rain_is_operational_and_queue_is_aggregate(self):
        self.seed_current_data()
        before = self.path.read_bytes()
        with mock.patch.object(database, "get_db", side_effect=AssertionError("write connection")), \
             mock.patch.object(database, "init_db", side_effect=AssertionError("migration")):
            report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(report["ready_to_start"])
        self.assertTrue(report["operational"], report["operational_blockers"])
        self.assertFalse(report["snapshot"]["weather_eligible"])
        self.assertEqual(report["snapshot"]["weather_reason"], "intensity_below_medium")
        self.assertEqual(report["queue"]["pendente"], 1)
        self.assertEqual(report["queue"]["enviado"], 1)
        self.assert_no_private_data(report)

    def test_suspect_future_frame_does_not_hide_latest_valid_observation(self):
        self.seed_current_data()
        future = (self.now + timedelta(hours=2)).isoformat()
        with closing(database.get_db()) as conn, conn:
            conn.execute(
                "INSERT INTO radar_frames (id, radar_codigo, produto, data_frame, data_frame_utc, "
                "timestamp_status, path_remoto, lat_center, lon_center, lat_min, lat_max, lon_min, "
                "lon_max, status_processamento) "
                "SELECT 2, radar_codigo, produto, ?, ?, 'suspect', path_remoto || '/future', "
                "lat_center, lon_center, lat_min, lat_max, lon_min, lon_max, status_processamento "
                "FROM radar_frames WHERE id=1",
                (future, future),
            )
        before = self.path.read_bytes()
        report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(report["operational"], report["operational_blockers"])
        self.assertEqual(report["radar"]["frame_id"], 1)
        self.assertTrue(report["snapshot"]["uses_latest_frame"])
        self.assertNotIn("radar_frame_invalid_or_stale", report["operational_blockers"])
        self.assertNotIn("snapshot_behind_latest_frame", report["operational_blockers"])

    def test_only_suspect_frames_do_not_make_diagnostics_operational(self):
        self.seed_current_data()
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE radar_frames SET timestamp_status='suspect'")
        report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertFalse(report["operational"])
        self.assertIsNone(report["radar"])
        self.assertIn("radar_frames_missing", report["operational_blockers"])

    def test_stale_data_and_invalid_snapshot_are_visible(self):
        self.seed_current_data(age=20)
        report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertTrue(report["ready_to_start"])
        self.assertFalse(report["operational"])
        self.assertEqual(report["radar"]["age_minutes"], 20)
        self.assertEqual(report["snapshot"]["weather_reason"], "radar_frame_stale")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE nowcasting_snapshots SET estado_json='[1, 2]'")
        report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertIn("snapshot_invalid", report["operational_blockers"])

    def test_image_age_is_split_between_collection_publication_and_elapsed_time(self):
        self.seed_current_data(age=18)
        cases = ((2, 1, "image_already_stale_at_collection", 16, 1, 1),
                 (10, 1, "processing_delay", 8, 9, 1),
                 (14, 13, "no_recent_local_publication", 4, 1, 13))
        for collected_age, published_age, expected, collection_age, processing, elapsed in cases:
            with self.subTest(expected=expected):
                collected = (self.now - timedelta(minutes=collected_age)).isoformat()
                # SQLite CURRENT_TIMESTAMP e UTC sem indicacao explicita de fuso.
                published = (self.now - timedelta(minutes=published_age)).strftime("%Y-%m-%d %H:%M:%S")
                with closing(database.get_db()) as conn, conn:
                    conn.execute("UPDATE radar_frames SET coletado_em_utc=?, processado_em=?", (collected, published))
                report = radar_alert_status.construir_diagnostico(now=self.now)
                radar = report["radar"]
                self.assertEqual(radar["age_minutes"], 18)
                self.assertEqual(radar["freshness_limit_minutes"], 15)
                self.assertEqual(radar["delay_diagnosis"], expected)
                self.assertEqual(radar["image_age_at_collection_minutes"], collection_age)
                self.assertEqual(radar["collection_to_publication_minutes"], processing)
                self.assertEqual(radar["minutes_since_local_publication"], elapsed)
                self.assertFalse(report["operational"])

    def test_missing_or_inconsistent_collection_times_do_not_invent_a_cause(self):
        self.seed_current_data(age=18)
        report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertEqual(report["radar"]["delay_diagnosis"], "timing_unavailable")
        with closing(database.get_db()) as conn, conn:
            conn.execute("UPDATE radar_frames SET coletado_em_utc=?, processado_em=?",
                         ((self.now - timedelta(minutes=1)).isoformat(),
                          (self.now - timedelta(minutes=4)).isoformat()))
        report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertEqual(report["radar"]["delay_diagnosis"], "inconsistent_timestamps")

    def test_source_check_distinguishes_source_delay_and_new_image_waiting_locally(self):
        self.seed_current_data(age=18)
        before = self.path.read_bytes()
        for remote_age, expected, lag in ((18, "source_delayed", 0),
                                          (3, "collector_behind_source", 15),
                                          (20, "remote_older_than_local", -2)):
            with self.subTest(expected=expected):
                remote = RadarFrame("jr", "maxcappi", self.now - timedelta(minutes=remote_age),
                                    "https://secret.invalid/do-not-print", 0, 0, -1, 1, -1, 1)
                client = mock.Mock()
                client.obter_frames.return_value = SimpleNamespace(frames=(remote,))
                client.baixar_imagem.side_effect = AssertionError("image download")
                with mock.patch.object(radar_alert_status, "RedemetRadarClient", return_value=client), \
                     mock.patch.object(radar_alert_status.requests, "Session"), \
                     mock.patch.object(database, "get_db", side_effect=AssertionError("write connection")), \
                     mock.patch.object(database, "init_db", side_effect=AssertionError("migration")):
                    report = radar_alert_status.construir_diagnostico(now=self.now, check_source=True)
                self.assertFalse(report["offline"])
                self.assertEqual(report["source_check"]["status"], "ok")
                self.assertEqual(report["source_check"]["delay_diagnosis"], expected)
                self.assertEqual(report["source_check"]["remote_ahead_of_local_minutes"], lag)
                self.assertEqual(report["source_check"]["age_minutes"], remote_age)
                client.obter_frames.assert_called_once_with()
                client.baixar_imagem.assert_not_called()
                self.assertEqual(self.path.read_bytes(), before)
                self.assert_no_private_data(report)
                self.assertNotIn("https://secret.invalid/do-not-print", json.dumps(report))

    def test_source_check_requires_key_and_sanitizes_external_failures(self):
        os.environ.pop("REDEMET_API_KEY")
        with mock.patch.object(radar_alert_status.requests, "Session") as session:
            report = radar_alert_status.construir_diagnostico(now=self.now, check_source=True)
        session.assert_not_called()
        self.assertEqual(report["source_check"]["status"], "api_key_missing")
        os.environ["REDEMET_API_KEY"] = self.environment["REDEMET_API_KEY"]
        with mock.patch.object(radar_alert_status, "RedemetRadarClient", side_effect=RadarServiceError("external-secret-do-not-print")), \
             mock.patch.object(radar_alert_status.requests, "Session"):
            report = radar_alert_status.construir_diagnostico(now=self.now, check_source=True)
        self.assertEqual(report["source_check"]["status"], "metadata_request_failed")
        self.assertNotIn("external-secret-do-not-print", json.dumps(report))
        self.assertFalse(self.path.exists())

    def test_administrative_override_and_invalid_public_state_block_operation(self):
        state = {**estado_padrao(), "pending_tracking_count": 99}
        self.seed_current_data(state=state)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO estado_alertas (chave, valor_json) VALUES (?, ?)",
                         ("nowcasting_runtime_controls", '{"public_alerts_enabled": false}'))
        report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertFalse(report["ready_to_start"])
        self.assertEqual(report["flags"]["public_alerts"]["source"], "admin")
        self.assertIn("public_episode_invalid", report["operational_blockers"])

    def test_missing_credential_only_names_missing_setting(self):
        os.environ.pop("REDEMET_API_KEY")
        report = radar_alert_status.construir_diagnostico(now=self.now)
        self.assertFalse(report["ready_to_start"])
        self.assertFalse(report["credentials"]["REDEMET_API_KEY"])
        self.assertIn("REDEMET_API_KEY missing", report["config_blockers"])
        self.assert_no_private_data(report)

    def test_json_cli_exit_status_and_both_entrypoints(self):
        output = io.StringIO()
        with mock.patch.object(radar_alert_status, "construir_diagnostico", return_value={"operational": True}), redirect_stdout(output):
            self.assertEqual(radar_alert_status.main(["--json"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"operational": True})
        with mock.patch.object(radar_alert_status, "construir_diagnostico", return_value={"operational": True}) as diagnose, redirect_stdout(io.StringIO()):
            self.assertEqual(radar_alert_status.main(["--json", "--check-source"]), 0)
        diagnose.assert_called_once_with(check_source=True)
        for args in (["-m", "estacao.workers.radar_alert_status", "--json"],
                     [str(ROOT / "estacao/workers/radar_alert_status.py"), "--json"]):
            result = subprocess.run([sys.executable, *args], cwd=ROOT, env={**self.host_environment, **os.environ},
                                    capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertTrue(result.stdout, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["ready_to_start"])
            self.assertFalse(report["operational"])
            self.assertFalse(self.path.exists())
            self.assert_no_private_data(report)


if __name__ == "__main__":
    unittest.main()
