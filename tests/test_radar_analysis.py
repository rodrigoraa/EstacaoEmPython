import sys
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

from services.radar_analysis import (  # noqa: E402
    DetectionConfig,
    GeoBounds,
    REFLECTIVITY_CLASS_NAMES,
    REFLECTIVITY_PALETTE,
    TrackPoint,
    analisar_track,
    abrir_imagem_png,
    criar_mascaras_eco,
    classificar_pixels_refletividade,
    custo_associacao,
    detectar_clusters,
    diagnosticar_paleta,
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
        imagem.putpixel((50, 50), (43, 185, 0))
        clusters = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)
        self.assertEqual(clusters, [])

    def test_cluster_maior_que_limite_e_detectado(self):
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((40, 40, 55, 55), fill=(43, 185, 0))
        clusters = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)
        self.assertEqual(len(clusters), 1)
        self.assertGreaterEqual(clusters[0].pixels_eco, 100)

    def test_dilatacao_nao_infla_manchinha_abaixo_do_limite(self):
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((40, 40, 47, 47), fill=(43, 185, 0))
        clusters = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)
        self.assertEqual(clusters, [])

    def test_cluster_real_de_cem_pixels_preserva_contagem(self):
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((40, 40, 49, 49), fill=(43, 185, 0))
        cluster = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)[0]
        self.assertEqual(cluster.pixels_eco, 100)

    def test_fragmentos_agrupados_contam_so_pixels_originais_e_intensidade(self):
        imagem = Image.new("RGB", (100, 100), "black")
        draw = ImageDraw.Draw(imagem)
        draw.rectangle((40, 40, 45, 49), fill=(43, 185, 0))
        draw.rectangle((47, 40, 52, 49), fill=(57, 170, 223))
        rgb = np.asarray(imagem)
        original, processada = criar_mascaras_eco(rgb, self.config)
        self.assertGreater(processada.sum(), original.sum())
        cluster = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)[0]
        self.assertEqual(cluster.pixels_eco, 120)
        self.assertEqual(cluster.pixels_refletividade_baixa, 60)
        self.assertEqual(cluster.pixels_refletividade_media, 60)
        self.assertEqual(cluster.classe_maxima, "REFLETIVIDADE_MEDIA")

    def test_distancia_da_borda_e_menor_que_do_centro(self):
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((40, 40, 60, 60), fill=(43, 185, 0))
        cluster = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)[0]
        self.assertLess(cluster.distancia_borda_escola_km, cluster.distancia_centro_escola_km)

    def test_distancia_da_borda_nao_e_aproximada_pela_dilatacao(self):
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((45, 45, 54, 54), fill=(43, 185, 0))
        sem_dilatacao = detectar_clusters(
            imagem, self.bounds, -0.8, -0.8, 0, 0,
            DetectionConfig(
                min_cluster_pixels=20, close_iterations=0, dilate_iterations=0
            ),
        )[0]
        com_dilatacao = detectar_clusters(
            imagem, self.bounds, -0.8, -0.8, 0, 0,
            DetectionConfig(
                min_cluster_pixels=20, close_iterations=0, dilate_iterations=6
            ),
        )[0]
        self.assertEqual(com_dilatacao.pixels_eco, 100)
        self.assertAlmostEqual(
            com_dilatacao.distancia_borda_escola_km,
            sem_dilatacao.distancia_borda_escola_km,
            places=9,
        )

    def test_cluster_perto_radar_marcado_e_nao_excluido(self):
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((45, 45, 55, 55), fill=(43, 185, 0))
        clusters = detectar_clusters(imagem, self.bounds, -0.8, -0.8, 0, 0, self.config)
        self.assertEqual(len(clusters), 1)
        self.assertTrue(clusters[0].suspeito_clutter)

    def test_todas_as_49_cores_reais_recebem_classe_confirmada(self):
        cores = [cor for grupo in REFLECTIVITY_PALETTE.values() for cor in grupo]
        rgb = np.asarray(cores, dtype=np.uint8).reshape(1, len(cores), 3)
        classes = classificar_pixels_refletividade(rgb)
        self.assertEqual(len(cores), 49)
        self.assertTrue(np.all(classes > 0))
        self.assertEqual(set(classes.flatten()), set(REFLECTIVITY_CLASS_NAMES))

    def test_legenda_colorida_fora_do_raio_nao_vira_cluster(self):
        imagem = Image.new("RGB", (100, 100), "black")
        draw = ImageDraw.Draw(imagem)
        draw.rectangle((0, 0, 4, 19), fill=(254, 0, 0))
        draw.rectangle((5, 0, 9, 19), fill=(255, 230, 0))
        draw.rectangle((40, 40, 59, 59), fill=(43, 185, 0))
        config = DetectionConfig(
            min_cluster_pixels=20, close_iterations=0, dilate_iterations=0,
            valid_radius_km=80,
        )
        clusters = detectar_clusters(imagem, self.bounds, 0, 0, 0, 0, config)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].pixels_eco, 400)
        self.assertEqual(clusters[0].classe_maxima, "REFLETIVIDADE_MEDIA")

    def test_nucleo_pequeno_de_classe_maxima_nao_some_na_predominante(self):
        imagem = Image.new("RGB", (100, 100), "black")
        draw = ImageDraw.Draw(imagem)
        draw.rectangle((30, 30, 69, 69), fill=(43, 185, 0))
        draw.rectangle((45, 45, 54, 54), fill=(254, 0, 0))
        cluster = detectar_clusters(
            imagem, self.bounds, 0, 0, 0, 0,
            DetectionConfig(min_cluster_pixels=20, close_iterations=0, dilate_iterations=0),
        )[0]
        self.assertEqual(cluster.classe_predominante, "REFLETIVIDADE_MEDIA")
        self.assertEqual(cluster.classe_maxima, "REFLETIVIDADE_MUITO_ALTA")
        self.assertEqual(cluster.pixels_refletividade_muito_alta, 100)

    def test_diagnostico_informa_cores_descartes_e_mascaras(self):
        imagem = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        draw = ImageDraw.Draw(imagem)
        draw.rectangle((8, 8, 11, 11), fill=(43, 185, 0, 255))
        draw.point((10, 10), fill=(254, 0, 0, 255))
        resumo, original, classes = diagnosticar_paleta(
            imagem, self.bounds, 0, 0, radius_km=100
        )
        self.assertEqual(resumo["dimensoes"], [20, 20])
        self.assertEqual(resumo["pixels_eco"], 16)
        self.assertEqual(resumo["classes"]["REFLETIVIDADE_MUITO_ALTA"], 1)
        self.assertEqual(original.mode, "L")
        self.assertEqual(classes.mode, "RGBA")

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

    def test_custo_prefere_posicao_prevista_e_rejeita_curva_incompativel(self):
        historico = [self._ponto(0, 0.8), self._ponto(20, 0.6)]
        esperado = self._ponto(40, 0.4)
        desvio = self._ponto(40, 0.45, 0.05)
        valido_esperado, custo_esperado, _ = custo_associacao(historico, esperado, 150)
        valido_desvio, custo_desvio, _ = custo_associacao(historico, desvio, 150)
        self.assertTrue(valido_esperado)
        self.assertTrue(valido_desvio)
        self.assertLess(custo_esperado, custo_desvio)

    def test_trackpoint_normaliza_datetime_naive_para_utc(self):
        ponto = self._ponto(0, 0.8)
        self.assertEqual(ponto.data_frame.tzinfo, timezone.utc)

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
