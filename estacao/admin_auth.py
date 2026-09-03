"""Autorizacao compartilhada das superficies administrativas."""

import time
from functools import wraps

from flask import current_app, flash, jsonify, redirect, session, url_for


def admin_autenticado():
    if not session.get("logado"):
        return False

    ultimo_acesso = session.get("ultimo_acesso")
    if not ultimo_acesso:
        session.clear()
        return False

    if time.time() - ultimo_acesso > current_app.permanent_session_lifetime.total_seconds():
        session.clear()
        flash("Sessão expirada. Faça login novamente.")
        return False

    session["ultimo_acesso"] = time.time()
    session.permanent = True
    return True


def admin_page_required(view):
    """Redireciona páginas privadas ao login administrativo."""
    @wraps(view)
    def protegida(*args, **kwargs):
        if not admin_autenticado():
            return redirect(url_for("admin.admin"))
        return view(*args, **kwargs)

    return protegida


def admin_api_required(view):
    """Bloqueia APIs e arquivos privados sem devolver o conteúdo protegido."""
    @wraps(view)
    def protegida(*args, **kwargs):
        if not admin_autenticado():
            return jsonify({"error": "admin_authentication_required"}), 401
        return view(*args, **kwargs)

    return protegida
