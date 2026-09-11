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
                "distance_km": 22.4,
                "pixels_refletividade_baixa": 800,
                "pixels_refletividade_media": 100,
                "pixels_refletividade_alta": 100,
                "pixels_refletividade_muito_alta": 0,
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
            self.snapshot(would_send=False),
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
                self.assertEqual(status["reason"], "snapshot_stale")

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
                conn.execute(
                    "DELETE FROM health_check_estado WHERE chave=?",
                    (self.service.ESTADO_CHAVE,),
                )
                conn.commit()
                conn.close()

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
        self.assertEqual(status["event_key"], "track:44")

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

    def test_mensagem_inclui_temperatura_rajada_atuais_e_link(self):
        snapshot = self.snapshot()
        snapshot["alerta_preventivo"]["distance_km"] = 20
        mensagem = self.service.montar_mensagem_alerta_teste(snapshot)
        self.assertEqual(mensagem, (
            "⚠️ Possível chuva chegando ao Distrito de São José.\n"
            "Chuva forte a aproximadamente 20 km e se aproximando.\n"
            "A temperatura atual é 27,5 °C.\n"
            "E rajadas de vento atuais de 18,2 km/h.\n\n"
            "Para mais informações acesse: https://meteo.eesjv.com.br"
        ))
        for proibido in ("EE São José", "Escola Estadual São José", "tracking",
                         "frames", "estações regionais", "Estimativa de chegada", "ETA"):
            self.assertNotIn(proibido, mensagem)

    def test_mensagem_novo_formato_nao_inclui_eta(self):
        for eta in (None, 25, -1, float("nan"), float("inf")):
            with self.subTest(eta=eta):
                snapshot = self.snapshot()
                snapshot["alerta_preventivo"]["eta_border_minutes"] = eta
                mensagem = self.service.montar_mensagem_alerta_teste(snapshot)
                self.assertNotIn("min", mensagem)
                self.assertNotIn("ETA", mensagem)
                self.assertIn("e se aproximando.", mensagem)

    def test_mensagem_nao_apresenta_dados_ausentes_ou_desatualizados_como_atuais(self):
        for local in (None, {}, {"stale": True, "temperature": 30, "wind_gust": 80},
                      {"temperature": 30, "wind_gust": 80}):
            with self.subTest(local=local):
                snapshot = self.snapshot()
                snapshot["escola"] = local
                mensagem = self.service.montar_mensagem_alerta_teste(snapshot)
                self.assertIn("Temperatura atual indisponível.", mensagem)
                self.assertIn("Rajadas de vento atuais indisponíveis.", mensagem)
                self.assertNotIn("30", mensagem)
                self.assertNotIn("80", mensagem)

    def test_mensagem_valida_cada_medicao_sem_inventar_zero(self):
        for campo in ("temperature", "wind_gust"):
            for valor in (None, "invalido", float("nan"), float("inf")):
                with self.subTest(campo=campo, valor=valor):
                    snapshot = self.snapshot()
                    snapshot["escola"][campo] = valor
                    mensagem = self.service.montar_mensagem_alerta_teste(snapshot)
                    if campo == "temperature":
                        self.assertIn("Temperatura atual indisponível.", mensagem)
                        self.assertIn("18,2 km/h", mensagem)
                    else:
                        self.assertIn("Rajadas de vento atuais indisponíveis.", mensagem)
                        self.assertIn("27,5 °C", mensagem)
        snapshot = self.snapshot()
        snapshot["escola"].update({"temperature": -2.5, "wind_gust": 0})
        mensagem = self.service.montar_mensagem_alerta_teste(snapshot)
        self.assertIn("-2,5 °C", mensagem)
        self.assertIn("0,0 km/h", mensagem)
        snapshot["escola"]["wind_gust"] = -1
        self.assertIn("Rajadas de vento atuais indisponíveis.",
                      self.service.montar_mensagem_alerta_teste(snapshot))

    def test_requisitos_meteorologicos_bloqueiam_mesmo_vermelho_e_would_send(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        casos = (
            ({"distance_km": 10, "pixels_refletividade_alta": 0}, "intensity_insufficient"),
            ({"distance_km": 10, "pixels_refletividade_baixa": 9999,
              "pixels_refletividade_alta": 0, "pixels_refletividade_muito_alta": 1,
              "classe_maxima": "REFLETIVIDADE_MUITO_ALTA"}, "intensity_insufficient"),
            ({"distance_km": 30}, "distance_not_critical"),
            ({"distance_km": None}, "distance_not_critical"),
            ({"distance_km": float("nan")}, "distance_not_critical"),
            ({"distance_km": -1}, "distance_not_critical"),
            ({"approaching": False}, "not_approaching"),
            ({"trajectory_compatible": False}, "trajectory_incompatible"),
            ({"tracking_valid": False}, "tracking_insufficient"),
            ({"track_id": None}, "tracking_insufficient"),
            ({"clutter_index": 0.75}, "clutter"),
            ({"pixels_refletividade_baixa": None}, "intensity_insufficient"),
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
            "distance_km": 20, "pixels_refletividade_baixa": 970,
            "pixels_refletividade_media": 0, "pixels_refletividade_alta": 0,
            "pixels_refletividade_muito_alta": 30,
            "classe_maxima": "REFLETIVIDADE_MUITO_ALTA",
        })
        sender = mock.Mock()
        self.processar(snapshot, sender=sender)
        sender.assert_called_once()

    def test_log_intensidade_mostra_percentuais(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        snapshot = self.snapshot()
        snapshot["alerta_preventivo"].update({
            "pixels_refletividade_baixa": 826, "pixels_refletividade_media": 100,
            "pixels_refletividade_alta": 66, "pixels_refletividade_muito_alta": 8,
        })
        with self.assertLogs("services.nowcasting_test_alerts", level="INFO") as logs:
            self.processar(snapshot, sender=mock.Mock())
        self.assertIn("intensidade insuficiente forte=7.4% muito_alta=0.8%", "\n".join(logs.output))

    def test_perda_de_intensidade_nao_rearma_episodio_vermelho(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        sender = mock.Mock()
        self.processar(sender=sender)
        for minutos in (5, 65):
            now = self.base + timedelta(minutes=minutos)
            fraco = self.snapshot(now=now)
            fraco["alerta_preventivo"]["pixels_refletividade_alta"] = 0
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
