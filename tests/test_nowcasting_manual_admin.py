"""Contrato administrativo para publicar um alerta já recebido pelo administrador."""

from security_helpers import autenticar_admin
import copy
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from werkzeug.datastructures import MultiDict


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


class ManualAlertAdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "ESTACAO_DB": str(Path(self.tmp.name) / "manual-admin.db"),
            "SECRET_KEY": "teste-manual", "RATELIMIT_ENABLED": "false",
            "NOWCASTING_ENABLED": "false", "NOWCASTING_ALERTS_ENABLED": "false",
            "NOWCASTING_TEST_ALERTS_ENABLED": "true",
        })
        env.start()
        self.addCleanup(env.stop)
        import database
        self.database = importlib.reload(database)
        self.database.init_db()
        from app import create_app
        self.app = create_app({"TESTING": True, "RATELIMIT_ENABLED": False})
        self.client = self.app.test_client()
        self.status = {
            "available": True, "reason": "available", "can_send": True,
            "alert": {"id": "alerta-salvo-1", "mensagem": "Chuva próxima.\n\nTexto original <seguro>.",
                      "received_at": "2026-10-23T16:30:00+00:00", "enqueued_at": None,
                      "enfileirados": 0, "alert_level": "ATENCAO"},
        }
        status_patch = mock.patch("routes.nowcasting.obter_status_alerta_manual",
                                  side_effect=lambda config: copy.deepcopy(self.status), create=True)
        self.status_mock = status_patch.start()
        self.addCleanup(status_patch.stop)
        send_patch = mock.patch("routes.nowcasting.enfileirar_alerta_manual", create=True,
                               return_value={"reason": "enqueued", "enfileirados": 2})
        self.send_mock = send_patch.start()
        self.addCleanup(send_patch.stop)

    def login(self):
        autenticar_admin(self.client, csrf_token="csrf-manual")

    def post(self, alert_id="alerta-salvo-1", **kwargs):
        kwargs.setdefault("data", {"csrf_token": "csrf-manual"})
        return self.client.post(f"/admin/nowcasting/alertas/{alert_id}/enviar", **kwargs)

    def test_superficies_e_envio_exigem_sessao_admin(self):
        with self.client.session_transaction() as sessao:
            sessao["csrf_token"] = "csrf-manual"
        self.assertEqual(self.client.get("/admin/monitoramento").status_code, 302)
        self.assertEqual(self.client.get("/admin/api/nowcasting/status").status_code, 401)
        self.assertEqual(self.post().status_code, 401)
        self.send_mock.assert_not_called()

    def test_envio_exige_post_e_csrf_valido(self):
        self.login()
        self.assertEqual(self.client.get("/admin/nowcasting/alertas/alerta-salvo-1/enviar").status_code, 405)
        for data in ({}, {"csrf_token": "incorreto"}):
            with self.subTest(data=data):
                self.assertEqual(self.post(data=data).status_code, 403)
        self.send_mock.assert_not_called()

    def test_formulario_rejeita_texto_destinatarios_e_tokens_duplicados(self):
        self.login()
        invalidos = [
            {"csrf_token": "csrf-manual", "mensagem": "Substituída"},
            {"csrf_token": "csrf-manual", "telefone": "67999999999"},
            {"csrf_token": "csrf-manual", "alert_id": "outro"},
            MultiDict([("csrf_token", "csrf-manual"), ("csrf_token", "csrf-manual")]),
        ]
        for data in invalidos:
            with self.subTest(data=data):
                self.assertEqual(self.post(data=data).status_code, 400)
        response = self.post(data=None, json={"csrf_token": "csrf-manual"},
                             headers={"X-CSRF-Token": "csrf-manual"})
        self.assertEqual(response.status_code, 400)
        self.send_mock.assert_not_called()

    def test_pagina_mostra_texto_original_escapado_horario_local_e_confirmacao(self):
        self.login()
        page = self.client.get("/admin/monitoramento")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        self.assertIn("Enviar este alerta a todos", text)
        self.assertIn("Chuva próxima.\n\nTexto original &lt;seguro&gt;.", text)
        self.assertIn("23/10/2026 12:30:00", text)
        self.assertIn('action="/admin/nowcasting/alertas/alerta-salvo-1/enviar"', text)
        self.assertIn('data-open-dialog="manual-alert-confirm"', text)
        self.assertIn('data-close-dialog="true"', text)
        self.assertIn("assinantes ativos do WhatsApp", text)
        self.assertIn("alertas automáticos continuam desativados", text)
        self.assertNotIn('name="mensagem"', text)
        self.assertNotIn('name="telefone"', text)

    def test_api_admin_inclui_candidato_sem_alterar_texto(self):
        self.login()
        response = self.client.get("/admin/api/nowcasting/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json().get("manual_alert"), self.status)

    def test_envio_usa_candidato_salvo_e_controle_efetivo_sem_ativar_automaticos(self):
        from services.runtime_alert_controls import obter_controles
        self.login()
        response = self.post()
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.location, "/admin/monitoramento")
        self.send_mock.assert_called_once()
        args = self.send_mock.call_args.args
        self.assertEqual(args[0], "alerta-salvo-1")
        self.assertFalse(args[1]["alerts_enabled"])
        self.assertFalse(obter_controles({"alerts_enabled": False})["public"]["enabled"])
        page = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("2 destinatários", page)
        self.assertIn("enfileirado", page)

    def test_controle_publico_efetivo_prevalece_sobre_configuracao_servidor(self):
        from services.runtime_alert_controls import salvar_controle
        self.login()
        salvar_controle("public", True)
        self.send_mock.return_value = {"reason": "public_alerts_enabled", "enfileirados": 0}
        response = self.post(follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.send_mock.call_args.args[1]["alerts_enabled"])
        self.assertIn("alertas públicos automáticos estão ativados", response.get_data(as_text=True))

    def test_candidato_desconhecido_retorna_404(self):
        self.login()
        self.send_mock.return_value = {"reason": "not_found", "enfileirados": 0}
        self.assertEqual(self.post("desconhecido").status_code, 404)

    def test_repeticao_nao_anuncia_novo_envio(self):
        self.login()
        self.send_mock.return_value = {"reason": "duplicate", "enfileirados": 2, "duplicado": True}
        page = self.post(follow_redirects=True).get_data(as_text=True)
        self.assertIn("já foi enfileirado", page)

    def test_rejeicoes_mostram_motivo_ao_administrador(self):
        self.login()
        motivos = {
            "expired": "expirou", "invalidated": "condições atuais",
            "unavailable": "temporariamente indisponível", "no_recipients": "Nenhum assinante ativo",
        }
        for reason, esperado in motivos.items():
            with self.subTest(reason=reason):
                self.send_mock.return_value = {"reason": reason, "enfileirados": 0}
                page = self.post(follow_redirects=True).get_data(as_text=True)
                self.assertIn(esperado, page)

    def test_falha_no_envio_exibe_erro_seguro(self):
        self.login()
        self.send_mock.side_effect = RuntimeError("telefone 5567999999999 segredo")
        with self.assertLogs("routes.nowcasting", level="ERROR") as logs:
            page = self.post(follow_redirects=True).get_data(as_text=True)
        self.assertIn("temporariamente indisponível", page)
        self.assertNotIn("5567999999999", page + "\n".join(logs.output))

    def test_status_indisponivel_preserva_pagina_e_api_sem_botao_de_envio(self):
        self.login()
        self.status_mock.side_effect = RuntimeError("segredo")
        with self.assertLogs("routes.nowcasting", level="WARNING"):
            page = self.client.get("/admin/monitoramento")
            api = self.client.get("/admin/api/nowcasting/status")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Enviar este alerta a todos", page.get_data(as_text=True))
        self.assertIn("temporariamente indisponível", page.get_data(as_text=True))
        self.assertEqual(api.get_json()["manual_alert"],
                         {"available": False, "reason": "unavailable", "alert": None, "can_send": False})

    def test_estados_sem_autorizacao_ocultam_formulario_e_explicam_motivo(self):
        self.login()
        casos = [
            ("public_alerts_enabled", "alertas públicos automáticos estão ativados"),
            ("expired", "expirou"), ("invalidated", "condições atuais"),
            ("unavailable", "temporariamente indisponível"),
        ]
        self.status["can_send"] = False
        for reason, esperado in casos:
            with self.subTest(reason=reason):
                self.status["reason"] = reason
                text = self.client.get("/admin/monitoramento").get_data(as_text=True)
                self.assertNotIn('action="/admin/nowcasting/alertas/', text)
                self.assertNotIn("Enviar este alerta a todos", text)
                self.assertIn(esperado, text)

    def test_controle_ligado_oculta_envio_mesmo_com_status_desatualizado(self):
        from services.runtime_alert_controls import salvar_controle
        self.login()
        salvar_controle("public", True)
        text = self.client.get("/admin/monitoramento").get_data(as_text=True)
        self.assertNotIn('action="/admin/nowcasting/alertas/', text)
        self.assertNotIn("Enviar este alerta a todos", text)

    def test_alerta_ja_enfileirado_mostra_contagem_e_impede_reenvio(self):
        self.login()
        self.status.update(can_send=False, reason="duplicate")
        self.status["alert"].update(enqueued_at="2026-10-23T16:35:00+00:00", enfileirados=2)
        text = self.client.get("/admin/monitoramento").get_data(as_text=True)
        self.assertIn("2 destinatários", text)
        self.assertIn("23/10/2026 12:35:00", text)
        self.assertNotIn('action="/admin/nowcasting/alertas/', text)


if __name__ == "__main__":
    unittest.main()
