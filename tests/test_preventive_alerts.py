import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

from services.preventive_alerts import (  # noqa: E402
    classificar_nivel_proximidade,
    criar_alerta_preventivo,
    estimar_eta_borda,
    selecionar_eco_alerta_proximidade,
)


class PreventiveAlertsTest(unittest.TestCase):
    def ameaca(self, distancia, clutter=None):
        return {
            "cluster_id": 109,
            "track_id": 9,
            "distance_km": distancia,
            "tracking_valid": True,
            "tracking_quality": "BOA",
            "frame_count": 5,
            "duration_minutes": 20,
            "approaching": True,
            "trajectory_compatible": True,
            "direction": "L",
            "speed_kmh": 45,
            "eta_minutes": 30,
            "eta_border_minutes": 20,
            "border_approach_rate_kmh": 36,
            "suspeito_clutter": clutter is not None,
            "indice_persistencia_clutter": clutter,
        }

    def alerta(self, distancia, clutter=None):
        return criar_alerta_preventivo(
            self.ameaca(distancia, clutter),
            radar_atualizado=True,
            evento_local=False,
        )

    def test_limites_inclusivos_usam_distancia_da_borda(self):
        casos = (
            (120, "NORMAL"),
            (100.1, "NORMAL"),
            (100, "AMARELO"),
            (99.9, "AMARELO"),
            (90, "AMARELO"),
            (50.1, "AMARELO"),
            (50, "LARANJA"),
            (49.9, "LARANJA"),
            (49, "LARANJA"),
            (25.1, "LARANJA"),
            (25, "VERMELHO"),
            (24.9, "VERMELHO"),
            (24, "VERMELHO"),
        )
        for distancia, esperado in casos:
            with self.subTest(distancia=distancia):
                self.assertEqual(classificar_nivel_proximidade(distancia), esperado)
                self.assertEqual(self.alerta(distancia)["nivel"], esperado)

    def test_distancia_invalida_nao_produz_alerta(self):
        for distancia in (None, -1, "invalida", float("nan"), float("inf")):
            with self.subTest(distancia=distancia):
                self.assertEqual(classificar_nivel_proximidade(distancia), "NORMAL")

    def test_radar_nao_operacional_e_indisponivel_em_vez_de_normal(self):
        alerta = criar_alerta_preventivo(
            None,
            radar_atualizado=False,
            evento_local=False,
        )
        self.assertEqual(alerta["nivel"], "INDISPONIVEL")
        self.assertEqual(alerta["cor"], "cinza")
        self.assertIn("indisponíveis", alerta["message"])

    def test_selecao_prefere_menor_distancia_confiavel_sem_exigir_tracking(self):
        longe_relevante = self.ameaca(60)
        longe_relevante.update({"track_id": 2, "cluster_id": 102})
        perto_sem_tracking = self.ameaca(20)
        perto_sem_tracking.update({
            "track_id": 1,
            "cluster_id": 101,
            "tracking_valid": False,
            "tracking_quality": "DADOS_INSUFICIENTES",
            "approaching": None,
            "trajectory_compatible": False,
            "direction": None,
            "speed_kmh": None,
            "eta_minutes": None,
            "eta_border_minutes": None,
        })
        selecionado = selecionar_eco_alerta_proximidade(
            [longe_relevante, perto_sem_tracking]
        )
        alerta = criar_alerta_preventivo(
            selecionado, radar_atualizado=True, evento_local=False
        )
        self.assertEqual(selecionado["track_id"], 1)
        self.assertEqual(alerta["nivel"], "VERMELHO")
        self.assertIsNone(alerta["speed_kmh"])
        self.assertIsNone(alerta["eta_minutes"])
        self.assertTrue(alerta["would_send"])

    def test_selecao_ignora_clutter_proximo_quando_ha_eco_confiavel(self):
        clutter = self.ameaca(8, clutter=0.96)
        clutter.update({"track_id": 1, "cluster_id": 101})
        confiavel = self.ameaca(47)
        confiavel.update({"track_id": 2, "cluster_id": 102})
        selecionado = selecionar_eco_alerta_proximidade([clutter, confiavel])
        alerta = criar_alerta_preventivo(
            selecionado, radar_atualizado=True, evento_local=False
        )
        self.assertEqual(selecionado["track_id"], 2)
        self.assertEqual(alerta["nivel"], "LARANJA")
        self.assertFalse(alerta["low_confidence"])

    def test_clutter_proximo_aparece_quando_confiavel_esta_fora_de_100_km(self):
        clutter = self.ameaca(15, clutter=0.96)
        clutter.update({"track_id": 1, "cluster_id": 101})
        confiavel = self.ameaca(180)
        confiavel.update({"track_id": 2, "cluster_id": 102})

        selecionado = selecionar_eco_alerta_proximidade([confiavel, clutter])
        alerta = criar_alerta_preventivo(
            selecionado, radar_atualizado=True, evento_local=False
        )

        self.assertEqual(selecionado["cluster_id"], 101)
        self.assertEqual(alerta["nivel"], "AMARELO")
        self.assertEqual(alerta["distance_km"], 15)
        self.assertTrue(alerta["low_confidence"])
        self.assertFalse(alerta["would_send"])

    def test_clutter_e_confiavel_fora_de_100_km_resultam_normal(self):
        clutter = self.ameaca(120, clutter=0.96)
        confiavel = self.ameaca(180)
        selecionado = selecionar_eco_alerta_proximidade([clutter, confiavel])
        alerta = criar_alerta_preventivo(
            selecionado, radar_atualizado=True, evento_local=False
        )
        self.assertEqual(alerta["nivel"], "NORMAL")
        self.assertFalse(alerta["would_send"])

    def test_confiavel_na_faixa_sempre_vence_clutter_mais_proximo(self):
        casos = (
            (5, 20, "VERMELHO", True),
            (5, 90, "AMARELO", False),
        )
        for distancia_clutter, distancia_confiavel, nivel, candidato in casos:
            with self.subTest(distancia_confiavel=distancia_confiavel):
                clutter = self.ameaca(distancia_clutter, clutter=0.96)
                clutter.update({"track_id": 1, "cluster_id": 101})
                confiavel = self.ameaca(distancia_confiavel)
                confiavel.update({"track_id": 2, "cluster_id": 102})
                selecionado = selecionar_eco_alerta_proximidade(
                    [clutter, confiavel]
                )
                alerta = criar_alerta_preventivo(
                    selecionado, radar_atualizado=True, evento_local=False
                )
                self.assertEqual(selecionado["cluster_id"], 102)
                self.assertEqual(alerta["nivel"], nivel)
                self.assertEqual(alerta["would_send"], candidato)

    def test_somente_clutter_proximo_fica_amarelo_diagnostico(self):
        selecionado = selecionar_eco_alerta_proximidade(
            [self.ameaca(10, clutter=0.96)]
        )
        alerta = criar_alerta_preventivo(
            selecionado, radar_atualizado=True, evento_local=False
        )
        self.assertEqual(alerta["nivel_base"], "VERMELHO")
        self.assertEqual(alerta["nivel"], "AMARELO")
        self.assertTrue(alerta["low_confidence"])
        self.assertFalse(alerta["would_send"])

    def test_radar_nao_operacional_com_clutter_tambem_fica_indisponivel(self):
        alerta = criar_alerta_preventivo(
            self.ameaca(10, clutter=0.96),
            radar_atualizado=False,
            evento_local=False,
        )
        self.assertEqual(alerta["nivel_base"], "INDISPONIVEL")
        self.assertEqual(alerta["nivel"], "INDISPONIVEL")
        self.assertEqual(alerta["cor"], "cinza")
        self.assertFalse(alerta["would_send"])

    def test_clutter_forte_nao_fica_vermelho_nem_candidato_a_envio(self):
        alerta = self.alerta(20, clutter=0.75)
        self.assertEqual(alerta["nivel_base"], "VERMELHO")
        self.assertEqual(alerta["nivel"], "AMARELO")
        self.assertTrue(alerta["low_confidence"])
        self.assertFalse(alerta["would_send"])
        self.assertIn("baixa confiabilidade", alerta["message"])

    def test_vermelho_confiavel_e_somente_candidato_simulado(self):
        alerta = self.alerta(24)
        self.assertEqual(alerta["nivel"], "VERMELHO")
        self.assertTrue(alerta["would_send"])
        self.assertEqual(alerta["preventive_sending"], "DESATIVADO")
        self.assertIn("envio está desativado", alerta["simulation_message"])
        self.assertIn("Confirmação regional ainda não disponível", alerta["message"])

    def test_confirmacao_regional_reforca_texto_sem_definir_o_nivel(self):
        alerta = criar_alerta_preventivo(
            self.ameaca(24),
            radar_atualizado=True,
            evento_local=False,
            confirmacao_regional={
                "confirmada": True, "stations": ["A749"], "evidence_count": 2,
            },
        )
        self.assertEqual(alerta["nivel"], "VERMELHO")
        self.assertTrue(alerta["regional_confirmation"])
        self.assertIn("Sinais também observados", alerta["message"])

    def test_evento_local_tem_prioridade_na_mensagem(self):
        alerta = criar_alerta_preventivo(
            self.ameaca(20),
            radar_atualizado=True,
            evento_local=True,
        )
        self.assertEqual(alerta["message"], "Chuva já observada na EE São José.")
        self.assertTrue(alerta["local_event"])

    def test_eta_borda_usa_tendencia_robusta_do_mesmo_track(self):
        inicio = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        historico = [
            {
                "data_frame": (inicio + timedelta(minutes=5 * indice)).isoformat(),
                "distancia_borda_km": distancia,
            }
            for indice, distancia in enumerate((42, 39, 36, 33, 30))
        ]
        eta = estimar_eta_borda(
            historico,
            tracking_valido=True,
            aproximando=True,
            trajetoria_compativel=True,
        )
        self.assertIsNotNone(eta)
        self.assertAlmostEqual(eta["approach_rate_kmh"], 36.0)
        self.assertAlmostEqual(eta["eta_minutes"], 50.0)
        self.assertEqual(eta["quality"], "BOA")

    def test_eta_borda_some_quando_ruidoso_ou_tracking_invalido(self):
        historico = [
            {"data_frame": "2026-09-04T12:00:00+00:00", "distancia_borda_km": 40},
            {"data_frame": "2026-09-04T12:05:00+00:00", "distancia_borda_km": 32},
            {"data_frame": "2026-09-04T12:10:00+00:00", "distancia_borda_km": 43},
            {"data_frame": "2026-09-04T12:15:00+00:00", "distancia_borda_km": 30},
        ]
        self.assertIsNone(
            estimar_eta_borda(
                historico,
                tracking_valido=True,
                aproximando=True,
                trajetoria_compativel=True,
            )
        )

    def test_eta_borda_ordena_timezone_e_descarta_duplicatas_invalidas(self):
        historico = [
            {"data_frame": "2026-09-04T08:20:00-04:00", "distancia_borda_km": 30},
            {"data_frame": "invalido", "distancia_borda_km": 20},
            {"data_frame": "2026-09-04T12:00:00+00:00", "distancia_borda_km": 42},
            {"data_frame": "2026-09-04T12:10:00+00:00", "distancia_borda_km": -1},
            {"data_frame": "2026-09-04T12:10:00+00:00", "distancia_borda_km": 36},
            {"data_frame": "2026-09-04T12:10:00+00:00", "distancia_borda_km": 36},
        ]
        eta = estimar_eta_borda(
            historico,
            tracking_valido=True,
            aproximando=True,
            trajetoria_compativel=True,
        )
        self.assertIsNotNone(eta)
        self.assertEqual(eta["sample_count"], 3)
        self.assertAlmostEqual(eta["approach_rate_kmh"], 36.0)

    def test_eta_borda_suprime_duracao_velocidade_e_janela_invalidas(self):
        curto = [
            {"data_frame": "2026-09-04T12:00:00+00:00", "distancia_borda_km": 40},
            {"data_frame": "2026-09-04T12:02:00+00:00", "distancia_borda_km": 35},
            {"data_frame": "2026-09-04T12:04:00+00:00", "distancia_borda_km": 30},
        ]
        lento = [
            {"data_frame": "2026-09-04T12:00:00+00:00", "distancia_borda_km": 100},
            {"data_frame": "2026-09-04T12:10:00+00:00", "distancia_borda_km": 99.8},
            {"data_frame": "2026-09-04T12:20:00+00:00", "distancia_borda_km": 99.6},
        ]
        rapido = [
            {"data_frame": "2026-09-04T12:00:00+00:00", "distancia_borda_km": 80},
            {"data_frame": "2026-09-04T12:10:00+00:00", "distancia_borda_km": 40},
            {"data_frame": "2026-09-04T12:20:00+00:00", "distancia_borda_km": 0},
        ]
        for historico in (curto, lento, rapido):
            with self.subTest(historico=historico):
                self.assertIsNone(
                    estimar_eta_borda(
                        historico,
                        tracking_valido=True,
                        aproximando=True,
                        trajetoria_compativel=True,
                    )
                )
        self.assertIsNone(
            estimar_eta_borda(
                historico,
                tracking_valido=False,
                aproximando=True,
                trajetoria_compativel=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
