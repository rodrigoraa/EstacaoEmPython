import sys
import unittest
from copy import deepcopy
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
            "algorithm_version": "1.1",
            "target_lat": -22.49,
            "target_lon": -54.46,
            "regional_confirm_min_signals": 2,
            "regional_confirm_min_stations": 1,
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
            "trend_source": "local_history_layer0", "trend_quality": "GOOD",
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
        self.assertTrue(state["confirmacao_regional"]["confirmada"])
        self.assertEqual(state["confirmacao_regional"]["stations"], ["A749"])

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

    def test_timestamp_suspect_bloqueia_tracking_e_eta_no_nowcasting(self):
        radar = self.radar()
        radar["frame"]["timestamp_status"] = "suspect"
        state = analisar_nowcasting(
            radar, {"stations": [self.station()]}, self.local(), self.config, self.now
        )
        self.assertIsNone(state["radar"]["eta_minutos"])
        self.assertFalse(state["radar"]["trajetoria_compativel"])
        self.assertNotEqual(state["status"], "ATENCAO_PREVENTIVA")

    def test_clutter_persistente_reduz_indice_sem_excluir_eco(self):
        normal = analisar_nowcasting(
            self.radar(), {"stations": []}, self.local(), self.config, self.now
        )
        clutter = analisar_nowcasting(
            self.radar(clutter=0.9), {"stations": []}, self.local(), self.config, self.now
        )
        self.assertLess(clutter["indice_evidencia"], normal["indice_evidencia"])
        self.assertTrue(clutter["radar"]["disponivel"])

    def test_radar_sozinho_nunca_gera_atencao_preventiva(self):
        radar = self.radar()
        radar["cluster_mais_proximo"]["distancia_borda_escola_km"] = 30
        state = analisar_nowcasting(
            radar, {"stations": []}, self.local(), self.config, self.now
        )
        self.assertNotEqual(state["status"], "ATENCAO_PREVENTIVA")
        self.assertEqual(state["status"], "TRAJETORIA_RELEVANTE")
        self.assertLessEqual(state["indice_evidencia"], 55)
        self.assertTrue(state["radar_only"])

    def test_confirmacao_regional_exige_sinais_frescos_a_montante(self):
        state = analisar_nowcasting(
            self.radar(), {"stations": [self.station()]},
            self.local(), self.config, self.now,
        )
        self.assertTrue(state["confirmacao_regional"]["confirmada"])
        self.assertGreaterEqual(state["confirmacao_regional"]["evidence_count"], 2)
        self.assertEqual(state["status"], "ATENCAO_PREVENTIVA")

    def test_estacao_com_alteracao_fora_do_corredor_nao_confirma(self):
        fora = self.station(lat=-22.8, lon=-53.3)
        state = analisar_nowcasting(
            self.radar(), {"stations": [fora]}, self.local(), self.config, self.now
        )
        self.assertFalse(state["confirmacao_regional"]["confirmada"])
        self.assertEqual(state["status"], "TRAJETORIA_RELEVANTE")

    def test_estacao_stale_com_sinais_fortes_nao_confirma(self):
        state = analisar_nowcasting(
            self.radar(), {"stations": [self.station(status="ATRASADA")]},
            self.local(), self.config, self.now,
        )
        self.assertFalse(state["confirmacao_regional"]["confirmada"])
        self.assertNotEqual(state["status"], "ATENCAO_PREVENTIVA")

    def test_multi_ameaca_preserva_secundaria_e_escolhe_trajetoria(self):
        radar = self.radar()
        track_a = deepcopy(radar["tracking"])
        cluster_a = deepcopy(radar["cluster_mais_proximo"])
        track_a["track_id"] = 12
        cluster_a["distancia_borda_escola_km"] = 60
        track_b = deepcopy(track_a)
        cluster_b = deepcopy(cluster_a)
        track_b.update({"track_id": 18, "trajetoria_compativel": False, "eta_minutos": None})
        cluster_b["distancia_borda_escola_km"] = 40
        radar["tracks_atuais"] = [
            {"track": track_b, "cluster": cluster_b},
            {"track": track_a, "cluster": cluster_a},
        ]
        state = analisar_nowcasting(
            radar, {"stations": []}, self.local(), self.config, self.now
        )
        self.assertEqual(len(state["ameacas"]), 2)
        self.assertEqual(state["ameaca_principal"]["track_id"], 12)
        self.assertEqual(state["ameacas"][1]["track_id"], 18)

    def test_duas_ameacas_em_direcao_a_escola_permanecem_no_estado(self):
        radar = self.radar()
        track_a = deepcopy(radar["tracking"])
        cluster_a = deepcopy(radar["cluster_mais_proximo"])
        track_b = deepcopy(track_a)
        cluster_b = deepcopy(cluster_a)
        track_a["track_id"] = 12
        cluster_a["distancia_borda_escola_km"] = 60
        track_b["track_id"] = 18
        track_b["eta_minutos"] = 110
        cluster_b["distancia_borda_escola_km"] = 90
        radar["tracks_atuais"] = [
            {"track": track_a, "cluster": cluster_a},
            {"track": track_b, "cluster": cluster_b},
        ]
        state = analisar_nowcasting(
            radar, {"stations": []}, self.local(), self.config, self.now
        )
        self.assertEqual([item["track_id"] for item in state["ameacas"]], [12, 18])
        self.assertTrue(all(item["trajectory_compatible"] for item in state["ameacas"]))

    def test_status_mais_alto_define_principal_mesmo_mais_distante(self):
        radar = self.radar()
        track_a = deepcopy(radar["tracking"])
        cluster_a = deepcopy(radar["cluster_mais_proximo"])
        track_b = deepcopy(track_a)
        cluster_b = deepcopy(cluster_a)
        track_a["track_id"] = 12
        cluster_a["distancia_borda_escola_km"] = 40
        track_b["track_id"] = 18
        cluster_b["distancia_borda_escola_km"] = 60
        radar["tracks_atuais"] = [
            {"track": track_a, "cluster": cluster_a},
            {"track": track_b, "cluster": cluster_b},
        ]
        # Só o track B tem uma estação a montante no seu corredor.
        track_a["centro_lon"] = -53.4
        state = analisar_nowcasting(
            radar, {"stations": [self.station()]}, self.local(), self.config, self.now
        )
        ameacas = {item["track_id"]: item for item in state["ameacas"]}
        self.assertEqual(ameacas[12]["status"], "TRAJETORIA_RELEVANTE")
        self.assertEqual(ameacas[18]["status"], "ATENCAO_PREVENTIVA")
        self.assertEqual(state["ameaca_principal"]["track_id"], 18)
        self.assertEqual(state["status"], "ATENCAO_PREVENTIVA")

    def test_layer0_fresca_sem_historico_suficiente_nao_confirma(self):
        station = self.station()
        station["trend_quality"] = "INSUFFICIENT"
        station["trend"] = {key: None for key in station["trend"]}
        state = analisar_nowcasting(
            self.radar(), {"stations": [station]}, self.local(), self.config, self.now
        )
        self.assertEqual(state["status"], "TRAJETORIA_RELEVANTE")
        self.assertFalse(state["confirmacao_regional"]["confirmada"])
        self.assertTrue(state["historico_regional_em_formacao"])

    def test_evento_local_observado_nao_vira_prevencao(self):
        local = self.local()
        local["rain_rate"] = 8
        state = analisar_nowcasting(
            self.radar(), {"stations": []}, local, self.config, self.now
        )
        self.assertTrue(state["evento_local_observado"])
        self.assertNotEqual(state["status"], "ATENCAO_PREVENTIVA")


if __name__ == "__main__":
    unittest.main()
