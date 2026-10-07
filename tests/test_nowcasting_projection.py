import sys
import math
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))

from services.radar_analysis import (GeoBounds, TrackPoint, latlon_para_pixel,
                                     projetar_footprint)


class ProjectionTest(unittest.TestCase):
    bounds = GeoBounds(-2, 2, -2, 2)

    def pontos(self, coords):
        inicio = datetime(2026, 9, 1, tzinfo=timezone.utc)
        return [TrackPoint(inicio + timedelta(minutes=15 * i), y / 111.195,
                           x / 111.195, (x*x + y*y) ** .5, 20, 150)
                for i, (x, y) in enumerate(coords)]

    def footprint(self, xs, ys):
        left, bottom = latlon_para_pixel(ys[0] / 111.195, xs[0] / 111.195, self.bounds, 1001, 1001)
        right, top = latlon_para_pixel(ys[1] / 111.195, xs[1] / 111.195, self.bounds, 1001, 1001)
        return {"format": "pixel_runs_v1", "runs": [
            [y, left, right] for y in range(math.ceil(top), math.floor(bottom) + 1)
        ]}

    def calcular(self, points, footprint):
        return projetar_footprint(points, footprint, self.bounds, 1001, 1001, 0, 0)

    def test_area_larga_intercepta_com_centro_a_18_km(self):
        points = self.pontos([(-70, 18), (-60, 18), (-50, 18), (-40, 18), (-30, 18)])
        result = self.calcular(points, self.footprint((-35, -25), (3, 28)))
        self.assertEqual(result["trajectory_confidence"], "ALTA")
        self.assertEqual(result["trajectory_frames_used"], 5)
        self.assertTrue(result["projected_impact"])
        self.assertLessEqual(result["projected_impact_eta_minutes"], 45)

    def test_passagem_lateral_com_distancia_inicial_decrescente(self):
        points = self.pontos([(-70, 22), (-60, 22), (-50, 22), (-40, 22), (-30, 22)])
        result = self.calcular(points, self.footprint((-35, -25), (19, 25)))
        self.assertEqual(result["trajectory_confidence"], "ALTA")
        self.assertFalse(result["projected_impact"])
        self.assertGreater(result["projected_impact_min_distance_km"], 12)

    def test_movimento_irregular_baixa_confianca(self):
        points = self.pontos([(-60, 0), (-50, 10), (-40, -12), (-46, 5), (-30, 0)])
        result = self.calcular(points, self.footprint((-35, -25), (-5, 5)))
        self.assertEqual(result["trajectory_confidence"], "BAIXA")

    def test_frames_insuficientes_e_repetidos(self):
        points = self.pontos([(-50, 0), (-40, 0), (-30, 0)])
        points.append(points[-1])
        result = self.calcular(points, self.footprint((-35, -25), (-5, 5)))
        self.assertEqual(result["trajectory_frames_used"], 3)
        self.assertEqual(result["trajectory_confidence"], "BAIXA")

    def test_passagem_rapida_entre_horizontes_tem_eta_preciso(self):
        points = self.pontos([(-135, 0), (-105, 0), (-75, 0), (-45, 0), (-15, 0)])
        result = self.calcular(points, self.footprint((-15.5, -14.5), (-.5, .5)))
        self.assertEqual(result["trajectory_confidence"], "ALTA")
        self.assertTrue(result["projected_impact"])
        self.assertAlmostEqual(result["projected_impact_eta_minutes"], 4.75, places=3)
        self.assertTrue(any(p["intersects"] for p in result["projections"]))

    def test_celula_distante_intercepta_dentro_de_duas_horas(self):
        points = self.pontos([(-180, 0), (-165, 0), (-150, 0), (-135, 0), (-120, 0)])
        result = self.calcular(points, self.footprint((-125, -115), (-5, 5)))
        self.assertTrue(result["projected_impact"])
        self.assertAlmostEqual(result["projected_impact_eta_minutes"], 110, places=3)
        self.assertEqual(result["projected_impact_horizon_minutes"], 120)

    def test_lacuna_reinicia_frames_de_confirmacao(self):
        points = self.pontos([(-70, 0), (-60, 0), (-50, 0), (-40, 0), (-30, 0)])
        final = points[-1]
        points[-1] = TrackPoint(final.data_frame + timedelta(minutes=20), final.centro_lat,
                               final.centro_lon, final.distancia_centro_escola_km,
                               final.distancia_borda_escola_km, final.pixels_eco)
        result = self.calcular(points, self.footprint((-35, -25), (-5, 5)))
        self.assertEqual(result["trajectory_frames_used"], 1)
        self.assertFalse(result["trajectory_recent_valid"])
        self.assertEqual(result["trajectory_confidence"], "BAIXA")

    def test_curva_recente_invalida_linha_historica(self):
        points = self.pontos([(-70, 0), (-60, 0), (-50, 0), (-40, 0), (-40, 10)])
        result = self.calcular(points, self.footprint((-45, -35), (5, 15)))
        self.assertFalse(result["trajectory_recent_valid"])
        self.assertIsNone(result["trajectory_approaching"])
        self.assertEqual(result["trajectory_confidence"], "BAIXA")

    def test_variacao_grande_de_velocidade_bloqueia_confianca(self):
        points = self.pontos([(-80, 0), (-75, 0), (-70, 0), (-55, 0), (-30, 0)])
        result = self.calcular(points, self.footprint((-35, -25), (-5, 5)))
        self.assertFalse(result["trajectory_recent_valid"])

    def test_historico_antigo_nao_sobrepoe_movimento_recente(self):
        points = self.pontos([(-10, 0), (-40, 0), (-80, 0), (-70, 0), (-60, 0),
                              (-50, 0), (-40, 0), (-30, 0), (-20, 0)])
        result = self.calcular(points, self.footprint((-25, -15), (-5, 5)))
        self.assertEqual(result["trajectory_frames_used"], 6)
        self.assertTrue(result["trajectory_recent_valid"])
        self.assertTrue(result["trajectory_approaching"])

    def test_corredor_vazio_do_casco_convexo_nao_intercepta(self):
        points = self.pontos([(-45, 0), (-35, 0), (-25, 0), (-15, 0), (-5, 0)])
        superior = self.footprint((-30, 20), (10, 12))
        inferior = self.footprint((-30, 20), (-12, -10))
        conexao = self.footprint((19, 20), (-10, 10))
        real = {"format": "pixel_runs_v1", "runs": superior["runs"] + inferior["runs"] + conexao["runs"]}
        result = self.calcular(points, real)
        self.assertTrue(result["trajectory_recent_valid"])
        self.assertFalse(result["projected_impact"])
        self.assertGreater(result["projected_impact_min_distance_km"], 9)
        legado = [latlon_para_pixel(y / 111.195, x / 111.195, self.bounds, 1001, 1001)
                  for x, y in ((-30, -12), (20, -12), (20, 12), (-30, 12))]
        result_legado = self.calcular(points, legado)
        self.assertTrue(result_legado["projected_impact"])
        self.assertEqual(result_legado["trajectory_confidence"], "BAIXA")

    def test_raio_pequeno_nao_pula_eco_rapido(self):
        points = self.pontos([(-135, 0), (-105, 0), (-75, 0), (-45, 0), (-15, 0)])
        result = projetar_footprint(points, self.footprint((-15.5, -14.5), (-.5, .5)),
                                   self.bounds, 1001, 1001, 0, 0, impact_radius_km=1)
        self.assertTrue(result["projected_impact"])
        self.assertAlmostEqual(result["projected_impact_eta_minutes"], 6.75, places=3)


if __name__ == "__main__":
    unittest.main()
