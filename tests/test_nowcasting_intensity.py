import os
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

from config import nowcasting_config
from services.nowcasting_intensity import CAMPOS_INTENSIDADE, analisar_intensidade_cluster
from services.nowcasting_service import analisar_nowcasting
from services.nowcasting_test_alerts import avaliar_alerta_teste_admin


def cluster(baixa=900, media=0, alta=100, muito_alta=0):
    return {
        "id": 101,
        "front_pixels_low": baixa, "front_pixels_medium": media,
        "front_pixels_high": alta, "front_pixels_very_high": muito_alta,
        "distancia_borda_escola_km": 20,
        "pixels_refletividade_baixa": baixa,
        "pixels_refletividade_media": media,
        "pixels_refletividade_alta": alta,
        "pixels_refletividade_muito_alta": muito_alta,
        "classe_predominante": "REFLETIVIDADE_BAIXA",
        "classe_maxima": "REFLETIVIDADE_MUITO_ALTA" if muito_alta else "REFLETIVIDADE_ALTA",
    }


class NowcastingIntensityTest(unittest.TestCase):
    def test_percentuais_e_limites_inclusivos_sem_arredondar_decisao(self):
        casos = (
            ((900, 0, 100, 0), 10, 0, True),
            ((901, 0, 99, 0), 9.9, 0, False),
            ((980, 0, 0, 20), 2, 2, True),
            ((981, 0, 0, 19), 1.9, 1.9, False),
            ((900, 0, 90, 10), 10, 1, True),
            ((500, 500, 0, 0), 0, 0, True),
            ((9999, 0, 0, 1), 0.01, 0.01, False),
            ((0, 0, 0, 1), 100, 100, False),
            ((0, 0, 0, 0), 0, 0, False),
        )
        for contagens, forte, muito_alta, suficiente in casos:
            with self.subTest(contagens=contagens):
                resultado = analisar_intensidade_cluster(cluster(*contagens))
                self.assertAlmostEqual(resultado["percentual_refletividade_forte"], forte)
                self.assertAlmostEqual(resultado["percentual_refletividade_muito_alta"], muito_alta)
                self.assertAlmostEqual(resultado["percentual_refletividade_alta"], forte - muito_alta)
                self.assertEqual(resultado["total_pixels_refletividade"], sum(contagens))
                self.assertEqual(resultado["intensidade_suficiente"], suficiente)

    def test_dados_invalidos_nao_viram_contagens_validas_na_propagacao(self):
        for valor in (None, -1, "100", 1.5, float("nan"), float("inf"), True):
            with self.subTest(valor=valor):
                resultado = analisar_intensidade_cluster(cluster(baixa=valor))
                self.assertFalse(resultado["intensidade_suficiente"])
                self.assertFalse(analisar_intensidade_cluster(resultado)["intensidade_suficiente"])
        self.assertFalse(analisar_intensidade_cluster({})["intensidade_suficiente"])
        self.assertFalse(analisar_intensidade_cluster({
            "classe_maxima": "REFLETIVIDADE_MUITO_ALTA"
        })["intensidade_suficiente"])

    def test_env_valida_percentuais_e_defaults_seguros(self):
        nomes = (
            "NOWCASTING_ALERT_MIN_STRONG_REFLECTIVITY_PERCENT",
            "NOWCASTING_ALERT_MIN_VERY_HIGH_REFLECTIVITY_PERCENT",
        )
        chaves = ("alert_min_strong_reflectivity_percent", "alert_min_very_high_reflectivity_percent")
        with mock.patch.dict(os.environ, {}, clear=True):
            config = nowcasting_config()
            self.assertEqual([config[chave] for chave in chaves], [10, 2])
            for valor in ("", "abc", "-1", "100.1", "nan", "inf", "-inf"):
                with self.subTest(valor=valor):
                    os.environ.update(dict.fromkeys(nomes, valor))
                    config = nowcasting_config()
                    self.assertEqual([config[chave] for chave in chaves], [10, 2])
            os.environ.update(dict(zip(nomes, ("12.5", "3.5"))))
            config = nowcasting_config()
            self.assertEqual([config[chave] for chave in chaves], [12.5, 3.5])
            # 10% forte não alcança HIGH configurado em 12,5% nem MEDIUM em 20%.
            self.assertEqual(analisar_intensidade_cluster(cluster(), config)["radar_intensity"], "LOW")
            os.environ.update(dict.fromkeys(nomes, "100"))
            config = nowcasting_config()
            self.assertTrue(analisar_intensidade_cluster(cluster(0, 0, 0, 100), config)["intensidade_suficiente"])

    def test_snapshot_propaga_intensidade_e_separa_cor_de_candidato(self):
        now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        config = nowcasting_config()
        config["test_alerts_enabled"] = True
        for entrada, esperado in ((cluster(), True), (cluster(500, 500, 0, 0), True)):
            with self.subTest(esperado=esperado):
                radar = {
                    "disponivel": True, "stale": False, "frame": {"id": 1},
                    "cluster_mais_proximo": entrada,
                    "tracking": {
                        "track_id": 1, "quantidade_frames": 4, "duracao_minutos": 15,
                        "velocidade_kmh": 40, "bearing_movimento": 0,
                        "aproximando": True, "trajetoria_compativel": True,
                    },
                }
                state = analisar_nowcasting(radar, {"stations": []}, {"rain_rate": 0}, config, now=now)
                ameaca = state["ameacas"][0]
                alerta = state["alerta_preventivo"]
                for campo in CAMPOS_INTENSIDADE:
                    self.assertEqual(alerta[campo], ameaca[campo])
                self.assertEqual(alerta["nivel"], "VERMELHO")
                self.assertEqual(alerta["would_send"], esperado)
                self.assertEqual(avaliar_alerta_teste_admin(
                    state, config, admin_phone="67999999999", now=now
                )["eligible"], esperado)
                # Mudança conservadora no .env também bloqueia um snapshot anterior.
                rigoroso = {**config, "alert_min_strong_reflectivity_percent": 100, "alert_min_medium_reflectivity_percent": 100}
                self.assertFalse(avaliar_alerta_teste_admin(
                    state, rigoroso, admin_phone="67999999999", now=now
                )["eligible"])


if __name__ == "__main__":
    unittest.main()
