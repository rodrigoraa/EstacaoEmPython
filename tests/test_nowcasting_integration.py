import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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
            "NOWCASTING_TEST_ALERTS_ENABLED": "false",
            "RATELIMIT_ENABLED": "false",
            "SECRET_KEY": "teste",
        })
        import database
        import app

        self.database = importlib.reload(database)
        self.database.init_db()
        self.app_module = importlib.reload(app)
        self.client = self.app_module.app.test_client()

    def autenticar_admin(self):
        with self.client.session_transaction() as session:
            session["logado"] = True
            session["ultimo_acesso"] = time.time()
            session["csrf_token"] = "csrf-teste"

    def tearDown(self):
        self.tmp.cleanup()
        for key in (
            "ESTACAO_DB", "NOWCASTING_ENABLED", "NOWCASTING_ALERTS_ENABLED",
            "NOWCASTING_TEST_ALERTS_ENABLED", "ADMIN_ALERT_PHONE",
            "NOWCASTING_TEST_ALERT_COOLDOWN_MINUTES",
            "NOWCASTING_TEST_ALERT_REARM_MINUTES", "RATELIMIT_ENABLED", "SECRET_KEY",
        ):
            os.environ.pop(key, None)

    def state(self):
        agora = datetime.now(timezone.utc).replace(microsecond=0)
        return {
            "status": "NORMAL", "nivel_evidencia": "SEM_EVIDENCIA",
            "indice_evidencia": 0,
            "radar": {
                "disponivel": False, "operacional": False,
                "stale": None, "frame_id": None,
                "track_id": None, "distancia_borda_km": None,
                "faixa_distancia": None, "direcao": None,
                "velocidade_kmh": None, "aproximando": None,
                "trajetoria_compativel": False, "eta_minutos": None,
                "eta_borda_minutos": None, "taxa_aproximacao_borda_kmh": None,
                "qualidade_tracking": "DADOS_INSUFICIENTES",
                "quantidade_frames": None, "suspeito_clutter": False,
                "duracao_tracking_minutos": None,
                "indice_persistencia_clutter": None, "imagem_disponivel": False,
            },
            "estacoes_relevantes": [], "escola": None,
            "ameaca_principal": None, "eco_alerta_proximidade": None,
            "ameacas": [],
            "confirmacao_regional": {
                "confirmada": False, "stations": [], "evidence_count": 0,
            },
            "radar_only": False, "evento_local_observado": False,
            "alerta_preventivo": {
                "nivel": "INDISPONIVEL", "nivel_base": "INDISPONIVEL",
                "cor": "cinza", "cluster_id": None,
                "distance_km": None, "center_distance_km": None,
                "relative_position": None, "track_id": None,
                "tracking_valid": False, "tracking_quality": "DADOS_INSUFICIENTES",
                "frame_count": None, "duration_minutes": None,
                "approaching": None, "trajectory_compatible": False,
                "direction": None, "speed_kmh": None, "eta_minutes": None,
                "eta_border_minutes": None, "border_approach_rate_kmh": None,
                "clutter": False, "clutter_index": None, "low_confidence": False,
                "regional_confirmation": False, "regional_stations": [],
                "local_event": False,
                "message": "Dados operacionais do radar indisponíveis.",
                "would_send": False, "preventive_sending": "DESATIVADO",
                "simulation_message": "Nenhum alerta preventivo será enviado aos usuários por esta versão.",
            },
            "historico_regional_em_formacao": False,
            "evidencias": ["Sem evidencia observacional relevante no momento"],
            "gerado_em": agora.isoformat(),
            "gerado_em_utc": agora.isoformat(),
            "versao_algoritmo": "1.4",
        }

    def state_vermelho(self, gerado_em_utc=None):
        state = self.state()
        state["gerado_em_utc"] = gerado_em_utc or datetime.now(
            timezone.utc
        ).replace(microsecond=0).isoformat()
        state["radar"].update({
            "disponivel": True,
            "operacional": True,
            "stale": False,
            "frame_id": None,
            "distancia_borda_km": 20,
        })
        state["alerta_preventivo"].update({
            "nivel": "VERMELHO",
            "nivel_base": "VERMELHO",
            "cor": "vermelho",
            "cluster_id": 101,
            "track_id": 12,
            "distance_km": 20,
            "message": "ALERTA OPERACIONAL ANTIGO",
            "would_send": True,
        })
        state["eco_alerta_proximidade"] = {
            "cluster_id": 101,
            "track_id": 12,
            "distance_km": 20,
        }
        return state

    def test_migration_aditiva_idempotente_cria_snapshot_schema_8(self):
        self.database.init_db()
        conn = self.database.get_db()
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = conn.execute("SELECT versao FROM schema_version WHERE id=1").fetchone()[0]
        conn.close()
        self.assertIn("nowcasting_snapshots", tables)
        self.assertEqual(version, 8)
        conn = self.database.get_db()
        frame_columns = {row[1] for row in conn.execute("PRAGMA table_info(radar_frames)")}
        cluster_columns = {row[1] for row in conn.execute("PRAGMA table_info(radar_clusters)")}
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(radar_track_points)")
        }
        conn.close()
        self.assertTrue({"data_frame_raw", "coletado_em_utc"} <= frame_columns)
        self.assertTrue({"classe_predominante", "classe_maxima"} <= cluster_columns)
        self.assertIn("idx_radar_track_points_track_data", indexes)

    def test_migration_7_para_8_preserva_snapshot_existente(self):
        from services.nowcasting_repository import salvar_snapshot

        self.assertIsNotNone(salvar_snapshot(self.state(), "snapshot-schema-7"))
        conn = self.database.get_db()
        conn.execute(
            "UPDATE regional_station_state SET external_history_status='STALE' "
            "WHERE station_code='A721'"
        )
        conn.execute("UPDATE schema_version SET versao=7 WHERE id=1")
        conn.commit()
        conn.close()
        self.database.init_db()
        conn = self.database.get_db()
        count = conn.execute(
            "SELECT COUNT(*) FROM nowcasting_snapshots WHERE input_fingerprint=?",
            ("snapshot-schema-7",),
        ).fetchone()[0]
        version = conn.execute("SELECT versao FROM schema_version WHERE id=1").fetchone()[0]
        external_status = conn.execute(
            "SELECT external_history_status FROM regional_station_state "
            "WHERE station_code='A721'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)
        self.assertEqual(version, 8)
        self.assertEqual(external_status, "STALE")

    def test_pagina_e_api_antes_do_primeiro_snapshot(self):
        self.autenticar_admin()
        page = self.client.get("/admin/monitoramento")
        api = self.client.get("/admin/api/nowcasting/status")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Aguardando a primeira análise".encode(), page.data)
        self.assertEqual(api.get_json()["status"], "SEM_DADOS")

    def test_monitoramento_snapshot_antigo_vira_historico_sem_apagar_snapshot(self):
        from services.nowcasting_repository import salvar_snapshot

        antigo = (
            datetime.now(timezone.utc) - timedelta(minutes=20)
        ).replace(microsecond=0).isoformat()
        state = self.state_vermelho(antigo)
        self.assertIsNotNone(salvar_snapshot(state, "snapshot-antigo"))

        self.autenticar_admin()
        page = self.client.get("/admin/monitoramento")

        self.assertEqual(page.status_code, 200)
        self.assertIn("MONITORAMENTO DESATUALIZADO".encode(), page.data)
        self.assertIn("INDISPONÍVEL".encode(), page.data)
        self.assertIn("Último nível calculado".encode(), page.data)
        self.assertNotIn(b"preventive-alert--vermelho", page.data)
        self.assertNotIn(b"ALERTA OPERACIONAL ANTIGO", page.data)
        conn = self.database.get_db()
        row = conn.execute(
            "SELECT estado_json FROM nowcasting_snapshots WHERE input_fingerprint=?",
            ("snapshot-antigo",),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(
            json.loads(row["estado_json"])["alerta_preventivo"]["nivel"],
            "VERMELHO",
        )

    def test_monitoramento_snapshot_recente_exibe_alerta_normalmente(self):
        from services.nowcasting_repository import salvar_snapshot

        state = self.state_vermelho()
        state["alerta_preventivo"]["message"] = "ALERTA RECENTE VISÍVEL"
        self.assertIsNotNone(salvar_snapshot(state, "snapshot-recente"))

        self.autenticar_admin()
        page = self.client.get("/admin/monitoramento")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"preventive-alert--vermelho", page.data)
        self.assertIn(b"ALERTA RECENTE VIS", page.data)
        self.assertNotIn("MONITORAMENTO DESATUALIZADO".encode(), page.data)

    def test_monitoramento_timestamp_invalido_ou_ausente_nao_quebra(self):
        self.autenticar_admin()
        for timestamp in ("invalido", None):
            with self.subTest(timestamp=timestamp):
                state = self.state_vermelho()
                if timestamp is None:
                    state.pop("gerado_em_utc")
                else:
                    state["gerado_em_utc"] = timestamp
                with mock.patch(
                    "routes.nowcasting._estado_seguro", return_value=state
                ):
                    page = self.client.get("/admin/monitoramento")
                self.assertEqual(page.status_code, 200)
                self.assertIn("MONITORAMENTO DESATUALIZADO".encode(), page.data)
                self.assertNotIn(b"preventive-alert--vermelho", page.data)

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
        state["historico_regional_em_formacao"] = True
        self.assertIsNotNone(salvar_snapshot(state, "entrada-1"))
        self.assertIsNone(salvar_snapshot(state, "entrada-1"))
        conn = self.database.get_db()
        count = conn.execute("SELECT COUNT(*) FROM nowcasting_snapshots").fetchone()[0]
        conn.close()
        self.autenticar_admin()
        payload = self.client.get("/admin/api/nowcasting/status").get_json()
        page = self.client.get("/admin/monitoramento")
        self.assertEqual(count, 1)
        self.assertEqual(page.status_code, 200)
        self.assertIn("AMEAÇA PRINCIPAL".encode(), page.data)
        self.assertIn("O que olhar primeiro".encode(), page.data)
        self.assertIn("Como interpretar".encode(), page.data)
        self.assertIn("Índice interno de evidência".encode(), page.data)
        self.assertIn("Ameaça principal do nowcasting".encode(), page.data)
        self.assertIn("Níveis de proximidade".encode(), page.data)
        self.assertIn("Envio preventivo a usuários: DESATIVADO".encode(), page.data)
        self.assertIn("INDISPONÍVEL".encode(), page.data)
        self.assertIn("Nenhum eco confiável dentro de 100 km".encode(), page.data)
        self.assertIn("Glossário do monitoramento".encode(), page.data)
        self.assertIn(b"Outros sistemas em monitoramento", page.data)
        self.assertIn("histórico regional ainda em formação".encode(), page.data)
        self.assertEqual(payload["versao_algoritmo"], "1.4")
        self.assertEqual(payload["alerta_preventivo"]["nivel"], "INDISPONIVEL")
        self.assertIn("ameaca_principal", payload)
        self.assertIn("ameacas", payload)
        self.assertIn("confirmacao_regional", payload)
        self.assertEqual(len(payload["ameacas"]), 2)
        self.assertNotIn("estado_json", json.dumps(payload))
        self.assertNotIn("input_fingerprint", json.dumps(payload))

    def test_snapshot_legado_sem_novos_campos_continua_renderizando(self):
        from services.nowcasting_repository import salvar_snapshot

        state = self.state()
        state.pop("alerta_preventivo")
        state.pop("eco_alerta_proximidade")
        for campo in (
            "eta_borda_minutos",
            "taxa_aproximacao_borda_kmh",
            "qualidade_tracking",
            "duracao_tracking_minutos",
        ):
            state["radar"].pop(campo)
        self.assertIsNotNone(salvar_snapshot(state, "snapshot-legado"))

        self.autenticar_admin()
        page = self.client.get("/admin/monitoramento")
        payload = self.client.get("/admin/api/nowcasting/status").get_json()

        self.assertEqual(page.status_code, 200)
        self.assertIn(b"AGUARDANDO", page.data)
        self.assertIn("Aguardando cálculo do nível preventivo".encode(), page.data)
        self.assertNotIn("alerta_preventivo", payload)

    def test_snapshot_persiste_ids_distintos_da_ameaca_e_do_eco_de_proximidade(self):
        from services.nowcasting_repository import salvar_snapshot

        state = self.state()
        state["ameaca_principal"] = {
            "cluster_id": 202, "track_id": 22, "distance_km": 60,
        }
        state["eco_alerta_proximidade"] = {
            "cluster_id": 101, "track_id": 11, "distance_km": 20,
        }
        state["alerta_preventivo"].update({
            "nivel": "VERMELHO", "nivel_base": "VERMELHO", "cor": "vermelho",
            "cluster_id": 101, "track_id": 11, "distance_km": 20,
            "would_send": True,
        })
        self.assertIsNotNone(salvar_snapshot(state, "ids-distintos"))

        self.autenticar_admin()
        payload = self.client.get("/admin/api/nowcasting/status").get_json()
        self.assertEqual(payload["ameaca_principal"]["cluster_id"], 202)
        self.assertEqual(payload["eco_alerta_proximidade"]["cluster_id"], 101)
        self.assertEqual(payload["alerta_preventivo"]["cluster_id"], 101)

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
        self.assertTrue(
            all(item["track"]["historico_borda"] for item in radar["tracks_atuais"])
        )
        self.assertEqual(
            len({item["track"]["track_id"] for item in radar["tracks_atuais"]}), 2
        )

    def test_alertas_preventivos_nunca_enfileiram_com_flag_false_ou_true(self):
        from workers.nowcasting_updater import enfileirar_alertas_nowcasting

        vermelho_simulado = {
            "alerta_preventivo": {
                "nivel": "VERMELHO",
                "would_send": True,
                "preventive_sending": "DESATIVADO",
            }
        }
        for habilitado in (False, True):
            with self.subTest(alerts_enabled=habilitado):
                self.assertEqual(
                    enfileirar_alertas_nowcasting(
                        {"alerts_enabled": habilitado}, vermelho_simulado
                    ),
                    0,
                )
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
        conn.close()

    def test_worker_vermelho_com_alerts_true_ainda_nao_cria_fila(self):
        from config import nowcasting_config
        from workers import nowcasting_updater

        os.environ["NOWCASTING_ENABLED"] = "true"
        os.environ["NOWCASTING_ALERTS_ENABLED"] = "true"
        estado = self.state()
        estado["alerta_preventivo"].update({
            "nivel": "VERMELHO",
            "nivel_base": "VERMELHO",
            "cor": "vermelho",
            "distance_km": 20,
            "would_send": True,
            "message": "ALERTA PREVENTIVO: possível chuva próxima.",
            "simulation_message": (
                "Este evento seria candidato a alerta por WhatsApp, "
                "mas o envio está desativado."
            ),
        })
        with (
            mock.patch.object(
                nowcasting_updater,
                "carregar_entradas_nowcasting",
                return_value=({}, {"stations": []}, None, "vermelho-simulado"),
            ),
            mock.patch.object(
                nowcasting_updater, "analisar_nowcasting", return_value=estado
            ),
        ):
            resultado = nowcasting_updater.executar_ciclo(nowcasting_config())

        self.assertTrue(resultado["snapshot"]["alerta_preventivo"]["would_send"])
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
        conn.close()

    def test_worker_modo_teste_envia_direto_so_ao_admin_sem_fila(self):
        from config import nowcasting_config
        from workers import nowcasting_updater

        telefone = "67987654321"
        os.environ["NOWCASTING_ENABLED"] = "true"
        os.environ["NOWCASTING_ALERTS_ENABLED"] = "true"
        os.environ["NOWCASTING_TEST_ALERTS_ENABLED"] = "true"
        os.environ["ADMIN_ALERT_PHONE"] = telefone
        estado = self.state_vermelho()
        estado["alerta_preventivo"].update({
            "tracking_valid": False,
            "clutter": False,
            "low_confidence": False,
        })
        with (
            mock.patch.object(
                nowcasting_updater,
                "carregar_entradas_nowcasting",
                return_value=({}, {"stations": []}, None, "teste-admin-vermelho"),
            ),
            mock.patch.object(
                nowcasting_updater, "analisar_nowcasting", return_value=estado
            ),
            mock.patch(
                "services.nowcasting_test_alerts.enviar_mensagem_admin"
            ) as enviar_admin,
        ):
            resultado = nowcasting_updater.executar_ciclo(nowcasting_config())

        enviar_admin.assert_called_once()
        self.assertEqual(enviar_admin.call_args.args[0], telefone)
        self.assertTrue(resultado["test_alert"]["sent_for_current_episode"])
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
        estado_teste = conn.execute(
            "SELECT mensagem FROM health_check_estado WHERE chave='nowcasting_test_alert'"
        ).fetchone()[0]
        conn.close()
        self.assertNotIn(telefone, estado_teste)

    def test_falha_whatsapp_do_teste_nao_derruba_worker(self):
        from config import nowcasting_config
        from workers import nowcasting_updater

        os.environ["NOWCASTING_ENABLED"] = "true"
        os.environ["NOWCASTING_TEST_ALERTS_ENABLED"] = "true"
        os.environ["ADMIN_ALERT_PHONE"] = "67987654321"
        estado = self.state_vermelho()
        estado["alerta_preventivo"].update({
            "tracking_valid": False,
            "clutter": False,
            "low_confidence": False,
        })
        with (
            mock.patch.object(
                nowcasting_updater,
                "carregar_entradas_nowcasting",
                return_value=({}, {"stations": []}, None, "teste-admin-falha"),
            ),
            mock.patch.object(
                nowcasting_updater, "analisar_nowcasting", return_value=estado
            ),
            mock.patch(
                "services.nowcasting_test_alerts.enviar_mensagem_admin",
                side_effect=RuntimeError("Evolution indisponível"),
            ),
        ):
            resultado = nowcasting_updater.executar_ciclo(nowcasting_config())

        self.assertFalse(resultado["disabled"])
        self.assertEqual(resultado["test_alert"]["reason"], "send_failed")
        conn = self.database.get_db()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0)
        conn.close()

    def test_telas_e_api_admin_exibem_modo_sem_expor_telefone(self):
        from services.nowcasting_repository import salvar_snapshot

        telefone = "67987654321"
        os.environ["NOWCASTING_TEST_ALERTS_ENABLED"] = "true"
        os.environ["ADMIN_ALERT_PHONE"] = telefone
        self.assertIsNotNone(salvar_snapshot(self.state_vermelho(), "modo-teste-ui"))
        self.autenticar_admin()

        monitoramento = self.client.get("/admin/monitoramento")
        radar = self.client.get("/admin/radar")
        api = self.client.get("/admin/api/nowcasting/status")
        conteudo = monitoramento.data + radar.data + api.data

        self.assertEqual(monitoramento.status_code, 200)
        self.assertEqual(radar.status_code, 200)
        self.assertEqual(api.status_code, 200)
        self.assertIn("Alertas de teste ao administrador: ATIVADOS".encode(), conteudo)
        self.assertNotIn(telefone.encode(), conteudo)
        test_alert = api.get_json()["test_alert"]
        self.assertTrue(test_alert["enabled"])
        self.assertTrue(test_alert["eligible"])
        self.assertEqual(test_alert["event_key"], "track:12")
        self.assertEqual(
            set(test_alert),
            {
                "enabled", "eligible", "sent_for_current_episode",
                "event_key", "last_sent_at", "cooldown_active",
                "rearm_pending", "reason",
            },
        )

    def test_rotas_publicas_nao_expoem_modo_nem_telefone(self):
        telefone = "67987654321"
        os.environ["NOWCASTING_TEST_ALERTS_ENABLED"] = "true"
        os.environ["ADMIN_ALERT_PHONE"] = telefone
        for path in ("/", "/historico", "/previsao", "/sobre"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b"Alertas de teste ao administrador", response.data)
                self.assertNotIn(telefone.encode(), response.data)

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
