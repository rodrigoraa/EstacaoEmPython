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


class CampanhaWhatsAppUnicoTest(unittest.TestCase):
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

        import workers.enviar_aviso_whatsapp_unico as campanha

        self.campanha = importlib.reload(campanha)
        self.campanha.log = lambda mensagem: None

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

    def cadastrar_usuarios(self):
        conn = self.abrir_banco()
        conn.executemany(
            """
            INSERT INTO usuarios (nome, telefone, receber_whatsapp, ativo)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("Maria", "(67) 99999-9999", 1, 1),
                ("Joao", "67888888888", 1, 0),
                ("Ana", "67777777777", 0, 1),
            ],
        )
        conn.commit()
        conn.close()

    def test_simulacao_nao_envia_nem_registra(self):
        self.cadastrar_usuarios()
        envios = []
        self.campanha.enviar_whatsapp = lambda numero, mensagem: envios.append(
            (numero, mensagem)
        )

        resultado = self.campanha.enviar_campanha(
            campanha_id="teste",
            template="Ola {nome}",
            intervalo=0,
            confirmar=False,
        )

        self.assertEqual(resultado, {"total": 1, "enviados": 0, "falhas": 0, "pulados": 0})
        self.assertEqual(envios, [])

        conn = self.abrir_banco()
        total = conn.execute("SELECT COUNT(*) FROM campanhas_whatsapp_envios").fetchone()[0]
        conn.close()
        self.assertEqual(total, 0)

    def test_envia_uma_vez_para_usuarios_inscritos_ativos(self):
        self.cadastrar_usuarios()
        envios = []
        pausas = []
        self.campanha.enviar_whatsapp = lambda numero, mensagem: envios.append(
            (numero, mensagem)
        )
        self.campanha.time.sleep = lambda segundos: pausas.append(segundos)

        primeiro = self.campanha.enviar_campanha(
            campanha_id="teste",
            template="Ola {nome}",
            intervalo=10,
            confirmar=True,
        )
        segundo = self.campanha.enviar_campanha(
            campanha_id="teste",
            template="Ola {nome}",
            intervalo=10,
            confirmar=True,
        )

        self.assertEqual(primeiro, {"total": 1, "enviados": 1, "falhas": 0, "pulados": 0})
        self.assertEqual(segundo, {"total": 1, "enviados": 0, "falhas": 0, "pulados": 1})
        self.assertEqual(envios, [("5567999999999", "Ola Maria")])
        self.assertEqual(pausas, [])

        conn = self.abrir_banco()
        row = conn.execute(
            """
            SELECT campanha_id, nome, telefone, status, mensagem
            FROM campanhas_whatsapp_envios
            """
        ).fetchone()
        conn.close()

        self.assertEqual(row["campanha_id"], "teste")
        self.assertEqual(row["nome"], "Maria")
        self.assertEqual(row["telefone"], "5567999999999")
        self.assertEqual(row["status"], "enviado")
        self.assertEqual(row["mensagem"], "Ola Maria")


if __name__ == "__main__":
    unittest.main()
