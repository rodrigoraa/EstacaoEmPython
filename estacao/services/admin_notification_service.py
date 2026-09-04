"""Envio direto e restrito de notificacoes administrativas."""

from __future__ import annotations

import os

from unsubscribe_tokens import telefone_com_codigo_pais


def obter_admin_alert_phone():
    """Le o unico destinatario administrativo sem expo-lo em configuracoes web."""
    return os.environ.get("ADMIN_ALERT_PHONE", "").strip()


def enviar_mensagem_admin(telefone, mensagem):
    """Normaliza e envia diretamente, sem passar pela fila de assinantes."""
    from services.whatsapp_service import enviar_whatsapp

    enviar_whatsapp(telefone_com_codigo_pais(telefone), mensagem)

