"""Protecao CSRF compartilhada por todos os formularios."""

import hmac
import secrets

from flask import abort, request, session


def gerar_csrf_token():
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validar_csrf():
    recebido = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    esperado = session.get("csrf_token", "")
    if not isinstance(recebido, str) or not isinstance(esperado, str):
        abort(403)
    if (not recebido or not esperado or len(recebido) > 256
            or not hmac.compare_digest(recebido.encode("utf-8"), esperado.encode("utf-8"))):
        abort(403)
