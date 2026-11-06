import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


class AlertEnqueueRecoveryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"ESTACAO_DB": str(Path(tmp.name) / "test.db"), "SECRET_KEY": "test-only"})
        env.start()
        self.addCleanup(env.stop)
        import database
        import workers.updater
        self.db = importlib.reload(database)
        self.db.init_db()
        self.updater = importlib.reload(workers.updater)
        self.updater.STATE_FILE = str(Path(tmp.name) / "state.json")
        self.updater.log = lambda mensagem: None
        self.estado = self.updater.estado_alertas_padrao("2026-11-05")
        with self.db.get_db() as conn:
            conn.executemany("INSERT INTO usuarios (id, nome, telefone, receber_whatsapp) VALUES (?, ?, ?, 1)",
                             [(1, "Maria", "67999999999"), (2, "Joao", "67888888888")])

    def marcar(self):
        return self.updater.marcar_alerta_enviado(self.estado, "nivel_calor", 2, "Calor crítico", valor=40, unidade="°C")

    def test_falha_total_pode_ser_retentada_sem_marcar_evento_vazio_como_enfileirado(self):
        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=sqlite3.IntegrityError("falha temporária")):
            self.assertTrue(self.marcar())
        self.assertEqual(self.estado["nivel_calor"], 2)
        self.assertTrue(self.marcar())
        with self.db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 1)
        self.assertEqual(self.estado["nivel_calor"], 2)

    def test_falha_parcial_repete_so_destinatario_ausente_sem_duplicar_entregas(self):
        original = self.updater.enfileirar_alerta_usuario

        def falhar_um(conn, usuario, *args, **kwargs):
            if usuario["id"] == 2:
                raise sqlite3.IntegrityError("falha temporária")
            return original(conn, usuario, *args, **kwargs)

        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=falhar_um):
            self.assertTrue(self.marcar())
        with self.db.get_db() as conn:
            conn.execute("UPDATE alertas_fila SET status = 'enviado', enviado_em = CURRENT_TIMESTAMP")
        self.assertTrue(self.marcar())
        with self.db.get_db() as conn:
            self.assertEqual([tuple(r) for r in conn.execute("SELECT usuario_id, status FROM alertas_fila ORDER BY usuario_id")],
                             [(1, "enviado"), (2, "pendente")])
            evento = conn.execute("SELECT destinatarios, enfileirados, falhas FROM alertas_eventos").fetchone()
            self.assertEqual(tuple(evento), (2, 2, 0))
        self.assertTrue(self.marcar())
        with self.db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 2)

    def test_evento_duplicado_nao_reabre_campanha_ja_concluida(self):
        self.assertTrue(self.marcar())
        with self.db.get_db() as conn:
            conn.execute("UPDATE alertas_fila SET status = 'enviado'")
            conn.execute("UPDATE alertas_eventos SET status = 'concluido', enviados = 2")
        self.assertTrue(self.marcar())
        with self.db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT status FROM alertas_eventos").fetchone()[0], "concluido")

    def test_falha_no_enqueue_nao_desaparece_quando_outro_destinatario_recebe(self):
        original = self.updater.enfileirar_alerta_usuario

        def falhar_um(conn, usuario, *args, **kwargs):
            if usuario["id"] == 2:
                raise sqlite3.IntegrityError("falha temporária")
            return original(conn, usuario, *args, **kwargs)

        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=falhar_um):
            self.marcar()
        import workers.whatsapp_sender
        sender = importlib.reload(workers.whatsapp_sender)
        sender.enviar_whatsapp = lambda telefone, mensagem: None
        sender.processar_fila(limite=1, intervalo=0)
        with self.db.get_db() as conn:
            evento = conn.execute("SELECT enviados, falhas, status FROM alertas_eventos").fetchone()
            self.assertEqual(tuple(evento), (1, 1, "concluido_com_falhas"))

    def leitura_calor(self, temperatura, dia="2026-11-05"):
        self.updater.verificar_alertas(
            temperatura, temperatura, 0, 0, 60, 0,
            data_referencia=dia, ocorrido_em_local=f"{dia}T12:00:00-04:00",
            validade_alertas={"temperatura": True, "sensacao": True},
        )

    def test_recuperacao_apos_falha_de_enqueue_permite_novo_ciclo(self):
        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=sqlite3.IntegrityError("falha temporária")):
            self.leitura_calor(40)
        self.leitura_calor(32)
        self.leitura_calor(36)
        self.leitura_calor(36)
        with self.db.get_db() as conn:
            eventos = [tuple(r) for r in conn.execute("SELECT evento_id, status FROM alertas_eventos ORDER BY id")]
            filas = [r[0] for r in conn.execute("SELECT evento_id FROM alertas_fila")]
        self.assertEqual(eventos[0], ("2026-11-05:calor:2:0", "concluido_com_falhas"))
        self.assertEqual(filas, ["2026-11-05:calor:1:1"] * 2)

    def test_nova_leitura_repara_enqueues_ausentes_preservando_mensagem_original(self):
        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=sqlite3.IntegrityError("falha temporária")):
            self.leitura_calor(40)
        self.leitura_calor(36, dia="2026-11-06")
        with self.db.get_db() as conn:
            filas = conn.execute("SELECT evento_id, mensagem FROM alertas_fila").fetchall()
            self.assertEqual(len(filas), 2)
            self.assertTrue(all(r["evento_id"] == "2026-11-05:calor:2:0" for r in filas))
            self.assertTrue(all("40°C" in r["mensagem"] for r in filas))

    def test_reparo_nao_recria_inferior_quando_superior_ja_foi_detectado(self):
        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=sqlite3.IntegrityError("falha temporária")):
            self.leitura_calor(36)
            self.leitura_calor(36)
        self.leitura_calor(40)
        self.leitura_calor(40)
        with self.db.get_db() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT evento_id FROM alertas_fila")], ["2026-11-05:calor:2:0"] * 2)

    def test_retry_explicito_usa_texto_do_evento_persistido(self):
        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=sqlite3.IntegrityError("falha temporária")):
            self.marcar()
        self.updater.marcar_alerta_enviado(self.estado, "nivel_calor", 2, "Mensagem de outra leitura", valor=41, unidade="°C")
        with self.db.get_db() as conn:
            self.assertTrue(all("Calor crítico" in r[0] for r in conn.execute("SELECT mensagem FROM alertas_fila")))

    def test_migracao_do_updater_nao_disputa_coluna_com_sender(self):
        with self.db.get_db() as conn:
            conn.execute("ALTER TABLE alertas_eventos DROP COLUMN substituidos")
        import workers.whatsapp_sender
        sender = importlib.reload(workers.whatsapp_sender)
        original = self.db.coluna_existe

        def intercalar_sender(conn, tabela, coluna):
            existe = original(conn, tabela, coluna)
            if tabela == "alertas_eventos" and coluna == "substituidos" and not existe and not conn.in_transaction:
                with self.db.get_db() as outra:
                    sender.reivindicar_proximo_envio(outra)
            return existe

        with mock.patch.object(self.db, "coluna_existe", intercalar_sender):
            self.assertTrue(self.marcar())

    def test_reparo_migra_banco_legado_antes_de_carregar_evento(self):
        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=sqlite3.IntegrityError("falha temporária")):
            self.marcar()
        with self.db.get_db() as conn:
            conn.execute("ALTER TABLE alertas_eventos DROP COLUMN ciclo")
            conn.execute("ALTER TABLE alertas_eventos DROP COLUMN substituidos")
        self.leitura_calor(41)
        with self.db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT ciclo FROM alertas_eventos").fetchone()[0], 0)

    def test_ocorrencia_sem_inscritos_nao_impede_ciclo_futuro(self):
        with self.db.get_db() as conn:
            conn.execute("UPDATE usuarios SET receber_whatsapp = 0")
        self.leitura_calor(40)
        self.leitura_calor(32)
        with self.db.get_db() as conn:
            conn.execute("UPDATE usuarios SET receber_whatsapp = 1")
        self.leitura_calor(36)
        self.leitura_calor(36)
        with self.db.get_db() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT evento_id FROM alertas_fila")], ["2026-11-05:calor:1:1"] * 2)
