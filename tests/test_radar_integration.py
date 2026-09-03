import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ESTACAO = ROOT / "estacao"
sys.path.insert(0, str(ESTACAO))

from services.radar_analysis import RadarCluster, haversine_km  # noqa: E402
from services.radar_service import RadarFetchResult, RadarFrame, normalizar_resposta  # noqa: E402


class RadarIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self.tmp.name)
        os.environ.update(
            {
                "ESTACAO_DB": str(self.raiz / "teste.db"),
                "RADAR_DATA_DIR": str(self.raiz / "radar"),
                "RADAR_ENABLED": "false",
                "RADAR_ALERTS_ENABLED": "false",
                "RATELIMIT_ENABLED": "false",
                "SECRET_KEY": "teste",
            }
        )
        import database
        import app

        self.database = importlib.reload(database)
        self.database.init_db()
        self.app_module = importlib.reload(app)
        self.app = self.app_module.app
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()
        for chave in (
            "ESTACAO_DB", "RADAR_DATA_DIR", "RADAR_ENABLED", "RADAR_ALERTS_ENABLED",
            "RADAR_RETENCAO_AUTOMATICA", "RADAR_RETENCAO_IMAGENS_DIAS",
            "RADAR_RETENCAO_FRAMES_DIAS", "REDEMET_API_KEY", "RATELIMIT_ENABLED",
            "SECRET_KEY",
        ):
            os.environ.pop(chave, None)

    def frame(self, horario="2026-09-02 18:00:00", sufixo="a"):
        return RadarFrame(
            "jr", "maxcappi", datetime.fromisoformat(horario),
            f"https://estatico.example/{sufixo}.png",
            -20.27855, -54.47396, -23.830664, -16.642761,
            -58.226281, -50.543479, 400, 1000,
        )

    def cluster(self, numero=1, lat=-22.0, lon=-54.46, clutter=False):
        distancia = haversine_km(lat, lon, -22.4925326, -54.4610352)
        return RadarCluster(
            numero, 200, 300, 400, lat, lon, 290, 390, 20, 20,
            distancia, max(0, distancia - 10),
            haversine_km(lat, lon, -20.27855, -54.47396), "N", clutter, "VERDE",
        )

    def test_migration_radar_idempotente_e_schema_atual(self):
        self.database.init_db()
        conn = self.database.get_db()
        try:
            versao = conn.execute("SELECT versao FROM schema_version WHERE id=1").fetchone()[0]
            tabelas = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertEqual(versao, self.database.SCHEMA_VERSION)
        self.assertTrue({"radar_frames", "radar_clusters", "radar_tracks", "radar_track_points"} <= tabelas)

    def test_deduplicacao_sqlite_por_path(self):
        from services.radar_repository import salvar_resultado_frame

        primeiro = salvar_resultado_frame(self.frame(), None, None, 750, 750, [self.cluster()])
        segundo = salvar_resultado_frame(self.frame(), None, None, 750, 750, [self.cluster()])
        self.assertEqual(primeiro[0], segundo[0])
        self.assertFalse(segundo[1])
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM radar_frames").fetchone()[0], 1)
        conn.close()

    def test_pagina_e_api_sem_dados(self):
        pagina = self.client.get("/radar")
        api = self.client.get("/api/radar/status")
        self.assertEqual(pagina.status_code, 200)
        self.assertIn("Radar temporariamente indisponível".encode(), pagina.data)
        self.assertFalse(api.get_json()["disponivel"])

    def test_pagina_api_e_imagem_com_dados_sem_expor_paths(self):
        from services.radar_repository import salvar_resultado_frame

        pasta = self.raiz / "radar" / "analisadas"
        pasta.mkdir(parents=True)
        Image.new("RGB", (20, 20), "black").save(pasta / "frame.png")
        frame_id, _ = salvar_resultado_frame(
            self.frame(), None, "analisadas/frame.png", 20, 20, [self.cluster()]
        )
        pagina = self.client.get("/radar")
        payload = self.client.get("/api/radar/status").get_json()
        imagem = self.client.get(f"/radar/imagem/{frame_id}")
        self.assertEqual(pagina.status_code, 200)
        self.assertIn(b"Eco significativo", pagina.data)
        self.assertTrue(payload["disponivel"])
        self.assertNotIn("arquivo_local", payload["frame"])
        self.assertNotIn("path_remoto", json.dumps(payload))
        self.assertEqual(imagem.status_code, 200)
        self.assertEqual(imagem.mimetype, "image/png")
        imagem.close()

    def test_path_traversal_registrado_e_bloqueado(self):
        from services.radar_repository import salvar_resultado_frame

        fora = self.raiz / "fora.png"
        Image.new("RGB", (10, 10), "black").save(fora)
        frame_id, _ = salvar_resultado_frame(self.frame(), None, "../fora.png", 10, 10, [])
        self.assertEqual(self.client.get(f"/radar/imagem/{frame_id}").status_code, 404)

    def test_ausencia_api_key_nao_impede_flask(self):
        os.environ.pop("REDEMET_API_KEY", None)
        aplicacao = self.app_module.create_app({"TESTING": True, "RATELIMIT_ENABLED": False})
        self.assertEqual(aplicacao.test_client().get("/radar").status_code, 200)

    def test_stale(self):
        from services.radar_repository import obter_estado_radar, salvar_resultado_frame

        salvar_resultado_frame(self.frame("2000-01-01 00:00:00", "antigo"), None, None, 20, 20, [])
        self.assertTrue(obter_estado_radar(45)["stale"])

    def test_tracking_persistido_com_timestamps_irregulares(self):
        from services.radar_repository import atualizar_tracking, obter_estado_radar, salvar_resultado_frame

        ids = []
        for horario, lat, sufixo in (
            ("2026-09-02 18:00:00", -21.70, "t1"),
            ("2026-09-02 18:20:00", -21.90, "t2"),
            ("2026-09-02 18:30:00", -22.10, "t3"),
        ):
            frame_id, _ = salvar_resultado_frame(self.frame(horario, sufixo), None, None, 20, 20, [self.cluster(lat=lat)])
            ids.append(frame_id)
            atualizar_tracking(frame_id, -22.4925326, -54.4610352, 3, 10, 150, 25)
        track = obter_estado_radar(10**9)["tracking"]
        self.assertEqual(track["quantidade_frames"], 3)
        self.assertTrue(track["aproximando"])
        self.assertIsNotNone(track["velocidade_kmh"])

    def test_alertas_desabilitados_nao_enfileiram(self):
        from config import radar_config
        from workers.radar_updater import enfileirar_alertas_radar

        self.assertEqual(enfileirar_alertas_radar(radar_config(), {}), 0)
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
        conn.close()

    def test_worker_com_mock_sem_internet(self):
        from config import radar_config
        from workers.radar_updater import executar_ciclo

        payload = json.loads((ROOT / "tests" / "fixtures" / "redemet_radar.json").read_text(encoding="utf-8"))
        fetch = normalizar_resposta(payload)
        png = BytesIO()
        imagem = Image.new("RGB", (100, 100), "black")
        ImageDraw.Draw(imagem).rectangle((40, 40, 55, 55), fill=(0, 180, 0))
        imagem.save(png, "PNG")

        class Client:
            def obter_frames(self):
                return fetch

            def baixar_imagem(self, _frame):
                return png.getvalue()

        os.environ.update({"RADAR_ENABLED": "true", "REDEMET_API_KEY": "somente-teste"})
        resultado = executar_ciclo(client=Client(), config=radar_config())
        self.assertEqual(resultado["recebidos"], 4)
        self.assertEqual(resultado["unicos"], 3)
        self.assertEqual(resultado["novos"], 3)
        self.assertEqual(resultado["falhas"], 0)

    def test_retencao_radar_exige_opt_in_e_nao_apaga_fora_da_raiz(self):
        from services.radar_repository import salvar_resultado_frame
        from workers.maintenance import executar_cleanup_radar

        fora = self.raiz / "nao-apagar.png"
        Image.new("RGB", (10, 10), "black").save(fora)
        salvar_resultado_frame(self.frame("2000-01-01 00:00:00", "retencao"), None, "../nao-apagar.png", 10, 10, [])
        conn = self.database.get_db()
        with self.assertRaises(RuntimeError):
            executar_cleanup_radar(conn)
        os.environ["RADAR_RETENCAO_AUTOMATICA"] = "true"
        resultado = executar_cleanup_radar(conn)
        conn.close()
        self.assertEqual(resultado["frames"], 1)
        self.assertTrue(fora.exists())

    def test_health_radar_desabilitado_nao_degrada(self):
        resposta = self.client.get("/health")
        self.assertEqual(resposta.get_json()["radar"], "disabled")

    def test_entrypoint_direto_expoe_once(self):
        resultado = subprocess.run(
            [sys.executable, "workers/radar_updater.py", "--help"],
            cwd=ESTACAO, capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        self.assertIn("--once", resultado.stdout)


if __name__ == "__main__":
    unittest.main()
