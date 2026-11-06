import importlib
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
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

    def preparar_falha_permanente(self):
        self.enfileirar([(1, "Maria", "5567999999999", "Alerta de teste")])
        conn = self.abrir_banco()
        conn.execute(
            """
            UPDATE alertas_fila
            SET status = 'falhou', tentativas = 4, erro_permanente = 1
            """
        )
        conn.commit()
        conn.close()

    def falhar_permanentemente(self, numero, mensagem):
        raise RuntimeError("Erro Evolution API 400: telefone inválido")

    def test_retry_failed_tenta_cada_item_uma_vez_por_execucao(self):
        self.preparar_falha_permanente()
        self.sender.enviar_whatsapp = self.falhar_permanentemente

        primeiro = self.sender.processar_fila(limite=3, intervalo=0, retry_failed=True)
        segundo = self.sender.processar_fila(limite=3, intervalo=0, retry_failed=True)

        self.assertEqual(primeiro, {"processados": 1, "enviados": 0, "falhas": 1})
        self.assertEqual(segundo, {"processados": 1, "enviados": 0, "falhas": 1})
        conn = self.abrir_banco()
        fila = conn.execute("SELECT status, tentativas FROM alertas_fila").fetchone()
        historico = conn.execute("SELECT COUNT(*) FROM alertas_envios").fetchone()[0]
        conn.close()
        self.assertEqual(tuple(fila), ("falhou", 6))
        self.assertEqual(historico, 2)

    def test_retry_failed_sem_limite_encerra_apos_uma_tentativa(self):
        self.preparar_falha_permanente()
        tentativas = []

        class RepeticaoIndevida(BaseException):
            pass

        def falhar_com_limite_de_seguranca(numero, mensagem):
            tentativas.append(numero)
            if len(tentativas) > 1:
                raise RepeticaoIndevida()
            self.falhar_permanentemente(numero, mensagem)

        self.sender.enviar_whatsapp = falhar_com_limite_de_seguranca
        try:
            resultado = self.sender.processar_fila(intervalo=0, retry_failed=True)
        except RepeticaoIndevida:
            self.fail("A execução sem limite voltou a tentar o mesmo item falho")

        self.assertEqual(resultado, {"processados": 1, "enviados": 0, "falhas": 1})

    def test_retry_failed_paralelo_compartilha_tentativas_do_lote(self):
        self.preparar_falha_permanente()
        self.sender.enviar_whatsapp = self.falhar_permanentemente
        processar = self.sender.processar_um_envio
        primeiro_finalizado = threading.Event()
        controle = threading.Lock()
        iniciados = 0

        def ordenar_primeiro_envio(**kwargs):
            nonlocal iniciados
            with controle:
                primeiro = iniciados == 0
                iniciados += 1
            if not primeiro:
                if not primeiro_finalizado.wait(5):
                    raise AssertionError("O primeiro envio não terminou")
                return processar(**kwargs)
            try:
                return processar(**kwargs)
            finally:
                primeiro_finalizado.set()

        with mock.patch.object(self.sender, "processar_um_envio", side_effect=ordenar_primeiro_envio):
            resultados = self.sender.processar_lote_paralelo(workers=3, retry_failed=True)

        self.assertEqual(resultados, ["falhou"])
        conn = self.abrir_banco()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_envios").fetchone()[0], 1)
        conn.close()

    def test_retry_failed_continuo_preserva_sessao_entre_lotes(self):
        self.preparar_falha_permanente()
        self.sender.enviar_whatsapp = self.falhar_permanentemente
        processar_lote = self.sender.processar_lote_paralelo

        class EncerrarSessao(BaseException):
            pass

        with (
            mock.patch.object(
                self.sender,
                "processar_lote_paralelo",
                side_effect=lambda **kwargs: processar_lote(workers=1, **kwargs),
            ),
            mock.patch.object(self.sender.time, "sleep", side_effect=[None, EncerrarSessao()]),
        ):
            with self.assertRaises(EncerrarSessao):
                self.sender.rodar_continuamente(intervalo=0, retry_failed=True)

        conn = self.abrir_banco()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_envios").fetchone()[0], 1)
        conn.close()

    def test_retry_failed_nao_antecipa_backoff_pendente(self):
        self.enfileirar([(1, "Maria", "5567999999999", "Alerta de teste")])
        conn = self.abrir_banco()
        conn.execute(
            "UPDATE alertas_fila SET proxima_tentativa_em = datetime('now', '+1 hour')"
        )
        conn.commit()
        conn.close()
        self.sender.enviar_whatsapp = self.falhar_permanentemente

        resultado = self.sender.processar_fila(limite=3, intervalo=0, retry_failed=True)

        self.assertEqual(resultado, {"processados": 0, "enviados": 0, "falhas": 0})

    def test_retry_failed_preserva_retry_pendente_apos_backoff_na_mesma_sessao(self):
        self.enfileirar([(1, "Maria", "5567999999999", "Alerta de teste")])
        tentativas = []

        def falhar_primeira_tentativa(numero, mensagem):
            tentativas.append(numero)
            if len(tentativas) == 1:
                raise RuntimeError("Evolution temporariamente fora")

        def avancar_ate_proximo_retry(intervalo):
            conn = self.abrir_banco()
            conn.execute(
                "UPDATE alertas_fila SET proxima_tentativa_em = '2000-01-01 00:00:00'"
            )
            conn.commit()
            conn.close()

        self.sender.enviar_whatsapp = falhar_primeira_tentativa
        with mock.patch.object(self.sender.time, "sleep", side_effect=avancar_ate_proximo_retry):
            resultado = self.sender.processar_fila(limite=2, intervalo=0, retry_failed=True)

        self.assertEqual(resultado, {"processados": 2, "enviados": 1, "falhas": 1})
        conn = self.abrir_banco()
        self.assertEqual(
            tuple(conn.execute("SELECT status, tentativas FROM alertas_fila").fetchone()),
            ("enviado", 2),
        )
        conn.close()

    def test_retry_failed_nao_repete_falha_permanente_criada_na_sessao(self):
        self.enfileirar([(1, "Maria", "5567999999999", "Alerta de teste")])
        self.sender.enviar_whatsapp = self.falhar_permanentemente

        resultado = self.sender.processar_fila(limite=3, intervalo=0, retry_failed=True)

        self.assertEqual(resultado, {"processados": 1, "enviados": 0, "falhas": 1})


if __name__ == "__main__":
    unittest.main()
