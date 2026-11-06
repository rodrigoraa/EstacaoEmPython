import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


class AlertCampaignsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "ESTACAO_DB": str(Path(tmp.name) / "test.db"),
            "SECRET_KEY": "test-only", "APP_ENV": "development",
        })
        env.start()
        self.addCleanup(env.stop)
        import database
        import workers.updater
        import workers.whatsapp_sender
        self.db = importlib.reload(database)
        self.db.init_db()
        self.updater = importlib.reload(workers.updater)
        self.sender = importlib.reload(workers.whatsapp_sender)
        self.updater.STATE_FILE = str(Path(tmp.name) / "state.json")
        self.updater.log = self.sender.log = lambda mensagem: None
        self.entregas = []
        self.sender.enviar_whatsapp = lambda telefone, mensagem: self.entregas.append((telefone, mensagem))
        with self.db.get_db() as conn:
            conn.executemany(
                "INSERT INTO usuarios (id, nome, telefone, receber_whatsapp) VALUES (?, ?, ?, 1)",
                [(1, "Maria", "67999999999"), (2, "Joao", "67888888888")],
            )

    def emitir(self, tipo, nivel, ciclo=0, dia="2026-11-05"):
        evento_id = f"{dia}:{tipo}:{nivel}:{ciclo}"
        self.updater.enviar_alerta(evento_id, evento={
            "evento_id": evento_id, "data_referencia": dia, "tipo": tipo,
            "nivel": nivel, "ciclo": ciclo, "prioridade": {1: 50, 2: 80, 3: 100}[nivel],
        })
        return evento_id

    def status(self, evento_id):
        with self.db.get_db() as conn:
            return [row[0] for row in conn.execute(
                "SELECT status FROM alertas_fila WHERE evento_id = ? ORDER BY usuario_id", (evento_id,))]

    def test_escalada_dos_outros_tipos_encerra_inferior_e_envia_superior_para_todos(self):
        for tipo in ("calor", "frio", "vento", "umidade"):
            with self.subTest(tipo=tipo):
                inferior = self.emitir(tipo, 1)
                self.sender.processar_fila(limite=1, intervalo=0)
                superior = self.emitir(tipo, 2)
                self.assertEqual(self.status(inferior), ["enviado", "substituido"])
                self.sender.processar_fila(intervalo=0)
                self.assertEqual(self.status(superior), ["enviado"] * 2)
                self.assertTrue(all(superior in mensagem for _, mensagem in self.entregas[-2:]))

    def test_nivel_3_de_frio_e_vento_substitui_niveis_1_e_2(self):
        for tipo in ("frio", "vento"):
            with self.subTest(tipo=tipo):
                baixo = self.emitir(tipo, 1)
                medio = self.emitir(tipo, 2)
                alto = self.emitir(tipo, 3)
                self.assertEqual(self.status(baixo), ["substituido"] * 2)
                self.assertEqual(self.status(medio), ["substituido"] * 2)
                self.sender.processar_fila(intervalo=0)
                self.assertEqual(self.status(alto), ["enviado"] * 2)

    def test_recuperacao_permite_novo_ciclo_apos_critico_no_mesmo_dia(self):
        for tipo in ("calor", "frio", "umidade"):
            with self.subTest(tipo=tipo):
                antigo = self.emitir(tipo, 2, ciclo=0)
                self.sender.processar_fila(intervalo=0)
                novo = self.emitir(tipo, 1, ciclo=1)
                self.assertEqual(self.status(novo), ["pendente"] * 2)
                self.sender.processar_fila(intervalo=0)
                self.assertEqual(self.status(novo), ["enviado"] * 2)
                self.assertEqual(self.status(antigo), ["enviado"] * 2)

    def test_novo_ciclo_nao_substitui_pendentes_de_outro_ciclo(self):
        antigo = self.emitir("calor", 1, ciclo=0)
        self.emitir("calor", 2, ciclo=1)
        self.assertEqual(self.status(antigo), ["pendente"] * 2)

    def test_escalada_de_mesmo_ciclo_instantaneo_atravessa_meia_noite(self):
        for tipo in ("calor", "frio", "umidade"):
            with self.subTest(tipo=tipo):
                inferior = self.emitir(tipo, 1, dia="2026-11-04")
                superior = self.emitir(tipo, 2)
                self.assertEqual(self.status(inferior), ["substituido"] * 2)
                self.sender.processar_fila(intervalo=0)
                self.assertEqual(self.status(superior), ["enviado"] * 2)

    def test_eventos_diarios_nao_substituem_avisos_de_outro_dia(self):
        for tipo in ("chuva", "vento"):
            with self.subTest(tipo=tipo):
                anterior = self.emitir(tipo, 1, dia="2026-11-04")
                self.emitir(tipo, 2)
                self.assertEqual(self.status(anterior), ["pendente"] * 2)

    def test_sender_suprime_retentaiva_antiga_do_mesmo_ciclo(self):
        for tipo in ("calor", "frio", "vento", "umidade"):
            with self.subTest(tipo=tipo):
                inferior = self.emitir(tipo, 1)
                self.emitir(tipo, 2)
                with self.db.get_db() as conn:
                    conn.execute("UPDATE alertas_fila SET status = 'falhou', tentativas = 3 WHERE evento_id = ?", (inferior,))
                self.sender.processar_fila(intervalo=0, retry_failed=True)
                self.assertEqual(self.status(inferior), ["substituido"] * 2)
                with self.db.get_db() as conn:
                    self.assertEqual([r[0] for r in conn.execute("SELECT tentativas FROM alertas_fila WHERE evento_id = ?", (inferior,))], [3, 3])

    def test_migracao_recupera_ciclo_apenas_de_ids_legados_validos(self):
        evento_id = self.emitir("frio", 1, ciclo=4)
        with self.db.get_db() as conn:
            colunas = {row[1] for row in conn.execute("PRAGMA table_info(alertas_eventos)")}
            if "ciclo" in colunas:
                conn.execute("ALTER TABLE alertas_eventos DROP COLUMN ciclo")
            conn.execute("""INSERT INTO alertas_eventos (evento_id, data_referencia, tipo, nivel)
                VALUES ('id-sem-ciclo', '2026-11-05', 'frio', 1)""")
        self.db.init_db()
        with self.db.get_db() as conn:
            colunas = {row[1] for row in conn.execute("PRAGMA table_info(alertas_eventos)")}
            self.assertIn("ciclo", colunas)
            self.assertEqual(conn.execute("SELECT ciclo FROM alertas_eventos WHERE evento_id = ?", (evento_id,)).fetchone()[0], 4)
            self.assertIsNone(conn.execute("SELECT ciclo FROM alertas_eventos WHERE evento_id = 'id-sem-ciclo'").fetchone()[0])

    def test_motor_gera_todos_os_niveis_configurados_uma_vez(self):
        casos = (
            ("calor", "temperatura", "temp", (35, 40)),
            ("frio", "temperatura", "temp", (12.4, 5, 2)),
            ("vento", "vento", "rajada", (40, 70, 100)),
            ("umidade", "umidade", "umidade", (30, 20)),
            ("chuva", "chuva", "chuva", (30, 50, 100)),
        )
        with mock.patch.dict(os.environ, {"ALERTA_CONFIRMACOES_NIVEL_1": "2", "ALERTA_VENTO_JANELA_MINUTOS": "0"}):
            for tipo, validade, campo, valores in casos:
                with self.subTest(tipo=tipo):
                    for valor in valores:
                        dados = {"temp": 25, "sensacao": 25, "rajada": 0, "chuva": 0, "umidade": 60}
                        dados[campo] = valor
                        if campo == "temp":
                            dados["sensacao"] = valor
                        for _ in range(3):
                            self.updater.verificar_alertas(
                                dados["temp"], dados["sensacao"], dados["rajada"], dados["chuva"], dados["umidade"], 0,
                                data_referencia="2026-11-05", validade_alertas={validade: True},
                            )
                    with self.db.get_db() as conn:
                        niveis = [r[0] for r in conn.execute("SELECT nivel FROM alertas_eventos WHERE tipo = ? ORDER BY id", (tipo,))]
                    self.assertEqual(niveis, list(range(1, len(valores) + 1)))
                    self.assertEqual(self.status(f"2026-11-05:{tipo}:1:0"), ["substituido"] * 2)

    def test_migracao_direta_serializa_adicao_de_coluna_com_sender(self):
        with self.db.get_db() as conn:
            conn.execute("ALTER TABLE alertas_eventos DROP COLUMN substituidos")
        original = self.db.coluna_existe

        def intercalar_sender(conn, tabela, coluna):
            existe = original(conn, tabela, coluna)
            if tabela == "alertas_eventos" and coluna == "substituidos" and not existe and not conn.in_transaction:
                with self.db.get_db() as outra:
                    self.sender.reivindicar_proximo_envio(outra)
            return existe

        with mock.patch.object(self.db, "coluna_existe", intercalar_sender):
            with self.db.get_db() as conn:
                self.db.garantir_tabela_alertas_eventos(conn)
