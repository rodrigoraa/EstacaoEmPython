import importlib
import os
import sys
import unittest
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class WhatsAppServiceTest(unittest.TestCase):
    def setUp(self):
        os.environ["EVOLUTION_URL"] = "http://evolution.local"
        os.environ["EVOLUTION_API_KEY"] = "fake-key"
        os.environ["EVOLUTION_INSTANCE"] = "fake-instance"

        import services.whatsapp_service as whatsapp_service

        self.whatsapp_service = importlib.reload(whatsapp_service)
        post_patch = patch.object(self.whatsapp_service.requests, "post")
        post_patch.start()
        self.addCleanup(post_patch.stop)

    def tearDown(self):
        for chave in (
            "EVOLUTION_URL",
            "EVOLUTION_API_KEY",
            "EVOLUTION_INSTANCE",
        ):
            os.environ.pop(chave, None)

    def test_enviar_whatsapp_desativa_preview_de_link(self):
        response = Mock(status_code=200, text="ok")
        post = Mock(return_value=response)
        self.whatsapp_service.requests.post = post

        self.assertTrue(
            self.whatsapp_service.enviar_whatsapp(
                "+55 67 999999999",
                "Acesse: https://meteo.eesjv.com.br",
            )
        )

        post.assert_called_once()
        _, kwargs = post.call_args
        self.assertEqual(
            kwargs["json"],
            {
                "number": "5567999999999",
                "text": "Acesse: https://meteo.eesjv.com.br",
                "linkPreview": False,
            },
        )
        self.assertFalse(kwargs["allow_redirects"])

    def test_http_de_erro_informa_status_sem_expor_resposta_da_api(self):
        for codigo in (400, 401, 403, 404, 408, 429, 500, 503):
            with self.subTest(codigo=codigo):
                self.whatsapp_service.requests.post = Mock(
                    return_value=Mock(status_code=codigo, text="segredo da resposta")
                )
                with self.assertRaises(RuntimeError) as erro:
                    self.whatsapp_service.enviar_whatsapp("5567999999999", "Alerta")
                self.assertEqual(str(erro.exception), f"Erro HTTP {codigo} na Evolution API")

    def test_redirect_rejeitado_sem_seguir_ou_expor_destino(self):
        post = Mock(return_value=Mock(status_code=307, headers={"Location": "https://evil.invalid"}))
        self.whatsapp_service.requests.post = post
        with self.assertRaisesRegex(RuntimeError, "HTTP 307"):
            self.whatsapp_service.enviar_whatsapp("5567999999999", "Alerta")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        post.assert_called_once()

    def test_configuracao_invalida_nao_faz_chamada_http(self):
        for url, instance in (
            ("file:///tmp/evolution", "instance"),
            ("https://user:secret@example.test", "instance"),
            ("https://example.test?apikey=secret", "instance"),
            ("https://example.test/#fragment", "instance"),
            ("https://example.test", "../instance"),
            ("https://example.test", "instance?redirect=evil"),
            ("https://example.test", "instance/endpoint"),
        ):
            with self.subTest(url=url, instance=instance):
                self.whatsapp_service.requests.post = Mock()
                with patch.dict(os.environ, {"EVOLUTION_URL": url, "EVOLUTION_INSTANCE": instance}):
                    with self.assertRaises(RuntimeError) as erro:
                        self.whatsapp_service.enviar_whatsapp("5567999999999", "Alerta")
                self.assertNotIn("secret", str(erro.exception))
                self.whatsapp_service.requests.post.assert_not_called()

    def test_evolution_local_configurada_e_permitida(self):
        with patch.dict(os.environ, {"EVOLUTION_URL": "http://127.0.0.1:8081/base", "EVOLUTION_INSTANCE": "local-instance"}):
            config = self.whatsapp_service.obter_configuracao_evolution()
        self.assertEqual(config["url"], "http://127.0.0.1:8081/base")

    def test_erro_de_requisicao_nao_revela_detalhes(self):
        self.whatsapp_service.requests.post = Mock(
            side_effect=requests.RequestException("https://secret-host/?apikey=secret")
        )
        with self.assertRaises(RuntimeError) as erro:
            self.whatsapp_service.enviar_whatsapp("5567999999999", "Alerta")
        self.assertEqual(str(erro.exception), "Erro de conexão Evolution API")
        self.assertNotIn("secret-host", "".join(traceback.format_exception(erro.exception)))
