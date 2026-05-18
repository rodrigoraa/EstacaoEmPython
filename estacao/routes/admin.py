import hmac
import os
import secrets
import time

import bcrypt
import requests
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
from time_utils import formatar_local

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


def obter_status_evolution():
    evolution_url = os.environ.get("EVOLUTION_URL", "").rstrip("/")
    api_key = os.environ.get("EVOLUTION_API_KEY")
    instance = os.environ.get("EVOLUTION_INSTANCE")

    if not evolution_url or not api_key or not instance:
        faltando = []
        if not evolution_url:
            faltando.append("EVOLUTION_URL")
        if not api_key:
            faltando.append("EVOLUTION_API_KEY")
        if not instance:
            faltando.append("EVOLUTION_INSTANCE")

        return {
            "ok": False,
            "status": "nao_configurada",
            "estado": "Configuração incompleta",
            "detalhe": "Faltando: " + ", ".join(faltando),
        }

    url = f"{evolution_url}/instance/connectionState/{instance}"
    headers = {"apikey": api_key}

    try:
        resposta = requests.get(url, headers=headers, timeout=5)
    except requests.exceptions.RequestException as e:
        return {
            "ok": False,
            "status": "erro_conexao",
            "estado": "Sem resposta",
            "detalhe": str(e),
        }

    try:
        payload = resposta.json()
    except ValueError:
        payload = {}

    estado = (
        payload.get("instance", {}).get("state")
        or payload.get("state")
        or payload.get("status")
        or "desconhecido"
    )

    return {
        "ok": resposta.ok and str(estado).lower() in ("open", "connected"),
        "status": "respondendo" if resposta.ok else "erro_http",
        "estado": estado,
        "detalhe": f"HTTP {resposta.status_code}",
    }


def formatar_data_admin(valor, assume_utc=True):
    return formatar_local(valor, assume_utc=assume_utc)


def preparar_eventos_admin(linhas, assume_utc=True):
    resultado = []
    for linha in linhas:
        item = dict(linha)
        item["data_hora_exibicao"] = formatar_data_admin(
            item.get("data_hora"), assume_utc=assume_utc
        )
        resultado.append(item)
    return resultado


def preparar_historico_admin(linhas):
    resultado = []
    for linha in linhas:
        item = dict(linha)
        valor = item.get("data_hora_local") or item.get("data_hora")
        item["data_hora_exibicao"] = formatar_data_admin(valor, assume_utc=False)
        resultado.append(item)
    return resultado


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alertas_envios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
            usuario_id INTEGER,
            nome TEXT,
            telefone TEXT,
            status TEXT NOT NULL,
            mensagem TEXT,
            erro TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cadastro_eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
            acao TEXT NOT NULL,
            usuario_id INTEGER,
            nome TEXT,
            telefone TEXT,
            endereco TEXT,
            receber_whatsapp INTEGER,
            detalhe TEXT
        )
        """
    )
    conn.commit()
    usuarios = conn.execute("SELECT * FROM usuarios ORDER BY id DESC").fetchall()
    historico = conn.execute(
        "SELECT * FROM historico_clima ORDER BY id DESC LIMIT 5"
    ).fetchall()
    alertas = conn.execute(
        "SELECT * FROM alertas_envios ORDER BY id DESC LIMIT 30"
    ).fetchall()
    eventos_cadastro = conn.execute(
        "SELECT * FROM cadastro_eventos ORDER BY id DESC LIMIT 30"
    ).fetchall()
    conn.close()

    evolution_status = obter_status_evolution()

    return render_template(
        "admin_painel.html",
        usuarios=usuarios,
        historico=preparar_historico_admin(historico),
        alertas=preparar_eventos_admin(alertas, assume_utc=True),
        eventos_cadastro=preparar_eventos_admin(eventos_cadastro, assume_utc=True),
        evolution_status=evolution_status,
    )
