"""Cenários com PNGs, tracking, snapshot, fila e entrega sem rede real."""

import math
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))


class RadarRainAlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "APP_ENV": "development", "NOWCASTING_TEST_ALERTS_ENABLED": "false",
        }))

        import database
        from config import nowcasting_config, radar_config
        from services.radar_analysis import GeoBounds

        self.database = database
        self.stack.enter_context(mock.patch.object(
            database, "DATABASE", str(Path(self.tmp.name) / "radar-alerts.db"),
        ))
        database.init_db()
        self.config = {
            **nowcasting_config(), "enabled": True, "alerts_enabled": True,
            "test_alerts_enabled": False,
        }
        self.radar_config = {
            **radar_config(), "data_dir": str(Path(self.tmp.name) / "radar"),
            "target_lat": self.config["target_lat"], "target_lon": self.config["target_lon"],
        }
        self.lat, self.lon = self.config["target_lat"], self.config["target_lon"]
        self.bounds = GeoBounds(self.lat - 1, self.lat + 1, self.lon - 1.3, self.lon + 1.3)
        self.start = datetime(2026, 10, 14, 16, tzinfo=timezone.utc)
        self.stack.enter_context(mock.patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("This test must not access the network"),
        ))
        conn = self.database.get_db()
        try:
            conn.execute(
                "INSERT INTO usuarios (nome, telefone, ativo, receber_whatsapp, status_cadastro) "
                "VALUES ('Teste', '67912345678', 1, 1, 'ativo')",
            )
            conn.commit()
        finally:
            conn.close()

    def pixel(self, x, y):
        from services.radar_analysis import latlon_para_pixel

        lat = self.lat + y / 111.195
        lon = self.lon + x / (111.195 * math.cos(math.radians(self.lat)))
        return latlon_para_pixel(lat, lon, self.bounds, 601, 601)

    def image(self, rectangles):
        image = Image.new("RGBA", (601, 601), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        for left, bottom, right, top, color in rectangles:
            x0, y1 = self.pixel(left, bottom)
            x1, y0 = self.pixel(right, top)
            draw.rectangle(tuple(round(p) for p in (x0, y0, x1, y1)), fill=(*color, 255))
        png = BytesIO()
        image.save(png, format="PNG")
        return png.getvalue()

    def cycle(self, index, rectangles):
        from services.radar_service import RadarFrame
        from workers.radar_updater import processar_frame
        from workers.nowcasting_updater import executar_ciclo

        self.now = self.start + timedelta(minutes=5 * index)
        client = mock.Mock()
        client.baixar_imagem.return_value = self.image(rectangles)
        # A posição real do radar está distante do alvo; um eco em São José
        # não é marcado como interferência só por proximidade à antena.
        frame = RadarFrame(
            "jr", "maxcappi", self.now, f"https://radar.example.test/{index}.png",
            -20.27855, -54.47396,
            self.bounds.lat_min, self.bounds.lat_max, self.bounds.lon_min, self.bounds.lon_max,
            400, len(client.baixar_imagem.return_value),
        )
        frame_id, clusters, changed = processar_frame(client, frame, self.radar_config, self.now)
        self.assertTrue(changed)
        self.assertEqual(clusters, 1)
        with ExitStack() as stack:
            for module in (
                "time_utils", "services.nowcasting_repository", "services.nowcasting_service",
                "services.nowcasting_public_alerts", "services.regional_stations_repository",
            ):
                stack.enter_context(mock.patch(f"{module}.agora_utc", return_value=self.now))
            result = executar_ciclo(self.config)
        self.assertEqual(result["snapshot"]["radar"]["frame_id"], frame_id)
        self.assertTrue(result["snapshot"]["radar"]["operacional"])
        return result

    def deliver(self):
        from workers import whatsapp_sender

        with mock.patch.object(whatsapp_sender, "nowcasting_config", return_value=self.config), \
                mock.patch.object(whatsapp_sender, "agora_utc", return_value=self.now), \
                mock.patch.object(whatsapp_sender, "enviar_whatsapp") as send:
            result = whatsapp_sender.processar_um_envio()
        return result, send

    def assert_no_delivery(self):
        result, send = self.deliver()
        self.assertIsNone(result)
        send.assert_not_called()
        conn = self.database.get_db()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        finally:
            conn.close()

    def test_chuva_cruzando_sao_jose_confirma_dois_frames_e_entrega(self):
        for index in range(5):
            x = -45 + 5 * index
            result = self.cycle(index, [(x - 5, -5, x + 5, 5, (84, 253, 0))])
            self.assertEqual(result["public_enqueued"], 1 if index == 4 else 0)
        alerta = result["snapshot"]["alerta_preventivo"]
        self.assertTrue(alerta["projected_impact"])
        self.assertGreater(alerta["projected_impact_eta_minutes"], 0)
        result, send = self.deliver()
        self.assertEqual(result, "enviado")
        send.assert_called_once()
        self.assertIn("São José", send.call_args.args[1])
        self.assertIn("Estimativa de entrada na área monitorada:", send.call_args.args[1])

    def test_centro_passando_ao_lado_com_chuva_atingindo_entrega(self):
        for index in range(5):
            x = -45 + 5 * index
            result = self.cycle(index, [(x - 5, 3, x + 5, 57, (84, 253, 0))])
        alerta = result["snapshot"]["alerta_preventivo"]
        self.assertGreater(alerta["closest_approach_km"], 25)
        self.assertTrue(alerta["projected_impact"])
        self.assertEqual(result["public_enqueued"], 1)
        result, send = self.deliver()
        self.assertEqual(result, "enviado")
        send.assert_called_once()

    def assert_area_ja_presente(self, bottom, top):
        for index in range(5):
            x = 5 * index
            result = self.cycle(index, [(x - 40, bottom, x + 40, top, (84, 253, 0))])
        alerta = result["snapshot"]["alerta_preventivo"]
        self.assertEqual(alerta["projected_impact_eta_minutes"], 0)
        self.assertEqual(result["public_enqueued"], 1)
        conn = self.database.get_db_readonly()
        try:
            event = conn.execute("SELECT mensagem FROM alertas_eventos").fetchone()[0]
            queued = conn.execute("SELECT mensagem FROM alertas_fila").fetchone()[0]
        finally:
            conn.close()
        for text in (event, queued):
            self.assertIn("já está na área monitorada", text)
            self.assertNotIn("se aproximando", text)
            self.assertNotIn("0 km", text)
            self.assertNotIn("0 min", text)
            self.assertIn("não confirma chuva no solo na estação", text)
        delivered, send = self.deliver()
        self.assertEqual(delivered, "enviado")
        self.assertEqual(send.call_args.args[1], queued)
        return alerta

    def test_png_sobre_estacao_preserva_zero_bruto_e_corrige_evento_fila_e_texto(self):
        alerta = self.assert_area_ja_presente(-5, 5)
        self.assertAlmostEqual(alerta["distance_km"], 0, places=6)

    def test_png_dentro_do_raio_sem_eco_na_estacao_nao_promete_chegada(self):
        alerta = self.assert_area_ja_presente(3, 57)
        self.assertGreater(alerta["distance_km"], 2)
        self.assertLess(alerta["distance_km"], 5)

    def test_centro_ja_afastando_com_chuva_atras_ainda_chegando_entrega(self):
        for index in range(5):
            x = 5 * index
            green, blue = (84, 253, 0), (29, 85, 191)
            result = self.cycle(index, [
                (x - 60, -5, x - 45, 5, green),
                (x - 53, 4, x - 51, 56, blue),
                (x - 53, 54, x + 40, 56, blue),
                (x - 5, 45, x + 40, 65, blue),
            ])
        alerta = result["snapshot"]["alerta_preventivo"]
        conn = self.database.get_db()
        try:
            centro = conn.execute("SELECT aproximando FROM radar_tracks WHERE id=?", (alerta["track_id"],)).fetchone()
            self.assertEqual(centro["aproximando"], 0)
        finally:
            conn.close()
        self.assertTrue(alerta["approaching"])
        self.assertGreater(alerta["projected_impact_eta_minutes"], 0)
        self.assertEqual(result["public_enqueued"], 1)
        result, send = self.deliver()
        self.assertEqual(result, "enviado")
        send.assert_called_once()

    def test_passagem_lateral_sem_chuva_no_alvo_nao_enfileira(self):
        for index in range(5):
            x = -45 + 5 * index
            result = self.cycle(index, [(x - 5, 25, x + 5, 35, (254, 0, 0))])
            self.assertEqual(result["public_enqueued"], 0)
        self.assertFalse(result["snapshot"]["alerta_preventivo"]["projected_impact"])
        self.assert_no_delivery()

    def test_chuva_muito_proxima_afastando_nao_enfileira(self):
        for index in range(5):
            x = 5 + index
            result = self.cycle(index, [(x - 2, -2, x + 2, 2, (254, 0, 0))])
            self.assertEqual(result["public_enqueued"], 0)
        self.assertFalse(result["snapshot"]["alerta_preventivo"]["approaching"])
        self.assert_no_delivery()

    def test_eco_fixo_nao_enfileira(self):
        for index in range(5):
            result = self.cycle(index, [(-15, -5, -5, 5, (254, 0, 0))])
            self.assertEqual(result["public_enqueued"], 0)
        self.assertFalse(result["snapshot"]["alerta_preventivo"]["tracking_valid"])
        self.assert_no_delivery()


if __name__ == "__main__":
    unittest.main()
