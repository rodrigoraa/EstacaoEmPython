import importlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from flask import Flask


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class AdminUsuariosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "estacao_teste.db")
        os.environ["ADMIN_PASSWORD"] = "senha-teste"

        import database
        import extensions
        import routes.admin as admin_module

        self.database = importlib.reload(database)
        self.extensions = importlib.reload(extensions)
        self.admin_module = importlib.reload(admin_module)
        self.database.init_db()

        self.app = Flask(
            __name__,
            template_folder=str(ESTACAO_DIR / "templates"),
            static_folder=str(ESTACAO_DIR / "static"),
        )
        self.app.config.update(
            SECRET_KEY="teste",
            TESTING=True,
            RATELIMIT_ENABLED=False,
            PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        )
        self.extensions.limiter.init_app(self.app)
        self.app.register_blueprint(self.admin_module.admin_routes)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("ESTACAO_DB", None)
        os.environ.pop("ADMIN_PASSWORD", None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def autenticar_admin(self):
        with self.client.session_transaction() as sess:
            sess["logado"] = True
            sess["ultimo_acesso"] = time.time()
            sess["csrf_token"] = "csrf-teste"

    def cadastrar_usuario(self, nome="Maria", telefone="67999999999"):
        conn = self.abrir_banco()
        cursor = conn.execute(
            """
            INSERT INTO usuarios (nome, telefone, endereco, receber_whatsapp, ativo)
            VALUES (?, ?, ?, ?, ?)
            """,
            (nome, telefone, "Linha 1", 0, 1),
        )
        conn.commit()
        usuario_id = cursor.lastrowid
        conn.close()
        return usuario_id

    def test_admin_edita_usuario_e_registra_evento(self):
        usuario_id = self.cadastrar_usuario()
        self.autenticar_admin()

        resposta = self.client.post(
            f"/admin/usuarios/{usuario_id}/editar",
            data={
                "csrf_token": "csrf-teste",
                "nome": "Maria Silva",
                "telefone": "(67) 98888-7777",
                "endereco": "Rua Central",
                "receber_whatsapp": "1",
            },
        )

        self.assertEqual(resposta.status_code, 302)
        self.assertIn("/admin#usuarios", resposta.headers["Location"])

        conn = self.abrir_banco()
        usuario = conn.execute(
            "SELECT nome, telefone, endereco, receber_whatsapp, ativo FROM usuarios WHERE id = ?",
            (usuario_id,),
        ).fetchone()
        evento = conn.execute(
            "SELECT acao, nome, telefone, detalhe FROM cadastro_eventos WHERE usuario_id = ?",
            (usuario_id,),
        ).fetchone()
        conn.close()

        self.assertEqual(usuario["nome"], "Maria Silva")
        self.assertEqual(usuario["telefone"], "67988887777")
        self.assertEqual(usuario["endereco"], "Rua Central")
        self.assertEqual(usuario["receber_whatsapp"], 1)
        self.assertEqual(usuario["ativo"], 0)
        self.assertEqual(evento["acao"], "edicao_admin")
        self.assertEqual(evento["nome"], "Maria Silva")
        self.assertEqual(evento["telefone"], "67988887777")
        self.assertIn("painel administrativo", evento["detalhe"])

    def test_admin_renderiza_tabela_de_usuarios(self):
        usuario_id = self.cadastrar_usuario()
        self.admin_module.obter_status_evolution = lambda: {
            "ok": True,
            "estado": "connected",
            "detalhe": "HTTP 200",
        }
        self.autenticar_admin()

        resposta = self.client.get("/admin")

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Usuários cadastrados".encode("utf-8"), resposta.data)
        self.assertIn(f"editar-usuario-{usuario_id}".encode("utf-8"), resposta.data)

    def test_admin_nao_permite_telefone_duplicado(self):
        usuario_id = self.cadastrar_usuario(nome="Maria", telefone="67999999999")
        self.cadastrar_usuario(nome="Joao", telefone="67888888888")
        self.autenticar_admin()

        resposta = self.client.post(
            f"/admin/usuarios/{usuario_id}/editar",
            data={
                "csrf_token": "csrf-teste",
                "nome": "Maria",
                "telefone": "67888888888",
                "endereco": "Linha 1",
                "receber_whatsapp": "1",
                "ativo": "1",
            },
        )

        self.assertEqual(resposta.status_code, 302)

        conn = self.abrir_banco()
        usuario = conn.execute(
            "SELECT telefone FROM usuarios WHERE id = ?",
            (usuario_id,),
        ).fetchone()
        eventos = conn.execute(
            "SELECT COUNT(*) FROM cadastro_eventos WHERE usuario_id = ?",
            (usuario_id,),
        ).fetchone()[0]
        conn.close()

        self.assertEqual(usuario["telefone"], "67999999999")
        self.assertEqual(eventos, 0)


if __name__ == "__main__":
    unittest.main()
