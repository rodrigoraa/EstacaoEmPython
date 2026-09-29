import sys
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
        return [latlon_para_pixel(y / 111.195, x / 111.195, self.bounds, 1001, 1001)
                for x, y in ((xs[0], ys[0]), (xs[1], ys[0]),
                             (xs[1], ys[1]), (xs[0], ys[1]))]

    def calcular(self, points, footprint):
        return projetar_footprint(points, footprint, self.bounds, 1001, 1001, 0, 0)

    def test_area_larga_intercepta_com_centro_a_18_km(self):
        points = self.pontos([(-70, 18), (-60, 18), (-50, 18), (-40, 18), (-30, 18)])
        result = self.calcular(points, self.footprint((-35, -25), (8, 28)))
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


if __name__ == "__main__":
    unittest.main()
