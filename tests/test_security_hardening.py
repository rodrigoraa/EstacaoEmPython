import hashlib
import importlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))
from security_helpers import autenticar_admin


class SecurityHardeningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ambiente = patch.dict(os.environ, {
            "APP_ENV": "development", "ESTACAO_DB": str(Path(self.tmp.name) / "test.db"),
            "SECRET_KEY": "test-key-0123456789-abcdefghijklmnopqrstuvwxyz",
            "ADMIN_PASSWORD": "test-password", "ADMIN_PASSWORD_HASH": "",
            "RATELIMIT_ENABLED": "false", "TRUST_PROXY": "false",
            "SESSION_COOKIE_SECURE": "false", "HSTS_ENABLED": "false",
        }, clear=True)
        self.ambiente.start()
        import database
        import app
        self.database = importlib.reload(database)
        self.database.init_db()
        self.app = app.create_app({"TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        self.ambiente.stop()
        self.tmp.cleanup()

    def assert_private_denied(self, client=None):
        response = (client or self.client).get("/admin/api/alert-controls")
        self.assertEqual(response.status_code, 401)
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_session_storage_contains_hash_only_and_logout_revokes_captured_cookie(self):
        token = autenticar_admin(self.client)
        cookie = self.client.get_cookie("session").value
        with self.database.get_db() as conn:
            stored = conn.execute("SELECT * FROM admin_sessoes").fetchone()
        conn.close()
        self.assertEqual(stored["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, repr(dict(stored)))
        self.assertEqual(self.client.get("/admin/api/alert-controls").status_code, 200)
        self.assertEqual(self.client.post("/admin/logout", data={"csrf_token": "csrf-teste"}).status_code, 302)
        replay = self.app.test_client()
        replay.set_cookie("session", cookie)
        self.assert_private_denied(replay)

    def test_password_change_invalidates_existing_session(self):
        autenticar_admin(self.client)
        os.environ["ADMIN_PASSWORD"] = "changed-password"
        self.assert_private_denied()

    def test_idle_and_absolute_expiry_are_server_side_even_with_valid_cookie(self):
        for column in ("ultimo_acesso", "expira_em"):
            with self.subTest(column=column):
                autenticar_admin(self.client)
                with self.database.get_db() as conn:
                    # Both identifiers are test constants, never request input.
                    conn.execute(f"UPDATE admin_sessoes SET {column} = 0")
                conn.close()
                self.assert_private_denied()

    def test_boolean_legacy_session_and_unknown_server_token_do_not_authorize(self):
        for values in ({"logado": True, "ultimo_acesso": 9999999999},
                       {"admin_session": "a" * 64}, {"admin_session": []}):
            with self.subTest(values=values):
                with self.client.session_transaction() as session:
                    session.clear()
                    session.update(values)
                self.assert_private_denied()

    def test_missing_secret_uses_random_key_and_old_public_fallback_cannot_forge_cookie(self):
        import app
        os.environ.pop("SECRET_KEY")
        first = app.create_app({"TESTING": True})
        second = app.create_app({"TESTING": True})
        self.assertNotEqual(first.secret_key, second.secret_key)
        forged_app = Flask("forgery")
        forged_app.secret_key = "dev-only-change-me"
        forged = forged_app.session_interface.get_signing_serializer(forged_app).dumps(
            {"logado": True, "ultimo_acesso": 9999999999})
        client = first.test_client()
        client.set_cookie("session", forged)
        self.assert_private_denied(client)

    def test_body_upload_and_cross_site_form_are_blocked_before_side_effect(self):
        oversized = self.client.post("/", data=b"x" * (1024 * 1024 + 1),
                                     content_type="application/x-www-form-urlencoded")
        self.assertEqual(oversized.status_code, 413)
        upload = self.client.post("/", data={"upload": (io.BytesIO(b"payload"), "file.png")})
        self.assertEqual(upload.status_code, 415)
        for path in ("/", "/unsubscribe/request", "/admin"):
            with self.subTest(path=path):
                response = self.client.post(path, data={"nome": "User", "telefone": "67988887777"})
                self.assertEqual(response.status_code, 403)

    def test_headers_nonce_private_cache_and_cors(self):
        first = self.client.get("/admin", headers={"Origin": "https://untrusted.test"})
        second = self.client.get("/admin")
        csp = first.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self' 'nonce-", csp)
        self.assertNotIn("'unsafe-inline'", csp.split("script-src", 1)[1].split(";", 1)[0])
        self.assertNotEqual(csp, second.headers["Content-Security-Policy"])
        self.assertIn("no-store", first.headers["Cache-Control"])
        self.assertNotIn("Access-Control-Allow-Origin", first.headers)
        self.assertIn("HttpOnly", first.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", first.headers["Set-Cookie"])

    def test_login_rate_limit_cannot_be_reset_by_untrusted_forwarded_headers(self):
        import app
        import routes.admin
        # Older isolated blueprint tests reload their Limiter. Use its current instance.
        importlib.reload(routes.admin)
        app_module = importlib.reload(app)
        os.environ["TRUST_PROXY"] = "true"
        protected = app_module.create_app({"TESTING": True, "RATELIMIT_ENABLED": True,
                                          "RATELIMIT_STORAGE_URI": "memory://"})
        client = protected.test_client()
        login = client.get("/admin")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', login.data).group(1).decode()
        statuses = []
        for index in range(6):
            response = client.post("/admin", data={"senha": "wrong", "csrf_token": token},
                headers={"X-Forwarded-For": f"203.0.113.{index + 1}"},
                environ_overrides={"REMOTE_ADDR": "198.51.100.25"})
            statuses.append(response.status_code)
        self.assertEqual(statuses, [200] * 5 + [429])

    def test_evolution_errors_hide_url_and_api_key_and_redirects_are_blocked(self):
        from routes.admin import obter_status_evolution
        os.environ.update(EVOLUTION_URL="http://127.0.0.1:8081", EVOLUTION_INSTANCE="test",
                          EVOLUTION_API_KEY="private-api-key")
        with patch("routes.admin.requests.get", side_effect=requests.ConnectionError(
                "private-api-key http://internal.example/secret")):
            status = obter_status_evolution()
            self.assertNotIn("private-api-key", repr(status))
            self.assertNotIn("internal.example", repr(status))
        response = Mock(status_code=302)
        with patch("routes.admin.requests.get", return_value=response) as get:
            self.assertFalse(obter_status_evolution()["ok"])
            self.assertFalse(get.call_args.kwargs["allow_redirects"])
            response.json.assert_not_called()


class TrustedProxyTest(unittest.TestCase):
    def test_untrusted_peer_cannot_spoof_client_ip_or_scheme(self):
        from trusted_proxy import TrustedProxyFix
        captured = []

        def wsgi(environ, start_response):
            captured.append((environ["REMOTE_ADDR"], environ["wsgi.url_scheme"]))
            start_response("200 OK", [])
            return [b"ok"]

        wrapped = TrustedProxyFix(wsgi, ["127.0.0.1", "::1"])
        for peer in ("198.51.100.25", "127.0.0.1"):
            wrapped({"REMOTE_ADDR": peer, "HTTP_X_FORWARDED_FOR": "203.0.113.5",
                     "HTTP_X_FORWARDED_PROTO": "https", "wsgi.url_scheme": "http"}, lambda *args: None)
        self.assertEqual(captured, [("198.51.100.25", "http"), ("203.0.113.5", "https")])


if __name__ == "__main__":
    unittest.main()
