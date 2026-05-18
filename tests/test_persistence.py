import importlib
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class PersistenciaMeteorologicaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "estacao_teste.db")

        import database
        import persistence

        self.database = importlib.reload(database)
        self.persistence = importlib.reload(persistence)
        self.database.init_db()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("ESTACAO_DB", None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def test_chuva_recebida_fica_persistida_mesmo_antes_do_historico(self):
        raw = {
            "dateutc": 1779100000000,
            "hourlyrainin": 0.5,
            "eventrainin": 1.2,
            "dailyrainin": 2.0,
        }
        dados = {
            "temp": 25.0,
            "sensacao": 26.0,
            "umidade": 90,
            "pressao": 1010.0,
            "uv": 0,
            "radiacao": 0,
            "vento": 4.0,
            "rajada": 8.0,
            "vento_dir": 180,
            "chuva_rate": 12.7,
            "chuva_evento": 30.5,
            "chuva_hoje": 50.8,
        }

        leitura_bruta_id = self.persistence.salvar_leitura_bruta(raw, dados)

        conn = self.abrir_banco()
        row = conn.execute(
            """
            SELECT station_timestamp_ms, chuva_rate, chuva_evento, chuva_hoje
            FROM leituras_brutas
            WHERE id = ?
            """,
            (leitura_bruta_id,),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["station_timestamp_ms"], raw["dateutc"])
        self.assertEqual(row["chuva_rate"], 12.7)
        self.assertEqual(row["chuva_evento"], 30.5)
        self.assertEqual(row["chuva_hoje"], 50.8)

    def test_historico_aponta_para_leitura_bruta_reconstruivel(self):
        raw = {"dateutc": 1779100000000, "dailyrainin": 3.0}
        dados = {
            "temp": 24.0,
            "sensacao": 24.5,
            "umidade": 88,
            "pressao": 1009.0,
            "uv": 1,
            "radiacao": 10,
            "vento": 5.0,
            "rajada": 11.0,
            "vento_dir": 90,
            "chuva_rate": 0.0,
            "chuva_evento": 76.2,
            "chuva_hoje": 76.2,
        }

        leitura_bruta_id = self.persistence.salvar_leitura_bruta(raw, dados)
        self.persistence.salvar_historico_clima(dados, leitura_bruta_id)

        conn = self.abrir_banco()
        row = conn.execute(
            """
            SELECT h.chuva_hoje, b.payload_json
            FROM historico_clima h
            JOIN leituras_brutas b ON b.id = h.leitura_bruta_id
            WHERE h.leitura_bruta_id = ?
            """,
            (leitura_bruta_id,),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["chuva_hoje"], 76.2)
        self.assertIn('"dailyrainin": 3.0', row["payload_json"])

    def test_salva_todos_os_campos_processados_com_tempo_bateria_e_sinal(self):
        raw = {
            "dateutc": 1779100000000,
            "tempf": 95,
            "feelsLike": 100,
            "humidity": 12,
            "baromrelin": 30.12,
            "uv": 11,
            "solarradiation": 1200,
            "windspeedmph": 30,
            "windgustmph": 65,
            "maxdailygust": 70,
            "winddir": 270,
            "rainratein": 2.5,
            "eventrainin": 4.0,
            "dailyrainin": 5.0,
            "battout": 1,
            "signal": -62,
            "campo_extra_futuro": "preservado",
        }

        import services.weather_service as weather_service

        if "requests" not in sys.modules:
            sys.modules["requests"] = types.SimpleNamespace(get=mock.Mock())

        weather_service = importlib.reload(weather_service)
        dados = {
            "station_timestamp_ms": raw["dateutc"],
            "station_data_hora_utc": "2026-05-18T03:46:40+00:00",
            "station_data_hora_local": "2026-05-17T23:46:40-04:00",
            "temp": weather_service.f_to_c(raw["tempf"]),
            "sensacao": weather_service.f_to_c(raw["feelsLike"]),
            "umidade": raw["humidity"],
            "pressao": round(raw["baromrelin"] * 33.8639, 1),
            "uv": raw["uv"],
            "radiacao": raw["solarradiation"],
            "vento": weather_service.mph_to_kmh(raw["windspeedmph"]),
            "rajada": weather_service.mph_to_kmh(raw["windgustmph"]),
            "vento_dir": raw["winddir"],
            "chuva_rate": weather_service.in_to_mm(raw["rainratein"]),
            "chuva_evento": weather_service.in_to_mm(raw["eventrainin"]),
            "chuva_hoje": weather_service.in_to_mm(raw["dailyrainin"]),
            "bateria": '{"battout": 1}',
            "sinal": "-62",
        }

        leitura_bruta_id = self.persistence.salvar_leitura_bruta(raw, dados)
        self.persistence.salvar_historico_clima(dados, leitura_bruta_id)

        conn = self.abrir_banco()
        row = conn.execute(
            """
            SELECT h.*, b.payload_json, b.bateria as bateria_bruta, b.sinal as sinal_bruto
            FROM historico_clima h
            JOIN leituras_brutas b ON b.id = h.leitura_bruta_id
            WHERE h.leitura_bruta_id = ?
            """,
            (leitura_bruta_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(row["temp"], 35.0)
        self.assertEqual(row["sensacao"], 37.8)
        self.assertEqual(row["umidade"], 12)
        self.assertEqual(row["pressao"], 1020.0)
        self.assertEqual(row["uv"], 11)
        self.assertEqual(row["radiacao"], 1200)
        self.assertEqual(row["vento_vel"], 48.3)
        self.assertEqual(row["vento_rajada"], 104.6)
        self.assertEqual(row["vento_dir"], 270)
        self.assertEqual(row["chuva_rate"], 63.5)
        self.assertEqual(row["chuva_evento"], 101.6)
        self.assertEqual(row["chuva_hoje"], 127.0)
        self.assertEqual(row["station_timestamp_ms"], raw["dateutc"])
        self.assertEqual(row["bateria"], '{"battout": 1}')
        self.assertEqual(row["sinal"], "-62")
        self.assertIn('"campo_extra_futuro": "preservado"', row["payload_json"])
        self.assertEqual(row["bateria_bruta"], '{"battout": 1}')
        self.assertEqual(row["sinal_bruto"], "-62")

    def test_sqlite_usa_wal(self):
        conn = self.database.get_db()
        modo = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()

        self.assertEqual(modo.lower(), "wal")

    def test_retry_de_gravacao_temporaria(self):
        chamadas = {"total": 0}

        def operacao_instavel():
            chamadas["total"] += 1
            if chamadas["total"] == 1:
                raise sqlite3.OperationalError("database is locked")
            return "gravado"

        resultado = self.persistence.executar_com_retry(
            operacao_instavel, tentativas=2, espera_inicial=0
        )

        self.assertEqual(resultado, "gravado")
        self.assertEqual(chamadas["total"], 2)

    def test_timezone_campo_grande_para_timestamp_da_estacao(self):
        from persistence import dados_tempo_estacao
        from time_utils import formatar_local

        utc, local = dados_tempo_estacao(1779100000000)

        self.assertTrue(utc.endswith("+00:00"))
        self.assertTrue(local.endswith("-04:00"))
        self.assertEqual(formatar_local("2026-05-18 12:00:00", assume_utc=True), "18/05/2026 08:00:00")

    def test_obter_dados_persiste_raw_antes_de_retornar(self):
        if "requests" not in sys.modules:
            sys.modules["requests"] = types.SimpleNamespace(get=mock.Mock())

        import services.weather_service as weather_service

        weather_service = importlib.reload(weather_service)
        agora_ms = int(time.time() * 1000)
        payload = {
            "data": [
                {
                    "lastData": {
                        "dateutc": agora_ms,
                        "tempf": 77,
                        "feelsLike": 78,
                        "humidity": 91,
                        "baromrelin": 29.9,
                        "uv": 0,
                        "solarradiation": 0,
                        "windspeedmph": 2,
                        "windgustmph": 5,
                        "winddir": 120,
                        "hourlyrainin": 0.1,
                        "eventrainin": 0.4,
                        "dailyrainin": 1.0,
                        "battout": 1,
                        "signal": -70,
                    }
                }
            ]
        }

        resposta = mock.Mock()
        resposta.json.return_value = payload
        resposta.raise_for_status.return_value = None

        with mock.patch("services.weather_service.requests.get", return_value=resposta):
            dados = weather_service.obter_dados()

        conn = self.abrir_banco()
        row = conn.execute(
            "SELECT chuva_hoje FROM leituras_brutas WHERE id = ?",
            (dados["leitura_bruta_id"],),
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["chuva_hoje"], 25.4)

    def test_obter_dados_trata_nulos_sem_perder_payload_bruto(self):
        if "requests" not in sys.modules:
            sys.modules["requests"] = types.SimpleNamespace(get=mock.Mock())

        import services.weather_service as weather_service

        weather_service = importlib.reload(weather_service)
        agora_ms = int(time.time() * 1000)
        payload = {
            "data": [
                {
                    "lastData": {
                        "dateutc": agora_ms,
                        "tempf": None,
                        "humidity": None,
                        "baromrelin": None,
                        "winddir": None,
                        "dailyrainin": None,
                        "campo_extra_nulo": None,
                    }
                }
            ]
        }

        resposta = mock.Mock()
        resposta.json.return_value = payload
        resposta.raise_for_status.return_value = None

        with mock.patch("services.weather_service.requests.get", return_value=resposta):
            dados = weather_service.obter_dados()

        self.assertEqual(dados["temp"], 0.0)
        self.assertEqual(dados["umidade"], 0)
        self.assertEqual(dados["pressao"], 0.0)
        self.assertEqual(dados["vento_dir"], 0)
        self.assertEqual(dados["chuva_hoje"], 0.0)

        conn = self.abrir_banco()
        row = conn.execute(
            "SELECT payload_json FROM leituras_brutas WHERE id = ?",
            (dados["leitura_bruta_id"],),
        ).fetchone()
        conn.close()

        self.assertIn('"campo_extra_nulo": null', row["payload_json"])


if __name__ == "__main__":
    unittest.main()
