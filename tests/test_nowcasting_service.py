import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

from services.nowcasting_service import (  # noqa: E402
    analisar_nowcasting,
    classificar_estacao_montante,
)


class NowcastingServiceTest(unittest.TestCase):
    def setUp(self):
        self.config = {
            "track_min_frames": 3,
            "upstream_corridor_km": 50,
            "regional_max_age_minutes": 180,
            "algorithm_version": "1.0",
            "target_lat": -22.49,
            "target_lon": -54.46,
        }
        self.now = datetime(2026, 9, 3, 13, 20, tzinfo=timezone.utc)

    def radar(self, trajectory=True, approaching=True, stale=False, clutter=None):
        return {
            "disponivel": True,
            "stale": stale,
            "frame": {"id": 7, "imagem_disponivel": True},
            "cluster_mais_proximo": {
                "distancia_borda_escola_km": 72,
                "suspeito_clutter": clutter is not None,
                "indice_persistencia_clutter": clutter,
            },
            "tracking": {
                "track_id": 12,
                "quantidade_frames": 4,
                "velocidade_kmh": 45,
                "bearing_movimento": 0,
                "direcao_movimento": "N",
                "centro_lat": -22.8,
                "centro_lon": -54.46,
                "aproximando": approaching,
                "trajetoria_compativel": trajectory,
                "eta_minutos": 80,
                "indice_persistencia_clutter": clutter,
            },
        }

    def station(self, status="OK", with_evidence=True, lat=-23.0, lon=-54.45):
        trend = {
            "rain_1h": 5.0 if with_evidence else 0,
            "gust_1h": 18 if with_evidence else 0,
            "wind_1h": 12 if with_evidence else 0,
            "temperature_1h": -2.5 if with_evidence else 0,
            "humidity_1h": 12 if with_evidence else 0,
            "pressure_1h": -1.2 if with_evidence else 0,
        }
        return {
            "code": "A749", "name": "Juti", "status": status,
            "age_minutes": 60, "latitude": lat, "longitude": lon,
            "distance_km": 80, "trend": trend,
        }

    def local(self):
        return {
            "stale": False, "temperature": 27, "humidity": 50,
            "pressure": 965, "wind_speed": 5, "wind_gust": 10,
            "wind_direction": 90, "rain_rate": 0, "rain_today": 0,
        }

    def test_geometria_montante_nao_depende_de_cardinal_exato(self):
        relevante = classificar_estacao_montante(-22.8, -54.46, 12, -23.05, -54.50, 50)
        fora = classificar_estacao_montante(-22.8, -54.46, 12, -22.8, -53.7, 50)
        self.assertTrue(relevante["upstream"])
        self.assertFalse(fora["upstream"])
        self.assertLess(relevante["cross_track_km"], 50)

    def test_corredor_oeste_leste(self):
        caarapo = classificar_estacao_montante(-22.65, -54.5, 90, -22.65, -54.9, 50)
        norte = classificar_estacao_montante(-22.65, -54.5, 90, -21.9, -54.8, 50)
        self.assertTrue(caarapo["upstream"])
        self.assertFalse(norte["upstream"])

    def test_sem_fontes_retorna_sem_dados(self):
        state = analisar_nowcasting({}, {"stations": []}, None, self.config, self.now)
        self.assertEqual(state["status"], "SEM_DADOS")

    def test_fontes_presentes_porem_invalidas_retorna_dados_insuficientes(self):
        state = analisar_nowcasting(
            {}, {"stations": [self.station(status="MUITO_ATRASADA")]},
            None, self.config, self.now,
        )
        self.assertEqual(state["status"], "DADOS_INSUFICIENTES")

    def test_eco_sem_movimento_fica_em_monitoramento(self):
        radar = self.radar()
        radar["tracking"]["quantidade_frames"] = 1
        radar["tracking"]["velocidade_kmh"] = None
        state = analisar_nowcasting(radar, {"stations": []}, self.local(), self.config, self.now)
        self.assertEqual(state["status"], "ECO_EM_MONITORAMENTO")

    def test_aproximando_sem_trajetoria_nao_recebe_eta(self):
        state = analisar_nowcasting(
            self.radar(trajectory=False), {"stations": []}, self.local(), self.config, self.now
        )
        self.assertEqual(state["status"], "SISTEMA_SE_APROXIMANDO")
        self.assertIsNone(state["radar"]["eta_minutos"])

    def test_trajetoria_valida_preserva_eta_conservador(self):
        state = analisar_nowcasting(
            self.radar(), {"stations": []}, self.local(), self.config, self.now
        )
        self.assertEqual(state["status"], "TRAJETORIA_RELEVANTE")
        self.assertEqual(state["radar"]["eta_minutos"], 80)

    def test_estacao_montante_eleva_evidencia_com_regras_explicitas(self):
        state = analisar_nowcasting(
            self.radar(), {"stations": [self.station()]}, self.local(), self.config, self.now
        )
        self.assertIn(state["nivel_evidencia"], {"ELEVADA", "MUITO_ELEVADA"})
        self.assertEqual(state["estacoes_relevantes"][0]["code"], "A749")
        self.assertTrue(any("pressao" in item for item in state["evidencias"]))

    def test_estacao_stale_nao_confirma(self):
        state = analisar_nowcasting(
            self.radar(), {"stations": [self.station(status="ATRASADA")]},
            self.local(), self.config, self.now,
        )
        self.assertFalse(state["estacoes_relevantes"][0]["evidencias"])
        self.assertNotEqual(state["status"], "EVIDENCIA_REGIONAL")

    def test_radar_stale_limita_evidencia(self):
        state = analisar_nowcasting(
            self.radar(stale=True), {"stations": [self.station()]}, self.local(), self.config, self.now
        )
        self.assertLessEqual(state["indice_evidencia"], 24)
        self.assertIn(state["nivel_evidencia"], {"BAIXA", "SEM_EVIDENCIA"})

    def test_clutter_persistente_reduz_indice_sem_excluir_eco(self):
        normal = analisar_nowcasting(
            self.radar(), {"stations": []}, self.local(), self.config, self.now
        )
        clutter = analisar_nowcasting(
            self.radar(clutter=0.9), {"stations": []}, self.local(), self.config, self.now
        )
        self.assertLess(clutter["indice_evidencia"], normal["indice_evidencia"])
        self.assertTrue(clutter["radar"]["disponivel"])


if __name__ == "__main__":
    unittest.main()
