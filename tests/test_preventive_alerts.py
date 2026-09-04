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
)


class PreventiveAlertsTest(unittest.TestCase):
    def ameaca(self, distancia, clutter=None):
        return {
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
            (90, "AMARELO"),
            (49, "LARANJA"),
            (24, "VERMELHO"),
            (100, "AMARELO"),
            (50, "LARANJA"),
            (25, "VERMELHO"),
        )
        for distancia, esperado in casos:
            with self.subTest(distancia=distancia):
                self.assertEqual(classificar_nivel_proximidade(distancia), esperado)
                self.assertEqual(self.alerta(distancia)["nivel"], esperado)

    def test_distancia_invalida_nao_produz_alerta(self):
        for distancia in (None, -1, "invalida", float("nan"), float("inf")):
            with self.subTest(distancia=distancia):
                self.assertEqual(classificar_nivel_proximidade(distancia), "NORMAL")

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
