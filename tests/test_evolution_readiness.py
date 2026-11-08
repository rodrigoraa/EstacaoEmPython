import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


def resposta(estado="open", status=200):
    return mock.Mock(status_code=status, json=mock.Mock(return_value={"instance": {"state": estado}}))


class EvolutionReadinessServiceTest(unittest.TestCase):
    def setUp(self):
        ambiente = mock.patch.dict(os.environ, {
            "EVOLUTION_URL": "http://evolution.local",
            "EVOLUTION_API_KEY": "segredo-teste",
            "EVOLUTION_INSTANCE": "escola-teste",
        })
        ambiente.start()
        self.addCleanup(ambiente.stop)
        import services.whatsapp_service
        self.service = importlib.reload(services.whatsapp_service)

    def test_consulta_somente_estado_da_instancia_validada(self):
        with (
            mock.patch.object(self.service.requests, "get", return_value=resposta()) as get,
            mock.patch.object(self.service.requests, "post") as post,
        ):
            pronta, _ = self.service.verificar_prontidao_evolution()
        self.assertTrue(pronta)
        get.assert_called_once_with(
            "http://evolution.local/instance/connectionState/escola-teste",
            headers={"apikey": "segredo-teste"}, timeout=5, allow_redirects=False,
        )
        post.assert_not_called()

    def test_http_sucesso_com_instancia_nao_conectada_nao_libera_envios(self):
        for corpo in (
            {"instance": {"state": "connecting"}}, {"instance": {"state": "close"}},
            {"instance": None}, {"state": "open"}, {}, [], None,
        ):
            with self.subTest(corpo=corpo):
                r = mock.Mock(status_code=200)
                r.json.return_value = corpo
                with mock.patch.object(self.service.requests, "get", return_value=r):
                    pronta, _ = self.service.verificar_prontidao_evolution()
                self.assertFalse(pronta)

    def test_http_erro_nao_expoe_corpo_da_resposta(self):
        for status in (302, 401, 404, 429, 500, 503):
            with self.subTest(status=status):
                r = mock.Mock(status_code=status, text="segredo-teste telefone mensagem")
                r.json.side_effect = AssertionError("Não deve interpretar corpo de erro")
                with mock.patch.object(self.service.requests, "get", return_value=r):
                    pronta, motivo = self.service.verificar_prontidao_evolution()
                self.assertFalse(pronta)
                self.assertIn(str(status), motivo)
                self.assertNotIn("segredo-teste", motivo)

    def test_falha_de_transporte_e_json_invalido_adiam_sem_expor_detalhes(self):
        for erro in (requests.ConnectionError("apikey=segredo-teste"), requests.Timeout("segredo-teste")):
            with self.subTest(tipo=type(erro).__name__):
                with mock.patch.object(self.service.requests, "get", side_effect=erro):
                    pronta, motivo = self.service.verificar_prontidao_evolution()
                self.assertFalse(pronta)
                self.assertNotIn("segredo-teste", motivo)
        r = resposta()
        r.json.side_effect = ValueError("segredo-teste")
        with mock.patch.object(self.service.requests, "get", return_value=r):
            pronta, motivo = self.service.verificar_prontidao_evolution()
        self.assertFalse(pronta)
        self.assertNotIn("segredo-teste", motivo)

    def test_configuracao_incompleta_nao_faz_requisicao(self):
        with mock.patch.dict(os.environ, {"EVOLUTION_API_KEY": ""}):
            with mock.patch.object(self.service.requests, "get") as get:
                pronta, motivo = self.service.verificar_prontidao_evolution()
        self.assertFalse(pronta)
        self.assertIn("EVOLUTION_API_KEY", motivo)
        get.assert_not_called()

    def test_destino_invalido_nao_faz_requisicao_nem_expoe_configuracao(self):
        for ambiente in (
            {"EVOLUTION_INSTANCE": "escola / segredo-teste"},
            {"EVOLUTION_URL": "http://usuario:segredo-teste@evolution.local"},
        ):
            with self.subTest(variavel=next(iter(ambiente))):
                with (
                    mock.patch.dict(os.environ, ambiente),
                    mock.patch.object(self.service.requests, "get") as get,
                ):
                    pronta, motivo = self.service.verificar_prontidao_evolution()
                self.assertFalse(pronta)
                self.assertNotIn("segredo-teste", motivo)
                get.assert_not_called()


class EvolutionReadinessWorkerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ambiente = mock.patch.dict(os.environ, {
            "ESTACAO_DB": str(Path(tmp.name) / "test.db"),
            "EVOLUTION_URL": "http://localhost:8080",
            "EVOLUTION_API_KEY": "segredo-teste", "EVOLUTION_INSTANCE": "teste",
        })
        ambiente.start()
        self.addCleanup(ambiente.stop)
        import database
        import services.whatsapp_service
        import workers.whatsapp_sender
        self.db = importlib.reload(database)
        self.db.init_db()
        self.service = importlib.reload(services.whatsapp_service)
        self.sender = importlib.reload(workers.whatsapp_sender)
        self.sender.log = lambda mensagem: None
        self.inserir()
        self.post = mock.patch.object(self.service.requests, "post", return_value=mock.Mock(status_code=201))
        self.enviar = self.post.start()
        self.addCleanup(self.post.stop)

    def inserir(self):
        with self.db.get_db() as conn:
            conn.execute("INSERT INTO alertas_fila (telefone, mensagem) VALUES ('5567999999999', 'Teste')")

    def filas(self):
        with self.db.get_db() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM alertas_fila ORDER BY id")]

    def test_modos_finitos_nao_consumem_tentativas_enquanto_api_indisponivel(self):
        antes = self.filas()
        for argumentos in (("--once",), ("--limite", "2")):
            with self.subTest(argumentos=argumentos):
                with (
                    mock.patch.object(sys, "argv", ["sender", *argumentos]),
                    mock.patch.object(self.service.requests, "get", side_effect=requests.ConnectionError()),
                ):
                    codigo = self.sender.main()
                self.assertEqual(self.filas(), antes)
                self.assertEqual(codigo, 1)
        with self.db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_envios").fetchone()[0], 0)
        self.enviar.assert_not_called()

    def test_instancia_conectando_nao_consumida_e_open_libera_primeira_tentativa(self):
        with mock.patch.object(sys, "argv", ["sender", "--once"]):
            with mock.patch.object(self.service.requests, "get", return_value=resposta("connecting")):
                self.sender.main()
            self.assertEqual(self.filas()[0]["tentativas"], 0)
            with mock.patch.object(self.service.requests, "get", return_value=resposta()):
                self.sender.main()
        self.assertEqual((self.filas()[0]["status"], self.filas()[0]["tentativas"]), ("enviado", 1))
        self.assertEqual(self.enviar.call_count, 1)

    def test_continuo_aguarda_e_revalida_antes_de_cada_lote(self):
        estados = []

        class Encerrar(BaseException):
            pass

        def pausa(_):
            estados.append([(r["status"], r["tentativas"]) for r in self.filas()])
            if len(estados) == 2:
                self.inserir()
            if len(estados) == 4:
                raise Encerrar()

        with (
            mock.patch.object(self.service.requests, "get", side_effect=[
                requests.ConnectionError(), resposta(), resposta("close"), resposta(),
            ]),
            mock.patch.object(self.sender.time, "sleep", side_effect=pausa),
        ):
            with self.assertRaises(Encerrar):
                self.sender.rodar_continuamente(intervalo=0)
        self.assertEqual(estados[0], [("pendente", 0)])
        self.assertEqual(estados[1], [("enviado", 1)])
        self.assertEqual(estados[2], [("enviado", 1), ("pendente", 0)])
        self.assertEqual(estados[3], [("enviado", 1), ("enviado", 1)])
        self.assertEqual(self.enviar.call_count, 2)

    def test_falha_de_post_apos_prontidao_mantem_retry_normal(self):
        self.enviar.return_value = mock.Mock(status_code=500)
        with (
            mock.patch.object(sys, "argv", ["sender", "--once"]),
            mock.patch.object(self.service.requests, "get", return_value=resposta()),
        ):
            self.sender.main()
        row = self.filas()[0]
        self.assertEqual((row["status"], row["tentativas"]), ("pendente", 1))
        self.assertIsNotNone(row["proxima_tentativa_em"])
        with self.db.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_envios WHERE status='falhou'").fetchone()[0], 1)
