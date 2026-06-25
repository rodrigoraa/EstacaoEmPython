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
        os.environ["EVOLUTION_URL"] = "http://localhost"
        os.environ["EVOLUTION_API_KEY"] = "fake"
        os.environ["EVOLUTION_INSTANCE"] = "fake"

        import database

        self.database = importlib.reload(database)
        self.database.init_db()

        import workers.updater

        self.updater = importlib.reload(workers.updater)

    def tearDown(self):
        self.tmp.cleanup()
        for chave in (
            "ESTACAO_DB",
            "EVOLUTION_URL",
            "EVOLUTION_API_KEY",
            "EVOLUTION_INSTANCE",
        ):
            os.environ.pop(chave, None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def test_enviar_alerta_registra_envio_para_usuario_inscrito(self):
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

        envios = []
        self.updater.log = lambda mensagem: None
        self.updater.enviar_whatsapp = lambda numero, mensagem: envios.append(
            (numero, mensagem)
        )

        resultado = self.updater.enviar_alerta("Alerta de teste")

        self.assertEqual(resultado, {"total": 1, "enviados": 1, "falhas": 0})
        self.assertEqual(envios[0][0], "5567999999999")
        self.assertIn("Alerta de teste", envios[0][1])

        conn = self.abrir_banco()
        row = conn.execute(
            """
            SELECT nome, telefone, status, mensagem, erro
            FROM alertas_envios
            """
        ).fetchone()
        conn.close()

        self.assertEqual(row["nome"], "Maria")
        self.assertEqual(row["telefone"], "5567999999999")
        self.assertEqual(row["status"], "enviado")
        self.assertIn("Alerta de teste", row["mensagem"])
        self.assertIsNone(row["erro"])

    def test_alerta_frio_rearmado_informa_temperatura_maxima(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.data_local = lambda: "2026-06-25"
        self.updater.salvar_resumo_diario_banco = lambda data: None

        mensagens = []
        self.updater.enviar_alerta = lambda mensagem: mensagens.append(
            mensagem
        ) or {"total": 1, "enviados": 1, "falhas": 0}

        self.updater.verificar_alertas(12.0, 12.0, 0, 0, 50, 0)
        self.updater.verificar_alertas(25.0, 25.0, 0, 0, 50, 0)
        self.updater.verificar_alertas(12.0, 12.0, 0, 0, 50, 0)

        self.assertEqual(len(mensagens), 2)
        self.assertIn("Temperatura Baixa!*", mensagens[0])
        self.assertNotIn("caiu novamente", mensagens[0])
        self.assertIn("Temperatura Baixa novamente!*", mensagens[1])
        self.assertIn("subiu até *25.0°C*", mensagens[1])
        self.assertIn("caiu novamente para *12.0°C*", mensagens[1])


if __name__ == "__main__":
    unittest.main()
