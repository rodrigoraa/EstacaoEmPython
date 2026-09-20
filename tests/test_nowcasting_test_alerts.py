import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
ESTACAO = ROOT / "estacao"
sys.path.insert(0, str(ESTACAO))


class NowcastingTestAlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(
            {
                "ESTACAO_DB": str(Path(self.tmp.name) / "teste.db"),
                "SECRET_KEY": "teste",
                "NOWCASTING_ALERTS_ENABLED": "false",
                "NOWCASTING_TEST_ALERTS_ENABLED": "false",
            }
        )
        os.environ.pop("ADMIN_ALERT_PHONE", None)

        import database
        from services import nowcasting_test_alerts

        self.database = importlib.reload(database)
        self.database.init_db()
        self.service = importlib.reload(nowcasting_test_alerts)
        self.base = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tmp.cleanup()
        for key in (
            "ESTACAO_DB",
            "SECRET_KEY",
            "ADMIN_ALERT_PHONE",
            "NOWCASTING_ALERTS_ENABLED",
            "NOWCASTING_TEST_ALERTS_ENABLED",
            "NOWCASTING_TEST_ALERT_COOLDOWN_MINUTES",
            "NOWCASTING_TEST_ALERT_REARM_MINUTES",
        ):
            os.environ.pop(key, None)

    def config(self, *, enabled=True, cooldown=60, rearm=30):
        return {
            "test_alerts_enabled": enabled,
            "test_alert_cooldown_minutes": cooldown,
            "test_alert_rearm_minutes": rearm,
            "poll_seconds": 300,
        }

    def snapshot(
        self,
        *,
        level="VERMELHO",
        now=None,
        track_id=27,
        cluster_id=101,
        would_send=True,
        clutter=False,
        stale=False,
        operational=True,
        local_event=False,
        rain_rate=0,
        tracking=True,
    ):
        now = now or self.base
        return {
            "gerado_em_utc": now.isoformat(),
            "evento_local_observado": local_event,
            "escola": {
                "rain_rate": rain_rate, "stale": False,
                "temperature": 27.5, "wind_gust": 18.2,
            },
            "radar": {
                "operacional": operational,
                "stale": stale,
                "frame_id": 70,
            },
            "alerta_preventivo": {
                "nivel": level,
                "would_send": would_send,
                "clutter": clutter,
                "low_confidence": clutter,
                "track_id": track_id,
                "cluster_id": cluster_id,
                "distance_km": 22.4 if level == "VERMELHO" else 180,
                "front_pixels_low": 800,
                "front_pixels_medium": 100,
                "front_pixels_high": 100,
                "front_pixels_very_high": 0,
                "classe_predominante": "REFLETIVIDADE_BAIXA",
                "classe_maxima": "REFLETIVIDADE_ALTA",
                "trajectory_compatible": True,
                "eta_border_quality": "BOA",
                "tracking_valid": tracking,
                "approaching": True,
                "speed_kmh": 45,
                "eta_minutes": 30,
                "eta_border_minutes": 25,
                "regional_confirmation": True,
            },
        }

    def processar(self, snapshot=None, *, now=None, config=None, sender=None):
        return self.service.processar_alerta_teste_admin(
            snapshot or self.snapshot(now=now),
            config or self.config(),
            now=now or self.base,
            sender=sender,
        )

    def estado_persistido(self):
        conn = self.database.get_db()
        try:
            row = conn.execute(
                "SELECT * FROM health_check_estado WHERE chave=?",
                (self.service.ESTADO_CHAVE,),
            ).fetchone()
            return row, json.loads(row["mensagem"]) if row else None
        finally:
            conn.close()

    def assert_sem_fila_preventiva(self):
        conn = self.database.get_db()
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 0
            )
        finally:
            conn.close()

    def test_flag_desabilitada_nao_envia_vermelho(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        status = self.processar(config=self.config(enabled=False), sender=sender)
        sender.assert_not_called()
        self.assertFalse(status["enabled"])

    def test_status_admin_usa_somente_leitura(self):
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = None
        with mock.patch.object(
            self.service.database, "get_db_readonly", return_value=conn
        ):
            status = self.service.obter_status_alerta_teste_admin(
                None, self.config(enabled=False), now=self.base
            )

        self.assertEqual(status["reason"], "disabled")
        sql = conn.execute.call_args.args[0]
        self.assertTrue(sql.lstrip().upper().startswith("SELECT"))
        self.assertNotIn("BEGIN", sql.upper())
        conn.commit.assert_not_called()
        conn.rollback.assert_not_called()
        conn.close.assert_called_once()

    def test_status_admin_falha_de_leitura_retorna_fallback_seguro(self):
        telefone = "67999999999"
        os.environ["ADMIN_ALERT_PHONE"] = telefone
        with (
            mock.patch.object(
                self.service.database,
                "get_db_readonly",
                side_effect=OSError(f"falha privada {telefone}"),
            ),
            self.assertLogs(
                "services.nowcasting_test_alerts", level="WARNING"
            ) as logs,
        ):
            status = self.service.obter_status_alerta_teste_admin(
                self.snapshot(), self.config(), now=self.base
            )

        self.assertEqual(
            status,
            {
                "enabled": True,
                "eligible": False,
                "sent_for_current_episode": False,
                "event_key": None,
                "last_sent_at": None,
                "cooldown_active": False,
                "rearm_pending": False,
                "reason": "status_unavailable",
            },
        )
        self.assertNotIn(telefone, "\n".join(logs.output))

    def test_flag_habilitada_sem_admin_phone_nao_envia_nem_quebra(self):
        sender = mock.Mock()
        status = self.processar(sender=sender)
        sender.assert_not_called()
        self.assertEqual(status["reason"], "admin_phone_missing")

    def test_configuracao_tem_defaults_e_flags_separadas(self):
        from config import nowcasting_config

        os.environ.pop("NOWCASTING_TEST_ALERTS_ENABLED", None)
        os.environ.pop("NOWCASTING_TEST_ALERT_COOLDOWN_MINUTES", None)
        os.environ.pop("NOWCASTING_TEST_ALERT_REARM_MINUTES", None)
        padrao = nowcasting_config()
        self.assertFalse(padrao["test_alerts_enabled"])
        self.assertEqual(padrao["test_alert_cooldown_minutes"], 60)
        self.assertEqual(padrao["test_alert_rearm_minutes"], 30)

        os.environ["NOWCASTING_ALERTS_ENABLED"] = "false"
        os.environ["NOWCASTING_TEST_ALERTS_ENABLED"] = "true"
        os.environ["NOWCASTING_TEST_ALERT_COOLDOWN_MINUTES"] = "75"
        os.environ["NOWCASTING_TEST_ALERT_REARM_MINUTES"] = "45"
        configurado = nowcasting_config()
        self.assertFalse(configurado["alerts_enabled"])
        self.assertTrue(configurado["test_alerts_enabled"])
        self.assertEqual(configurado["test_alert_cooldown_minutes"], 75)
        self.assertEqual(configurado["test_alert_rearm_minutes"], 45)

    def test_vermelho_elegivel_envia_uma_vez_ao_admin_sem_fila(self):
        telefone = "67999999999"
        os.environ["ADMIN_ALERT_PHONE"] = telefone
        sender = mock.Mock()
        status = self.processar(sender=sender)

        sender.assert_called_once()
        self.assertEqual(sender.call_args.args[0], telefone)
        self.assertTrue(status["sent_for_current_episode"])
        self.assertEqual(status["event_key"], "track:27")
        self.assert_sem_fila_preventiva()

        row, estado = self.estado_persistido()
        self.assertEqual(row["chave"], "nowcasting_test_alert")
        self.assertTrue(estado["active"])
        self.assertEqual(estado["last_track_id"], 27)
        self.assertEqual(estado["last_cluster_id"], 101)
        self.assertEqual(estado["last_distance_km"], 22.4)
        self.assertTrue(
            {
                "active", "event_key", "last_level", "last_track_id",
                "last_cluster_id", "last_distance_km", "last_sent_at",
                "last_seen_at", "clear_since", "last_result", "last_error",
            } <= set(estado)
        )
        self.assertNotIn(telefone, row["mensagem"])

    def test_niveis_nao_vermelhos_e_indisponivel_nao_enviam(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        for level in ("NORMAL", "AMARELO", "LARANJA", "INDISPONIVEL"):
            with self.subTest(level=level):
                sender = mock.Mock()
                self.processar(self.snapshot(level=level), sender=sender)
                sender.assert_not_called()

    def test_clutter_diagnostico_nao_envia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        status = self.processar(
            self.snapshot(level="AMARELO", would_send=False, clutter=True),
            sender=sender,
        )
        sender.assert_not_called()
        self.assertEqual(status["reason"], "clutter")

        vermelho_inconsistente = self.snapshot(clutter=False)
        vermelho_inconsistente["alerta_preventivo"]["clutter_index"] = 0.96
        status = self.processar(vermelho_inconsistente, sender=sender)
        sender.assert_not_called()
        self.assertEqual(status["reason"], "clutter")

    def test_vermelho_sem_would_send_ou_radar_operacional_nao_envia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        for snapshot in (
            self.snapshot(operational=False),
            self.snapshot(operational=False),
        ):
            with self.subTest(snapshot=snapshot):
                sender = mock.Mock()
                self.processar(snapshot, sender=sender)
                sender.assert_not_called()

    def test_vermelho_stale_ou_snapshot_velho_nao_envia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        casos = (
            (self.snapshot(stale=True), self.base),
            (self.snapshot(now=self.base), self.base + timedelta(minutes=11)),
        )
        for snapshot, now in casos:
            with self.subTest(snapshot=snapshot["radar"], now=now):
                sender = mock.Mock()
                status = self.processar(snapshot, now=now, sender=sender)
                sender.assert_not_called()
                self.assertEqual(status["reason"], "radar_stale" if snapshot["radar"]["stale"] else "snapshot_stale")

    def test_evento_local_ou_chuva_atual_suprime_todo_o_episodio(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        for campo in ("evento", "chuva"):
            with self.subTest(campo=campo):
                sender = mock.Mock()
                snapshot = self.snapshot(
                    local_event=campo == "evento", rain_rate=1.2 if campo == "chuva" else 0
                )
                status = self.processar(snapshot, sender=sender)
                sender.assert_not_called()
                self.assertEqual(status["reason"], "local_event_observed")
                _, estado = self.estado_persistido()
                self.assertTrue(estado["suppressed_for_current_episode"])

                seco = self.snapshot(now=self.base + timedelta(minutes=5))
                self.processar(seco, now=self.base + timedelta(minutes=5), sender=sender)
                sender.assert_not_called()
                conn = self.database.get_db()
                self.service.salvar_estado_alerta_teste(conn, self.service.estado_alerta_teste_padrao())
                conn.commit()
                conn.close()

    def test_chuva_local_exige_leitura_atual_na_avaliacao(self):
        casos = (
            (True, None, True),
            (True, {"rain_rate": 1, "stale": True}, True),
            (False, {"rain_rate": 1, "stale": False}, True),
            (False, {"rain_rate": 1, "stale": True}, False),
            (False, {"rain_rate": 0, "stale": False}, False),
            (False, {"rain_rate": None, "stale": False}, False),
            (False, {"rain_rate": "invalido", "stale": False}, False),
            (False, {"rain_rate": float("nan"), "stale": False}, False),
            (False, {"rain_rate": float("inf"), "stale": False}, False),
            (False, {"rain_rate": -1, "stale": False}, False),
            (False, {"rain_rate": True, "stale": False}, False),
            (False, None, False),
            (False, {}, False),
            (False, {"rain_rate": 1}, False),
            (False, {"rain_rate": 1, "stale": None}, False),
        )
        from config import nowcasting_config
        from services.nowcasting_service import analisar_nowcasting

        for flag, local, observado in casos:
            with self.subTest(flag=flag, local=local):
                snapshot = self.snapshot(local_event=flag, tracking=False, track_id=None)
                snapshot["escola"] = local
                if local is None:
                    snapshot.pop("escola")
                self.assertEqual(self.service._evento_local_observado(snapshot), observado)
                avaliacao = self.service.avaliar_alerta_teste_admin(
                    snapshot, self.config(), admin_phone="67999999999", now=self.base
                )
                self.assertEqual(avaliacao["eligible"], not observado)
                self.assertEqual(avaliacao["reason"], "local_event_observed" if observado else "eligible")
                if not flag:
                    radar = {"disponivel": True, "stale": False, "frame": {"id": 70},
                             "cluster_mais_proximo": {**snapshot["alerta_preventivo"],
                                                     "id": 101, "distancia_borda_escola_km": 20}}
                    criado = analisar_nowcasting(radar, {"stations": []}, local, nowcasting_config(), now=self.base)
                    self.assertEqual(criado["evento_local_observado"], observado)
                    self.assertEqual(criado["alerta_preventivo"]["local_event"], observado)
                    self.assertEqual(self.service._evento_local_observado(criado), observado)

    def test_regressao_chuva_local_stale_nao_suprime_medium_por_proximidade(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        snapshot = self.snapshot(tracking=False, track_id=None, rain_rate=2)
        snapshot["escola"]["stale"] = True
        snapshot["alerta_preventivo"].update(
            front_pixels_low=0, front_pixels_medium=100, front_pixels_high=0,
        )
        avaliacao = self.service.avaliar_alerta_teste_admin(
            snapshot, self.config(), admin_phone="67999999999", now=self.base
        )
        self.assertEqual(avaliacao["decision"]["authorization"], "PROXIMIDADE")
        self.assertEqual(avaliacao["decision"]["radar_intensity"], "MEDIUM")
        sender = mock.Mock()
        status = self.processar(snapshot, sender=sender)
        sender.assert_called_once()
        self.assertEqual(status["reason"], "sent")
        _, estado = self.estado_persistido()
        self.assertFalse(estado["suppressed_for_current_episode"])
        self.assertEqual(estado["last_sent_alert_level"], "INFORMATIVO")
        self.assert_sem_fila_preventiva()

    def test_mesmo_episodio_e_reinicio_nao_reenviam(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        self.processar(
            self.snapshot(now=self.base + timedelta(minutes=5)),
            now=self.base + timedelta(minutes=5),
            sender=sender,
        )
        self.service = importlib.reload(self.service)
        self.processar(
            self.snapshot(now=self.base + timedelta(minutes=10)),
            now=self.base + timedelta(minutes=10),
            sender=sender,
        )
        self.assertEqual(sender.call_count, 1)

    def test_volta_antes_do_rearm_nao_envia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        saida = self.base + timedelta(minutes=10)
        self.processar(
            self.snapshot(level="LARANJA", now=saida), now=saida, sender=sender
        )
        retorno = self.base + timedelta(minutes=35)
        status = self.processar(
            self.snapshot(now=retorno), now=retorno, sender=sender
        )
        self.assertEqual(sender.call_count, 1)
        self.assertTrue(status["sent_for_current_episode"])

    def test_rearm_completo_e_cooldown_permite_novo_envio(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        saida = self.base + timedelta(minutes=10)
        self.processar(
            self.snapshot(level="LARANJA", now=saida), now=saida, sender=sender
        )
        rearmado = self.base + timedelta(minutes=41)
        status = self.processar(
            self.snapshot(level="NORMAL", now=rearmado),
            now=rearmado,
            sender=sender,
        )
        self.assertFalse(status["rearm_pending"])

        novo = self.base + timedelta(minutes=61)
        status = self.processar(
            self.snapshot(now=novo, track_id=88, cluster_id=202),
            now=novo,
            sender=sender,
        )
        self.assertEqual(sender.call_count, 2)
        self.assertEqual(status["event_key"], "track:88")

    def test_rearm_sem_fim_do_cooldown_ainda_bloqueia(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        saida = self.base + timedelta(minutes=1)
        self.processar(
            self.snapshot(level="NORMAL", now=saida), now=saida, sender=sender
        )
        rearmado = self.base + timedelta(minutes=32)
        self.processar(
            self.snapshot(level="NORMAL", now=rearmado), now=rearmado, sender=sender
        )
        novo = self.base + timedelta(minutes=40)
        status = self.processar(
            self.snapshot(now=novo, track_id=99), now=novo, sender=sender
        )
        self.assertEqual(sender.call_count, 1)
        self.assertTrue(status["cooldown_active"])

    def test_sem_track_troca_cluster_e_depois_track_nao_gera_spam(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(self.snapshot(track_id=None, cluster_id=1), sender=sender)
        cinco = self.base + timedelta(minutes=5)
        self.processar(
            self.snapshot(now=cinco, track_id=None, cluster_id=2),
            now=cinco,
            sender=sender,
        )
        dez = self.base + timedelta(minutes=10)
        status = self.processar(
            self.snapshot(now=dez, track_id=44, cluster_id=3),
            now=dez,
            sender=sender,
        )
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(status["event_key"], "untracked_rain_episode")

    def test_track_estavel_envia_somente_uma_vez(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        for minutos in (0, 5, 10):
            now = self.base + timedelta(minutes=minutos)
            self.processar(
                self.snapshot(now=now, track_id=27), now=now, sender=sender
            )
        self.assertEqual(sender.call_count, 1)

    def test_falha_nao_derruba_worker_e_retry_respeita_cooldown(self):
        telefone = "67999999999"
        os.environ["ADMIN_ALERT_PHONE"] = telefone
        falhar = mock.Mock(side_effect=RuntimeError(f"falha para {telefone}"))
        with self.assertLogs(
            "services.nowcasting_test_alerts", level="ERROR"
        ) as logs:
            status = self.processar(sender=falhar)
        self.assertEqual(status["reason"], "send_failed")
        self.assertNotIn(telefone, "\n".join(logs.output))
        _, estado = self.estado_persistido()
        self.assertEqual(estado["last_error"], "RuntimeError")
        self.assertNotIn(telefone, json.dumps(estado))

        cedo = self.base + timedelta(minutes=5)
        self.processar(
            self.snapshot(now=cedo), now=cedo, sender=falhar
        )
        self.assertEqual(falhar.call_count, 1)

        ainda_cedo = self.base + timedelta(minutes=10)
        self.processar(
            self.snapshot(now=ainda_cedo), now=ainda_cedo, sender=falhar
        )
        self.assertEqual(falhar.call_count, 1)

        sucesso = mock.Mock()
        tarde = self.base + timedelta(minutes=61)
        status = self.processar(
            self.snapshot(now=tarde), now=tarde, sender=sucesso
        )
        sucesso.assert_called_once()
        self.assertTrue(status["sent_for_current_episode"])

    def test_mensagem_informativa_exata_com_eta_confiavel(self):
        snapshot = self.snapshot()
        snapshot["alerta_preventivo"].update(distance_km=20, alert_level="INFORMATIVO")
        self.assertEqual(self.service.montar_mensagem_alerta_teste(snapshot), (
            "🌧️ Possível chuva se aproximando do Distrito de São José.\n\n"
            "Uma área de chuva está a aproximadamente 20 km da região.\n\n"
            "Estimativa de chegada: 25 min.\n\n"
            "Para mais informações acesse:\nhttps://meteo.eesjv.com.br"
        ))

    def test_mensagem_independe_de_temperatura_e_rajada(self):
        snapshot = self.snapshot()
        esperado = self.service.montar_mensagem_alerta_teste(snapshot)
        for local in (None, {}, {"stale": True, "temperature": 30, "wind_gust": 80}):
            snapshot["escola"] = local
            self.assertEqual(self.service.montar_mensagem_alerta_teste(snapshot), esperado)

    def test_requisitos_meteorologicos_bloqueiam_mesmo_vermelho_e_would_send(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        casos = (
            ({"distance_km": 10, "front_pixels_medium": 0, "front_pixels_high": 0}, "intensity_below_medium"),
            ({"distance_km": 10, "front_pixels_low": 9999,
              "front_pixels_medium": 0, "front_pixels_high": 0, "front_pixels_very_high": 1,
              "classe_maxima": "REFLETIVIDADE_MUITO_ALTA"}, "intensity_below_medium"),
            ({"distance_km": 80}, "outside_proximity_range"),
            ({"distance_km": None}, "inconsistent_data"),
            ({"distance_km": float("nan")}, "inconsistent_data"),
            ({"distance_km": -1}, "inconsistent_data"),
            ({"distance_km": 60, "approaching": False}, "not_approaching"),
            ({"distance_km": 60, "trajectory_compatible": False}, "trajectory_incompatible"),
            ({"distance_km": 60, "tracking_valid": False}, "tracking_insufficient_for_early_warning"),
            ({"distance_km": 60, "track_id": None}, "tracking_insufficient_for_early_warning"),
            ({"clutter_index": 0.75}, "clutter"),
            ({"front_pixels_low": None}, "inconsistent_data"),
        )
        for alteracoes, motivo in casos:
            with self.subTest(alteracoes=alteracoes):
                snapshot = self.snapshot()
                snapshot["alerta_preventivo"].update({"distance_km": 20, **alteracoes})
                sender = mock.Mock()
                with self.assertLogs("services.nowcasting_test_alerts", level="INFO") as logs:
                    status = self.processar(snapshot, sender=sender)
                sender.assert_not_called()
                self.assertEqual(status["reason"], motivo)
                self.assertNotIn(os.environ["ADMIN_ALERT_PHONE"], "\n".join(logs.output))
        self.assert_sem_fila_preventiva()

    def test_escalonamento_reinicio_rearm_e_isolamento_sql(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        sqls = []
        get_db = self.database.get_db

        def conexao_monitorada():
            conn = get_db()
            conn.set_trace_callback(sqls.append)
            return conn

        with mock.patch.object(self.database, "get_db", side_effect=conexao_monitorada):
            for minuto, classe, esperados in ((0, "medium", 1), (1, "medium", 1),
                                              (2, "high", 2), (3, "high", 2),
                                              (4, "very_high", 3), (5, "very_high", 3),
                                              (6, "high", 3), (7, "medium", 3)):
                now = self.base + timedelta(minutes=minuto)
                snapshot = self.snapshot(now=now, tracking=False, track_id=None)
                alerta = snapshot["alerta_preventivo"]
                for campo in ("low", "medium", "high", "very_high"):
                    alerta[f"front_pixels_{campo}"] = 100 if campo == classe else 0
                self.processar(snapshot, now=now, sender=sender)
                self.assertEqual(sender.call_count, esperados)
                self.service = importlib.reload(self.service)
        _, estado = self.estado_persistido()
        self.assertEqual(estado["highest_sent_severity"], 3)
        self.assertEqual(estado["last_sent_alert_level"], "ALERTA")
        self.assertEqual(estado["last_sent_radar_intensity"], "VERY_HIGH")
        for sql in sqls:
            for proibido in ("usuarios", "alertas_fila", "alertas_eventos"):
                self.assertNotIn(proibido, sql.lower())
        self.assert_sem_fila_preventiva()
        for minuto in (10, 41):
            now = self.base + timedelta(minutes=minuto)
            self.processar(self.snapshot(level="NORMAL", now=now), now=now, sender=sender)
        _, estado = self.estado_persistido()
        self.assertEqual(estado["highest_sent_severity"], 0)
        self.assertIsNone(estado["last_sent_alert_level"])
        self.assertIsNone(estado["last_sent_radar_intensity"])
        now = self.base + timedelta(minutes=70)
        snapshot = self.snapshot(now=now)
        snapshot["alerta_preventivo"].update(front_pixels_low=0, front_pixels_medium=100, front_pixels_high=0)
        self.processar(snapshot, now=now, sender=sender)
        self.assertEqual(sender.call_count, 4)
        for chamada in sender.call_args_list:
            self.assertEqual(chamada.args[0], os.environ["ADMIN_ALERT_PHONE"])

    def test_estado_antigo_e_snapshot_sem_frente_sao_compativeis(self):
        self.assertEqual(self.service._normalizar_estado({})["highest_sent_severity"], 0)
        self.assertEqual(self.service._normalizar_estado({"sent_for_current_episode": True})["highest_sent_severity"], 3)
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        snapshot = self.snapshot()
        for campo in list(snapshot["alerta_preventivo"]):
            if campo.startswith("front_"):
                snapshot["alerta_preventivo"].pop(campo)
        sender = mock.Mock()
        self.assertEqual(self.processar(snapshot, sender=sender)["reason"], "inconsistent_data")
        sender.assert_not_called()

    def test_falha_no_escalonamento_preserva_maximo_e_cooldown(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        self.processar(sender=mock.Mock())
        _, estado = self.estado_persistido()
        self.assertEqual(estado["highest_sent_severity"], 2)
        erro = mock.Mock(side_effect=RuntimeError("indisponivel"))
        for minutos in (1, 2, 3):
            now = self.base + timedelta(minutes=minutos)
            snapshot = self.snapshot(now=now)
            snapshot["alerta_preventivo"].update(front_pixels_very_high=100)
            self.processar(snapshot, now=now, sender=erro)
        self.assertEqual(erro.call_count, 1)
        _, estado = self.estado_persistido()
        self.assertTrue(estado["sent_for_current_episode"])
        self.assertEqual(estado["highest_sent_severity"], 2)

    def test_chuva_local_durante_radar_stale_tambem_suprime_episodio(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(self.snapshot(stale=True, rain_rate=1), sender=sender)
        self.processar(self.snapshot(), sender=sender)
        sender.assert_not_called()
        self.assertTrue(self.estado_persistido()[1]["suppressed_for_current_episode"])

    def test_intensidade_suficiente_envia_a_20_km(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        snapshot = self.snapshot()
        snapshot["alerta_preventivo"]["distance_km"] = 20
        sender = mock.Mock()
        self.processar(snapshot, sender=sender)
        sender.assert_called_once()

    def test_muito_alta_acima_do_minimo_envia_sem_10_porcento_forte(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        snapshot = self.snapshot()
        snapshot["alerta_preventivo"].update({
            "distance_km": 20, "front_pixels_low": 970,
            "front_pixels_medium": 0, "front_pixels_high": 0,
            "front_pixels_very_high": 30,
            "classe_maxima": "REFLETIVIDADE_MUITO_ALTA",
        })
        sender = mock.Mock()
        self.processar(snapshot, sender=sender)
        sender.assert_called_once()

    def test_log_intensidade_mostra_percentuais(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        snapshot = self.snapshot()
        snapshot["alerta_preventivo"].update({
            "front_pixels_low": 826, "front_pixels_medium": 100,
            "front_pixels_high": 66, "front_pixels_very_high": 8,
        })
        with self.assertLogs("services.nowcasting_test_alerts", level="INFO") as logs:
            self.processar(snapshot, sender=mock.Mock())
        self.assertIn("front_percent_strong", "\n".join(logs.output))
        self.assertIn("7.4", "\n".join(logs.output))

    def test_reducao_para_medium_nao_reenvia_mesmo_episodio(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        for minutos in (5, 65):
            now = self.base + timedelta(minutes=minutos)
            fraco = self.snapshot(now=now)
            fraco["alerta_preventivo"].update(front_pixels_high=0, front_pixels_medium=200)
            self.processar(fraco, now=now, sender=sender)
        now = self.base + timedelta(minutes=70)
        self.processar(self.snapshot(now=now), now=now, sender=sender)
        sender.assert_called_once()

    def test_servico_admin_reutiliza_normalizacao_e_whatsapp_existentes(self):
        from services.admin_notification_service import enviar_mensagem_admin

        with mock.patch("services.whatsapp_service.enviar_whatsapp") as enviar:
            enviar_mensagem_admin("(67) 99999-9999", "teste")
        enviar.assert_called_once_with("5567999999999", "teste")


if __name__ == "__main__":
    unittest.main()
