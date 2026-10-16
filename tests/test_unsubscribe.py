import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from flask import Flask
from itsdangerous import SignatureExpired


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
        import signup_tokens
        import routes.public as public_module
        import unsubscribe_tokens

        self.database = importlib.reload(database)
        self.extensions = importlib.reload(extensions)
        self.signup_tokens = importlib.reload(signup_tokens)
        self.unsubscribe_tokens = importlib.reload(unsubscribe_tokens)
        self.public_module = importlib.reload(public_module)
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
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "csrf-public-test"

    def post(self, url, *, data=None, **kwargs):
        data = dict(data or {})
        data.setdefault("csrf_token", "csrf-public-test")
        return self.client.post(url, data=data, **kwargs)

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

    def usuario_por_telefone(self, telefone):
        conn = self.abrir_banco()
        usuario = conn.execute(
            """
            SELECT id, nome, telefone, endereco, receber_whatsapp, status_cadastro
            FROM usuarios WHERE telefone = ? OR telefone_canonico = ?
            """,
            (telefone, self.unsubscribe_tokens.telefone_com_codigo_pais(telefone)),
        ).fetchone()
        conn.close()
        return usuario

    def test_get_com_telefone_nao_remove_usuario(self):
        self.cadastrar_usuario()

        resposta = self.client.get("/unsubscribe?tel=5567999999999")

        self.assertEqual(resposta.status_code, 400)
        self.assertIn("Confirmação necessária".encode("utf-8"), resposta.data)
        self.assertEqual(self.total_usuarios(), 1)

    def test_cadastro_com_whatsapp_fica_pendente_e_envia_confirmacao(self):
        envios = []
        self.public_module.enviar_link_confirmacao_whatsapp = (
            lambda numero, link: envios.append((numero, link))
        )
        resposta = self.post(
            "/",
            data={
                "nome": "Maria",
                "telefone": "(67) 99999-9999",
                "endereco": "Linha 1",
                "whatsapp": "1",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Cadastro pendente".encode("utf-8"), resposta.data)

        conn = self.abrir_banco()
        usuario = conn.execute(
            "SELECT id, nome, telefone, receber_whatsapp, status_cadastro FROM usuarios"
        ).fetchone()
        conn.close()

        self.assertEqual(usuario["nome"], "Maria")
        self.assertEqual(usuario["telefone"], "5567999999999")
        self.assertEqual(usuario["receber_whatsapp"], 0)
        self.assertEqual(usuario["status_cadastro"], "pendente")
        self.assertEqual(envios[0][0], "5567999999999")
        token = parse_qs(urlparse(envios[0][1]).query)["token"][0]
        self.assertEqual(
            self.signup_tokens.validar_token_confirmacao(token),
            (usuario["id"], "5567999999999"),
        )
        self.assertEqual(
            [evento["acao"] for evento in self.eventos_cadastro()],
            ["cadastro"],
        )

    def test_cadastro_sem_whatsapp_salva_sem_confirmacao(self):
        resposta = self.post(
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

    def test_cadastro_rejeita_telefone_invalido(self):
        resposta = self.post(
            "/",
            data={"nome": "Joao", "telefone": "123", "endereco": "Linha 2"},
        )
        self.assertEqual(resposta.status_code, 400)
        self.assertIn(b"telefone brasileiro", resposta.data)
        self.assertEqual(self.total_usuarios(), 0)

    def test_falha_da_evolution_preserva_cadastro_pendente(self):
        self.public_module.enviar_link_confirmacao_whatsapp = (
            lambda *_args: (_ for _ in ()).throw(RuntimeError("indisponível"))
        )
        resposta = self.post(
            "/",
            data={
                "nome": "Ana",
                "telefone": "(67) 97777-6666",
                "endereco": "Linha 3",
                "whatsapp": "1",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Cadastro salvo como pendente".encode("utf-8"), resposta.data)
        self.assertEqual(self.total_usuarios(), 1)
        self.assertEqual(
            [evento["acao"] for evento in self.eventos_cadastro()],
            ["cadastro"],
        )

    def test_cadastro_pendente_repetido_aplica_cooldown_sem_alterar_usuario(self):
        envios = []
        self.public_module.enviar_link_confirmacao_whatsapp = (
            lambda numero, link: envios.append((numero, link))
        )
        primeiro = {
            "nome": "Maria Original",
            "telefone": "(67) 99999-9999",
            "endereco": "Linha Original",
            "whatsapp": "1",
        }
        repetido = {
            "nome": "Nome Alterado",
            "telefone": "(67) 99999-9999",
            "endereco": "Endereço Alterado",
            "whatsapp": "1",
        }

        self.post("/", data=primeiro)
        resposta = self.post("/", data=repetido)

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Se o número estiver".encode("utf-8"), resposta.data)
        self.assertEqual(self.total_usuarios(), 1)
        usuario = self.usuario_por_telefone("67999999999")
        self.assertEqual(usuario["nome"], "Maria Original")
        self.assertEqual(usuario["endereco"], "Linha Original")
        self.assertEqual(usuario["receber_whatsapp"], 0)
        self.assertEqual(usuario["status_cadastro"], "pendente")
        self.assertEqual(len(envios), 1)
        primeiro_token = parse_qs(urlparse(envios[0][1]).query)["token"][0]
        self.assertEqual(
            self.signup_tokens.validar_token_confirmacao(primeiro_token),
            (usuario["id"], "5567999999999"),
        )
        self.assertEqual(
            [evento["acao"] for evento in self.eventos_cadastro()],
            ["cadastro", "cadastro_duplicado"],
        )

    def test_cadastro_ativo_repetido_nao_reenvia_confirmacao(self):
        self.cadastrar_usuario()
        envios = []
        self.public_module.enviar_link_confirmacao_whatsapp = (
            lambda numero, link: envios.append((numero, link))
        )

        resposta = self.post(
            "/",
            data={
                "nome": "Nome Alterado",
                "telefone": "(67) 99999-9999",
                "endereco": "Endereço Alterado",
                "whatsapp": "1",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Se o número estiver".encode("utf-8"), resposta.data)
        self.assertEqual(envios, [])
        self.assertEqual(self.total_usuarios(), 1)
        usuario = self.usuario_por_telefone("67999999999")
        self.assertEqual(usuario["nome"], "Maria")
        self.assertEqual(usuario["endereco"], "Linha 1")
        self.assertEqual(usuario["receber_whatsapp"], 1)
        self.assertEqual(usuario["status_cadastro"], "ativo")
        self.assertEqual(
            [evento["acao"] for evento in self.eventos_cadastro()],
            ["cadastro_duplicado"],
        )

    def test_falha_no_reenvio_mantem_pendente_e_resposta_generica(self):
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO usuarios (
                nome, telefone, endereco, receber_whatsapp, ativo, status_cadastro
            ) VALUES ('Ana', '67977776666', 'Linha 3', 0, 1, 'pendente')
            """
        )
        conn.commit()
        conn.close()
        self.public_module.enviar_link_confirmacao_whatsapp = (
            lambda *_args: (_ for _ in ()).throw(RuntimeError("indisponível"))
        )

        resposta = self.post(
            "/",
            data={
                "nome": "Ana Alterada",
                "telefone": "(67) 97777-6666",
                "endereco": "Outra linha",
                "whatsapp": "1",
            },
        )

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Se o número estiver".encode("utf-8"), resposta.data)
        self.assertEqual(self.total_usuarios(), 1)
        usuario = self.usuario_por_telefone("67977776666")
        self.assertEqual(usuario["nome"], "Ana")
        self.assertEqual(usuario["receber_whatsapp"], 0)
        self.assertEqual(usuario["status_cadastro"], "pendente")
        self.assertEqual(
            [evento["acao"] for evento in self.eventos_cadastro()],
            ["confirmacao_reenvio_falhou"],
        )

    def test_limite_de_reenvio_tem_padrao_conservador(self):
        self.assertEqual(self.public_module.SIGNUP_RESEND_RATE_LIMIT, "5 per hour")

    def test_limite_de_reenvio_bloqueia_sexta_tentativa_do_ip(self):
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO usuarios (
                nome, telefone, endereco, receber_whatsapp, ativo, status_cadastro
            ) VALUES ('Bia', '67966665555', 'Linha 4', 0, 1, 'pendente')
            """
        )
        conn.commit()
        conn.close()
        envios = []
        self.public_module.enviar_link_confirmacao_whatsapp = (
            lambda numero, link: envios.append((numero, link))
        )

        app_limitado = Flask(
            __name__,
            template_folder=str(ESTACAO_DIR / "templates"),
            static_folder=str(ESTACAO_DIR / "static"),
        )
        app_limitado.config.update(
            SECRET_KEY="segredo-teste",
            SERVER_NAME="meteo.test",
            TESTING=True,
            RATELIMIT_ENABLED=True,
            RATELIMIT_STORAGE_URI="memory://",
            RATELIMIT_KEY_PREFIX=f"reenvio-{self.tmp.name}",
        )
        self.extensions.limiter.init_app(app_limitado)
        app_limitado.register_blueprint(self.public_module.public_routes)
        client = app_limitado.test_client()
        with client.session_transaction() as sess:
            sess["csrf_token"] = "csrf-public-test"
        dados = {
            "csrf_token": "csrf-public-test",
            "nome": "Bia",
            "telefone": "(67) 96666-5555",
            "endereco": "Linha 4",
            "whatsapp": "1",
        }

        respostas = [client.post("/", data=dados) for _ in range(6)]

        self.assertEqual([resposta.status_code for resposta in respostas[:5]], [200] * 5)
        self.assertEqual(respostas[5].status_code, 429)
        self.assertEqual(len(envios), 1)
        self.assertEqual(self.total_usuarios(), 1)

    def test_confirmacao_valida_e_idempotente_ativa_whatsapp(self):
        conn = self.abrir_banco()
        cursor = conn.execute(
            """
            INSERT INTO usuarios (
                nome, telefone, endereco, receber_whatsapp, ativo, status_cadastro
            ) VALUES ('Maria', '67999999999', 'Linha 1', 0, 1, 'pendente')
            """
        )
        usuario_id = cursor.lastrowid
        conn.commit()
        conn.close()
        token = self.signup_tokens.gerar_token_confirmacao(usuario_id, "67999999999")

        primeira = self.client.get(f"/signup/confirm?token={token}")
        segunda = self.client.get(f"/signup/confirm?token={token}")

        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 200)
        conn = self.abrir_banco()
        usuario = conn.execute(
            "SELECT receber_whatsapp, status_cadastro, confirmado_em FROM usuarios WHERE id = ?",
            (usuario_id,),
        ).fetchone()
        eventos = conn.execute(
            "SELECT COUNT(*) FROM cadastro_eventos WHERE acao = 'cadastro_confirmado'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(usuario["receber_whatsapp"], 1)
        self.assertEqual(usuario["status_cadastro"], "ativo")
        self.assertIsNotNone(usuario["confirmado_em"])
        self.assertEqual(eventos, 1)

    def test_confirmacao_invalida_e_usuario_inexistente(self):
        invalida = self.client.get("/signup/confirm?token=invalido")
        token = self.signup_tokens._serializer().dumps({
            "usuario_id": 999, "telefone": "5567999999999", "cadastro_nonce": "0" * 64,
        })
        inexistente = self.client.get(f"/signup/confirm?token={token}")

        self.assertEqual(invalida.status_code, 400)
        self.assertEqual(inexistente.status_code, 400)

    def test_confirmacao_expirada(self):
        class SerializerExpirado:
            def loads(self, *args, **kwargs):
                raise SignatureExpired("expirado")

        with patch.object(
            self.signup_tokens, "_serializer", return_value=SerializerExpirado()
        ):
            resposta = self.client.get("/signup/confirm?token=token-expirado")

        self.assertEqual(resposta.status_code, 400)
        self.assertIn("Link expirado".encode("utf-8"), resposta.data)

    def test_token_get_confirma_e_post_remove_usuario(self):
        self.cadastrar_usuario()
        token = self.unsubscribe_tokens.gerar_token_cancelamento("67999999999")

        confirmacao = self.client.get(f"/unsubscribe?token={token}")

        self.assertEqual(confirmacao.status_code, 200)
        self.assertIn("Confirmar cancelamento".encode("utf-8"), confirmacao.data)
        self.assertEqual(self.total_usuarios(), 1)

        resposta = self.post("/unsubscribe", data={"token": token})

        self.assertEqual(resposta.status_code, 200)
        self.assertIn("Cancelado com sucesso".encode("utf-8"), resposta.data)
        self.assertEqual(self.total_usuarios(), 0)

    def test_solicitar_cancelamento_envia_link_assinado(self):
        self.cadastrar_usuario()
        envios = []
        self.public_module.enviar_link_cancelamento_whatsapp = (
            lambda numero, link: envios.append((numero, link))
        )

        resposta = self.post(
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

    def test_public_posts_require_csrf_before_database_or_messaging(self):
        self.cadastrar_usuario()
        with patch.object(self.public_module, "enviar_link_confirmacao_whatsapp") as signup_send, \
             patch.object(self.public_module, "enviar_link_cancelamento_whatsapp") as cancel_send:
            signup = self.client.post("/", data={
                "nome": "Maria", "telefone": "67988887777", "endereco": "Rua 1", "whatsapp": "1",
            })
            cancel = self.client.post("/unsubscribe/request", data={"telefone": "67999999999"})
            confirm = self.client.post("/unsubscribe", data={"token": "qualquer"})
        self.assertEqual([signup.status_code, cancel.status_code, confirm.status_code], [403] * 3)
        signup_send.assert_not_called()
        cancel_send.assert_not_called()
        self.assertEqual(self.total_usuarios(), 1)

    def test_country_prefix_duplicate_has_one_record_and_one_confirmation(self):
        with patch.object(self.public_module, "enviar_link_confirmacao_whatsapp") as send:
            for telefone in ("67999999999", "+55 (67) 99999-9999"):
                resposta = self.post("/", data={
                    "nome": "Maria", "telefone": telefone, "endereco": "Rua 1", "whatsapp": "1",
                })
                self.assertEqual(resposta.status_code, 200)
        self.assertEqual(self.total_usuarios(), 1)
        self.assertEqual(send.call_count, 1)

    def test_cancel_token_is_consumed_and_cannot_cancel_new_registration(self):
        self.cadastrar_usuario()
        token = self.unsubscribe_tokens.gerar_token_cancelamento("67999999999")
        self.assertEqual(self.post("/unsubscribe", data={"token": token}).status_code, 200)
        self.assertEqual(self.post("/unsubscribe", data={"token": token}).status_code, 400)
        self.cadastrar_usuario()
        self.assertEqual(self.post("/unsubscribe", data={"token": token}).status_code, 400)
        self.assertEqual(self.total_usuarios(), 1)

    def test_phone_edit_revokes_both_tokens_even_when_changed_back(self):
        self.cadastrar_usuario()
        conn = self.abrir_banco()
        usuario_id = conn.execute("SELECT id FROM usuarios").fetchone()[0]
        conn.close()
        cancel_token = self.unsubscribe_tokens.gerar_token_cancelamento("67999999999")
        signup_token = self.signup_tokens.gerar_token_confirmacao(usuario_id, "67999999999")
        conn = self.abrir_banco()
        conn.execute("UPDATE usuarios SET telefone = '67988887777' WHERE id = ?", (usuario_id,))
        conn.execute("UPDATE usuarios SET telefone = '67999999999' WHERE id = ?", (usuario_id,))
        conn.commit()
        conn.close()
        self.assertEqual(self.post("/unsubscribe", data={"token": cancel_token}).status_code, 400)
        self.assertEqual(self.client.get(f"/signup/confirm?token={signup_token}").status_code, 400)
        self.assertEqual(self.total_usuarios(), 1)

    def test_cancel_legacy_duplicate_destination_allows_fresh_opt_in(self):
        from services.subscriber_security import garantir_seguranca_cadastros
        self.cadastrar_usuario()
        conn = self.abrir_banco()
        conn.execute("DROP INDEX idx_usuarios_telefone_canonico")
        conn.execute("DROP TRIGGER usuarios_seguranca_insert")
        conn.execute("UPDATE usuarios SET cadastro_nonce = NULL")
        conn.execute("""INSERT INTO usuarios (nome, telefone, endereco, receber_whatsapp)
                        VALUES ('Duplicate', '5567999999999', 'Rua 2', 1)""")
        garantir_seguranca_cadastros(conn)
        conn.commit()
        conn.close()
        token = self.unsubscribe_tokens.gerar_token_cancelamento("67999999999")
        self.assertEqual(self.post("/unsubscribe", data={"token": token}).status_code, 200)
        self.assertEqual(self.total_usuarios(), 0)
        self.assertEqual(len([e for e in self.eventos_cadastro() if e["acao"] == "cancelamento"]), 2)
        fresh = self.post("/", data={"nome": "Maria Nova", "telefone": "67999999999", "endereco": "Rua 1"})
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(self.total_usuarios(), 1)
        self.assertEqual(self.post("/unsubscribe", data={"token": token}).status_code, 400)

    def test_previous_phone_only_cancel_token_is_rejected(self):
        self.cadastrar_usuario()
        token = self.unsubscribe_tokens.serializer().dumps(
            {"telefone": "5567999999999"}, salt="estacao-cancelamento-alertas",
        )
        self.assertEqual(self.post("/unsubscribe", data={"token": token}).status_code, 400)
        self.assertEqual(self.total_usuarios(), 1)

    def test_cancel_requests_and_provider_failure_keep_cooldown(self):
        self.cadastrar_usuario()
        with patch.object(self.public_module, "enviar_link_cancelamento_whatsapp",
                          side_effect=RuntimeError("unavailable")) as send:
            first = self.post("/unsubscribe/request", data={"telefone": "67999999999"})
            second = self.post("/unsubscribe/request", data={"telefone": "5567999999999"})
        self.assertEqual(first.status_code, 500)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(send.call_count, 1)

    def test_signup_oversized_fields_and_fake_number_never_send(self):
        with patch.object(self.public_module, "enviar_link_confirmacao_whatsapp") as send:
            for values in (
                {"nome": "x" * 2000}, {"endereco": "x" * 2000},
                {"telefone": "1111111111111"}, {"nome": "A\x00na"},
            ):
                data = {"nome": "Maria", "telefone": "67999999999", "endereco": "Rua 1", "whatsapp": "1"}
                data.update(values)
                self.assertEqual(self.post("/", data=data).status_code, 400)
        send.assert_not_called()
        self.assertEqual(self.total_usuarios(), 0)

    def test_cancel_expiry_setting_cannot_disable_expiry_or_expand_unbounded(self):
        for setting, days in (("0", 1), ("999999", 30), ("invalid", 7)):
            with patch.dict(os.environ, {"UNSUBSCRIBE_TOKEN_MAX_AGE_DAYS": setting}):
                self.assertEqual(self.unsubscribe_tokens.token_max_age_seconds(), days * 86400)


if __name__ == "__main__":
    unittest.main()
