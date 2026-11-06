import importlib
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class CampanhaWhatsAppUnicoTest(unittest.TestCase):
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

        import workers.enviar_aviso_whatsapp_unico as campanha

        self.campanha = importlib.reload(campanha)
        self.campanha.log = lambda mensagem: None

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

    def cadastrar_usuarios(self):
        conn = self.abrir_banco()
        conn.executemany(
            """
            INSERT INTO usuarios (nome, telefone, receber_whatsapp, ativo)
            VALUES (?, ?, ?, ?)
            """,
            [
                ("Maria", "(67) 99999-9999", 1, 1),
                ("Joao", "67888888888", 1, 0),
                ("Ana", "67777777777", 0, 1),
            ],
        )
        conn.commit()
        conn.close()

    def test_simulacao_nao_envia_nem_registra(self):
        self.cadastrar_usuarios()
        envios = []
        self.campanha.enviar_whatsapp = lambda numero, mensagem: envios.append(
            (numero, mensagem)
        )

        resultado = self.campanha.enviar_campanha(
            campanha_id="teste",
            template="Ola {nome}",
            intervalo=0,
            confirmar=False,
        )

        self.assertEqual(resultado, {"total": 1, "enviados": 0, "falhas": 0, "pulados": 0})
        self.assertEqual(envios, [])

        conn = self.abrir_banco()
        total = conn.execute("SELECT COUNT(*) FROM campanhas_whatsapp_envios").fetchone()[0]
        conn.close()
        self.assertEqual(total, 0)

    def test_envia_uma_vez_para_usuarios_inscritos_ativos(self):
        self.cadastrar_usuarios()
        envios = []
        pausas = []
        self.campanha.enviar_whatsapp = lambda numero, mensagem: envios.append(
            (numero, mensagem)
        )
        self.campanha.time.sleep = lambda segundos: pausas.append(segundos)

        primeiro = self.campanha.enviar_campanha(
            campanha_id="teste",
            template="Ola {nome}",
            intervalo=10,
            confirmar=True,
        )
        segundo = self.campanha.enviar_campanha(
            campanha_id="teste",
            template="Ola {nome}",
            intervalo=10,
            confirmar=True,
        )

        self.assertEqual(primeiro, {"total": 1, "enviados": 1, "falhas": 0, "pulados": 0})
        self.assertEqual(segundo, {"total": 1, "enviados": 0, "falhas": 0, "pulados": 1})
        self.assertEqual(envios, [("5567999999999", "Ola Maria")])
        self.assertEqual(pausas, [])

        conn = self.abrir_banco()
        row = conn.execute(
            """
            SELECT campanha_id, nome, telefone, status, mensagem
            FROM campanhas_whatsapp_envios
            """
        ).fetchone()
        conn.close()

        self.assertEqual(row["campanha_id"], "teste")
        self.assertEqual(row["nome"], "Maria")
        self.assertEqual(row["telefone"], "5567999999999")
        self.assertEqual(row["status"], "enviado")
        self.assertEqual(row["mensagem"], "Ola Maria")

    def test_execucao_concorrente_pula_envio_em_andamento(self):
        self.cadastrar_usuarios()
        primeiro_enviando = threading.Event()
        liberar_primeiro = threading.Event()
        envios = []

        def enviar(numero, mensagem):
            envios.append((numero, mensagem))
            if len(envios) == 1:
                primeiro_enviando.set()
                if not liberar_primeiro.wait(timeout=5):
                    raise AssertionError("Primeiro envio nao foi liberado")

        def executar():
            return self.campanha.enviar_campanha("teste", "Ola {nome}", 0, confirmar=True)

        with patch.object(self.campanha, "enviar_whatsapp", enviar), ThreadPoolExecutor(
            max_workers=2
        ) as executor:
            primeiro = executor.submit(executar)
            try:
                self.assertTrue(primeiro_enviando.wait(timeout=5))
                segundo = executor.submit(executar).result(timeout=5)
            finally:
                liberar_primeiro.set()
            resultado_primeiro = primeiro.result(timeout=5)

        self.assertEqual(len(envios), 1)
        self.assertEqual(resultado_primeiro["enviados"], 1)
        self.assertEqual(segundo, {"total": 1, "enviados": 0, "falhas": 0, "pulados": 1})

    def test_falha_ao_registrar_entrega_nao_libera_reenvio(self):
        self.cadastrar_usuarios()
        salvar_envio = self.campanha.salvar_envio
        envios = []

        def falhar_gravacao(conn, campanha_id, usuario, telefone, status, mensagem, erro=None):
            if status == "enviado":
                raise sqlite3.OperationalError("gravacao indisponivel")
            return salvar_envio(conn, campanha_id, usuario, telefone, status, mensagem, erro)

        with patch.object(self.campanha, "salvar_envio", falhar_gravacao), patch.object(
            self.campanha,
            "enviar_whatsapp",
            lambda numero, mensagem: envios.append((numero, mensagem)),
        ):
            with self.assertRaisesRegex(sqlite3.OperationalError, "gravacao indisponivel"):
                self.campanha.enviar_campanha("teste", "Ola {nome}", 0, confirmar=True)

            resultado = self.campanha.enviar_campanha(
                "teste", "Ola {nome}", 0, confirmar=True, retry_failed=True
            )

        self.assertEqual(len(envios), 1)
        self.assertEqual(resultado["pulados"], 1)
        conn = self.abrir_banco()
        status = conn.execute("SELECT status FROM campanhas_whatsapp_envios").fetchone()[0]
        conn.close()
        self.assertEqual(status, "enviando")

    def test_interrupcao_preserva_reserva_e_retry_failed_nao_reenvia(self):
        self.cadastrar_usuarios()
        with patch.object(self.campanha, "enviar_whatsapp", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.campanha.enviar_campanha("teste", "Ola {nome}", 0, confirmar=True)

        conn = self.abrir_banco()
        conn.execute("UPDATE campanhas_whatsapp_envios SET data_hora = '2000-01-01 00:00:00'")
        conn.commit()
        conn.close()
        envios = []
        with patch.object(
            self.campanha,
            "enviar_whatsapp",
            lambda numero, mensagem: envios.append((numero, mensagem)),
        ):
            resultado = self.campanha.enviar_campanha(
                "teste", "Ola {nome}", 0, confirmar=True, retry_failed=True
            )

        self.assertEqual(resultado, {"total": 1, "enviados": 0, "falhas": 0, "pulados": 1})
        self.assertEqual(envios, [])

    def test_retry_failed_exige_opcao_e_nao_repete_apos_sucesso(self):
        self.cadastrar_usuarios()
        with patch.object(self.campanha, "enviar_whatsapp", side_effect=RuntimeError("indisponivel")):
            falha = self.campanha.enviar_campanha("teste", "Ola {nome}", 0, confirmar=True)

        envios = []
        with patch.object(
            self.campanha,
            "enviar_whatsapp",
            lambda numero, mensagem: envios.append((numero, mensagem)),
        ):
            sem_retry = self.campanha.enviar_campanha("teste", "Ola {nome}", 0, confirmar=True)
            com_retry = self.campanha.enviar_campanha(
                "teste", "Ola {nome}", 0, confirmar=True, retry_failed=True
            )
            repetida = self.campanha.enviar_campanha(
                "teste", "Ola {nome}", 0, confirmar=True, retry_failed=True
            )

        self.assertEqual(falha["falhas"], 1)
        self.assertEqual(sem_retry["pulados"], 1)
        self.assertEqual(com_retry["enviados"], 1)
        self.assertEqual(repetida["pulados"], 1)
        self.assertEqual(len(envios), 1)

    def test_http_em_andamento_nao_bloqueia_outra_campanha(self):
        self.cadastrar_usuarios()
        primeiro_enviando = threading.Event()
        liberar_primeiro = threading.Event()

        def enviar(numero, mensagem):
            if mensagem == "Primeira Maria":
                primeiro_enviando.set()
                if not liberar_primeiro.wait(timeout=5):
                    raise AssertionError("Primeiro envio nao foi liberado")

        with patch.object(self.campanha, "enviar_whatsapp", enviar), ThreadPoolExecutor(
            max_workers=2
        ) as executor:
            primeiro = executor.submit(
                self.campanha.enviar_campanha, "primeira", "Primeira {nome}", 0, confirmar=True
            )
            try:
                self.assertTrue(primeiro_enviando.wait(timeout=5))
                segundo = executor.submit(
                    self.campanha.enviar_campanha, "segunda", "Segunda {nome}", 0, confirmar=True
                ).result(timeout=5)
            finally:
                liberar_primeiro.set()
            resultado_primeiro = primeiro.result(timeout=5)

        self.assertEqual(resultado_primeiro["enviados"], 1)
        self.assertEqual(segundo["enviados"], 1)
        conn = self.abrir_banco()
        total = conn.execute(
            "SELECT COUNT(*) FROM campanhas_whatsapp_envios WHERE status = 'enviado'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(total, 2)


if __name__ == "__main__":
    unittest.main()
