import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import Flask


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class CancelamentoAlertasTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "estacao_teste.db")
        os.environ["SECRET_KEY"] = "segredo-teste"

        import database
        import extensions
        import routes.public as public_module
        import unsubscribe_tokens

        self.database = importlib.reload(database)
        self.extensions = importlib.reload(extensions)
        self.public_module = importlib.reload(public_module)
        self.unsubscribe_tokens = importlib.reload(unsubscribe_tokens)
        self.database.init_db()

        self.app = Flask(
            __name__,
            template_folder=str(ESTACAO_DIR / "templates"),
            static_folder=str(ESTACAO_DIR / "static"),
        )
        self.app.config.update(
            SECRET_KEY="segredo-teste",
            SERVER_NAME="meteo.test",
            TESTING=True,
            RATELIMIT_ENABLED=False,
        )
        self.extensions.limiter.init_app(self.app)
        self.app.register_blueprint(self.public_module.public_routes)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("ESTACAO_DB", None)
        os.environ.pop("SECRET_KEY", None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def cadastrar_usuario(self, telefone="67999999999"):
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO usuarios (nome, telefone, endereco, receber_whatsapp, ativo)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("Maria", telefone, "Linha 1", 1, 1),
        )
        conn.commit()
        conn.close()

    def total_usuarios(self):
        conn = self.abrir_banco()
        total = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
        conn.close()
        return total

    def eventos_cadastro(self):
        conn = self.abrir_banco()
        eventos = conn.execute(
            "SELECT acao, telefone, receber_whatsapp FROM cadastro_eventos ORDER BY id"
        ).fetchall()
        conn.close()
        return eventos

    def test_get_com_telefone_nao_remove_usuario(self):
        self.cadastrar_usuario()

        resposta = self.client.get("/unsubscribe?tel=5567999999999")

        self.assertEqual(resposta.status_code, 400)
        self.assertIn("Confirmação necessária".encode("utf-8"), resposta.data)
        self.assertEqual(self.total_usuarios(), 1)

    def test_cadastro_com_whatsapp_nao_envia_confirmacao(self):
        resposta = self.client.post(
            "/",
            data={
                "nome": "Maria",
                "telefone": "(67) 99999-9999",
                "endereco": "Linha 1",
                "whatsapp": "1",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Cadastro realizado com sucesso".encode("utf-8"), resposta.data)

        conn = self.abrir_banco()
        usuario = conn.execute(
            "SELECT nome, telefone, receber_whatsapp FROM usuarios"
        ).fetchone()
        conn.close()

        self.assertEqual(usuario["nome"], "Maria")
        self.assertEqual(usuario["telefone"], "67999999999")
        self.assertEqual(usuario["receber_whatsapp"], 1)
        self.assertEqual(
            [evento["acao"] for evento in self.eventos_cadastro()],
            ["cadastro"],
        )

    def test_cadastro_sem_whatsapp_salva_sem_confirmacao(self):
        resposta = self.client.post(
            "/",
            data={
                "nome": "Joao",
                "telefone": "(67) 98888-7777",
                "endereco": "Linha 2",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Cadastro realizado com sucesso".encode("utf-8"), resposta.data)
        self.assertEqual(
            [evento["acao"] for evento in self.eventos_cadastro()],
            ["cadastro"],
        )

    def test_cadastro_com_whatsapp_nao_depende_da_evolution(self):
        resposta = self.client.post(
            "/",
            data={
                "nome": "Ana",
                "telefone": "(67) 97777-6666",
                "endereco": "Linha 3",
                "whatsapp": "1",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Cadastro realizado com sucesso".encode("utf-8"), resposta.data)
        self.assertEqual(self.total_usuarios(), 1)
        self.assertEqual(
            [evento["acao"] for evento in self.eventos_cadastro()],
            ["cadastro"],
        )

    def test_token_get_confirma_e_post_remove_usuario(self):
        self.cadastrar_usuario()
        token = self.unsubscribe_tokens.gerar_token_cancelamento("67999999999")

        confirmacao = self.client.get(f"/unsubscribe?token={token}")

        self.assertEqual(confirmacao.status_code, 200)
        self.assertIn("Confirmar cancelamento".encode("utf-8"), confirmacao.data)
        self.assertEqual(self.total_usuarios(), 1)

        resposta = self.client.post("/unsubscribe", data={"token": token})

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Cancelado com sucesso".encode("utf-8"), resposta.data)
        self.assertEqual(self.total_usuarios(), 0)

    def test_solicitar_cancelamento_envia_link_assinado(self):
        self.cadastrar_usuario()
        envios = []
        self.public_module.enviar_link_cancelamento_whatsapp = (
            lambda numero, link: envios.append((numero, link))
        )

        resposta = self.client.post(
            "/unsubscribe/request",
            data={"telefone": "(67) 99999-9999"},
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(envios[0][0], "5567999999999")
        token = parse_qs(urlparse(envios[0][1]).query)["token"][0]
        self.assertEqual(
            self.unsubscribe_tokens.validar_token_cancelamento(token),
            "5567999999999",
        )
        self.assertEqual(self.total_usuarios(), 1)


if __name__ == "__main__":
    unittest.main()
