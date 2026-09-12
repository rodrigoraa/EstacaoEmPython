"""Regressão do evento perdido e matriz das novas rotas experimentais."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))
from config import nowcasting_config, radar_config
from services.nowcasting_intensity import classificar_intensidade_frente
from services.preventive_alerts import decidir_alerta_preventivo
from services.radar_analysis import calcular_frente_relevante
from services.nowcasting_test_alerts import montar_mensagem_alerta_teste


def frente(classe="MEDIUM", distance=20, tracked=False):
    return {
        **dict.fromkeys(("front_pixels_low", "front_pixels_medium", "front_pixels_high", "front_pixels_very_high"), 0),
        f"front_pixels_{classe.lower()}": 100,
        "distance_km": distance, "tracking_valid": tracked,
        "track_id": 1 if tracked else None, "approaching": tracked,
        "trajectory_compatible": tracked,
    }


def decidir(alerta, **kwargs):
    return decidir_alerta_preventivo(alerta, radar_atualizado=True, evento_local=False, **kwargs)


class PreventiveFrontTest(unittest.TestCase):
    def test_matriz_intensidade_distancia_tracking(self):
        for classe, distancia, tracked, rota in (
            ("LOW", 10, False, "NENHUMA"),
            ("MEDIUM", 20, False, "PROXIMIDADE"), ("MEDIUM", 40, False, "NENHUMA"),
            ("MEDIUM", 40, True, "TRACKING"), ("MEDIUM", 60, True, "NENHUMA"),
            ("HIGH", 30, False, "PROXIMIDADE"), ("HIGH", 60, True, "TRACKING"),
            ("HIGH", 60, False, "NENHUMA"), ("VERY_HIGH", 45, False, "PROXIMIDADE"),
            ("VERY_HIGH", 80, True, "TRACKING"), ("VERY_HIGH", 80, False, "NENHUMA"),
            ("VERY_HIGH", 101, True, "NENHUMA"), ("MEDIUM", 15, False, "PROXIMIDADE"),
        ):
            with self.subTest(classe=classe, distance=distancia, tracked=tracked):
                resultado = decidir(frente(classe, distancia, tracked))
                self.assertEqual(resultado["authorization"], rota)
                self.assertEqual(resultado["would_send"], rota != "NENHUMA")
                self.assertEqual(resultado["urgency"], {"NENHUMA": "MONITORAMENTO", "PROXIMIDADE": "IMEDIATO", "TRACKING": "ESPERADO"}[rota])
                self.assertEqual(resultado["certainty"], "PROVAVEL" if tracked else "POSSIVEL")

    def test_regressao_evento_real_primeiro_frame_verde_a_12km(self):
        alerta = frente(distance=12)
        resultado = decidir(alerta)
        self.assertTrue(resultado["would_send"])
        self.assertEqual(resultado["authorization"], "PROXIMIDADE")
        self.assertEqual(resultado["alert_level"], "INFORMATIVO")

    def test_tracking_so_e_exigido_na_rota_antecipada(self):
        for campo, valor, motivo in (("track_id", None, "tracking_insufficient_for_early_warning"),
                                    ("tracking_valid", False, "tracking_insufficient_for_early_warning"),
                                    ("approaching", False, "not_approaching"),
                                    ("trajectory_compatible", False, "trajectory_incompatible")):
            with self.subTest(campo=campo):
                alerta = {**frente(distance=40, tracked=True), campo: valor}
                self.assertEqual(decidir(alerta)["block_reason"], motivo)
                alerta["distance_km"] = 12
                self.assertTrue(decidir(alerta)["would_send"])

    def test_bloqueios_e_observado(self):
        for kwargs, motivo in (({"radar_atualizado": False}, "radar_unavailable"),
                               ({"radar_stale": True}, "radar_stale"),
                               ({"frame_valido": False}, "invalid_frame"),
                               ({"evento_local": True}, "local_event_observed")):
            resultado = decidir_alerta_preventivo(frente(), **{"radar_atualizado": True, "evento_local": False, **kwargs})
            self.assertEqual(resultado["block_reason"], motivo)
            if kwargs.get("evento_local"):
                self.assertEqual(resultado["certainty"], "OBSERVADO")
        self.assertEqual(decidir({**frente(), "clutter_index": .8})["block_reason"], "clutter")
        self.assertEqual(decidir({**frente(), "low_confidence": True})["block_reason"], "clutter")
        self.assertEqual(decidir({"distance_km": 12})["block_reason"], "inconsistent_data")

    def test_frente_vermelha_nao_e_diluida_e_traseira_nao_contamina(self):
        for perto, longe, esperado in ((4, 2, "VERY_HIGH"), (2, 4, "MEDIUM")):
            classes = np.array([perto] * 10 + [longe] * 1000)
            distancias = np.array([20] * 10 + [60] * 1000)
            dados = calcular_frente_relevante(classes, distancias)
            self.assertEqual(dados["front_pixels_total"], 10)
            self.assertEqual(classificar_intensidade_frente(dados)["radar_intensity"], esperado)

    def test_pixels_percentuais_zero_e_inconsistencia(self):
        unico = {**frente("VERY_HIGH"), "front_pixels_very_high": 1}
        self.assertFalse(decidir(unico)["would_send"])
        self.assertFalse(decidir(unico, config={"alert_min_very_high_reflectivity_pixels": 1})["would_send"])
        dados = {**frente("LOW"), "front_pixels_low": 999, "front_pixels_very_high": 2}
        self.assertNotEqual(classificar_intensidade_frente(dados)["radar_intensity"], "VERY_HIGH")
        self.assertFalse(decidir({**frente(), "front_pixels_total": 999})["would_send"])
        invalida = classificar_intensidade_frente({**frente(), "front_pixels_total": 999})
        self.assertFalse(decidir({**invalida, "distance_km": 12})["would_send"])
        self.assertEqual(calcular_frente_relevante([], [])["front_pixels_total"], 0)

    def test_limites_inclusivos_de_todas_as_faixas(self):
        for classe, near, tracked in (("MEDIUM", 25, 50), ("HIGH", 35, 75), ("VERY_HIGH", 50, 100)):
            with self.subTest(classe=classe):
                self.assertEqual(decidir(frente(classe, near))["authorization"], "PROXIMIDADE")
                self.assertFalse(decidir(frente(classe, near + .01))["would_send"])
                self.assertEqual(decidir(frente(classe, tracked, True))["authorization"], "TRACKING")
                self.assertFalse(decidir(frente(classe, tracked + .01, True))["would_send"])

    def test_sem_ecos_permite_rearm_e_azul_nao_oculta_candidato(self):
        from services.nowcasting_service import analisar_nowcasting
        config = nowcasting_config()
        radar = {"disponivel": True, "stale": False, "frame": {"id": 1}}
        vazio = analisar_nowcasting(radar, {}, {"rain_rate": 0}, config)
        self.assertEqual(vazio["alerta_preventivo"]["block_reason"], "insufficient_pixels")
        radar["tracks_atuais"] = [
            {"cluster": {**frente("LOW"), "id": 1, "distancia_borda_escola_km": 5}},
            {"cluster": {**frente("MEDIUM"), "id": 2, "distancia_borda_escola_km": 12}},
        ]
        resultado = analisar_nowcasting(radar, {}, {"rain_rate": 0}, config)
        self.assertEqual(resultado["alerta_preventivo"]["cluster_id"], 2)
        self.assertTrue(resultado["alerta_preventivo"]["would_send"])

    def test_defaults_invalidos_e_limites(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            defaults = nowcasting_config()
            for key in (k for k in defaults if k.startswith("alert_")):
                for value in ("nan", "inf", "-inf", "abc", "-1"):
                    with self.subTest(key=key, value=value), mock.patch.dict(os.environ, {"NOWCASTING_" + key.upper(): value}):
                        self.assertEqual(nowcasting_config()[key], defaults[key])
            for value in ("0", "nan", "inf", "-1"):
                with mock.patch.dict(os.environ, {"RADAR_ALERT_FRONT_DEPTH_KM": value}):
                    self.assertEqual(radar_config()["alert_front_depth_km"], 15)
            with mock.patch.dict(os.environ, {"NOWCASTING_ALERT_MIN_MEDIUM_REFLECTIVITY_PERCENT": "0"}):
                self.assertEqual(nowcasting_config()["alert_min_medium_reflectivity_percent"], 0)

    def test_mensagens_exatas_sem_jargao_e_eta_so_confiavel(self):
        for classe in ("MEDIUM", "HIGH", "VERY_HIGH"):
            for tracked in (False, True):
                alerta = frente(classe, tracked=tracked)
                alerta.update(decidir(alerta), eta_border_minutes=25, eta_border_quality="BOA")
                texto = montar_mensagem_alerta_teste({"alerta_preventivo": alerta})
                for proibido in ("eco", "refletividade", "cluster", "tracking", "célula", "maxcappi", "frame", "EE São José", "Escola Estadual São José"):
                    self.assertNotIn(proibido.lower(), texto.lower())
                self.assertIn("Distrito de São José", texto)
                self.assertEqual("se aproximando" in texto, tracked)
                self.assertEqual("Estimativa de chegada: 25 min." in texto, tracked)
                alerta["eta_border_minutes"] = None
                self.assertNotIn("Estimativa", montar_mensagem_alerta_teste({"alerta_preventivo": alerta}))


if __name__ == "__main__":
    unittest.main()
