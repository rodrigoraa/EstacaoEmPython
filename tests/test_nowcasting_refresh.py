import json
import math
import os
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


class NowcastingRefreshTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db_path = str(Path(temporary.name) / "nowcasting.db")
        environment = mock.patch.dict(os.environ, {
            "ESTACAO_DB": db_path,
            "APP_ENV": "development",
            "SECRET_KEY": "test-nowcasting-refresh",
            "RATELIMIT_ENABLED": "false",
            "NOWCASTING_ALERTS_ENABLED": "false",
            "NOWCASTING_TEST_ALERTS_ENABLED": "false",
        })
        environment.start()
        self.addCleanup(environment.stop)

        import database
        from config import nowcasting_config

        database_path = mock.patch.object(database, "DATABASE", db_path)
        database_path.start()
        self.addCleanup(database_path.stop)
        self.database = database
        database.init_db()
        self.config = {
            **nowcasting_config(),
            "enabled": True,
            "poll_seconds": 300,
            "radar_max_age_minutes": 45,
            "alerts_enabled": False,
            "test_alerts_enabled": False,
        }
        self.now = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)

        import app

        self.client = app.create_app({
            "TESTING": True, "RATELIMIT_ENABLED": False,
        }).test_client()
        with self.client.session_transaction() as session:
            session["logado"] = True
            session["ultimo_acesso"] = time.time()

        network = mock.patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Network access is forbidden in this test"),
        )
        network.start()
        self.addCleanup(network.stop)

    @contextmanager
    def at(self, moment):
        with ExitStack() as stack:
            stack.enter_context(mock.patch("time_utils.agora_utc", return_value=moment))
            stack.enter_context(mock.patch(
                "services.nowcasting_repository.agora_utc", return_value=moment,
            ))
            stack.enter_context(mock.patch(
                "services.nowcasting_service.agora_utc", return_value=moment,
            ))
            stack.enter_context(mock.patch(
                "services.regional_stations_repository.agora_utc", return_value=moment,
            ))
            stack.enter_context(mock.patch(
                "routes.nowcasting.nowcasting_config", return_value=self.config,
            ))
            yield

    def persist_radar(self, latest, *, near=False):
        from services.radar_analysis import (
            GeoBounds, RadarCluster, haversine_km, latlon_para_pixel,
        )
        from services.radar_repository import atualizar_tracking, salvar_resultado_frame
        from services.radar_service import RadarFrame

        bounds = GeoBounds(-23.830664, -16.642761, -58.226281, -50.543479)
        target_lat, target_lon = self.config["target_lat"], self.config["target_lon"]
        frame_id = None
        for index in range(5):
            moment = latest - timedelta(minutes=15 * (4 - index))
            lat = target_lat + .30 - index * .05 if near else -22.0 - index * .05
            lon = -54.46
            distance = haversine_km(lat, lon, target_lat, target_lon)
            left, bottom = latlon_para_pixel(lat - .05, lon - .05, bounds, 750, 750)
            right, top = latlon_para_pixel(lat + .05, lon + .05, bounds, 750, 750)
            footprint = {
                "format": "pixel_runs_v1",
                "runs": [[y, left, right] for y in range(math.ceil(top), math.floor(bottom) + 1)],
            }
            cluster = RadarCluster(
                1, 200, 300, 400, lat, lon, 290, 390, 20, 20,
                distance, max(0, distance - 5), 180, "N", False, "VERDE",
                pixels_refletividade_media=200,
                frente_relevante={
                    "front_pixels_low": 0, "front_pixels_medium": 200,
                    "front_pixels_high": 0, "front_pixels_very_high": 0,
                    "front_pixels_total": 200, "front_depth_km": 15,
                },
                footprint=footprint,
            )
            frame = RadarFrame(
                "jr", "maxcappi", moment, f"https://example.test/{index}.png",
                -20.27855, -54.47396,
                bounds.lat_min, bounds.lat_max, bounds.lon_min, bounds.lon_max,
                400, 1000,
            )
            frame_id, _ = salvar_resultado_frame(
                frame, None, None, 750, 750, [cluster],
            )
            atualizar_tracking(frame_id, target_lat, target_lon, 3, 10, 150, 25)
        return frame_id

    def cycle(self, moment):
        from workers.nowcasting_updater import executar_ciclo

        with self.at(moment), mock.patch(
            "workers.nowcasting_updater.enfileirar_alertas_nowcasting", return_value=0,
        ), mock.patch(
            "workers.nowcasting_updater.processar_alerta_teste_admin",
            return_value={"enabled": False},
        ):
            return executar_ciclo(self.config)

    def snapshots(self):
        connection = self.database.get_db()
        try:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM nowcasting_snapshots ORDER BY id",
            )]
        finally:
            connection.close()

    def test_same_inputs_change_fingerprint_only_in_new_polling_window(self):
        from services.nowcasting_repository import carregar_entradas_nowcasting

        frame_id = self.persist_radar(self.now)
        fingerprints = []
        for moment in (self.now + timedelta(seconds=30),
                       self.now + timedelta(minutes=3),
                       self.now + timedelta(minutes=5, seconds=30)):
            with self.at(moment):
                radar, regional, local, fingerprint = carregar_entradas_nowcasting(self.config)
            self.assertEqual(radar["frame"]["id"], frame_id)
            self.assertFalse(radar["stale"])
            self.assertEqual(radar["tracks_atuais"][0]["track"]["trajectory_frames_used"], 5)
            self.assertIsNone(local)
            fingerprints.append(fingerprint)
        self.assertEqual(fingerprints[0], fingerprints[1])
        self.assertNotEqual(fingerprints[1], fingerprints[2])

    def test_same_window_refreshes_one_snapshot_without_regressing(self):
        self.persist_radar(self.now)
        first = self.cycle(self.now + timedelta(seconds=30))
        original = self.snapshots()[0]
        later = self.now + timedelta(minutes=3)
        repeated = self.cycle(later)
        self.assertTrue(first["new"])
        self.assertFalse(repeated["new"])
        rows = self.snapshots()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], original["id"])
        self.assertEqual(rows[0]["input_fingerprint"], original["input_fingerprint"])
        self.assertEqual(rows[0]["calculado_em_utc"], later.isoformat())
        self.assertEqual(
            json.loads(rows[0]["estado_json"])["radar"]["data_frame"],
            json.loads(original["estado_json"])["radar"]["data_frame"],
        )
        older = self.cycle(self.now + timedelta(minutes=1))
        self.assertFalse(older["new"])
        self.assertEqual(self.snapshots(), rows)

    def test_new_cycle_refreshes_admin_api_without_overwriting_history(self):
        frame_id = self.persist_radar(self.now)
        first = self.cycle(self.now)
        original = self.snapshots()[0]
        later = self.now + timedelta(minutes=15)
        with self.at(later):
            stale = self.client.get("/admin/api/nowcasting/status").get_json()
        self.assertTrue(stale["snapshot_desatualizado"])

        refreshed = self.cycle(later)
        self.assertTrue(refreshed["new"])
        rows = self.snapshots()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], original)
        self.assertNotEqual(first["snapshot_id"], refreshed["snapshot_id"])
        with self.at(later):
            response = self.client.get("/admin/api/nowcasting/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["monitoramento_atual"])
        self.assertFalse(payload["snapshot_desatualizado"])
        self.assertEqual(payload["gerado_em_utc"], later.isoformat())
        self.assertEqual(payload["radar"]["frame_id"], frame_id)
        self.assertTrue(payload["radar"]["operacional"])

    def test_fresh_evaluation_never_makes_old_radar_operational(self):
        frame_id = self.persist_radar(self.now - timedelta(hours=2))
        first = self.cycle(self.now)
        later = self.now + timedelta(minutes=5)
        refreshed = self.cycle(later)
        self.assertTrue(first["new"])
        self.assertTrue(refreshed["new"])
        self.assertEqual(len(self.snapshots()), 2)
        with self.at(later):
            response = self.client.get("/admin/api/nowcasting/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["gerado_em_utc"], later.isoformat())
        self.assertEqual(payload["radar"]["frame_id"], frame_id)
        self.assertTrue(payload["radar"]["stale"])
        self.assertFalse(payload["radar"]["operacional"])
        self.assertFalse(payload["monitoramento_atual"])
        self.assertEqual(payload["alerta_preventivo"]["nivel"], "INDISPONIVEL")

    def test_reanalysis_of_same_radar_frame_never_confirms_impact_alert(self):
        from services.nowcasting_public_alerts import processar_alerta_publico

        frame_id = self.persist_radar(self.now, near=True)
        self.config["alerts_enabled"] = True
        connection = self.database.get_db()
        try:
            connection.execute(
                "INSERT INTO usuarios (nome, telefone, ativo, receber_whatsapp, status_cadastro) "
                "VALUES ('Test', '67912345678', 1, 1, 'ativo')",
            )
            connection.commit()
        finally:
            connection.close()

        for minutes in (0, 5, 10):
            moment = self.now + timedelta(minutes=minutes)
            cycle = self.cycle(moment)
            self.assertTrue(cycle["new"])
            self.assertEqual(cycle["snapshot"]["radar"]["frame_id"], frame_id)
            self.assertEqual(cycle["snapshot"]["radar"]["data_frame"], self.now.isoformat())
            result = processar_alerta_publico(cycle["snapshot"], self.config, now=moment)
            self.assertEqual(result["reason"], "awaiting_projected_impact_confirmation")
            self.assertEqual(result["state"]["pending_tracking_count"], 1)
            self.assertEqual(result["enfileirados"], 0)

        self.assertEqual(len(self.snapshots()), 3)
        connection = self.database.get_db()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
