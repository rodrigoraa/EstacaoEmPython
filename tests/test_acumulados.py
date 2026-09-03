import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def atualizar(self, chuva, rajada=0.0, rajada_max=0.0, data="2026-06-25"):
        return self.acumulados.atualizar_acumulado_diario(
            {
                "chuva_hoje": chuva,
                "rajada": rajada,
                "rajada_max": rajada_max,
            },
            data,
        )

    def test_incremental_primeira_leitura_crescimento_e_valor_igual(self):
        primeiro = self.atualizar(5.0)
        crescimento = self.atualizar(8.0)
        repetido = self.atualizar(8.0)

        self.assertEqual(primeiro["chuva_total_corrigida"], 5.0)
        self.assertEqual(crescimento["chuva_total_corrigida"], 8.0)
        self.assertEqual(repetido["chuva_total_corrigida"], 8.0)
        self.assertEqual(repetido["chuva_reset_count"], 0)

    def test_incremental_exemplos_de_crescimento_sem_duplicar(self):
        resultados = [self.atualizar(chuva) for chuva in (0.0, 5.0, 10.0)]
        self.assertEqual(resultados[-1]["chuva_total_corrigida"], 10.0)

        outro_dia = [
            self.atualizar(chuva, data="2026-06-26")
            for chuva in (30.0, 30.0, 31.0)
        ]
        self.assertEqual(outro_dia[-1]["chuva_total_corrigida"], 31.0)
        self.assertEqual(outro_dia[-1]["chuva_reset_count"], 0)

    def test_incremental_reset_do_contador(self):
        resultados = [self.atualizar(chuva) for chuva in (30.0, 0.0, 10.0)]

        self.assertEqual(resultados[-1]["chuva_total_corrigida"], 40.0)
        self.assertEqual(resultados[-1]["chuva_ultima_leitura"], 10.0)
        self.assertEqual(resultados[-1]["chuva_reset_count"], 1)

    def test_incremental_multiplos_resets(self):
        resultados = [
            self.atualizar(chuva)
            for chuva in (20.0, 0.0, 5.0, 0.0, 3.0)
        ]

        self.assertEqual(resultados[-1]["chuva_total_corrigida"], 28.0)
        self.assertEqual(resultados[-1]["chuva_reset_count"], 2)

    def test_incremental_reinicia_processo_a_partir_do_sqlite(self):
        self.atualizar(30.0, rajada=45.0)

        self.acumulados = importlib.reload(self.acumulados)
        depois_restart = self.atualizar(35.0, rajada=10.0)

        self.assertEqual(depois_restart["chuva_total_corrigida"], 35.0)
        self.assertEqual(depois_restart["chuva_ultima_leitura"], 35.0)
        self.assertEqual(depois_restart["rajada_max_corrigida"], 45.0)

    def test_incremental_rajada_maxima_persistida_nunca_diminui(self):
        primeiro = self.atualizar(0.0, rajada=12.0, rajada_max=72.5)
        segundo = self.atualizar(0.0, rajada=5.0, rajada_max=8.0)

        self.assertEqual(primeiro["rajada_max_corrigida"], 72.5)
        self.assertEqual(segundo["rajada_max_corrigida"], 72.5)

    def test_bootstrap_ocorre_uma_vez_e_depois_fluxo_e_incremental(self):
        self.inserir_historico("2026-06-25T08:00:00-04:00", 30.0, 45.0)
        self.inserir_historico("2026-06-25T09:00:00-04:00", 0.0, 0.0)
        self.inserir_historico("2026-06-25T10:00:00-04:00", 10.0, 5.0)

        original = self.acumulados.calcular_acumulado_pelo_historico
        with mock.patch.object(
            self.acumulados,
            "calcular_acumulado_pelo_historico",
            wraps=original,
        ) as reconstruir:
            bootstrap = self.atualizar(10.0, rajada=5.0)
            incremental = self.atualizar(12.0, rajada=4.0)

        self.assertEqual(reconstruir.call_count, 1)
        self.assertEqual(bootstrap["chuva_total_corrigida"], 40.0)
        self.assertEqual(incremental["chuva_total_corrigida"], 42.0)
        self.assertEqual(incremental["rajada_max_corrigida"], 45.0)

    def test_estado_existente_nao_consulta_tabelas_historicas(self):
        self.atualizar(5.0)
        comandos = []
        conn = self.database.get_db()
        conn.set_trace_callback(comandos.append)

        with mock.patch.object(
            self.acumulados.database,
            "get_db",
            return_value=conn,
        ):
            acumulado = self.atualizar(8.0)

        sql_executado = "\n".join(comandos).lower()
        self.assertEqual(acumulado["chuva_total_corrigida"], 8.0)
        self.assertNotIn("from historico_clima", sql_executado)
        self.assertNotIn("from leituras_brutas", sql_executado)

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
