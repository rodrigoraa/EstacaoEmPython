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


class WhatsAppSenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "estacao_teste.db")
        os.environ["EVOLUTION_URL"] = "http://localhost"
        os.environ["EVOLUTION_API_KEY"] = "fake"
        os.environ["EVOLUTION_INSTANCE"] = "fake"
        os.environ["SECRET_KEY"] = "segredo-teste"

        import database

        self.database = importlib.reload(database)
        self.database.init_db()

        import workers.whatsapp_sender

        self.sender = importlib.reload(workers.whatsapp_sender)
        self.sender.log = lambda mensagem: None

    def tearDown(self):
        self.tmp.cleanup()
        for chave in (
            "ESTACAO_DB",
            "EVOLUTION_URL",
            "EVOLUTION_API_KEY",
            "EVOLUTION_INSTANCE",
            "SECRET_KEY",
        ):
            os.environ.pop(chave, None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def enfileirar(self, usuarios):
        conn = self.abrir_banco()
        conn.executemany(
            """
            INSERT INTO alertas_fila (
                usuario_id,
                nome,
                telefone,
                mensagem,
                status
            ) VALUES (?, ?, ?, ?, 'pendente')
            """,
            usuarios,
        )
        conn.commit()
        conn.close()

    def test_processa_item_pendente_e_registra_historico(self):
        self.enfileirar([(1, "Maria", "5567999999999", "Alerta de teste")])
        envios = []
        self.sender.enviar_whatsapp = lambda numero, mensagem: envios.append(
            (numero, mensagem)
        )

        resultado = self.sender.processar_fila(limite=1, intervalo=0)

        self.assertEqual(resultado, {"processados": 1, "enviados": 1, "falhas": 0})
        self.assertEqual(envios, [("5567999999999", "Alerta de teste")])

        conn = self.abrir_banco()
        fila = conn.execute(
            "SELECT status, tentativas, erro, enviado_em FROM alertas_fila"
        ).fetchone()
        envio = conn.execute(
            "SELECT nome, telefone, status, mensagem, erro FROM alertas_envios"
        ).fetchone()
        conn.close()

        self.assertEqual(fila["status"], "enviado")
        self.assertEqual(fila["tentativas"], 1)
        self.assertIsNone(fila["erro"])
        self.assertIsNotNone(fila["enviado_em"])
        self.assertEqual(envio["nome"], "Maria")
        self.assertEqual(envio["telefone"], "5567999999999")
        self.assertEqual(envio["status"], "enviado")
        self.assertEqual(envio["mensagem"], "Alerta de teste")
        self.assertIsNone(envio["erro"])

    def test_processa_fila_respeita_intervalo_entre_envios(self):
        self.enfileirar(
            [
                (1, "Maria", "5567999999999", "Alerta 1"),
                (2, "Joao", "5567888888888", "Alerta 2"),
            ]
        )
        envios = []
        pausas = []
        self.sender.enviar_whatsapp = lambda numero, mensagem: envios.append(
            (numero, mensagem)
        )
        self.sender.time.sleep = lambda segundos: pausas.append(segundos)

        resultado = self.sender.processar_fila(limite=2, intervalo=20)

        self.assertEqual(resultado, {"processados": 2, "enviados": 2, "falhas": 0})
        self.assertEqual(len(envios), 2)
        self.assertEqual(pausas, [20])

    def test_falha_da_evolution_fica_na_fila_e_no_historico(self):
        self.enfileirar([(1, "Maria", "5567999999999", "Alerta de teste")])

        def falhar(numero, mensagem):
            raise Exception("Evolution fora")

        self.sender.enviar_whatsapp = falhar

        resultado = self.sender.processar_fila(limite=1, intervalo=0)

        self.assertEqual(resultado, {"processados": 1, "enviados": 0, "falhas": 1})

        conn = self.abrir_banco()
        fila = conn.execute("SELECT status, tentativas, erro FROM alertas_fila").fetchone()
        envio = conn.execute("SELECT status, erro FROM alertas_envios").fetchone()
        conn.close()

        self.assertEqual(fila["status"], "falhou")
        self.assertEqual(fila["tentativas"], 1)
        self.assertIn("Evolution fora", fila["erro"])
        self.assertEqual(envio["status"], "falhou")
        self.assertIn("Evolution fora", envio["erro"])


if __name__ == "__main__":
    unittest.main()
