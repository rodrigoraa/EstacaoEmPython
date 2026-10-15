import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))
from config import nowcasting_config, radar_config


class NowcastingConfigTest(unittest.TestCase):
    def test_radar_consulta_a_cada_minuto_sem_ampliar_validade_da_imagem(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(radar_config()["poll_seconds"], 60)
            self.assertEqual(nowcasting_config()["radar_max_age_minutes"], 15)
            self.assertEqual(nowcasting_config()["alert_delivery_max_age_minutes"], 15)

    def test_intervalo_radar_explicito_continua_prevalecendo(self):
        for seconds, expected in (("300", 300), ("10", 30), ("invalid", 60)):
            with self.subTest(seconds=seconds), mock.patch.dict(os.environ, {
                "RADAR_POLL_SECONDS": seconds,
            }, clear=True):
                self.assertEqual(radar_config()["poll_seconds"], expected)

    def test_defaults_antecipam_monitoramento_com_interceptacao_local(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = nowcasting_config()
        self.assertEqual(config["alert_medium_tracked_km"], 100)
        self.assertEqual(config["alert_high_tracked_km"], 150)
        self.assertEqual(config["alert_very_high_tracked_km"], 150)
        self.assertEqual(config["public_projection_minutes"], 120)
        self.assertEqual(config["public_impact_radius_km"], 5)
        self.assertEqual(config["public_trajectory_min_frames"], 4)
        self.assertEqual(config["public_trajectory_max_gap_minutes"], 15)
        self.assertEqual(config["radar_max_age_minutes"], 15)
        self.assertEqual(config["radar_display_max_age_minutes"], 45)
        self.assertEqual(config["alert_delivery_max_age_minutes"], 15)
        self.assertEqual(config["algorithm_version"], "1.8")

    def test_alias_radar_habilita_motor_e_envio(self):
        with mock.patch.dict(os.environ, {"RADAR_ALERTS_ENABLED": "true"}, clear=True):
            config = nowcasting_config()
        self.assertTrue(config["enabled"])
        self.assertTrue(config["alerts_enabled"])

    def test_opcoes_explicitas_nowcasting_prevalecem_sobre_alias(self):
        with mock.patch.dict(os.environ, {
            "RADAR_ALERTS_ENABLED": "true",
            "NOWCASTING_ENABLED": "false",
            "NOWCASTING_ALERTS_ENABLED": "false",
        }, clear=True):
            config = nowcasting_config()
        self.assertFalse(config["enabled"])
        self.assertFalse(config["alerts_enabled"])

    def test_alias_desligado_nao_bloqueia_opcoes_especificas(self):
        with mock.patch.dict(os.environ, {
            "RADAR_ALERTS_ENABLED": "false",
            "NOWCASTING_ENABLED": "true",
            "NOWCASTING_ALERTS_ENABLED": "true",
        }, clear=True):
            config = nowcasting_config()
        self.assertTrue(config["enabled"])
        self.assertTrue(config["alerts_enabled"])

    def test_limite_visual_do_radar_nao_altera_validade_de_alertas(self):
        with mock.patch.dict(os.environ, {"RADAR_STALE_MINUTES": "30"}, clear=True):
            config = nowcasting_config()
        self.assertEqual(config["radar_display_max_age_minutes"], 30)
        self.assertEqual(config["radar_max_age_minutes"], 15)
        self.assertEqual(config["alert_delivery_max_age_minutes"], 15)

    def test_raio_local_configuravel_e_limites(self):
        for valor, esperado in (("1", 1), ("5", 5), ("15", 15),
                                ("0", 5), ("16", 5), ("nan", 5), ("inf", 5)):
            with self.subTest(valor=valor), mock.patch.dict(os.environ, {
                "NOWCASTING_PUBLIC_IMPACT_RADIUS_KM": valor,
            }, clear=True):
                self.assertEqual(nowcasting_config()["public_impact_radius_km"], esperado)

    def test_janelas_invalidas_nao_autorizam_dados_antigos(self):
        for chave in ("PUBLIC_TRAJECTORY_MAX_GAP_MINUTES", "ALERT_DELIVERY_MAX_AGE_MINUTES"):
            for valor in ("0", "-1", "nan", "inf", "abc"):
                with self.subTest(chave=chave, valor=valor), mock.patch.dict(os.environ, {
                    "NOWCASTING_" + chave: valor,
                }, clear=True):
                    self.assertEqual(nowcasting_config()[chave.lower()], 15)


if __name__ == "__main__":
    unittest.main()
