import sys
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

from services.radar_analysis import (  # noqa: E402
    DetectionConfig,
    GeoBounds,
    TrackPoint,
    analisar_track,
    abrir_imagem_png,
    detectar_clusters,
    direcao_cardinal,
    haversine_km,
    latlon_para_pixel,
    pixel_para_latlon,
    pode_associar,
)


class RadarAnalysisTest(unittest.TestCase):
    def setUp(self):
        self.bounds = GeoBounds(-1, 1, -1, 1)
        self.config = DetectionConfig(
            min_cluster_pixels=100,
            close_iterations=2,
            dilate_iterations=1,
            clutter_radius_km=50,
        )

    def test_latlon_pixel_e_operacao_inversa(self):
        x, y = latlon_para_pixel(-0.25, 0.4, self.bounds, 750, 750)
        lat, lon = pixel_para_latlon(x, y, self.bounds, 750, 750)
        self.assertAlmostEqual(lat, -0.25, places=8)
        self.assertAlmostEqual(lon, 0.4, places=8)

    def test_posicao_escola_dentro_imagem_real(self):
        bounds = GeoBounds(-23.830664, -16.642761, -58.226281, -50.543479)
        x, y = latlon_para_pixel(-22.4925326, -54.4610352, bounds, 750, 750)
        self.assertTrue(0 <= x < 750)
        self.assertTrue(0 <= y < 750)

    def test_haversine_um_grau(self):
        self.assertAlmostEqual(haversine_km(0, 0, 1, 0), 111.195, places=2)

    def test_direcoes_cardinais_em_portugues(self):
        self.assertEqual([direcao_cardinal(v) for v in (0, 45, 90, 180, 270)], ["N", "NE", "L", "S", "O"])

    def test_ruido_de_um_pixel_nao_vira_cluster(self):
        imagem = Image.new("RGB", (100, 100), "black")
        imagem.putpixel((50, 50), (0, 180, 0))
        clusters = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)
        self.assertEqual(clusters, [])

    def test_cluster_maior_que_limite_e_detectado(self):
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((40, 40, 55, 55), fill=(0, 180, 0))
        clusters = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)
        self.assertEqual(len(clusters), 1)
        self.assertGreaterEqual(clusters[0].pixels_eco, 100)

    def test_distancia_da_borda_e_menor_que_do_centro(self):
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((40, 40, 60, 60), fill=(0, 180, 0))
        cluster = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)[0]
        self.assertLess(cluster.distancia_borda_escola_km, cluster.distancia_centro_escola_km)

    def test_cluster_perto_radar_marcado_e_nao_excluido(self):
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((45, 45, 55, 55), fill=(0, 180, 0))
        clusters = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)
        self.assertEqual(len(clusters), 1)
        self.assertTrue(clusters[0].suspeito_clutter)

    def _ponto(self, minuto, lat, lon=0):
        return TrackPoint(
            datetime(2026, 9, 2, 18, 0) + timedelta(minutes=minuto),
            lat,
            lon,
            haversine_km(lat, lon, 0, 0),
            max(0, haversine_km(lat, lon, 0, 0) - 8),
            200,
        )

    def test_associacao_respeita_timestamp_e_velocidade(self):
        valido, _ = pode_associar(self._ponto(0, 0.8), self._ponto(20, 0.6), 150)
        impossivel, _ = pode_associar(self._ponto(0, 0.8), self._ponto(1, 0.1), 150)
        self.assertTrue(valido)
        self.assertFalse(impossivel)

    def test_tracking_intervalos_irregulares_velocidade_e_direcao(self):
        pontos = [self._ponto(0, 0.8), self._ponto(20, 0.6), self._ponto(30, 0.4)]
        track = analisar_track(pontos, 0, 0)
        self.assertEqual(track.quantidade_frames, 3)
        self.assertAlmostEqual(track.duracao_minutos, 30)
        self.assertGreater(track.velocidade_media_kmh, 80)
        self.assertEqual(track.direcao_movimento, "S")
        self.assertTrue(track.aproximando)

    def test_eta_apenas_com_trajetoria_compativel(self):
        pontos = [self._ponto(0, 0.8), self._ponto(20, 0.6), self._ponto(40, 0.4)]
        track = analisar_track(pontos, 0, 0, intercept_radius_km=25)
        self.assertTrue(track.trajetoria_compativel)
        self.assertIsNotNone(track.eta_minutos)
        self.assertEqual(track.status, "TRAJETORIA_COMPATIVEL")

    def test_eta_null_quando_trajetoria_nao_intercepta(self):
        pontos = [self._ponto(0, 0.6, -1), self._ponto(20, 0.6, -0.8), self._ponto(40, 0.6, -0.6)]
        track = analisar_track(pontos, 0, 0, intercept_radius_km=25)
        self.assertTrue(track.aproximando)
        self.assertFalse(track.trajetoria_compativel)
        self.assertIsNone(track.eta_minutos)

    def test_afastamento(self):
        track = analisar_track([self._ponto(0, 0.4), self._ponto(20, 0.6), self._ponto(40, 0.8)], 0, 0)
        self.assertFalse(track.aproximando)
        self.assertEqual(track.status, "AFASTANDO")

    def test_dados_insuficientes_nao_geram_eta(self):
        track = analisar_track([self._ponto(0, 0.8), self._ponto(20, 0.6)], 0, 0)
        self.assertEqual(track.status, "DADOS_INSUFICIENTES")
        self.assertIsNone(track.eta_minutos)

    def test_apenas_png_e_aceito(self):
        png = BytesIO()
        Image.new("RGB", (10, 10), "black").save(png, "PNG")
        self.assertEqual(abrir_imagem_png(png.getvalue()).size, (10, 10))
        jpeg = BytesIO()
        Image.new("RGB", (10, 10), "black").save(jpeg, "JPEG")
        with self.assertRaisesRegex(ValueError, "PNG"):
            abrir_imagem_png(jpeg.getvalue())


if __name__ == "__main__":
    unittest.main()
