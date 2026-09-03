import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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
        self.real_freshness = json.loads(
            (fixtures / "pinms_layer_freshness_real.json").read_text(encoding="utf-8")
        )
        self.collected = datetime(2026, 9, 2, 20, tzinfo=timezone.utc)

    def autenticar_admin(self):
        with self.client.session_transaction() as session:
            session["logado"] = True
            session["ultimo_acesso"] = time.time()
            session["csrf_token"] = "csrf-teste"

    def tearDown(self):
        self.tmp.cleanup()
        for key in (
            "ESTACAO_DB", "REGIONAL_STATIONS_ENABLED", "REGIONAL_STATIONS_ALERTS_ENABLED",
            "REGIONAL_STATIONS_RETENTION_ENABLED", "REGIONAL_STATIONS_RETENTION_DAYS",
            "REGIONAL_LAYER2_MAX_AGE_HOURS", "REGIONAL_LAYER2_POLL_SECONDS",
            "REGIONAL_STATION_STAGNANT_MINUTES",
            "RATELIMIT_ENABLED", "SECRET_KEY",
        ):
            os.environ.pop(key, None)

    def test_migration_idempotente_e_catalogo(self):
        self.database.init_db()
        conn = self.database.get_db()
        tabelas = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        total = conn.execute("SELECT COUNT(*) FROM regional_stations").fetchone()[0]
        versao = conn.execute("SELECT versao FROM schema_version WHERE id=1").fetchone()[0]
        sample_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(regional_station_samples)")
        }
        state_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(regional_station_state)")
        }
        conn.close()
        self.assertTrue({
            "regional_stations", "regional_station_observations",
            "regional_station_samples", "regional_station_state",
        } <= tabelas)
        self.assertEqual(total, 6)
        self.assertEqual(versao, self.database.SCHEMA_VERSION)
        self.assertTrue({
            "source_observation_id", "sample_time_utc", "sample_time_type",
            "bucket_hour_utc", "chuva_mm", "fingerprint",
        } <= sample_columns)
        self.assertTrue({
            "current_fingerprint", "current_fingerprint_first_seen",
            "current_fingerprint_last_seen", "last_layer2_poll_utc",
        } <= state_columns)

    def test_migration_7_para_8_adiciona_estado_sem_perder_linha(self):
        legacy_path = Path(self.tmp.name) / "legacy-schema-7.db"
        conn = sqlite3.connect(legacy_path)
        conn.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY, versao INTEGER NOT NULL,
                atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO schema_version (id, versao) VALUES (1, 7);
            CREATE TABLE regional_station_state (
                station_code TEXT PRIMARY KEY,
                source_status TEXT NOT NULL DEFAULT 'SEM_DADOS',
                current_source_status TEXT NOT NULL DEFAULT 'SEM_DADOS',
                external_history_status TEXT NOT NULL DEFAULT 'SEM_DADOS',
                ultimo_erro TEXT,
                ultima_tentativa_em TEXT,
                ultimo_sucesso_em TEXT,
                atualizado_em TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO regional_station_state (
                station_code, source_status, current_source_status,
                external_history_status
            ) VALUES ('A721', 'OK', 'OK', 'STALE');
            """
        )
        conn.close()
        original_path = os.environ["ESTACAO_DB"]
        try:
            os.environ["ESTACAO_DB"] = str(legacy_path)
            database = importlib.reload(self.database)
            database.init_db()
            conn = database.get_db()
            columns = {
                row[1] for row in conn.execute(
                    "PRAGMA table_info(regional_station_state)"
                )
            }
            row = conn.execute(
                "SELECT current_source_status, external_history_status "
                "FROM regional_station_state WHERE station_code='A721'"
            ).fetchone()
            version = conn.execute(
                "SELECT versao FROM schema_version WHERE id=1"
            ).fetchone()[0]
            conn.close()
            self.assertTrue({
                "current_fingerprint", "current_fingerprint_first_seen",
                "current_fingerprint_last_seen", "last_layer2_poll_utc",
            } <= columns)
            self.assertEqual(tuple(row), ("OK", "STALE"))
            self.assertEqual(version, 8)
        finally:
            os.environ["ESTACAO_DB"] = original_path
            self.database = importlib.reload(self.database)

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
        self.autenticar_admin()
        response = self.client.get("/admin/estacoes-regionais")
        self.assertEqual(response.status_code, 200)
        for nome in ("Dourados", "Caarapó", "Juti", "Naviraí", "Ivinhema", "Culturama"):
            self.assertIn(nome.encode(), response.data)
        self.assertIn("Resumo da rede".encode(), response.data)
        self.assertIn("Como interpretar".encode(), response.data)
        self.assertIn("Condição atual".encode(), response.data)

    def test_api_sem_dados_nao_expoe_payload_bruto(self):
        self.autenticar_admin()
        payload = self.client.get("/admin/api/regional-stations").get_json()
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
        self.autenticar_admin()
        payload = self.client.get("/admin/api/regional-stations").get_json()
        dourados = next(item for item in payload["stations"] if item["code"] == "A721")
        self.assertEqual(dourados["observation"]["temperature"], 21.6)
        self.assertIsNone(dourados["trend"]["temperature_1h"])
        self.assertEqual(dourados["trend_source"], "local_history_layer0")
        self.assertEqual(dourados["trend_quality"], "INSUFFICIENT")
        self.assertEqual(dourados["current_source"]["same_values_minutes"], 0)
        self.assertFalse(dourados["current_source"]["stagnant"])
        self.assertEqual(dourados["current_source"]["status"], "OK")
        self.assertEqual(dourados["data_freshness"]["status"], "MUITO_ATRASADA")
        self.assertGreater(dourados["distance_km"], 0)
        page = self.client.get("/admin/estacoes-regionais")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Fonte atual".encode(), page.data)
        self.assertIn("Valores sem alteração".encode(), page.data)
        self.assertIn("Qualidade da coleta".encode(), page.data)
        self.assertIn("Tendências meteorológicas".encode(), page.data)
        self.assertIn("Ver tendências detalhadas".encode(), page.data)

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
        self.assertEqual(primeiro["samples_updated"], 6)
        self.assertEqual(primeiro["layer2_recent"], 0)
        self.assertEqual(primeiro["layer2_stale"], 6)
        self.assertTrue(primeiro["layer2_checked"])
        self.assertEqual(primeiro["empty"], 12)
        self.assertTrue(primeiro["time_diagnostics"])
        self.assertTrue(
            all("raw_date" in item and "timestamp_status" in item
                for item in primeiro["time_diagnostics"])
        )
        self.assertEqual(segundo["new"], 0)
        self.assertFalse(segundo["layer2_checked"])
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
        self.autenticar_admin()
        self.assertEqual(self.client.get("/admin/estacoes-regionais").status_code, 200)
        self.assertEqual(self.client.get("/admin/api/regional-stations").status_code, 200)

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
        self.assertIn("--verbose-history", direto.stdout)

    def test_fixture_real_layer0_fresca_layer2_marco_stale(self):
        from config import regional_stations_config
        from services.regional_stations_repository import (
            obter_estado_rede, registrar_status_estacao, salvar_observacao,
        )
        from services.regional_stations_service import normalizar_registro

        now = datetime.fromisoformat(self.real_freshness["collected_at"])
        atual = normalizar_registro(self.real_freshness["layer0"], 0, now)
        externa = normalizar_registro(self.real_freshness["layer2"], 2, now)
        salvar_observacao(atual)
        salvar_observacao(externa)
        registrar_status_estacao(
            "S735", "OK", sucesso=True, external_history_status="STALE"
        )
        estado = obter_estado_rede(regional_stations_config(), now=now)
        navirai = next(item for item in estado["stations"] if item["code"] == "S735")
        self.assertEqual(navirai["status"], "OK")
        self.assertEqual(navirai["observation"]["temperature"], 20.6)
        self.assertEqual(navirai["observation"]["reference_time_type"], "collection_time_proxy")
        self.assertEqual(navirai["external_hourly_source"]["status"], "STALE")
        self.assertFalse(navirai["external_hourly_source"]["usable"])
        self.assertEqual(navirai["trend_source"], "local_history_layer0")
        self.assertEqual(navirai["trend_quality"], "INSUFFICIENT")

    def _salvar_layer0(self, momento, temperatura, umidade=60, pressao=970,
                       vento=2, rajada=4, chuva=0, code="A721"):
        from services.regional_stations_repository import salvar_observacao
        from services.regional_stations_service import normalizar_registro

        raw = dict(next(
            feature["attributes"] for feature in self.layer0["features"]
            if feature["attributes"]["CD_ESTACAO"] == code
        ))
        raw.update({
            "TEM_INS": temperatura, "UMD_INS": umidade, "PRE_INS": pressao,
            "VEN_VEL": vento, "VEN_RAJ": rajada, "CHUVA": chuva,
        })
        salvar_observacao(normalizar_registro(raw, 0, momento))

    def test_historico_proprio_usa_ultima_observacao_de_cada_bucket(self):
        momentos = (
            (datetime(2026, 9, 3, 17, 5, tzinfo=timezone.utc), 25.1),
            (datetime(2026, 9, 3, 17, 20, tzinfo=timezone.utc), 25.0),
            (datetime(2026, 9, 3, 17, 55, tzinfo=timezone.utc), 24.4),
            (datetime(2026, 9, 3, 18, 5, tzinfo=timezone.utc), 23.8),
            (datetime(2026, 9, 3, 18, 55, tzinfo=timezone.utc), 23.0),
            (datetime(2026, 9, 3, 19, 50, tzinfo=timezone.utc), 22.5),
        )
        for momento, temperatura in momentos:
            self._salvar_layer0(momento, temperatura)
        conn = self.database.get_db()
        rows = conn.execute(
            "SELECT bucket_hour_local, temperatura_atual FROM regional_station_samples "
            "WHERE station_code='A721' ORDER BY bucket_hour_utc"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 3)
        self.assertEqual([row["temperatura_atual"] for row in rows], [24.4, 23.0, 22.5])
        self.assertTrue(rows[0]["bucket_hour_local"].startswith("2026-09-03T13:00"))

    def test_tendencia_local_exige_base_horaria_e_calcula_deltas(self):
        from config import regional_stations_config
        from services.regional_stations_repository import obter_estado_rede

        self._salvar_layer0(
            datetime(2026, 9, 3, 17, 55, tzinfo=timezone.utc),
            26, umidade=50, pressao=970,
        )
        self._salvar_layer0(
            datetime(2026, 9, 3, 18, 55, tzinfo=timezone.utc),
            23, umidade=70, pressao=968.5,
        )
        estado = obter_estado_rede(
            regional_stations_config(),
            now=datetime(2026, 9, 3, 18, 56, tzinfo=timezone.utc),
        )
        station = next(item for item in estado["stations"] if item["code"] == "A721")
        self.assertEqual(station["trend_quality"], "GOOD")
        self.assertEqual(station["trend"]["temperature_1h"], -3)
        self.assertEqual(station["trend"]["humidity_1h"], 20)
        self.assertEqual(station["trend"]["pressure_1h"], -1.5)

    def test_poll_repetido_nao_multiplica_observacao_nem_chuva(self):
        from config import regional_stations_config
        from services.regional_stations_repository import obter_estado_rede

        for hora in (15, 16, 17, 18):
            self._salvar_layer0(
                datetime(2026, 9, 3, hora, 5, tzinfo=timezone.utc), 24, chuva=2
            )
        conn = self.database.get_db()
        observacoes = conn.execute(
            "SELECT COUNT(*) FROM regional_station_observations "
            "WHERE station_code='A721' AND source_layer=0"
        ).fetchone()[0]
        buckets = conn.execute(
            "SELECT COUNT(*) FROM regional_station_samples WHERE station_code='A721'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(observacoes, 1)
        self.assertEqual(buckets, 4)
        estado = obter_estado_rede(
            regional_stations_config(),
            now=datetime(2026, 9, 3, 18, 6, tzinfo=timezone.utc),
        )
        station = next(item for item in estado["stations"] if item["code"] == "A721")
        self.assertEqual(station["trend"]["rain_3h"], 2.0)

    def test_vinte_minutos_de_coleta_mantem_status_ok_e_trend_insufficient(self):
        from config import regional_stations_config
        from services.regional_stations_repository import obter_estado_rede

        self._salvar_layer0(datetime(2026, 9, 3, 17, 5, tzinfo=timezone.utc), 25)
        self._salvar_layer0(datetime(2026, 9, 3, 17, 25, tzinfo=timezone.utc), 24)
        estado = obter_estado_rede(
            regional_stations_config(),
            now=datetime(2026, 9, 3, 17, 26, tzinfo=timezone.utc),
        )
        station = next(item for item in estado["stations"] if item["code"] == "A721")
        self.assertEqual(station["status"], "OK")
        self.assertEqual(station["trend_quality"], "INSUFFICIENT")
        self.assertTrue(all(value is None for value in station["trend"].values()))

    def test_fingerprint_igual_por_30_120_e_181_minutos_detecta_estagnacao(self):
        from config import regional_stations_config
        from services.regional_stations_repository import (
            obter_estado_rede, registrar_status_estacao,
        )

        inicio = datetime(2026, 9, 3, 14, tzinfo=timezone.utc)
        self._salvar_layer0(inicio, 22.3)
        registrar_status_estacao("A721", "OK", sucesso=True, now=inicio)
        for minutos, esperado in ((30, "OK"), (120, "OK"), (181, "DADOS_ESTAGNADOS")):
            momento = inicio + timedelta(minutes=minutos)
            self._salvar_layer0(momento, 22.3)
            station = next(
                item for item in obter_estado_rede(
                    regional_stations_config(), now=momento
                )["stations"] if item["code"] == "A721"
            )
            self.assertEqual(station["status"], esperado)
            self.assertEqual(station["current_source"]["same_values_minutes"], minutos)
        self.assertTrue(station["current_source"]["stagnant"])
        self.assertEqual(station["current_source"]["status"], "OK")

    def test_novo_fingerprint_reseta_estagnacao_e_erro_http_tem_prioridade(self):
        from config import regional_stations_config
        from services.regional_stations_repository import (
            obter_estado_rede, registrar_status_estacao,
        )

        inicio = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
        self._salvar_layer0(inicio, 22.3)
        quatro_horas = inicio + timedelta(hours=4)
        self._salvar_layer0(quatro_horas, 22.3)
        registrar_status_estacao("A721", "ERRO_FONTE", "falha simulada", now=quatro_horas)
        station = next(
            item for item in obter_estado_rede(
                regional_stations_config(), now=quatro_horas
            )["stations"] if item["code"] == "A721"
        )
        self.assertTrue(station["current_source"]["stagnant"])
        self.assertEqual(station["status"], "ERRO_FONTE")

        mudou = quatro_horas + timedelta(minutes=5)
        self._salvar_layer0(mudou, 22.4)
        registrar_status_estacao("A721", "OK", sucesso=True, now=mudou)
        station = next(
            item for item in obter_estado_rede(
                regional_stations_config(), now=mudou
            )["stations"] if item["code"] == "A721"
        )
        self.assertEqual(station["current_source"]["same_values_minutes"], 0)
        self.assertFalse(station["current_source"]["stagnant"])
        self.assertEqual(station["status"], "OK")

    def test_layer2_poll_independente_persiste_apos_restart(self):
        from config import regional_stations_config
        from workers.regional_stations_updater import executar_ciclo

        class Client:
            def __init__(inner_self):
                inner_self.history_calls = 0

            def obter_atuais(inner_self):
                return self.layer0

            def obter_historico(inner_self, code):
                inner_self.history_calls += 1
                raw = dict(self.layer2["features"][2]["attributes"])
                raw["CD_ESTACAO"] = code
                return {"features": [{"attributes": raw}]}

        os.environ["REGIONAL_STATIONS_ENABLED"] = "true"
        os.environ["REGIONAL_LAYER2_POLL_SECONDS"] = "3600"
        config = regional_stations_config()
        inicio = datetime(2026, 9, 3, 18, tzinfo=timezone.utc)
        primeiro_client = Client()
        primeiro = executar_ciclo(primeiro_client, config, now=inicio)
        cinco_min = executar_ciclo(
            primeiro_client, config, now=inicio + timedelta(minutes=5)
        )
        reiniciado = Client()
        dez_min = executar_ciclo(
            reiniciado, config, now=inicio + timedelta(minutes=10)
        )
        uma_hora = executar_ciclo(
            reiniciado, config, now=inicio + timedelta(hours=1)
        )
        self.assertTrue(primeiro["layer2_checked"])
        self.assertEqual(primeiro_client.history_calls, 6)
        self.assertFalse(cinco_min["layer2_checked"])
        self.assertFalse(dez_min["layer2_checked"])
        self.assertTrue(uma_hora["layer2_checked"])
        self.assertEqual(reiniciado.history_calls, 6)
        self.assertEqual(cinco_min["layer2_stale"], 6)
        conn = self.database.get_db()
        polls = conn.execute(
            "SELECT COUNT(DISTINCT last_layer2_poll_utc) FROM regional_station_state"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(polls, 1)

    def test_layer2_stale_recupera_automaticamente_quando_atualiza(self):
        from config import regional_stations_config
        from workers.regional_stations_updater import executar_ciclo

        class Client:
            recent = False

            def obter_atuais(inner_self):
                return self.layer0

            def obter_historico(inner_self, code):
                if not inner_self.recent:
                    raw = dict(self.layer2["features"][2]["attributes"])
                    raw["CD_ESTACAO"] = code
                    return {"features": [{"attributes": raw}]}
                features = []
                for hour, temperature in ((18, 24), (17, 26)):
                    raw = dict(self.layer2["features"][2]["attributes"])
                    raw.update({
                        "CD_ESTACAO": code, "DT_MEDICAO": "2026-09-03",
                        "HR_MEDICAO": f"{hour}:00", "TEM_INS": temperature,
                    })
                    features.append({"attributes": raw})
                return {"features": features}

        os.environ["REGIONAL_STATIONS_ENABLED"] = "true"
        config = regional_stations_config()
        inicio = datetime(2026, 9, 3, 18, tzinfo=timezone.utc)
        client = Client()
        primeiro = executar_ciclo(client, config, now=inicio)
        client.recent = True
        recuperado = executar_ciclo(
            client, config, now=inicio + timedelta(hours=1, seconds=1)
        )
        self.assertEqual(primeiro["layer2_stale"], 6)
        self.assertEqual(recuperado["layer2_recent"], 6)
        self.assertTrue(all(
            station["external_hourly_source"]["status"] == "OK"
            and station["external_hourly_source"]["usable"]
            for station in recuperado["state"]["stations"]
        ))

    def test_nowcasting_pode_confirmar_com_historico_proprio_layer0(self):
        from config import nowcasting_config, regional_stations_config
        from services.nowcasting_service import analisar_nowcasting
        from services.regional_stations_repository import obter_estado_rede

        anterior = datetime(2026, 9, 3, 17, 55, tzinfo=timezone.utc)
        atual = datetime(2026, 9, 3, 18, 55, tzinfo=timezone.utc)
        self._salvar_layer0(
            anterior, 26, umidade=50, pressao=970, vento=1, rajada=2,
            chuva=0, code="A749",
        )
        self._salvar_layer0(
            atual, 23, umidade=70, pressao=968.5, vento=5, rajada=8,
            chuva=1, code="A749",
        )
        regional = obter_estado_rede(
            regional_stations_config(),
            now=datetime(2026, 9, 3, 18, 56, tzinfo=timezone.utc),
        )
        radar = {
            "disponivel": True, "stale": False,
            "frame": {"id": 7, "imagem_disponivel": False},
            "tracking": {
                "track_id": 12, "quantidade_frames": 4,
                "velocidade_kmh": 45, "bearing_movimento": 0,
                "direcao_movimento": "N", "centro_lat": -22.8,
                "centro_lon": -54.46, "aproximando": True,
                "trajetoria_compativel": True, "eta_minutos": 80,
            },
            "cluster_mais_proximo": {"distancia_borda_escola_km": 60},
        }
        estado = analisar_nowcasting(
            radar, regional, None, nowcasting_config(),
            now=datetime(2026, 9, 3, 18, 56, tzinfo=timezone.utc),
        )
        self.assertEqual(estado["status"], "ATENCAO_PREVENTIVA")
        self.assertEqual(estado["confirmacao_regional"]["stations"], ["A749"])
        self.assertEqual(
            estado["estacoes_relevantes"][0]["trend_source"],
            "local_history_layer0",
        )

    def test_layer2_recente_pode_servir_de_bootstrap_temporario(self):
        from config import regional_stations_config
        from services.regional_stations_repository import obter_estado_rede, salvar_observacao
        from services.regional_stations_service import normalizar_registro

        now = datetime(2026, 9, 3, 18, 30, tzinfo=timezone.utc)
        self._salvar_layer0(now, 24)
        for hour, temperature in ((18, 24), (17, 26)):
            raw = dict(self.layer2["features"][2]["attributes"])
            raw.update({
                "DT_MEDICAO": "2026-09-03", "HR_MEDICAO": f"{hour}:00",
                "TEM_INS": temperature, "UMD_INS": 60, "PRE_INS": 970,
            })
            salvar_observacao(normalizar_registro(raw, 2, now))
        estado = obter_estado_rede(regional_stations_config(), now=now)
        station = next(item for item in estado["stations"] if item["code"] == "A721")
        self.assertEqual(station["status"], "OK")
        self.assertEqual(station["trend_source"], "external_layer2_bootstrap")
        self.assertEqual(station["trend_quality"], "GOOD")
        self.assertTrue(station["external_hourly_source"]["usable"])


if __name__ == "__main__":
    unittest.main()
