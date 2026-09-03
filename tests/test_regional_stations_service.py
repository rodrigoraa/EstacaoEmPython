import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

from services.regional_stations_catalog import REGIONAL_STATIONS  # noqa: E402
from services.regional_stations_service import (  # noqa: E402
    PinMsRegionalClient,
    RegionalStationsError,
    interpretar_timestamp,
    normalizar_features,
    normalizar_registro,
    normalizar_valor_meteorologico,
    registro_tem_dados_meteorologicos,
)


class RegionalStationsServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures = ROOT / "tests" / "fixtures"
        cls.layer0 = json.loads((fixtures / "pinms_layer0.json").read_text(encoding="utf-8"))
        cls.layer2 = json.loads((fixtures / "pinms_layer2.json").read_text(encoding="utf-8"))
        cls.collected = datetime(2026, 9, 2, 20, tzinfo=timezone.utc)

    def test_catalogo_possui_as_seis_estacoes(self):
        self.assertEqual(tuple(REGIONAL_STATIONS), ("A721", "S706", "A749", "S735", "A709", "S708"))

    def test_consulta_atual_constroi_where_apenas_com_allowlist(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"features": []}
        session = Mock(get=Mock(return_value=response))
        PinMsRegionalClient(session=session).obter_atuais()
        params = session.get.call_args.kwargs["params"]
        self.assertIn("A721", params["where"])
        self.assertIn("S708", params["where"])
        self.assertEqual(params["returnGeometry"], "false")

    def test_codigo_arbitrario_e_rejeitado(self):
        with self.assertRaisesRegex(RegionalStationsError, "allowlist"):
            PinMsRegionalClient().obter_historico("A721' OR 1=1")

    def test_culturama_preserva_nome_de_exibicao(self):
        raw = self.layer0["features"][-1]["attributes"]
        obs = normalizar_registro(raw, 0, self.collected)
        self.assertEqual(REGIONAL_STATIONS[obs.station_code].display_name, "Culturama")
        self.assertEqual(obs.nome_fonte, "FÁTIMA DO SUL")

    def test_none_e_ausencia_orgao_nao_quebram(self):
        raw = dict(self.layer0["features"][0]["attributes"])
        raw.pop("ORGAO")
        obs = normalizar_registro(raw, 0, self.collected)
        self.assertIsNone(obs.umidade_min)
        self.assertIsNone(obs.orgao)
        self.assertEqual(obs.chuva_mm, 0)

    def test_coordenada_ausente_usa_catalogo_sem_fingir_origem(self):
        raw = dict(self.layer0["features"][0]["attributes"])
        raw.pop("VL_LATITUDE")
        raw.pop("VL_LONGITUDE")
        obs = normalizar_registro(raw, 0, self.collected)
        station = REGIONAL_STATIONS[obs.station_code]
        self.assertIsNone(obs.latitude_fonte)
        self.assertIsNone(obs.longitude_fonte)
        self.assertEqual(obs.latitude, station.configured_lat)
        self.assertEqual(obs.longitude, station.configured_lon)

    def test_camada_atual_sem_hora_e_date_only(self):
        obs = normalizar_registro(self.layer0["features"][0]["attributes"], 0, self.collected)
        self.assertIsNone(obs.source_hr_medicao_raw)
        self.assertEqual(obs.timestamp_status, "date_only")
        self.assertIsNone(obs.medido_em_utc)

    def test_slots_vazios_sao_ignorados(self):
        raw = self.layer2["features"][0]["attributes"]
        self.assertFalse(registro_tem_dados_meteorologicos(raw))
        self.assertIsNone(normalizar_registro(raw, 2, self.collected))

    def test_primeiro_valido_nao_e_primeira_feature(self):
        observacoes = normalizar_features(self.layer2, 2, self.collected)
        validas = [obs for obs in observacoes if obs]
        self.assertEqual(validas[0].source_hr_medicao_raw, "19:00")

    def test_unidades_e_conversao_de_vento(self):
        obs = normalizar_registro(self.layer0["features"][1]["attributes"], 0, self.collected)
        self.assertEqual(obs.vento_velocidade_raw, 1.7)
        self.assertAlmostEqual(obs.vento_velocidade_ms, 1.7)
        self.assertAlmostEqual(obs.vento_velocidade_kmh, 6.12)
        self.assertAlmostEqual(obs.rajada_kmh, 17.64)
        self.assertEqual(obs.radiacao_unidade, "kJ/m²")

    def test_timestamp_horario_e_timezone_aware(self):
        parsed = interpretar_timestamp(1788307200000, "19:00")
        self.assertEqual(parsed.status, "reconciled")
        self.assertIsNotNone(parsed.measured_utc.tzinfo)
        self.assertIsNotNone(parsed.measured_local.tzinfo)
        self.assertEqual(parsed.measured_utc.hour, 19)

    def test_timestamp_ambiguo_nao_inventa_data(self):
        parsed = interpretar_timestamp("02/09/2026", "19:00")
        self.assertEqual(parsed.status, "suspect")
        self.assertIsNone(parsed.measured_utc)

    def test_sentinela_e_vazio_viram_none_sem_virar_zero(self):
        self.assertIsNone(normalizar_valor_meteorologico(9999))
        self.assertIsNone(normalizar_valor_meteorologico(""))
        self.assertEqual(normalizar_valor_meteorologico(0), 0.0)

    def test_timeout_e_json_invalido_sao_sanitizados(self):
        session = Mock()
        session.get.side_effect = requests.Timeout("detalhe externo")
        with self.assertRaisesRegex(RegionalStationsError, "Timeout"):
            PinMsRegionalClient(session=session).obter_atuais()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("json")
        session.get.side_effect = None
        session.get.return_value = response
        with self.assertRaisesRegex(RegionalStationsError, "JSON invalido"):
            PinMsRegionalClient(session=session).obter_atuais()


if __name__ == "__main__":
    unittest.main()
