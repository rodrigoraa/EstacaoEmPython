import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock


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
