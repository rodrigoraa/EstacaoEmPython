import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ESTACAO = ROOT / "estacao"
sys.path.insert(0, str(ESTACAO))


class NowcastingIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update({
            "ESTACAO_DB": str(Path(self.tmp.name) / "teste.db"),
            "NOWCASTING_ENABLED": "false",
            "NOWCASTING_ALERTS_ENABLED": "false",
            "RATELIMIT_ENABLED": "false",
            "SECRET_KEY": "teste",
        })
        import database
        import app

        self.database = importlib.reload(database)
        self.database.init_db()
        self.app_module = importlib.reload(app)
        self.client = self.app_module.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()
        for key in (
            "ESTACAO_DB", "NOWCASTING_ENABLED", "NOWCASTING_ALERTS_ENABLED",
            "RATELIMIT_ENABLED", "SECRET_KEY",
        ):
            os.environ.pop(key, None)

    def state(self):
        return {
            "status": "NORMAL", "nivel_evidencia": "SEM_EVIDENCIA",
            "indice_evidencia": 0,
            "radar": {
                "disponivel": False, "stale": None, "frame_id": None,
                "track_id": None, "distancia_borda_km": None,
                "faixa_distancia": None, "direcao": None,
                "velocidade_kmh": None, "aproximando": None,
                "trajetoria_compativel": False, "eta_minutos": None,
                "quantidade_frames": None, "suspeito_clutter": False,
                "indice_persistencia_clutter": None, "imagem_disponivel": False,
            },
            "estacoes_relevantes": [], "escola": None,
            "ameaca_principal": None, "ameacas": [],
            "confirmacao_regional": {
                "confirmada": False, "stations": [], "evidence_count": 0,
            },
            "radar_only": False, "evento_local_observado": False,
            "evidencias": ["Sem evidencia observacional relevante no momento"],
            "gerado_em": "2026-09-03T09:20:00-04:00",
            "gerado_em_utc": "2026-09-03T13:20:00+00:00",
            "versao_algoritmo": "1.1",
        }

    def test_migration_aditiva_idempotente_cria_snapshot_schema_6(self):
        self.database.init_db()
        conn = self.database.get_db()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("SELECT versao FROM schema_version WHERE id=1").fetchone()[0]
        conn.close()
        self.assertIn("nowcasting_snapshots", tables)
        self.assertEqual(version, 6)
        conn = self.database.get_db()
        frame_columns = {row[1] for row in conn.execute("PRAGMA table_info(radar_frames)")}
        cluster_columns = {row[1] for row in conn.execute("PRAGMA table_info(radar_clusters)")}
        conn.close()
        self.assertTrue({"data_frame_raw", "coletado_em_utc"} <= frame_columns)
        self.assertTrue({"classe_predominante", "classe_maxima"} <= cluster_columns)

    def test_migration_5_para_6_preserva_snapshot_existente(self):
        from services.nowcasting_repository import salvar_snapshot

        self.assertIsNotNone(salvar_snapshot(self.state(), "snapshot-schema-5"))
        conn = self.database.get_db()
        conn.execute("UPDATE schema_version SET versao=5 WHERE id=1")
        conn.commit()
        conn.close()
        self.database.init_db()
        conn = self.database.get_db()
        count = conn.execute(
            "SELECT COUNT(*) FROM nowcasting_snapshots WHERE input_fingerprint=?",
            ("snapshot-schema-5",),
        ).fetchone()[0]
        version = conn.execute("SELECT versao FROM schema_version WHERE id=1").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(version, 6)

    def test_pagina_e_api_antes_do_primeiro_snapshot(self):
        page = self.client.get("/monitoramento")
        api = self.client.get("/api/nowcasting/status")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Aguardando a primeira análise".encode(), page.data)
        self.assertEqual(api.get_json()["status"], "SEM_DADOS")

    def test_snapshot_idempotente_e_api_sem_payload_bruto(self):
        from services.nowcasting_repository import salvar_snapshot

        state = self.state()
        state["estacoes_relevantes"] = [{
            "code": "A749", "name": "Juti", "distance_km": 80,
            "upstream": True, "status": "OK", "age_minutes": 60,
            "along_km": -40, "cross_track_km": 12,
            "evidencias": ["Chuva observada em Juti na ultima hora"],
        }]
        state["escola"] = {
            "temperature": 25, "humidity": 60, "pressure": 965,
            "wind_speed": 5, "wind_gust": 9, "rain_rate": 0,
        }
        state["radar"].update({"distancia_borda_km": 60})
        state["ameaca_principal"] = {"track_id": 12}
        state["ameacas"] = [
            {
                "track_id": 12, "status": "TRAJETORIA_RELEVANTE",
                "distance_km": 60, "direction": "N", "eta_minutes": 75,
                "confirmacao_regional": {"confirmada": True},
            },
            {
                "track_id": 18, "status": "SISTEMA_EM_MOVIMENTO",
                "distance_km": 90, "direction": "L", "eta_minutes": None,
                "confirmacao_regional": {"confirmada": False},
            },
        ]
        state["confirmacao_regional"] = {
            "confirmada": True, "stations": ["A749"], "evidence_count": 2,
        }
        self.assertIsNotNone(salvar_snapshot(state, "entrada-1"))
        self.assertIsNone(salvar_snapshot(state, "entrada-1"))
        conn = self.database.get_db()
        count = conn.execute("SELECT COUNT(*) FROM nowcasting_snapshots").fetchone()[0]
        conn.close()
        payload = self.client.get("/api/nowcasting/status").get_json()
        page = self.client.get("/monitoramento")
        self.assertEqual(count, 1)
        self.assertEqual(page.status_code, 200)
        self.assertIn("AMEAÇA PRINCIPAL".encode(), page.data)
        self.assertIn(b"Outros sistemas em monitoramento", page.data)
        self.assertEqual(payload["versao_algoritmo"], "1.1")
        self.assertIn("ameaca_principal", payload)
        self.assertIn("ameacas", payload)
        self.assertIn("confirmacao_regional", payload)
        self.assertEqual(len(payload["ameacas"]), 2)
        self.assertNotIn("estado_json", json.dumps(payload))
        self.assertNotIn("input_fingerprint", json.dumps(payload))

    def test_worker_mockado_nao_chama_internet_nem_alertas(self):
        from config import nowcasting_config
        from workers import nowcasting_updater

        os.environ["NOWCASTING_ENABLED"] = "true"
        entradas = ({}, {"stations": []}, None, "entrada-worker")
        with mock.patch.object(nowcasting_updater, "carregar_entradas_nowcasting", return_value=entradas):
            result = nowcasting_updater.executar_ciclo(nowcasting_config())
        self.assertTrue(result["new"])
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
        conn.close()

    def test_worker_real_le_somente_sqlite_persistido(self):
        from config import nowcasting_config
        from workers.nowcasting_updater import executar_ciclo

        now = datetime.now(timezone.utc).replace(microsecond=0)
        conn = self.database.get_db()
        conn.execute(
            """
            INSERT INTO historico_clima (
                temp, umidade, pressao, vento_vel, vento_rajada, vento_dir,
                chuva_rate, chuva_hoje, station_data_hora_utc,
                station_data_hora_local, data_hora
            ) VALUES (25, 60, 965, 5, 9, 90, 0, 0, ?, ?, ?)
            """,
            (now.isoformat(), now.astimezone().isoformat(), now.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()
        os.environ["NOWCASTING_ENABLED"] = "true"
        result = executar_ciclo(nowcasting_config())
        self.assertTrue(result["new"])
        self.assertEqual(result["snapshot"]["escola"]["temperature"], 25)

    def test_repository_carrega_todos_os_tracks_do_frame_atual(self):
        from config import nowcasting_config
        from services.nowcasting_repository import carregar_entradas_nowcasting
        from services.radar_analysis import RadarCluster
        from services.radar_repository import atualizar_tracking, salvar_resultado_frame
        from services.radar_service import RadarFrame

        now = datetime.now(timezone.utc).replace(microsecond=0)
        frame = RadarFrame(
            "jr", "maxcappi", now, "https://example.test/multi.png",
            -20.27855, -54.47396, -23.830664, -16.642761,
            -58.226281, -50.543479, 400, 1000,
        )
        clusters = [
            RadarCluster(1, 200, 300, 400, -22.0, -54.5, 290, 390, 20, 20,
                         60, 50, 190, "N", False, "REFLETIVIDADE_MEDIA"),
            RadarCluster(2, 180, 350, 420, -21.8, -54.8, 340, 410, 20, 20,
                         90, 80, 170, "NO", False, "REFLETIVIDADE_BAIXA"),
        ]
        frame_id, _ = salvar_resultado_frame(frame, None, None, 750, 750, clusters)
        atualizar_tracking(frame_id, -22.4925326, -54.4610352, 3, 10, 150, 25)
        radar, _regional, _local, _fingerprint = carregar_entradas_nowcasting(
            nowcasting_config()
        )
        self.assertEqual(len(radar["tracks_atuais"]), 2)
        self.assertEqual(
            len({item["track"]["track_id"] for item in radar["tracks_atuais"]}), 2
        )

    def test_alerts_true_continua_sem_enfileirar(self):
        from workers.nowcasting_updater import enfileirar_alertas_nowcasting

        self.assertEqual(enfileirar_alertas_nowcasting({"alerts_enabled": True}, {}), 0)
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
        conn.close()

    def test_health_auxiliar_desabilitado(self):
        response = self.client.get("/health")
        self.assertEqual(response.get_json()["nowcasting"], "disabled")

    def test_health_nowcasting_sem_snapshot_e_warning_sem_mudar_regra_principal(self):
        os.environ["NOWCASTING_ENABLED"] = "true"
        now = datetime.now(timezone.utc).replace(microsecond=0)
        conn = self.database.get_db()
        conn.execute(
            "INSERT INTO historico_clima (data_hora_utc, data_hora_local, data_hora) VALUES (?, ?, ?)",
            (now.isoformat(), now.isoformat(), now.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        conn.close()
        response = self.client.get("/health")
        self.assertEqual(response.get_json()["nowcasting"], "warning")
        self.assertEqual(response.status_code, 200)

    def test_entrypoint_direto_e_modular_expoe_once(self):
        direct = subprocess.run(
            [sys.executable, "workers/nowcasting_updater.py", "--help"], cwd=ESTACAO,
            capture_output=True, text=True, timeout=30, check=False,
        )
        module = subprocess.run(
            [sys.executable, "-m", "estacao.workers.nowcasting_updater", "--help"], cwd=ROOT,
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(module.returncode, 0, module.stderr)
        self.assertIn("--once", direct.stdout)


if __name__ == "__main__":
    unittest.main()
