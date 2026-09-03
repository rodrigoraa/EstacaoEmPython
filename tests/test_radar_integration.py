import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
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

    def cluster(self, numero=1, lat=-22.0, lon=-54.46, clutter=False, pixels=200):
        distancia = haversine_km(lat, lon, -22.4925326, -54.4610352)
        return RadarCluster(
            numero, pixels, 300, 400, lat, lon, 290, 390, 20, 20,
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

    def test_frame_utc_recente_nao_fica_stale_por_confusao_de_fuso(self):
        from services.radar_repository import obter_estado_radar, salvar_resultado_frame

        now = datetime.now(timezone.utc).replace(microsecond=0)
        salvar_resultado_frame(
            self.frame(now.strftime("%Y-%m-%d %H:%M:%S"), "recente-utc"),
            None, None, 20, 20, [],
        )
        state = obter_estado_radar(45)
        self.assertFalse(state["stale"])
        self.assertTrue(state["frame"]["data_frame_utc"].endswith("+00:00"))

    def test_novo_frame_persiste_utc_local_sem_reescrever_legado(self):
        from services.radar_repository import salvar_resultado_frame

        frame_id, _ = salvar_resultado_frame(
            self.frame("2026-09-03 00:30:00", "timezone"), None, None, 20, 20, []
        )
        conn = self.database.get_db()
        row = conn.execute("SELECT * FROM radar_frames WHERE id=?", (frame_id,)).fetchone()
        conn.execute(
            """
            INSERT INTO radar_frames (
                radar_codigo, produto, data_frame, path_remoto, lat_center,
                lon_center, lat_min, lat_max, lon_min, lon_max
            ) VALUES ('jr','maxcappi','2020-01-01 00:00:00','https://example/legado.png',
                      0,0,-1,1,-1,1)
            """
        )
        legado = conn.execute(
            "SELECT data_frame, data_frame_utc, timestamp_status FROM radar_frames WHERE path_remoto LIKE '%legado.png'"
        ).fetchone()
        conn.commit()
        conn.close()
        self.assertEqual(row["data_frame_utc"], "2026-09-03T00:30:00+00:00")
        self.assertEqual(row["data_frame_local"], "2026-09-02T20:30:00-04:00")
        self.assertEqual(row["data_frame_raw"], "2026-09-03 00:30:00")
        self.assertEqual(row["timestamp_status"], "utc_assumed")
        self.assertEqual(legado["data_frame"], "2020-01-01 00:00:00")
        self.assertIsNone(legado["data_frame_utc"])
        self.assertEqual(legado["timestamp_status"], "legacy_unverified")

    def test_frame_suspect_e_persistido_mas_nao_produz_tracking_ou_eta(self):
        from services.radar_repository import atualizar_tracking, salvar_resultado_frame

        frame = RadarFrame(
            "jr", "maxcappi", datetime(2026, 9, 3, 18, tzinfo=timezone.utc),
            "https://estatico.example/suspect.png", -20.27855, -54.47396,
            -23.830664, -16.642761, -58.226281, -50.543479, 400, 1000,
            "2026-09-03 18:00:00", "suspect",
        )
        frame_id, _ = salvar_resultado_frame(
            frame, None, None, 20, 20, [self.cluster()]
        )
        self.assertEqual(
            atualizar_tracking(frame_id, -22.4925326, -54.4610352, 3, 10, 150, 25),
            [],
        )
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM radar_track_points").fetchone()[0], 0)
        row = conn.execute("SELECT timestamp_status FROM radar_frames WHERE id=?", (frame_id,)).fetchone()
        conn.close()
        self.assertEqual(row["timestamp_status"], "suspect")

    def test_worker_finaliza_frame_suspect_sem_criar_track(self):
        from config import radar_config
        from workers.radar_updater import processar_frame

        coletado = datetime(2026, 9, 3, 14, tzinfo=timezone.utc)
        frame = self.frame("2026-09-03 18:00:00", "worker-suspect")
        png = BytesIO()
        imagem = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        ImageDraw.Draw(imagem).rectangle((40, 40, 55, 55), fill=(43, 185, 0, 255))
        imagem.save(png, "PNG")

        class Client:
            def baixar_imagem(self, _frame):
                return png.getvalue()

        frame_id, _clusters, _alterado = processar_frame(
            Client(), frame, radar_config(), coletado
        )
        conn = self.database.get_db()
        row = conn.execute(
            "SELECT timestamp_status, status_processamento FROM radar_frames WHERE id=?",
            (frame_id,),
        ).fetchone()
        points = conn.execute("SELECT COUNT(*) FROM radar_track_points").fetchone()[0]
        conn.close()
        self.assertEqual(row["timestamp_status"], "suspect")
        self.assertEqual(row["status_processamento"], "processado")
        self.assertEqual(points, 0)

    def test_tracking_atravessa_meia_noite_utc(self):
        from services.radar_repository import atualizar_tracking, obter_estado_radar, salvar_resultado_frame

        for horario, lat, sufixo in (
            ("2026-09-02 23:50:00", -21.9, "m1"),
            ("2026-09-03 00:00:00", -22.0, "m2"),
            ("2026-09-03 00:10:00", -22.1, "m3"),
        ):
            frame_id, _ = salvar_resultado_frame(
                self.frame(horario, sufixo), None, None, 20, 20, [self.cluster(lat=lat)]
            )
            atualizar_tracking(frame_id, -22.4925326, -54.4610352, 3, 10, 150, 25)
        track = obter_estado_radar(10**9)["tracking"]
        self.assertEqual(track["quantidade_frames"], 3)
        self.assertGreater(track["velocidade_kmh"], 0)

    def test_multitrack_cruzamento_preserva_ids_por_posicao_prevista(self):
        from services.radar_repository import atualizar_tracking, salvar_resultado_frame

        frames = (
            ("2026-09-03 00:00:00", -54.70, -54.20, 180, 260, "cross1"),
            ("2026-09-03 00:10:00", -54.55, -54.35, 220, 220, "cross2"),
            ("2026-09-03 00:30:00", -54.40, -54.50, 280, 170, "cross3"),
        )
        initial = None
        final = None
        for horario, lon_a, lon_b, size_a, size_b, suffix in frames:
            frame_id, _ = salvar_resultado_frame(
                self.frame(horario, suffix), None, None, 20, 20,
                [
                    self.cluster(1, -22.0, lon_a, pixels=size_a),
                    self.cluster(2, -22.0, lon_b, pixels=size_b),
                ],
            )
            atualizar_tracking(frame_id, -22.4925326, -54.4610352, 3, 10, 150, 25)
            conn = self.database.get_db()
            mapping = {
                row["cluster_numero"]: row["track_id"]
                for row in conn.execute(
                    """
                    SELECT c.cluster_numero, p.track_id
                    FROM radar_clusters c JOIN radar_track_points p ON p.cluster_id=c.id
                    WHERE c.frame_id=?
                    """,
                    (frame_id,),
                )
            }
            conn.close()
            initial = initial or mapping
            final = mapping
        self.assertEqual(final[1], initial[1])
        self.assertEqual(final[2], initial[2])

    def test_track_ausente_permanece_temporario_e_nova_celula_nasce(self):
        from services.radar_repository import atualizar_tracking, salvar_resultado_frame

        first_id, _ = salvar_resultado_frame(
            self.frame("2026-09-03 01:00:00", "life1"), None, None, 20, 20,
            [self.cluster(1, -22.0, -54.7), self.cluster(2, -22.0, -54.2)],
        )
        atualizar_tracking(first_id, -22.4925326, -54.4610352, 3, 10, 150, 25)
        conn = self.database.get_db()
        old_tracks = {row[0] for row in conn.execute("SELECT id FROM radar_tracks")}
        conn.close()

        second_id, _ = salvar_resultado_frame(
            self.frame("2026-09-03 01:20:00", "life2"), None, None, 20, 20,
            [self.cluster(1, -22.1, -54.7, pixels=260), self.cluster(2, -23.0, -56.0)],
        )
        atualizar_tracking(second_id, -22.4925326, -54.4610352, 3, 10, 150, 25)
        conn = self.database.get_db()
        active = {row[0] for row in conn.execute("SELECT id FROM radar_tracks WHERE ativo=1")}
        total = conn.execute("SELECT COUNT(*) FROM radar_tracks").fetchone()[0]
        conn.close()
        self.assertTrue(old_tracks <= active)
        self.assertEqual(total, 3)

    def test_indice_clutter_exige_historico_e_nao_exclui_cluster(self):
        from services.radar_repository import salvar_resultado_frame

        last_id = None
        for index in range(13):
            minute = index * 10
            horario = f"2026-09-03 {2 + minute // 60:02d}:{minute % 60:02d}:00"
            last_id, _ = salvar_resultado_frame(
                self.frame(horario, f"clutter-{index}"), None, None, 20, 20,
                [self.cluster(1, -22.0, -54.46, clutter=True)],
            )
        conn = self.database.get_db()
        row = conn.execute(
            "SELECT indice_persistencia_clutter, clutter_amostras FROM radar_clusters WHERE frame_id=?",
            (last_id,),
        ).fetchone()
        count = conn.execute(
            "SELECT COUNT(*) FROM radar_clusters WHERE frame_id=?", (last_id,)
        ).fetchone()[0]
        conn.close()
        self.assertGreaterEqual(row["clutter_amostras"], 12)
        self.assertGreaterEqual(row["indice_persistencia_clutter"], 0.75)
        self.assertEqual(count, 1)

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
        ImageDraw.Draw(imagem).rectangle((40, 40, 55, 55), fill=(43, 185, 0))
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
        self.assertIn("--diagnose-palette", resultado.stdout)
        self.assertIn("--diagnose-time", resultado.stdout)


if __name__ == "__main__":
    unittest.main()
