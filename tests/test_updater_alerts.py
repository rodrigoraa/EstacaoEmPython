import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class AlertasWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "estacao_teste.db")
        os.environ["SECRET_KEY"] = "segredo-teste"

        import database

        self.database = importlib.reload(database)
        self.database.init_db()

        import workers.updater

        self.updater = importlib.reload(workers.updater)

    def tearDown(self):
        self.tmp.cleanup()
        for chave in (
            "ESTACAO_DB",
            "SECRET_KEY",
        ):
            os.environ.pop(chave, None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def test_enviar_alerta_enfileira_para_usuario_inscrito(self):
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO usuarios (nome, telefone, receber_whatsapp)
            VALUES (?, ?, ?)
            """,
            ("Maria", "(67) 99999-9999", 1),
        )
        conn.commit()
        conn.close()

        self.updater.log = lambda mensagem: None

        resultado = self.updater.enviar_alerta("Alerta de teste")

        self.assertEqual(resultado, {"total": 1, "enfileirados": 1, "falhas": 0})

        conn = self.abrir_banco()
        row = conn.execute(
            """
            SELECT nome, telefone, status, tentativas, mensagem, erro
            FROM alertas_fila
            """
        ).fetchone()
        total_envios = conn.execute("SELECT COUNT(*) FROM alertas_envios").fetchone()[0]
        conn.close()

        self.assertEqual(row["nome"], "Maria")
        self.assertEqual(row["telefone"], "5567999999999")
        self.assertEqual(row["status"], "pendente")
        self.assertEqual(row["tentativas"], 0)
        self.assertIn("Alerta de teste", row["mensagem"])
        self.assertNotIn("/unsubscribe?token=", row["mensagem"])
        self.assertNotIn("Para cancelar os alertas", row["mensagem"])
        self.assertIsNone(row["erro"])
        self.assertEqual(total_envios, 0)

    def test_enviar_alerta_nao_aguarda_entre_usuarios(self):
        conn = self.abrir_banco()
        conn.executemany(
            """
            INSERT INTO usuarios (nome, telefone, receber_whatsapp)
            VALUES (?, ?, ?)
            """,
            [
                ("Maria", "67999999999", 1),
                ("Joao", "67888888888", 1),
            ],
        )
        conn.commit()
        conn.close()

        pausas = []
        self.updater.log = lambda mensagem: None
        self.updater.time.sleep = lambda segundos: pausas.append(segundos)

        resultado = self.updater.enviar_alerta("Alerta de teste")

        self.assertEqual(resultado, {"total": 2, "enfileirados": 2, "falhas": 0})
        self.assertEqual(pausas, [])

        conn = self.abrir_banco()
        total_fila = conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0]
        conn.close()
        self.assertEqual(total_fila, 2)

    def test_alerta_frio_rearmado_informa_temperatura_maxima(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.data_local = lambda: "2026-06-25"
        self.updater.salvar_resumo_diario_banco = lambda data: None

        mensagens = []
        self.updater.enviar_alerta = lambda mensagem: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(12.0, 12.0, 0, 0, 50, 0)
        self.updater.verificar_alertas(25.0, 25.0, 0, 0, 50, 0)
        self.updater.verificar_alertas(12.0, 12.0, 0, 0, 50, 0)

        self.assertEqual(len(mensagens), 2)
        self.assertIn("Temperatura Baixa!*", mensagens[0])
        self.assertNotIn("caiu novamente", mensagens[0])
        self.assertIn("Temperatura Baixa novamente!*", mensagens[1])
        self.assertIn("subiu até *25°C*", mensagens[1])
        self.assertIn("caiu novamente para *12°C*", mensagens[1])
        self.assertNotIn(".0°C", mensagens[1])

    def test_alerta_formata_temperatura_sem_casas_decimais(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.data_local = lambda: "2026-06-25"
        self.updater.salvar_resumo_diario_banco = lambda data: None

        mensagens = []
        self.updater.enviar_alerta = lambda mensagem: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(12.5, 12.4, 0, 0, 50, 0)

        self.assertEqual(len(mensagens), 1)
        self.assertIn("Registrados *13°C*", mensagens[0])
        self.assertIn("Sensação térmica de *12°C*", mensagens[0])
        self.assertNotIn("12.5°C", mensagens[0])
        self.assertNotIn("12.4°C", mensagens[0])

    def test_alerta_frio_nao_repete_na_virada_do_dia_sem_rearme(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.salvar_resumo_diario_banco = lambda data: None

        datas = iter(["2026-06-25", "2026-06-26"])
        self.updater.data_local = lambda: next(datas)

        mensagens = []
        self.updater.enviar_alerta = lambda mensagem: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(12.0, 12.0, 0, 0, 50, 0)
        self.updater.verificar_alertas(11.8, 11.8, 0, 0, 50, 0)

        estado = self.updater.carregar_estado()

        self.assertEqual(len(mensagens), 1)
        self.assertEqual(estado["data"], "2026-06-26")
        self.assertEqual(estado["nivel_frio"], 1)
        self.assertFalse(estado["frio_rearmado"])
        self.assertEqual(estado["temp_max_apos_alerta_frio"], 12.0)

    def test_carregar_estado_migra_arquivo_para_banco(self):
        arquivo_estado = Path(self.tmp.name) / "alert_state.json"
        arquivo_estado.write_text(
            '{"data": "2026-06-25", "nivel_frio": 2, "rajada_max_nuvem": 72.5}',
            encoding="utf-8",
        )
        self.updater.STATE_FILE = str(arquivo_estado)
        self.updater.log = lambda mensagem: None

        estado = self.updater.carregar_estado()
        estado_banco = self.database.obter_estado_alertas()

        self.assertEqual(estado["data"], "2026-06-25")
        self.assertEqual(estado["nivel_frio"], 2)
        self.assertEqual(estado["rajada_max_nuvem"], 72.5)
        self.assertEqual(estado_banco["nivel_frio"], 2)
        self.assertEqual(estado_banco["rajada_max_nuvem"], 72.5)

    def test_api_le_rajada_maxima_do_estado_no_banco(self):
        import routes.api as api_module

        api_module = importlib.reload(api_module)
        self.database.salvar_estado_alertas(
            {"data": "2026-06-25", "rajada_max_nuvem": "81.4"}
        )

        self.assertEqual(api_module.rajada_maxima_estado_alertas(), 81.4)


if __name__ == "__main__":
    unittest.main()
