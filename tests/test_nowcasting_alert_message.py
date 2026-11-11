"""Regressões do significado das distâncias e tempos no texto enviado."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))

from services.nowcasting_alert_evaluation import montar_mensagem_preventiva


class RadarAlertMessageTest(unittest.TestCase):
    levels = ("OBSERVACAO", "INFORMATIVO", "ATENCAO", "ALERTA")

    def message(self, **changes):
        alert = {
            "alert_level": "INFORMATIVO", "distance_km": 20,
            "center_distance_km": 70, "track_id": 1, "tracking_valid": True,
            "approaching": True, "trajectory_compatible": True,
            "trajectory_confidence": "ALTA", "projected_impact": True,
            "projected_impact_eta_minutes": 25,
            # A estimativa legada para a estação não deve substituir a projeção.
            "eta_border_minutes": 99, "eta_border_quality": "BOA",
        }
        alert.update(changes)
        return montar_mensagem_preventiva({"alerta_preventivo": alert})

    def test_zero_eta_descreve_area_ja_presente_sem_chegada_futura(self):
        for level in self.levels:
            with self.subTest(level=level):
                text = self.message(alert_level=level, distance_km=3,
                                    projected_impact_eta_minutes=0)
                self.assertIn("já está na área monitorada", text)
                self.assertNotIn("se aproximando", text)
                self.assertNotIn("Estimativa de entrada", text)
                self.assertNotIn("Estimativa de chegada", text)
                self.assertNotIn("0 min", text)
                self.assertIn("não confirma chuva no solo na estação", text)

    def test_distancias_positivas_pequenas_nao_arredondam_para_zero(self):
        for level in self.levels:
            for distance in (0.0001, 0.2, 0.49, 0.5, 0.99):
                with self.subTest(level=level, distance=distance):
                    text = self.message(alert_level=level, distance_km=distance)
                    self.assertIn("menos de 1 km da estação", text)
                    self.assertNotIn("0 km", text)
                    self.assertNotIn("70 km", text)

    def test_distancia_zero_nao_promete_chuva_observada_na_estacao(self):
        for level in self.levels:
            text = self.message(alert_level=level, distance_km=0,
                                projected_impact_eta_minutes=0)
            self.assertIn("sobre ou muito próxima da estação", text)
            self.assertNotIn("0 km", text)
            self.assertNotIn("se aproximando", text)

    def test_eta_positivo_pequeno_usa_menos_de_um_minuto(self):
        for level in self.levels:
            for eta in (0.001, 0.2, 0.49, 0.5, 0.99):
                with self.subTest(level=level, eta=eta):
                    text = self.message(alert_level=level, projected_impact_eta_minutes=eta)
                    self.assertIn("Estimativa de entrada na área monitorada: menos de 1 min.", text)
                    self.assertNotIn("0 min", text)
                    self.assertNotIn("já está", text)
                    self.assertIn("não prevê a chegada exata da chuva à estação", text)

    def test_eta_normal_tem_referencia_explicita_e_aproximacao(self):
        for level in self.levels:
            for eta in (1, 25, 120):
                with self.subTest(level=level, eta=eta):
                    text = self.message(alert_level=level, projected_impact_eta_minutes=eta)
                    self.assertIn("se aproximando", text)
                    self.assertIn(f"Estimativa de entrada na área monitorada: aproximadamente {eta} min.", text)
                    self.assertIn("não prevê a chegada exata da chuva à estação", text)
                    self.assertIn("20 km da estação", text)
                    self.assertNotIn("99 min", text)

    def test_distancia_ausente_ou_invalida_nao_e_convertida_em_zero(self):
        for value in (None, "", "inválido", -1, float("nan"), float("inf"), True, False):
            for level in self.levels:
                with self.subTest(value=value, level=level):
                    text = self.message(alert_level=level, distance_km=value)
                    self.assertIn("Distância da área de chuva à estação indisponível.", text)
                    self.assertNotIn("0 km", text)
                    self.assertNotIn("1 km", text)
                    self.assertNotIn("70 km", text)

    def test_sem_estimativa_confiavel_nao_afirma_chegada_ou_area_presente(self):
        changes = [
            {"projected_impact_eta_minutes": value}
            for value in (None, "", "inválido", -1, 121, float("nan"), float("inf"), True, False)
        ] + [
            {field: value, "projected_impact_eta_minutes": eta}
            for field, value in (("tracking_valid", False), ("track_id", None),
                                 ("approaching", False), ("trajectory_compatible", False),
                                 ("trajectory_confidence", "BAIXA"), ("projected_impact", False))
            for eta in (0, 25)
        ]
        for level in self.levels:
            for change in changes:
                with self.subTest(level=level, change=change):
                    text = self.message(alert_level=level, **change)
                    self.assertIn("Ainda não há estimativa confiável de entrada na área monitorada.", text)
                    self.assertNotIn("Estimativa de entrada", text)
                    self.assertNotIn("Estimativa de chegada", text)
                    self.assertNotIn("já está", text)
                    self.assertNotIn("se aproximando", text)


if __name__ == "__main__":
    unittest.main()
