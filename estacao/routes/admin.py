import hmac
import os
import secrets
import time

import bcrypt
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import database
from extensions import limiter

admin_routes = Blueprint("admin", __name__)

SENHA_ADMIN = os.environ.get("ADMIN_PASSWORD")
SENHA_ADMIN_HASH = os.environ.get("ADMIN_PASSWORD_HASH")

if not SENHA_ADMIN and not SENHA_ADMIN_HASH:
    raise RuntimeError("ADMIN_PASSWORD ou ADMIN_PASSWORD_HASH não configurado")


def gerar_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@admin_routes.app_context_processor
def injetar_csrf_token():
    return {"csrf_token": gerar_csrf_token}


def validar_csrf():
    token_form = request.form.get("csrf_token", "")
    token_sessao = session.get("csrf_token", "")

    if not token_form or not token_sessao:
        abort(403)

    if not hmac.compare_digest(token_form, token_sessao):
        abort(403)


def senha_admin_valida(senha):
    senha = senha or ""

    if SENHA_ADMIN_HASH:
        try:
            return bcrypt.checkpw(senha.encode("utf-8"), SENHA_ADMIN_HASH.encode("utf-8"))
        except ValueError:
            return False

    if not SENHA_ADMIN:
        return False

    return hmac.compare_digest(senha, SENHA_ADMIN)


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


@admin_routes.route("/admin/deletar/<int:id>", methods=["POST"])
@limiter.limit("20 per hour")
def deletar_usuario(id):
    if not admin_autenticado():
        return redirect(url_for("admin.admin"))

    validar_csrf()

    conn = database.get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM usuarios WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    flash("Utilizador removido com sucesso.")
    return redirect(url_for("admin.admin"))


@admin_routes.route("/admin/logout", methods=["POST"])
@limiter.limit("20 per hour")
def admin_logout():
    if admin_autenticado():
        validar_csrf()

    session.clear()
    return redirect(url_for("admin.admin"))


@admin_routes.route("/admin", methods=["GET", "POST"])
@limiter.limit("5 per 15 minutes", methods=["POST"])
def admin():
    if request.method == "POST":
        validar_csrf()

        if senha_admin_valida(request.form.get("senha")):
            session.clear()
            session["logado"] = True
            session["ultimo_acesso"] = time.time()
            session["csrf_token"] = secrets.token_urlsafe(32)
            session.permanent = True
            return redirect(url_for("admin.admin"))

        flash("Senha incorreta!")

    if not admin_autenticado():
        return render_template("admin_login.html")

    conn = database.get_db()
    usuarios = conn.execute("SELECT * FROM usuarios ORDER BY id DESC").fetchall()
    historico = conn.execute(
        "SELECT * FROM historico_clima ORDER BY id DESC LIMIT 5"
    ).fetchall()
    conn.close()

    return render_template("admin_painel.html", usuarios=usuarios, historico=historico)
