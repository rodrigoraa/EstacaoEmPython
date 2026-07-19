import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class AcumuladosDiariosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "estacao_teste.db")

        import acumulados
        import database
        import routes.api as api_module

        self.database = importlib.reload(database)
        self.acumulados = importlib.reload(acumulados)
        self.api_module = importlib.reload(api_module)
        self.database.init_db()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("ESTACAO_DB", None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def inserir_historico(self, data_hora, chuva_hoje, vento_rajada):
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO historico_clima (
                data_hora,
                data_hora_local,
                chuva_hoje,
                vento_rajada
            ) VALUES (?, ?, ?, ?)
            """,
            (data_hora.replace("T", " "), data_hora, chuva_hoje, vento_rajada),
        )
        conn.commit()
        conn.close()

    def test_chuva_corrigida_soma_apos_reset_do_contador(self):
        self.inserir_historico("2026-06-25T08:00:00-04:00", 30.0, 45.0)
        self.inserir_historico("2026-06-25T09:00:00-04:00", 0.0, 0.0)
        self.inserir_historico("2026-06-25T10:00:00-04:00", 10.0, 5.0)

        acumulado = self.acumulados.atualizar_acumulado_diario(
            {"chuva_hoje": 10.0, "rajada": 5.0, "rajada_max": 0.0},
            "2026-06-25",
        )

        self.assertEqual(acumulado["chuva_total_corrigida"], 40.0)
        self.assertEqual(acumulado["chuva_ultima_leitura"], 10.0)
        self.assertEqual(acumulado["chuva_reset_count"], 1)
        self.assertEqual(acumulado["rajada_max_corrigida"], 45.0)

    def test_rajada_maxima_nao_diminui_apos_reinicio_da_estacao(self):
        self.inserir_historico("2026-06-25T08:00:00-04:00", 0.0, 12.0)
        primeiro = self.acumulados.atualizar_acumulado_diario(
            {"chuva_hoje": 0.0, "rajada": 12.0, "rajada_max": 72.5},
            "2026-06-25",
        )
        self.assertEqual(primeiro["rajada_max_corrigida"], 72.5)

        self.inserir_historico("2026-06-25T09:00:00-04:00", 0.0, 0.0)
        segundo = self.acumulados.atualizar_acumulado_diario(
            {"chuva_hoje": 0.0, "rajada": 0.0, "rajada_max": 0.0},
            "2026-06-25",
        )

        self.assertEqual(segundo["rajada_max_corrigida"], 72.5)

    def test_maxdailygust_bruto_antigo_nao_contamina_novo_dia(self):
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO leituras_brutas (
                station_data_hora_local,
                recebido_em,
                payload_json,
                dados_convertidos_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                "2026-07-19T00:00:10-04:00",
                "2026-07-19 00:00:10",
                "{}",
                '{"rajada": 8.0, "rajada_max": 40.4}',
            ),
        )
        conn.commit()
        conn.close()

        acumulado = self.acumulados.atualizar_acumulado_diario(
            {"chuva_hoje": 0.0, "rajada": 8.0, "rajada_max": 8.0},
            "2026-07-19",
        )

        self.assertEqual(acumulado["rajada_max_corrigida"], 8.0)

    def test_api_clima_exibe_acumulados_corrigidos(self):
        self.inserir_historico("2026-06-25T08:00:00-04:00", 30.0, 45.0)
        self.inserir_historico("2026-06-25T09:00:00-04:00", 0.0, 0.0)
        self.inserir_historico("2026-06-25T10:00:00-04:00", 10.0, 5.0)
        self.acumulados.atualizar_acumulado_diario(
            {"chuva_hoje": 10.0, "rajada": 5.0, "rajada_max": 0.0},
            "2026-06-25",
        )
        self.api_module.data_local = lambda: "2026-06-25"

        app = Flask(__name__)
        app.register_blueprint(self.api_module.api_routes)
        resposta = app.test_client().get("/api/clima")

        self.assertEqual(resposta.status_code, 200)
        dados = resposta.get_json()
        self.assertEqual(dados["chuva_hoje"], 40.0)
        self.assertEqual(dados["vento_rajada_max"], 45.0)


if __name__ == "__main__":
    unittest.main()
