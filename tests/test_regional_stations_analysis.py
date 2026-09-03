import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

from services.regional_stations_analysis import (  # noqa: E402
    calcular_tendencias,
    classificar_status,
    diferenca_angular_graus,
    direcao_cardinal,
    haversine_km,
)


class RegionalStationsAnalysisTest(unittest.TestCase):
    def row(self, hours_ago, temp, humidity, pressure, wind, gust, direction, rain):
        momento = datetime(2026, 9, 2, 19, tzinfo=timezone.utc) - timedelta(hours=hours_ago)
        return {
            "medido_em_utc": momento.isoformat(),
            "temperatura_atual": temp,
            "umidade_atual": humidity,
            "pressao_atual": pressure,
            "vento_velocidade_kmh": wind,
            "rajada_kmh": gust,
            "vento_direcao_graus": direction,
            "chuva_mm": rain,
        }

    def test_diferenca_angular_359_para_1(self):
        self.assertEqual(diferenca_angular_graus(359, 1), 2)
        self.assertEqual(diferenca_angular_graus(1, 359), -2)

    def test_haversine_e_posicao(self):
        distancia = haversine_km(-22.4925326, -54.4610352, -22.19388888, -54.91138888)
        self.assertGreater(distancia, 40)
        self.assertLess(distancia, 70)
        self.assertIn(direcao_cardinal(315), {"NO"})

    def test_tendencias_1h_3h_e_chuva(self):
        rows = [
            self.row(0, 26, 38, 965.4, 10.8, 28.8, 1, 1.0),
            self.row(1, 25, 42, 966.1, 7.2, 21.6, 359, 0.5),
            self.row(3, 23, 50, 966.8, 3.6, 14.4, 350, 0),
        ]
        trend = calcular_tendencias(rows)
        self.assertEqual(trend["temperatura_1h"], 1)
        self.assertEqual(trend["temperatura_3h"], 3)
        self.assertEqual(trend["umidade_1h"], -4)
        self.assertAlmostEqual(trend["pressao_3h"], -1.4)
        self.assertEqual(trend["direcao_vento_1h"], 2)
        self.assertEqual(trend["chuva_1h"], 1.0)
        self.assertEqual(trend["chuva_3h"], 1.5)

    def test_tendencias_sem_dados_sao_null(self):
        trend = calcular_tendencias([])
        self.assertTrue(all(value is None for value in trend.values()))

    def test_vinte_minutos_nao_sao_tratados_como_tendencia_de_uma_hora(self):
        atual = self.row(0, 23, 70, 968.5, 18, 29, 10, 2)
        anterior = self.row(0, 26, 50, 970, 4, 7, 5, 2)
        anterior["medido_em_utc"] = (
            datetime(2026, 9, 2, 19, tzinfo=timezone.utc) - timedelta(minutes=20)
        ).isoformat()
        trend = calcular_tendencias([atual, anterior])
        self.assertIsNone(trend["temperatura_1h"])
        self.assertIsNone(trend["chuva_1h"])

    def test_status_freshness(self):
        now = datetime(2026, 9, 2, 20, tzinfo=timezone.utc)
        def obs(minutes):
            return {"medido_em_utc": (now - timedelta(minutes=minutes)).isoformat()}
        self.assertEqual(classificar_status(obs(60), now=now), "OK")
        self.assertEqual(classificar_status(obs(180), now=now), "ATRASADA")
        self.assertEqual(classificar_status(obs(300), now=now), "MUITO_ATRASADA")
        self.assertEqual(classificar_status({}, now=now), "SEM_DADOS")
        self.assertEqual(classificar_status({"medido_em_utc": None}, now=now), "TIMESTAMP_INDEFINIDO")
        self.assertEqual(classificar_status(obs(-30), now=now), "TIMESTAMP_INDEFINIDO")
        self.assertEqual(classificar_status(obs(10), source_status="AUSENTE", now=now), "ERRO_FONTE")


if __name__ == "__main__":
    unittest.main()
