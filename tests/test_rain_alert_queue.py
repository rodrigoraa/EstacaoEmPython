import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


class RainAlertQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "ESTACAO_DB": str(Path(self.tmp.name) / "test.db"),
            "APP_ENV": "development", "SECRET_KEY": "test-only",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        import database
        import workers.updater
        import workers.whatsapp_sender
        self.database = importlib.reload(database)
        self.database.init_db()
        self.updater = importlib.reload(workers.updater)
        self.sender = importlib.reload(workers.whatsapp_sender)
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "state.json")
        self.updater.log = self.sender.log = lambda message: None
        self.envios = []
        self.sender.enviar_whatsapp = lambda numero, mensagem: self.envios.append(mensagem)
        with self.database.get_db() as conn:
            conn.executemany(
                "INSERT INTO usuarios (id, nome, telefone, receber_whatsapp) VALUES (?, ?, ?, 1)",
                [(1, "Maria", "67999999999"), (2, "Joao", "67888888888")],
            )

    def evento(self, nivel, dia="2026-11-05", tipo="chuva"):
        evento_id = f"{dia}:{tipo}:{nivel}:0"
        return {
            "evento_id": evento_id, "data_referencia": dia,
            "tipo": tipo, "nivel": nivel, "valor": {1: 30, 2: 50, 3: 100}[nivel],
            "unidade": "mm", "prioridade": {1: 50, 2: 80, 3: 100}[nivel],
        }

    def enfileirar(self, nivel, **kwargs):
        evento = self.evento(nivel, **kwargs)
        self.updater.enviar_alerta(f"{evento['tipo']} nivel {nivel}", evento=evento)
        return evento["evento_id"]

    def filas(self, evento_id):
        with self.database.get_db() as conn:
            return conn.execute(
                "SELECT * FROM alertas_fila WHERE evento_id = ? ORDER BY usuario_id", (evento_id,),
            ).fetchall()

    def test_superior_substitui_pendentes_sem_enviar_ou_tentar_o_inferior(self):
        inferior = self.enfileirar(1)
        superior = self.enfileirar(2)
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["substituido"] * 2)
        self.assertEqual([r["tentativas"] for r in self.filas(inferior)], [0, 0])
        self.assertTrue(all(r["enviado_em"] is None for r in self.filas(inferior)))
        resultado = self.sender.processar_fila(intervalo=0)
        self.assertEqual(resultado, {"processados": 2, "enviados": 2, "falhas": 0})
        self.assertTrue(all("chuva nivel 2" in mensagem for mensagem in self.envios))
        with self.database.get_db() as conn:
            evento = conn.execute("SELECT * FROM alertas_eventos WHERE evento_id = ?", (inferior,)).fetchone()
            self.assertEqual((evento["status"], evento["enviados"], evento["falhas"], evento["substituidos"]),
                             ("substituido", 0, 0, 2))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_envios").fetchone()[0], 2)
        self.assertTrue(all(r["status"] == "enviado" for r in self.filas(superior)))

    def test_substituicao_encerra_campanha_sem_afetar_outro_dia_ou_tipo(self):
        anterior = self.enfileirar(1, dia="2026-11-04")
        vento = self.enfileirar(1, tipo="vento")
        inferior = self.enfileirar(1)
        with self.database.get_db() as conn:
            conn.execute("UPDATE usuarios SET receber_whatsapp = 0 WHERE id = 2")
        self.enfileirar(2)
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["substituido"] * 2)
        for evento_id in (anterior, vento):
            self.assertEqual([r["status"] for r in self.filas(evento_id)], ["pendente"] * 2)

    def test_preserva_enviado_e_envio_em_andamento(self):
        inferior = self.enfileirar(1)
        with self.database.get_db() as conn:
            conn.execute("UPDATE alertas_fila SET status = 'enviado', enviado_em = CURRENT_TIMESTAMP WHERE usuario_id = 1")
            conn.execute("UPDATE alertas_fila SET status = 'enviando', atualizado_em = CURRENT_TIMESTAMP WHERE usuario_id = 2")
        self.enfileirar(2)
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["enviado", "enviando"])

    def test_falha_parcial_no_enqueue_superior_nao_retoma_campanha_inferior(self):
        inferior = self.enfileirar(1)
        real = self.updater.enfileirar_alerta_usuario

        def falhar_para_joao(conn, usuario, *args, **kwargs):
            if usuario["id"] == 2:
                raise sqlite3.IntegrityError("falha simulada no enqueue")
            return real(conn, usuario, *args, **kwargs)

        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=falhar_para_joao):
            self.enfileirar(2)
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["substituido"] * 2)

    def test_falha_total_no_enqueue_superior_tambem_encerra_campanha_inferior(self):
        inferior = self.enfileirar(1)
        with mock.patch.object(self.updater, "enfileirar_alerta_usuario", side_effect=sqlite3.IntegrityError("falha simulada")):
            superior = self.enfileirar(2)
        self.assertEqual(self.filas(superior), [])
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["substituido"] * 2)
        self.assertEqual(self.sender.processar_fila(intervalo=0),
                         {"processados": 0, "enviados": 0, "falhas": 0})

    def test_escalada_durante_distribuicao_reenvia_novo_nivel_para_todos(self):
        inferior = self.enfileirar(1)
        self.sender.processar_fila(limite=1, intervalo=0)
        self.assertEqual(len(self.envios), 1)
        self.assertIn("ATENÇÃO, Maria,", self.envios[0])
        self.assertIn("chuva nivel 1", self.envios[0])

        superior = self.enfileirar(2)
        self.assertEqual([r["usuario_id"] for r in self.filas(superior)], [1, 2])
        self.sender.processar_fila(intervalo=0)
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["enviado", "substituido"])
        self.assertEqual(len(self.envios), 3)
        self.assertTrue(all("chuva nivel 2" in mensagem for mensagem in self.envios[1:]))
        self.assertIn("ATENÇÃO, Maria,", self.envios[1])
        self.assertIn("ATENÇÃO, Joao,", self.envios[2])

        extremo = self.enfileirar(3)
        self.sender.processar_fila(intervalo=0)
        self.assertEqual([r["usuario_id"] for r in self.filas(extremo)], [1, 2])
        self.assertEqual(len(self.envios), 5)
        self.assertTrue(all("chuva nivel 3" in mensagem for mensagem in self.envios[3:]))

    def test_fila_antiga_inferior_nao_retorna_para_destinatario_sem_superior(self):
        inferior = self.enfileirar(1)
        with self.database.get_db() as conn:
            conn.execute("UPDATE usuarios SET receber_whatsapp = 0 WHERE id = 2")
        superior = self.enfileirar(2)
        with self.database.get_db() as conn:
            conn.execute("UPDATE alertas_fila SET status = 'pendente' WHERE evento_id = ? AND usuario_id = 2", (inferior,))
            conn.execute("UPDATE alertas_fila SET status = 'enviado' WHERE evento_id = ?", (superior,))
        self.assertIsNone(self.sender.processar_um_envio())
        self.assertEqual(self.filas(inferior)[1]["status"], "substituido")
        self.assertEqual(self.envios, [])

    def test_fila_antiga_e_suprimida_mesmo_se_superior_ja_foi_enviado(self):
        inferior = self.enfileirar(1)
        superior = self.enfileirar(2)
        # Reproduz filas antigas, anteriores à correção, com prioridades invertidas.
        with self.database.get_db() as conn:
            conn.execute("UPDATE alertas_fila SET status = 'pendente', prioridade = 200 WHERE evento_id = ?", (inferior,))
            conn.execute("UPDATE alertas_fila SET status = 'enviado' WHERE evento_id = ?", (superior,))
        resultado = self.sender.processar_fila(intervalo=0)
        self.assertEqual(resultado, {"processados": 0, "enviados": 0, "falhas": 0})
        self.assertEqual(self.envios, [])
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["substituido"] * 2)

    def test_retry_e_envio_expirado_nao_ressuscitam_chuva_inferior(self):
        inferior = self.enfileirar(1)
        self.enfileirar(2)
        with self.database.get_db() as conn:
            conn.execute("UPDATE alertas_fila SET status = 'falhou', tentativas = 4 WHERE evento_id = ? AND usuario_id = 1", (inferior,))
            conn.execute("UPDATE alertas_fila SET status = 'enviando', tentativas = 1, atualizado_em = '2000-01-01' WHERE evento_id = ? AND usuario_id = 2", (inferior,))
        resultado = self.sender.processar_fila(intervalo=0, retry_failed=True)
        self.assertEqual(resultado, {"processados": 2, "enviados": 2, "falhas": 0})
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["substituido"] * 2)
        self.assertEqual([r["tentativas"] for r in self.filas(inferior)], [4, 1])

    def test_substitui_retry_adiado_sem_aguardar_proxima_tentativa(self):
        inferior = self.enfileirar(1)
        with self.database.get_db() as conn:
            conn.execute("UPDATE alertas_fila SET tentativas = 1, proxima_tentativa_em = '2099-01-01' WHERE evento_id = ?", (inferior,))
        self.enfileirar(2)
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["substituido"] * 2)
        self.assertTrue(all(r["proxima_tentativa_em"] is None for r in self.filas(inferior)))

    def test_evento_parcial_preserva_contagem_de_entregas(self):
        inferior = self.enfileirar(1)
        self.sender.processar_fila(limite=1, intervalo=0)
        self.enfileirar(2)
        with self.database.get_db() as conn:
            evento = conn.execute("SELECT * FROM alertas_eventos WHERE evento_id = ?", (inferior,)).fetchone()
            self.assertEqual((evento["status"], evento["enviados"], evento["falhas"], evento["substituidos"]),
                             ("concluido", 1, 0, 1))

    def test_schema_existente_recebe_contador_sem_perder_eventos(self):
        with self.database.get_db() as conn:
            conn.execute("DROP TABLE alertas_eventos")
            conn.execute("""CREATE TABLE alertas_eventos (
                id INTEGER PRIMARY KEY, evento_id TEXT UNIQUE, data_referencia TEXT,
                tipo TEXT, nivel INTEGER, enviados INTEGER, falhas INTEGER, status TEXT)
            """)
            conn.execute("INSERT INTO alertas_eventos VALUES (1, 'legado', '2026-11-04', 'chuva', 1, 10, 0, 'concluido')")
            self.database.garantir_tabela_alertas_eventos(conn)
            self.database.garantir_tabela_alertas_eventos(conn)
            colunas = {r[1] for r in conn.execute("PRAGMA table_info(alertas_eventos)")}
            self.assertIn("substituidos", colunas)
            evento = conn.execute("SELECT enviados, substituidos FROM alertas_eventos WHERE evento_id = 'legado'").fetchone()
            self.assertEqual(tuple(evento), (10, 0))

    def test_falha_na_substituicao_reverte_enqueue_superior(self):
        inferior = self.enfileirar(1)
        with self.database.get_db() as conn:
            conn.execute("""CREATE TRIGGER impedir_substituicao
                BEFORE UPDATE OF status ON alertas_fila
                WHEN NEW.status = 'substituido'
                BEGIN SELECT RAISE(ABORT, 'falha simulada na substituicao'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "falha simulada"):
            self.enfileirar(2)
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["pendente"] * 2)
        with self.database.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 2)

    def test_init_db_migra_versao_8_preservando_eventos_e_fila(self):
        inferior = self.enfileirar(1)
        with self.database.get_db() as conn:
            conn.execute("ALTER TABLE alertas_eventos DROP COLUMN substituidos")
            conn.execute("UPDATE schema_version SET versao = 8 WHERE id = 1")
        self.database.init_db()
        self.database.init_db()
        with self.database.get_db() as conn:
            self.assertEqual(conn.execute("SELECT versao FROM schema_version WHERE id = 1").fetchone()[0], 10)
            self.assertEqual(conn.execute("SELECT substituidos FROM alertas_eventos WHERE evento_id = ?", (inferior,)).fetchone()[0], 0)
        self.assertEqual([r["status"] for r in self.filas(inferior)], ["pendente"] * 2)

    def test_workers_paralelos_enviam_so_superior_sem_duplicar(self):
        inferior = self.enfileirar(1)
        self.enfileirar(2)
        with ThreadPoolExecutor(max_workers=3) as executor:
            resultados = list(executor.map(lambda _: self.sender.processar_um_envio(), range(3)))
        self.assertEqual(resultados.count("enviado"), 2)
        self.assertEqual(resultados.count(None), 1)
        self.assertEqual(len(self.envios), 2)
        self.assertTrue(all("chuva nivel 2" in mensagem for mensagem in self.envios))
        self.assertEqual([r["tentativas"] for r in self.filas(inferior)], [0, 0])

    def test_workers_paralelos_migram_coluna_ausente_sem_corrida(self):
        self.enfileirar(2)
        with self.database.get_db() as conn:
            conn.execute("ALTER TABLE alertas_eventos DROP COLUMN substituidos")
        original = self.database.coluna_existe
        barreira = Barrier(2)

        def sincronizar_leituras_sem_trava(conn, tabela, coluna):
            existe = original(conn, tabela, coluna)
            # Força a janela de corrida quando a consulta não está protegida
            # por uma transação de escrita. Com a trava, o segundo aguarda.
            if (tabela == "alertas_eventos" and coluna == "substituidos"
                    and not existe and not conn.in_transaction):
                barreira.wait(timeout=5)
            return existe

        with mock.patch.object(self.database, "coluna_existe", sincronizar_leituras_sem_trava):
            with ThreadPoolExecutor(max_workers=2) as executor:
                resultados = list(executor.map(lambda _: self.sender.processar_um_envio(), range(2)))
        self.assertEqual(resultados, ["enviado", "enviado"])
        self.assertEqual(len(self.envios), 2)
        with self.database.get_db() as conn:
            self.assertEqual(conn.execute("SELECT enviados, substituidos FROM alertas_eventos").fetchone()[:], (2, 0))


if __name__ == "__main__":
    unittest.main()
