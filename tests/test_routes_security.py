import hashlib
import hmac
import importlib
import os
import re
import sys
import tempfile
import unittest
import bcrypt
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))
ADMIN_HASH = bcrypt.hashpw(b"strong-test-password", bcrypt.gensalt(rounds=12)).decode()


class ConfiguracaoProducaoTest(unittest.TestCase):
    def configuracao_valida(self):
        return {
            "APP_ENV": "production",
            "SECRET_KEY": "production-secret-0123456789-abcdefghijklmnop",
            "ADMIN_PASSWORD_HASH": ADMIN_HASH,
            "WEBHOOK_SECRET": "production-webhook-0123456789-abcdefghijklmnop",
            "PUBLIC_BASE_URL": "https://meteo.exemplo.test",
            "RATELIMIT_STORAGE_URI": "redis://127.0.0.1:6379/0",
        }

    def test_producao_exige_segredos_e_url_publica(self):
        import config

        casos = (
            ("SECRET_KEY", "SECRET_KEY"),
            ("ADMIN_PASSWORD_HASH", "ADMIN_PASSWORD_HASH"),
            ("WEBHOOK_SECRET", "WEBHOOK_SECRET"),
            ("PUBLIC_BASE_URL", "PUBLIC_BASE_URL"),
        )
        for ausente, esperado in casos:
            with self.subTest(ausente=ausente):
                ambiente = self.configuracao_valida()
                ambiente.pop(ausente)
                with patch.dict(os.environ, ambiente, clear=True):
                    with self.assertRaisesRegex(RuntimeError, esperado):
                        config.validar_configuracao_web()

    def test_hash_admin_substitui_senha_em_producao(self):
        import config

        ambiente = self.configuracao_valida()
        with patch.dict(os.environ, ambiente, clear=True):
            config.validar_configuracao_web()

    def test_producao_recusa_hash_senha_segredos_e_storage_inseguros(self):
        import config

        for campo, valor in (("ADMIN_PASSWORD_HASH", "scrypt:hash-de-teste"),
                             ("ADMIN_PASSWORD", "plaintext"), ("SECRET_KEY", "short"),
                             ("WEBHOOK_SECRET", "a" * 64),
                             ("SIGNUP_CONFIRM_SECRET", "weak"),
                             ("UNSUBSCRIBE_SECRET", "weak"),
                             ("PUBLIC_BASE_URL", "http://meteo.exemplo.test"),
                             ("RATELIMIT_STORAGE_URI", "memory://"),
                             ("RATELIMIT_ENABLED", "false"), ("HSTS_ENABLED", "false")):
            with self.subTest(campo=campo):
                ambiente = self.configuracao_valida()
                ambiente[campo] = valor
                with patch.dict(os.environ, ambiente, clear=True), self.assertRaises(RuntimeError):
                    config.validar_configuracao_web()

    def test_public_base_url_sem_fallback_em_producao(self):
        import config

        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "PUBLIC_BASE_URL"):
                config.public_base_url()

    def test_public_base_url_recusa_host_porta_credenciais_e_controles(self):
        import config

        for url in ("https://", "https://:443", "https://user:secret@meteo.test",
                    "https://@meteo.test", "https://meteo.test:not-a-port",
                    "https://meteo.test:99999", "https://meteo.test:0",
                    "https://[invalid", "https://bad host.test", "https://bad_host.test",
                    "https://meteo.test/path\\secret", "https://meteo.test/?secret=token",
                    "https://meteo.test/#token", "https://meteo.\ttest"):
            with self.subTest(url=url), patch.dict(os.environ, {"PUBLIC_BASE_URL": url}, clear=True):
                with self.assertRaises(RuntimeError):
                    config.public_base_url()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(config.public_base_url(), "https://meteo.eesjv.com.br")

    def test_producao_recusa_cookie_secure_desativado(self):
        import app as app_module

        ambiente = self.configuracao_valida()
        ambiente.update(
            {
                "SESSION_COOKIE_SECURE": "false",
            }
        )
        with patch.dict(os.environ, ambiente, clear=True):
            with self.assertRaisesRegex(RuntimeError, "SESSION_COOKIE_SECURE"):
                app_module.create_app()


class RotasSegurancaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(
            {
                "ESTACAO_DB": str(Path(self.tmp.name) / "teste.db"),
                "SECRET_KEY": "segredo-teste-forte",
                "ADMIN_PASSWORD": "senha-teste",
                "WEBHOOK_SECRET": "webhook-teste",
                "PUBLIC_BASE_URL": "https://meteo.test",
                "RATELIMIT_ENABLED": "false",
            }
        )
        import database
        import app
        import routes.public
        import routes.webhook
        from time_utils import sqlite_local

        self.database = importlib.reload(database)
        self.database.init_db()
        self.app_module = importlib.reload(app)
        self.public = routes.public
        self.webhook = routes.webhook
        self.public.obter_previsao = lambda **kwargs: None
        self.app = self.app_module.app
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

        conn = self.database.get_db()
        agora = sqlite_local()
        conn.execute(
            """
            INSERT INTO historico_clima (
                temp, sensacao, umidade, pressao, uv, radiacao,
                vento_vel, vento_rajada, vento_dir,
                chuva_rate, chuva_evento, chuva_hoje,
                data_hora, data_hora_local
            ) VALUES (25, 25, 60, 1010, 1, 100, 5, 10, 90, 0, 0, 0, ?, ?)
            """,
            (agora, agora),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()
        for chave in (
            "ESTACAO_DB",
            "SECRET_KEY",
            "ADMIN_PASSWORD",
            "WEBHOOK_SECRET",
            "PUBLIC_BASE_URL",
            "RATELIMIT_ENABLED",
        ):
            os.environ.pop(chave, None)

    def test_smoke_rotas_e_headers(self):
        esperados = {
            "/": 200,
            "/historico": 200,
            "/previsao": 200,
            "/sobre": 200,
            "/admin": 200,
            "/api/clima": 200,
            "/api/historico": 200,
            "/health": 200,
        }
        for rota, status in esperados.items():
            with self.subTest(rota=rota):
                resposta = self.client.get(rota)
                self.assertEqual(resposta.status_code, status)
                self.assertEqual(resposta.headers["X-Content-Type-Options"], "nosniff")
                self.assertIn("frame-ancestors", resposta.headers["Content-Security-Policy"])

    def test_admin_csrf_senha_e_sessao(self):
        login = self.client.get("/admin")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', login.data).group(1).decode()
        ausente = self.client.post("/admin", data={"senha": "senha-teste"})
        invalido = self.client.post(
            "/admin", data={"senha": "senha-teste", "csrf_token": "invalido"}
        )
        incorreta = self.client.post(
            "/admin", data={"senha": "errada", "csrf_token": token}
        )
        correta = self.client.post(
            "/admin", data={"senha": "senha-teste", "csrf_token": token}
        )
        self.assertEqual(ausente.status_code, 403)
        self.assertEqual(invalido.status_code, 403)
        self.assertEqual(incorreta.status_code, 200)
        self.assertEqual(correta.status_code, 302)

        with self.database.get_db() as conn:
            conn.execute("UPDATE admin_sessoes SET ultimo_acesso = 0")
        conn.close()
        expirada = self.client.get("/admin")
        self.assertEqual(expirada.status_code, 200)
        with self.client.session_transaction() as sessao:
            self.assertNotIn("admin_session", sessao)

    def assinatura(self, corpo):
        digest = hmac.new(b"webhook-teste", corpo, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def test_webhook_rejeita_tipos_json_e_assinaturas_malformadas_sem_executar(self):
        headers = {"Content-Type": "application/json", "X-GitHub-Event": "push"}
        with patch.object(self.webhook.subprocess, "Popen") as popen:
            for corpo in (b'[]', b'null', b'123', b'{"repository":[]}', b'{"repository":null}'):
                with self.subTest(corpo=corpo):
                    response = self.client.post("/deploy/python", data=corpo, headers={
                        **headers, "X-Hub-Signature-256": self.assinatura(corpo)})
                    self.assertEqual(response.status_code, 200)
                    self.assertIn("payload inv".encode(), response.data)
            for signature in ("sha256=0", "sha256=" + "g" * 64, "sha256=" + "é" * 64):
                with self.subTest(signature=signature):
                    response = self.client.post("/deploy/python", data=b'{}', headers={
                        **headers, "X-Hub-Signature-256": signature})
                    self.assertEqual(response.status_code, 403)
            popen.assert_not_called()

    def test_webhook_valida_assinatura_repo_branch_e_concorrencia(self):
        corpo = (
            b'{"ref":"refs/heads/master","repository":'
            b'{"full_name":"rodrigoraa/EstacaoEmPython"}}'
        )
        headers = {
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": self.assinatura(corpo),
        }
        processo = type("Processo", (), {"pid": 123, "poll": lambda self: None})()
        self.webhook._deploy_processos.clear()
        with patch.object(self.webhook.subprocess, "Popen", return_value=processo):
            primeira = self.client.post("/deploy/python", data=corpo, headers=headers)
            segunda = self.client.post("/deploy/python", data=corpo, headers=headers)
        invalida = self.client.post(
            "/deploy/python",
            data=corpo,
            headers={**headers, "X-Hub-Signature-256": "sha256=0"},
        )
        repo_errado_corpo = (
            b'{"ref":"refs/heads/master","repository":{"full_name":"outro/repo"}}'
        )
        repo_errado = self.client.post(
            "/deploy/php",
            data=repo_errado_corpo,
            headers={
                **headers,
                "X-Hub-Signature-256": self.assinatura(repo_errado_corpo),
            },
        )
        branch_errada_corpo = (
            b'{"ref":"refs/heads/main","repository":'
            b'{"full_name":"rodrigoraa/EstacaoEmPython"}}'
        )
        branch_errada = self.client.post(
            "/deploy/php",
            data=branch_errada_corpo,
            headers={
                **headers,
                "X-Hub-Signature-256": self.assinatura(branch_errada_corpo),
            },
        )
        evento_errado = self.client.post(
            "/deploy/php",
            data=corpo,
            headers={**headers, "X-GitHub-Event": "ping"},
        )
        self.assertEqual(primeira.status_code, 200)
        self.assertEqual(segunda.status_code, 409)
        self.assertEqual(invalida.status_code, 403)
        self.assertIn(b"repo ignorado", repo_errado.data)
        self.assertIn(b"branch ignorada", branch_errada.data)
        self.assertIn(b"evento ignorado", evento_errado.data)


if __name__ == "__main__":
    unittest.main()
