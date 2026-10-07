import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class WhatsAppSenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "estacao_teste.db")
        os.environ["EVOLUTION_URL"] = "http://localhost"
        os.environ["EVOLUTION_API_KEY"] = "fake"
        os.environ["EVOLUTION_INSTANCE"] = "fake"
        os.environ["SECRET_KEY"] = "segredo-teste"

        import database

        self.database = importlib.reload(database)
        self.database.init_db()

        import workers.whatsapp_sender

        self.sender = importlib.reload(workers.whatsapp_sender)
        self.sender.log = lambda mensagem: None
        self.now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        self.clock = mock.patch.object(self.sender, "agora_utc", return_value=self.now)
        self.agora_mock = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.nowcasting_env = mock.patch.dict(os.environ, {
            "NOWCASTING_ENABLED": "true", "NOWCASTING_ALERTS_ENABLED": "true",
            "NOWCASTING_ALERT_DELIVERY_MAX_AGE_MINUTES": "15",
        })
        self.nowcasting_env.start()
        self.addCleanup(self.nowcasting_env.stop)
        self.previsoes = 0

    def tearDown(self):
        self.tmp.cleanup()
        for chave in (
            "ESTACAO_DB",
            "EVOLUTION_URL",
            "EVOLUTION_API_KEY",
            "EVOLUTION_INSTANCE",
            "SECRET_KEY",
        ):
            os.environ.pop(chave, None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def enfileirar(self, usuarios):
        conn = self.abrir_banco()
        conn.executemany(
            """
            INSERT INTO alertas_fila (
                usuario_id,
                nome,
                telefone,
                mensagem,
                status
            ) VALUES (?, ?, ?, ?, 'pendente')
            """,
            usuarios,
        )
        conn.commit()
        conn.close()

    def snapshot(self):
        return {
            "gerado_em_utc": self.now.isoformat(),
            "escola": {"rain_rate": 0, "stale": False},
            "radar": {"stale": False, "operacional": True, "frame_id": 70,
                      "data_frame": self.now.isoformat()},
            "alerta_preventivo": {
                "nivel": "LARANJA", "distance_km": 60,
                "front_pixels_low": 0, "front_pixels_medium": 0,
                "front_pixels_high": 100, "front_pixels_very_high": 0,
                "track_id": 27, "tracking_valid": True, "approaching": True,
                "trajectory_compatible": True, "trajectory_frames_used": 5,
                "trajectory_duration_minutes": 20,
                "trajectory_method": "linear_xy_6_pixel_runs",
                "trajectory_confidence": "ALTA", "projected_impact": True,
                "projected_impact_eta_minutes": 40,
                "projected_impact_min_distance_km": 3,
                "projected_impact_horizon_minutes": 120,
                "clutter": False,
            },
        }

    def enfileirar_previsao(self, *, idade_minutos=0, nivel=2, status="pendente"):
        from services.nowcasting_public_alerts import estado_padrao, salvar_estado
        from time_utils import iso_local

        self.previsoes += 1
        episode_id = f"{self.previsoes:032x}"
        nivel_texto = {1: "INFORMATIVO", 2: "ATENCAO", 3: "ALERTA"}[nivel]
        evento_id = f"nowcasting:{episode_id}:{nivel_texto.lower()}"
        momento = self.now - timedelta(minutes=idade_minutos)
        conn = self.abrir_banco()
        conn.execute(
            """INSERT INTO alertas_eventos
                (evento_id, data_referencia, tipo, nivel, ocorrido_em_local,
                 status, enfileirados, destinatarios)
                VALUES (?, '2026-10-03', 'nowcasting_radar', ?, ?, 'enfileirado', 1, 1)""",
            (evento_id, nivel, iso_local(momento)),
        )
        conn.execute(
            """INSERT INTO alertas_fila
                (usuario_id, nome, telefone, mensagem, evento_id, status)
                VALUES (1, 'Maria', '5567999999999', 'Previsao de chuva', ?, ?)""",
            (evento_id, status),
        )
        estado = {**estado_padrao(), "active": True, "episode_id": episode_id,
                  "episode_started_at": momento.isoformat(),
                  "highest_enqueued_severity": nivel,
                  "last_enqueued_alert_level": nivel_texto,
                  "last_enqueued_radar_intensity": {1: "MEDIUM", 2: "HIGH", 3: "VERY_HIGH"}[nivel],
                  "last_enqueued_at": momento.isoformat(),
                  "pending_tracking_track_id": 27, "pending_tracking_frame_id": 70,
                  "pending_tracking_count": 2,
                  "pending_tracking_observed_at": self.now.isoformat()}
        salvar_estado(conn, estado)
        conn.execute(
            """INSERT INTO nowcasting_snapshots
                (calculado_em_utc, calculado_em_local, status, nivel_evidencia,
                 indice_evidencia, estacoes_relevantes_json, evidencias_json,
                 dados_escola_json, estado_json, input_fingerprint, versao_algoritmo)
                VALUES (?, ?, 'SISTEMA_SE_APROXIMANDO', 'ELEVADA', 50,
                        '[]', '[]', '{}', ?, ?, '1.7')""",
            (self.now.isoformat(), iso_local(self.now), json.dumps(self.snapshot()), evento_id),
        )
        conn.commit()
        conn.close()
        return evento_id

    def atualizar_snapshot(self, snapshot):
        conn = self.abrir_banco()
        conn.execute("UPDATE nowcasting_snapshots SET estado_json=?", (json.dumps(snapshot),))
        conn.commit()
        conn.close()

    def assert_previsao_cancelada(self, *, retry_failed=False, motivo=None):
        with mock.patch.object(self.sender, "enviar_whatsapp") as enviar:
            self.assertEqual(self.sender.processar_um_envio(retry_failed=retry_failed), "cancelado")
            enviar.assert_not_called()
        conn = self.abrir_banco()
        fila = conn.execute("SELECT * FROM alertas_fila ORDER BY id DESC LIMIT 1").fetchone()
        historico = conn.execute("SELECT * FROM alertas_envios ORDER BY id DESC LIMIT 1").fetchone()
        evento = conn.execute("SELECT status FROM alertas_eventos WHERE evento_id=?",
                              (fila["evento_id"],)).fetchone()
        conn.close()
        self.assertEqual(fila["status"], "cancelado")
        self.assertIsNone(fila["proxima_tentativa_em"])
        self.assertIsNone(fila["enviado_em"])
        self.assertEqual(historico["status"], "cancelado")
        self.assertEqual(historico["erro"], fila["erro"])
        if evento:
            self.assertEqual(evento["status"], "cancelado")
        if motivo:
            self.assertIn(motivo, fila["erro"])

    def test_processa_item_pendente_e_registra_historico(self):
        self.enfileirar([(1, "Maria", "5567999999999", "Alerta de teste")])
        envios = []
        self.sender.enviar_whatsapp = lambda numero, mensagem: envios.append(
            (numero, mensagem)
        )

        resultado = self.sender.processar_fila(limite=1, intervalo=0)

        self.assertEqual(resultado, {"processados": 1, "enviados": 1, "falhas": 0})
        self.assertEqual(envios, [("5567999999999", "Alerta de teste")])

        conn = self.abrir_banco()
        fila = conn.execute(
            "SELECT status, tentativas, erro, enviado_em FROM alertas_fila"
        ).fetchone()
        envio = conn.execute(
            "SELECT nome, telefone, status, mensagem, erro FROM alertas_envios"
        ).fetchone()
        conn.close()

        self.assertEqual(fila["status"], "enviado")
        self.assertEqual(fila["tentativas"], 1)
        self.assertIsNone(fila["erro"])
        self.assertIsNotNone(fila["enviado_em"])
        self.assertEqual(envio["nome"], "Maria")
        self.assertEqual(envio["telefone"], "5567999999999")
        self.assertEqual(envio["status"], "enviado")
        self.assertEqual(envio["mensagem"], "Alerta de teste")
        self.assertIsNone(envio["erro"])

    def test_processa_fila_respeita_intervalo_entre_envios(self):
        self.enfileirar(
            [
                (1, "Maria", "5567999999999", "Alerta 1"),
                (2, "Joao", "5567888888888", "Alerta 2"),
            ]
        )
        envios = []
        pausas = []
        self.sender.enviar_whatsapp = lambda numero, mensagem: envios.append(
            (numero, mensagem)
        )
        self.sender.time.sleep = lambda segundos: pausas.append(segundos)

        resultado = self.sender.processar_fila(limite=2, intervalo=20)

        self.assertEqual(resultado, {"processados": 2, "enviados": 2, "falhas": 0})
        self.assertEqual(len(envios), 2)
        self.assertEqual(pausas, [20])

    def test_falha_da_evolution_fica_na_fila_e_no_historico(self):
        self.enfileirar([(1, "Maria", "5567999999999", "Alerta de teste")])

        def falhar(numero, mensagem):
            raise Exception("Evolution fora")

        self.sender.enviar_whatsapp = falhar

        resultado = self.sender.processar_fila(limite=1, intervalo=0)

        self.assertEqual(resultado, {"processados": 1, "enviados": 0, "falhas": 1})

        conn = self.abrir_banco()
        fila = conn.execute("SELECT status, tentativas, erro FROM alertas_fila").fetchone()
        envio = conn.execute("SELECT status, erro FROM alertas_envios").fetchone()
        conn.close()

        self.assertEqual(fila["status"], "pendente")
        self.assertEqual(fila["tentativas"], 1)
        self.assertIn("Evolution fora", fila["erro"])
        self.assertEqual(envio["status"], "falhou")
        self.assertIn("Evolution fora", envio["erro"])

    def test_falha_permanente_nao_e_retentada(self):
        self.enfileirar([(1, "Maria", "5567999999999", "Alerta de teste")])
        self.sender.enviar_whatsapp = lambda numero, mensagem: (_ for _ in ()).throw(
            Exception("Erro Evolution API 400: telefone inválido")
        )

        self.sender.processar_fila(limite=1, intervalo=0)

        conn = self.abrir_banco()
        fila = conn.execute(
            "SELECT status, erro_permanente, proxima_tentativa_em FROM alertas_fila"
        ).fetchone()
        conn.close()
        self.assertEqual(fila["status"], "falhou")
        self.assertEqual(fila["erro_permanente"], 1)
        self.assertIsNone(fila["proxima_tentativa_em"])

    def test_prioridade_critica_e_processada_primeiro(self):
        self.enfileirar(
            [
                (1, "Normal", "5567111111111", "Normal"),
                (2, "Critico", "5567222222222", "Critico"),
            ]
        )
        conn = self.abrir_banco()
        conn.execute("UPDATE alertas_fila SET prioridade = 100 WHERE nome = 'Critico'")
        conn.commit()
        conn.close()
        envios = []
        self.sender.enviar_whatsapp = lambda numero, mensagem: envios.append(mensagem)

        self.sender.processar_fila(limite=1, intervalo=0)

        self.assertEqual(envios, ["Critico"])

    def test_previsao_expirada_e_cancelada_sem_envio_ou_retentativa(self):
        self.enfileirar_previsao(idade_minutos=16)
        self.assert_previsao_cancelada(motivo="expirada")
        with mock.patch.object(self.sender, "enviar_whatsapp") as enviar:
            self.assertIsNone(self.sender.processar_um_envio(retry_failed=True))
            enviar.assert_not_called()

    def test_previsao_sem_horario_valido_ou_futuro_e_cancelada(self):
        for horario in (None, "invalido", (self.now + timedelta(minutes=2)).isoformat()):
            with self.subTest(horario=horario):
                evento_id = self.enfileirar_previsao()
                conn = self.abrir_banco()
                conn.execute("UPDATE alertas_eventos SET ocorrido_em_local=? WHERE evento_id=?",
                             (horario, evento_id))
                conn.commit()
                conn.close()
                self.assert_previsao_cancelada()

    def test_previsao_orfa_e_cancelada_sem_imitar_alerta_generico(self):
        evento_id = self.enfileirar_previsao()
        conn = self.abrir_banco()
        conn.execute("DELETE FROM alertas_eventos WHERE evento_id=?", (evento_id,))
        conn.commit()
        conn.close()
        self.assert_previsao_cancelada(motivo="metadados")

    def test_previsao_atual_com_impacto_confirmado_pode_ser_enviada(self):
        self.enfileirar_previsao(idade_minutos=5)
        snapshot = self.snapshot()
        snapshot["alerta_preventivo"]["projected_impact_eta_minutes"] = 25
        self.atualizar_snapshot(snapshot)
        with mock.patch.object(self.sender, "enviar_whatsapp") as enviar:
            self.assertEqual(self.sender.processar_um_envio(), "enviado")
            enviar.assert_called_once()
            numero, mensagem = enviar.call_args.args
            self.assertEqual(numero, "5567999999999")
            self.assertTrue(mensagem.startswith("ATENÇÃO, Maria,\n"))
            self.assertIn("Estimativa de chegada: 25 min.", mensagem)
            self.assertNotIn("40 min.", mensagem)
        conn = self.abrir_banco()
        fila = conn.execute("SELECT mensagem FROM alertas_fila").fetchone()
        historico = conn.execute("SELECT mensagem FROM alertas_envios").fetchone()
        conn.close()
        self.assertEqual(fila["mensagem"], mensagem)
        self.assertEqual(historico["mensagem"], mensagem)

    def test_ttl_e_configuravel_e_horario_local_sem_offset_e_interpretado_corretamente(self):
        evento_id = self.enfileirar_previsao(idade_minutos=20)
        conn = self.abrir_banco()
        conn.execute("UPDATE alertas_eventos SET ocorrido_em_local=? WHERE evento_id=?",
                     ("2026-10-03 07:40:00", evento_id))
        conn.commit()
        conn.close()
        with (mock.patch.dict(os.environ, {"NOWCASTING_ALERT_DELIVERY_MAX_AGE_MINUTES": "30"}),
              mock.patch.object(self.sender, "enviar_whatsapp") as enviar):
            self.assertEqual(self.sender.processar_um_envio(), "enviado")
            enviar.assert_called_once()

    def test_limites_inclusivos_de_ttl_e_tolerancia_de_relogio(self):
        for idade in (15, -1):
            with self.subTest(idade=idade):
                self.enfileirar_previsao(idade_minutos=idade)
                with mock.patch.object(self.sender, "enviar_whatsapp") as enviar:
                    self.assertEqual(self.sender.processar_um_envio(), "enviado")
                    enviar.assert_called_once()

    def test_cancelamento_nao_e_contado_como_falha_de_rede(self):
        self.enfileirar_previsao(idade_minutos=16)
        with mock.patch.object(self.sender, "enviar_whatsapp") as enviar:
            resultado = self.sender.processar_fila(limite=1, intervalo=0)
            enviar.assert_not_called()
        self.assertEqual(resultado, {"processados": 1, "enviados": 0, "falhas": 0})

    def test_evento_parcialmente_entregue_registra_cancelamento_dos_restantes(self):
        evento_id = self.enfileirar_previsao(idade_minutos=16)
        self.enfileirar([(2, "Joao", "5567888888888", "Previsao antiga")])
        conn = self.abrir_banco()
        conn.execute("UPDATE alertas_fila SET evento_id=?, status='enviado' WHERE usuario_id=2",
                     (evento_id,))
        conn.commit()
        conn.close()
        with mock.patch.object(self.sender, "enviar_whatsapp") as enviar:
            self.assertEqual(self.sender.processar_um_envio(), "cancelado")
            enviar.assert_not_called()
        conn = self.abrir_banco()
        evento = conn.execute("SELECT status, enviados, falhas FROM alertas_eventos").fetchone()
        conn.close()
        self.assertEqual(dict(evento), {"status": "concluido_com_cancelamentos", "enviados": 1, "falhas": 0})

    def test_retentativa_e_retomada_de_envio_revalidam_idade(self):
        for status in ("pendente", "falhou", "enviando"):
            with self.subTest(status=status):
                evento_id = self.enfileirar_previsao(idade_minutos=16, status=status)
                conn = self.abrir_banco()
                conn.execute("UPDATE alertas_fila SET atualizado_em=datetime('now', '-1 hour') "
                             "WHERE evento_id=?", (evento_id,))
                conn.commit()
                conn.close()
                self.assert_previsao_cancelada(retry_failed=True, motivo="expirada")

    def test_retentativa_de_falha_temporaria_nao_envia_previsao_que_expirou(self):
        self.enfileirar_previsao()
        with mock.patch.object(self.sender, "enviar_whatsapp", side_effect=RuntimeError("offline")):
            self.assertEqual(self.sender.processar_um_envio(), "falhou")
        self.agora_mock.return_value = self.now + timedelta(minutes=16)
        conn = self.abrir_banco()
        conn.execute("UPDATE alertas_fila SET proxima_tentativa_em=NULL")
        conn.commit()
        conn.close()
        self.assert_previsao_cancelada(motivo="expirada")

    def test_desativacao_no_painel_cancela_previsao_ja_enfileirada(self):
        from services.runtime_alert_controls import salvar_controle
        self.enfileirar_previsao()
        salvar_controle("public", False)
        self.assert_previsao_cancelada(motivo="desativados")

    def test_previsao_sem_snapshot_valido_e_cancelada(self):
        for snapshot in (None, {}, {"radar": []}):
            with self.subTest(snapshot=snapshot):
                self.enfileirar_previsao()
                self.atualizar_snapshot(snapshot)
                self.assert_previsao_cancelada()

    def test_snapshot_corrompido_ou_desatualizado_cancela_previsao(self):
        self.enfileirar_previsao()
        conn = self.abrir_banco()
        conn.execute("UPDATE nowcasting_snapshots SET estado_json='{json-invalido'")
        conn.commit()
        conn.close()
        self.assert_previsao_cancelada(motivo="indisponivel")
        self.enfileirar_previsao()
        snapshot = self.snapshot()
        snapshot["gerado_em_utc"] = (self.now - timedelta(minutes=11)).isoformat()
        self.atualizar_snapshot(snapshot)
        self.assert_previsao_cancelada(motivo="snapshot_stale")

    def test_perda_da_trajetoria_ou_interceptacao_cancela_previsao(self):
        for campo, valor in (("approaching", False), ("trajectory_compatible", False),
                             ("projected_impact", False), ("trajectory_frames_used", 3)):
            with self.subTest(campo=campo):
                self.enfileirar_previsao()
                snapshot = self.snapshot()
                snapshot["alerta_preventivo"][campo] = valor
                self.atualizar_snapshot(snapshot)
                self.assert_previsao_cancelada(motivo="revalidacao")

    def test_intensidade_reduzida_cancela_texto_mais_severo(self):
        self.enfileirar_previsao(nivel=3)
        self.assert_previsao_cancelada(motivo="Intensidade atual inferior")

    def test_chuva_local_observada_cancela_previsao_pendente(self):
        self.enfileirar_previsao()
        snapshot = self.snapshot()
        snapshot["escola"]["rain_rate"] = 1
        self.atualizar_snapshot(snapshot)
        self.assert_previsao_cancelada(motivo="local_event_observed")

    def test_outro_episodio_nao_revalida_previsao_antiga(self):
        from services.nowcasting_public_alerts import carregar_estado, salvar_estado
        self.enfileirar_previsao()
        conn = self.abrir_banco()
        estado = carregar_estado(conn)
        estado["episode_id"] = "f" * 32
        salvar_estado(conn, estado)
        conn.commit()
        conn.close()
        self.assert_previsao_cancelada(motivo="nao esta mais ativo")

    def test_novo_track_sem_confirmacao_nao_autoriza_item_antigo(self):
        self.enfileirar_previsao()
        snapshot = self.snapshot()
        snapshot["alerta_preventivo"]["track_id"] = 28
        self.atualizar_snapshot(snapshot)
        self.assert_previsao_cancelada(motivo="dupla confirmacao")

    def test_perda_de_confirmacao_publica_cancela_item_pendente(self):
        from services.nowcasting_public_alerts import carregar_estado, salvar_estado
        self.enfileirar_previsao()
        conn = self.abrir_banco()
        estado = carregar_estado(conn)
        estado["pending_tracking_count"] = 1
        salvar_estado(conn, estado)
        conn.commit()
        conn.close()
        self.assert_previsao_cancelada(motivo="dupla confirmacao")

    def test_confirmacao_antiga_futura_ou_sem_horario_nao_revalida_previsao(self):
        from services.nowcasting_public_alerts import carregar_estado, salvar_estado
        for minutos in (None, -16, 1):
            with self.subTest(minutos=minutos):
                self.enfileirar_previsao()
                conn = self.abrir_banco()
                estado = carregar_estado(conn)
                estado["pending_tracking_observed_at"] = (
                    (self.now + timedelta(minutes=minutos)).isoformat()
                    if minutos is not None else None
                )
                salvar_estado(conn, estado)
                conn.commit()
                conn.close()
                self.assert_previsao_cancelada(motivo="dupla confirmacao recente")

    def test_escalonamento_substitui_item_antigo_por_nivel_maior(self):
        from services.nowcasting_public_alerts import carregar_estado, salvar_estado
        self.enfileirar_previsao(nivel=1)
        conn = self.abrir_banco()
        estado = carregar_estado(conn)
        estado.update(highest_enqueued_severity=2, last_enqueued_alert_level="ATENCAO",
                      last_enqueued_radar_intensity="HIGH")
        salvar_estado(conn, estado)
        conn.commit()
        conn.close()
        self.assert_previsao_cancelada(motivo="substituida")

    def test_alerta_de_chuva_observada_nao_usa_ttl_nem_gates_de_previsao(self):
        evento_id = self.enfileirar_previsao(idade_minutos=120)
        conn = self.abrir_banco()
        conn.execute("UPDATE alertas_eventos SET tipo='chuva', evento_id='chuva:observada', "
                     "ocorrido_em_local=NULL WHERE evento_id=?", (evento_id,))
        conn.execute("UPDATE alertas_fila SET evento_id='chuva:observada'")
        conn.commit()
        conn.close()
        with (mock.patch.dict(os.environ, {"NOWCASTING_ENABLED": "false"}),
              mock.patch("services.nowcasting_repository.obter_ultimo_snapshot") as carregar,
              mock.patch.object(self.sender, "enviar_whatsapp") as enviar):
            self.assertEqual(self.sender.processar_um_envio(), "enviado")
            enviar.assert_called_once()
            carregar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
