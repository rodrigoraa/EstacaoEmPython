import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ESTACAO = ROOT / "estacao"
sys.path.insert(0, str(ESTACAO))


class RegionalStationsIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(
            {
                "ESTACAO_DB": str(Path(self.tmp.name) / "teste.db"),
                "REGIONAL_STATIONS_ENABLED": "false",
                "REGIONAL_STATIONS_ALERTS_ENABLED": "false",
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
        fixtures = ROOT / "tests" / "fixtures"
        self.layer0 = json.loads((fixtures / "pinms_layer0.json").read_text(encoding="utf-8"))
        self.layer2 = json.loads((fixtures / "pinms_layer2.json").read_text(encoding="utf-8"))
        self.collected = datetime(2026, 9, 2, 20, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()
        for key in (
            "ESTACAO_DB", "REGIONAL_STATIONS_ENABLED", "REGIONAL_STATIONS_ALERTS_ENABLED",
            "REGIONAL_STATIONS_RETENTION_ENABLED", "REGIONAL_STATIONS_RETENTION_DAYS",
            "RATELIMIT_ENABLED", "SECRET_KEY",
        ):
            os.environ.pop(key, None)

    def test_migration_idempotente_e_catalogo(self):
        self.database.init_db()
        conn = self.database.get_db()
        tabelas = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        total = conn.execute("SELECT COUNT(*) FROM regional_stations").fetchone()[0]
        versao = conn.execute("SELECT versao FROM schema_version WHERE id=1").fetchone()[0]
        conn.close()
        self.assertTrue({"regional_stations", "regional_station_observations", "regional_station_state"} <= tabelas)
        self.assertEqual(total, 6)
        self.assertEqual(versao, self.database.SCHEMA_VERSION)

    def test_deduplicacao_polling_repetido(self):
        from services.regional_stations_repository import salvar_observacao
        from services.regional_stations_service import normalizar_registro

        obs = normalizar_registro(self.layer2["features"][2]["attributes"], 2, self.collected)
        self.assertTrue(salvar_observacao(obs))
        self.assertFalse(salvar_observacao(obs))
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM regional_station_observations").fetchone()[0], 1)
        row = conn.execute("SELECT source_dt_medicao_raw, source_hr_medicao_raw FROM regional_station_observations").fetchone()
        conn.close()
        self.assertEqual(row["source_hr_medicao_raw"], "19:00")
        self.assertEqual(row["source_dt_medicao_raw"], "1788307200000")

    def test_pagina_sem_observacoes_exibe_seis_estacoes(self):
        response = self.client.get("/estacoes-regionais")
        self.assertEqual(response.status_code, 200)
        for nome in ("Dourados", "Caarapó", "Juti", "Naviraí", "Ivinhema", "Culturama"):
            self.assertIn(nome.encode(), response.data)
        self.assertIn("Dado horário de estação externa".encode(), response.data)

    def test_api_sem_dados_nao_expoe_payload_bruto(self):
        payload = self.client.get("/api/regional-stations").get_json()
        self.assertEqual(len(payload["stations"]), 6)
        self.assertTrue(all(item["status"] == "SEM_DADOS" for item in payload["stations"]))
        self.assertNotIn("payload_json", json.dumps(payload))

    def test_pagina_e_api_com_dados(self):
        from config import regional_stations_config
        from services.regional_stations_repository import registrar_status_estacao, salvar_observacao
        from services.regional_stations_service import normalizar_registro

        for index in (3, 2):
            salvar_observacao(normalizar_registro(self.layer2["features"][index]["attributes"], 2, self.collected))
        salvar_observacao(normalizar_registro(self.layer0["features"][0]["attributes"], 0, self.collected))
        registrar_status_estacao("A721", "OK", sucesso=True)
        payload = self.client.get("/api/regional-stations").get_json()
        dourados = next(item for item in payload["stations"] if item["code"] == "A721")
        self.assertEqual(dourados["observation"]["temperature"], 21.6)
        self.assertEqual(dourados["trend"]["temperature_1h"], 1.0)
        self.assertGreater(dourados["distance_km"], 0)
        self.assertEqual(self.client.get("/estacoes-regionais").status_code, 200)

    def test_worker_mockado_processa_seis_e_ignora_slots_vazios(self):
        from config import regional_stations_config
        from workers.regional_stations_updater import executar_ciclo

        class Client:
            def obter_atuais(inner_self):
                return self.layer0

            def obter_historico(inner_self, code):
                features = []
                for feature in self.layer2["features"]:
                    raw = dict(feature["attributes"])
                    raw["CD_ESTACAO"] = code
                    features.append({"attributes": raw})
                return {"features": features}

        os.environ["REGIONAL_STATIONS_ENABLED"] = "true"
        primeiro = executar_ciclo(Client(), regional_stations_config())
        segundo = executar_ciclo(Client(), regional_stations_config())
        self.assertEqual(primeiro["found"], 6)
        self.assertEqual(primeiro["with_current_data"], 6)
        self.assertEqual(primeiro["new"], 24)
        self.assertEqual(primeiro["empty"], 12)
        self.assertTrue(primeiro["time_diagnostics"])
        self.assertTrue(
            all("raw_date" in item and "timestamp_status" in item
                for item in primeiro["time_diagnostics"])
        )
        self.assertEqual(segundo["new"], 0)
        self.assertGreater(segundo["duplicates"], 0)

    def test_estacao_ausente_nao_impede_as_demais(self):
        from config import regional_stations_config
        from workers.regional_stations_updater import executar_ciclo

        class Client:
            def obter_atuais(inner_self):
                return {"features": self.layer0["features"][:-1]}

            def obter_historico(inner_self, code):
                raw = dict(self.layer2["features"][2]["attributes"])
                raw["CD_ESTACAO"] = code
                return {"features": [{"attributes": raw}]}

        os.environ["REGIONAL_STATIONS_ENABLED"] = "true"
        result = executar_ciclo(Client(), regional_stations_config())
        states = {item["code"]: item for item in result["state"]["stations"]}
        self.assertEqual(result["found"], 5)
        self.assertEqual(states["S708"]["status"], "ERRO_FONTE")
        self.assertNotEqual(states["A721"]["source_status"], "ERRO_FONTE")

    def test_fonte_indisponivel_nao_derruba_flask(self):
        self.assertEqual(self.client.get("/estacoes-regionais").status_code, 200)
        self.assertEqual(self.client.get("/api/regional-stations").status_code, 200)

    def test_alertas_desabilitados_nao_criam_fila(self):
        from config import regional_stations_config
        from workers.regional_stations_updater import enfileirar_alertas_regionais

        self.assertEqual(enfileirar_alertas_regionais(regional_stations_config(), {}), 0)
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
        conn.close()

    def test_retencao_exige_opt_in(self):
        from workers.maintenance import executar_cleanup_regional

        conn = self.database.get_db()
        with self.assertRaises(RuntimeError):
            executar_cleanup_regional(conn)
        os.environ["REGIONAL_STATIONS_RETENTION_ENABLED"] = "true"
        self.assertEqual(executar_cleanup_regional(conn), 0)
        conn.close()

    def test_health_regional_desabilitado(self):
        self.assertEqual(self.client.get("/health").get_json()["regional_stations"], "disabled")

    def test_entrypoint_direto_e_modular_expoe_once(self):
        direto = subprocess.run(
            [sys.executable, "workers/regional_stations_updater.py", "--help"],
            cwd=ESTACAO, capture_output=True, text=True, timeout=30, check=False,
        )
        modular = subprocess.run(
            [sys.executable, "-m", "estacao.workers.regional_stations_updater", "--help"],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(direto.returncode, 0, direto.stderr)
        self.assertEqual(modular.returncode, 0, modular.stderr)
        self.assertIn("--once", direto.stdout)
        self.assertIn("--verbose-time", direto.stdout)


if __name__ == "__main__":
    unittest.main()
