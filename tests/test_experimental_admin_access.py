import importlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ESTACAO = ROOT / "estacao"
sys.path.insert(0, str(ESTACAO))


class ExperimentalAdminAccessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update({
            "ESTACAO_DB": str(Path(self.tmp.name) / "teste.db"),
            "RADAR_DATA_DIR": str(Path(self.tmp.name) / "radar"),
            "RADAR_ENABLED": "false",
            "REGIONAL_STATIONS_ENABLED": "false",
            "NOWCASTING_ENABLED": "false",
            "RATELIMIT_ENABLED": "false",
            "ADMIN_PASSWORD": "senha-teste",
            "SECRET_KEY": "teste",
        })
        import database
        import app

        self.database = importlib.reload(database)
        self.database.init_db()
        self.app_module = importlib.reload(app)
        self.app = self.app_module.app
        self.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()
        for key in (
            "ESTACAO_DB", "RADAR_DATA_DIR", "RADAR_ENABLED",
            "REGIONAL_STATIONS_ENABLED", "NOWCASTING_ENABLED",
            "RATELIMIT_ENABLED", "ADMIN_PASSWORD", "SECRET_KEY",
        ):
            os.environ.pop(key, None)

    def autenticar(self):
        with self.client.session_transaction() as session:
            session["logado"] = True
            session["ultimo_acesso"] = time.time()
            session["csrf_token"] = "csrf-teste"

    def test_publico_preserva_paginas_e_menu_da_master(self):
        for path in ("/", "/historico", "/previsao", "/sobre"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b'href="/"', response.data)
                self.assertIn(b'href="/historico"', response.data)
                self.assertIn(b'href="/previsao"', response.data)
                self.assertIn(b'href="/sobre"', response.data)
                self.assertNotIn(b'href="/radar"', response.data)
                self.assertNotIn(b'href="/monitoramento"', response.data)
                self.assertNotIn(b'href="/estacoes-regionais"', response.data)
                for experimental_text in (
                    "Níveis de proximidade", "ALERTA PREVENTIVO",
                    "Alerta de proximidade", "Ameaça principal do nowcasting",
                    "Envio preventivo",
                    "Alertas de teste ao administrador",
                ):
                    self.assertNotIn(experimental_text.encode(), response.data)
        home = self.client.get("/").data
        for label in ("Dashboard", "Histórico", "Previsão do tempo", "Sobre a estação"):
            self.assertIn(label.encode(), home)

    def test_paginas_experimentais_sem_login_redirecionam_sem_dados(self):
        for path in (
            "/radar", "/monitoramento", "/estacoes-regionais",
            "/admin/radar", "/admin/monitoramento", "/admin/estacoes-regionais",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/admin"))
                self.assertNotIn(b"Radar Meteorol", response.data)
                self.assertNotIn(b"PIN-MS", response.data)

    def test_apis_e_imagens_sem_login_retornam_401_sem_dados(self):
        paths = (
            "/admin/api/radar/status", "/admin/api/nowcasting/status",
            "/admin/api/regional-stations", "/api/radar/status",
            "/api/nowcasting/status", "/api/regional-stations",
            "/admin/radar/imagem/1", "/admin/radar/imagem/atual",
            "/radar/imagem/1", "/radar/imagem/atual",
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.get_json(), {"error": "admin_authentication_required"}
                )

    def test_admin_autenticado_acessa_telas_apis_e_abas(self):
        self.autenticar()
        for path in (
            "/admin/radar", "/admin/monitoramento", "/admin/estacoes-regionais",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Painel Administrativo", response.data)
                self.assertIn(b"Sair", response.data)
        for path in (
            "/admin/api/radar/status", "/admin/api/nowcasting/status",
            "/admin/api/regional-stations",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

        panel = self.client.get("/admin")
        self.assertEqual(panel.status_code, 200)
        self.assertIn(b'href="/admin/radar"', panel.data)
        self.assertIn(b'href="/admin/estacoes-regionais"', panel.data)
        self.assertIn(b'href="/admin/monitoramento"', panel.data)
        self.assertEqual(self.client.get("/admin/radar/imagem/1").status_code, 404)

    def test_aliases_antigos_autenticados_redirecionam_para_admin(self):
        self.autenticar()
        aliases = {
            "/radar": "/admin/radar",
            "/monitoramento": "/admin/monitoramento",
            "/estacoes-regionais": "/admin/estacoes-regionais",
            "/api/radar/status": "/admin/api/radar/status",
            "/api/nowcasting/status": "/admin/api/nowcasting/status",
            "/api/regional-stations": "/admin/api/regional-stations",
            "/radar/imagem/1": "/admin/radar/imagem/1",
            "/radar/imagem/atual": "/admin/radar/imagem/atual",
        }
        for path, destination in aliases.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertIn(response.status_code, {302, 308})
                self.assertTrue(response.headers["Location"].endswith(destination))


if __name__ == "__main__":
    unittest.main()
