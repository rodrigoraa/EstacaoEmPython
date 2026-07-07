import hmac
import os
import secrets
import sqlite3
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
from time_utils import agora_local, formatar_local, para_local
from unsubscribe_tokens import normalizar_telefone

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


def garantir_estruturas_admin(conn):
    database.garantir_tabela_usuarios(conn)
    database.garantir_tabela_alertas_envios(conn)
    database.garantir_tabela_alertas_fila(conn)
    database.garantir_tabela_cadastro_eventos(conn)


def registrar_evento_admin(
    conn,
    acao,
    usuario_id=None,
    nome=None,
    telefone=None,
    endereco=None,
    receber_whatsapp=None,
    detalhe=None,
):
    database.registrar_cadastro_evento(
        conn,
        acao,
        usuario_id=usuario_id,
        nome=nome,
        telefone=telefone,
        endereco=endereco,
        receber_whatsapp=receber_whatsapp,
        detalhe=detalhe,
    )


def preparar_usuarios_admin(linhas):
    resultado = []
    for linha in linhas:
        item = dict(linha)
        item["ativo"] = 1 if item.get("ativo") is None else item.get("ativo")
        item["receber_whatsapp"] = item.get("receber_whatsapp") or 0
        item["criado_em_exibicao"] = formatar_data_admin(
            item.get("criado_em"), assume_utc=True
        )
        resultado.append(item)
    return resultado


def resumo_usuarios_admin(conn):
    return {
        "total_usuarios": conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0],
        "usuarios_ativos": conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE ativo = 1 OR ativo IS NULL"
        ).fetchone()[0],
        "usuarios_whatsapp": conn.execute(
            """
            SELECT COUNT(*)
            FROM usuarios
            WHERE (ativo = 1 OR ativo IS NULL)
            AND receber_whatsapp = 1
            """
        ).fetchone()[0],
        "usuarios_pausados": conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE ativo = 0"
        ).fetchone()[0],
    }


def minutos_desde(valor, assume_utc=True):
    dt = para_local(valor, assume_utc=assume_utc)
    if not dt:
        return None

    segundos = (agora_local() - dt).total_seconds()
    return max(0, int(segundos // 60))


def texto_tempo_decorrido(minutos):
    if minutos is None:
        return "sem registro"
    if minutos < 1:
        return "agora"
    if minutos == 1:
        return "1 min"
    if minutos < 60:
        return f"{minutos} min"

    horas = minutos // 60
    resto = minutos % 60
    if resto == 0:
        return f"{horas} h"
    return f"{horas} h {resto} min"


def obter_saude_sistema_admin(conn):
    limite_atraso_minutos = int(os.environ.get("ADMIN_UPDATER_ATRASO_MINUTOS", "5"))

    ultima_leitura = conn.execute(
        """
        SELECT id, data_hora, data_hora_local
        FROM historico_clima
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    data_leitura = None
    minutos_leitura = None
    if ultima_leitura:
        data_leitura = ultima_leitura["data_hora_local"] or ultima_leitura["data_hora"]
        minutos_leitura = minutos_desde(data_leitura, assume_utc=False)

    filas = conn.execute(
        """
        SELECT status, COUNT(*) as total
        FROM alertas_fila
        GROUP BY status
        """
    ).fetchall()
    totais_fila = {row["status"]: row["total"] for row in filas}

    ultimo_envio = conn.execute(
        """
        SELECT data_hora, status
        FROM alertas_envios
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    pendentes = totais_fila.get("pendente", 0)
    enviando = totais_fila.get("enviando", 0)
    falhou = totais_fila.get("falhou", 0)

    coleta_ok = minutos_leitura is not None and minutos_leitura <= limite_atraso_minutos
    fila_ok = pendentes == 0 and enviando == 0 and falhou == 0

    if coleta_ok and fila_ok:
        status_geral = "ok"
        status_texto = "Operando"
    elif minutos_leitura is None:
        status_geral = "atencao"
        status_texto = "Sem leituras"
    else:
        status_geral = "atencao"
        status_texto = "Atenção"

    return {
        "status_geral": status_geral,
        "status_texto": status_texto,
        "coleta_ok": coleta_ok,
        "ultima_leitura": formatar_data_admin(data_leitura, assume_utc=False)
        if data_leitura
        else "-",
        "ultima_leitura_tempo": texto_tempo_decorrido(minutos_leitura),
        "limite_atraso_minutos": limite_atraso_minutos,
        "fila_pendentes": pendentes,
        "fila_enviando": enviando,
        "fila_falhou": falhou,
        "fila_ok": fila_ok,
        "ultimo_envio": formatar_data_admin(ultimo_envio["data_hora"], assume_utc=True)
        if ultimo_envio
        else "-",
        "ultimo_envio_status": ultimo_envio["status"] if ultimo_envio else "-",
    }


@admin_routes.route("/admin/deletar/<int:id>", methods=["POST"])
@limiter.limit("20 per hour")
def deletar_usuario(id):
    if not admin_autenticado():
        return redirect(url_for("admin.admin"))

    validar_csrf()

    conn = database.get_db()
    garantir_estruturas_admin(conn)
    cursor = conn.cursor()
    usuario = cursor.execute(
        """
        SELECT id, nome, telefone, endereco, receber_whatsapp
        FROM usuarios
        WHERE id = ?
        """,
        (id,),
    ).fetchone()

    if usuario:
        registrar_evento_admin(
            conn,
            "exclusao_admin",
            usuario_id=usuario["id"],
            nome=usuario["nome"],
            telefone=usuario["telefone"],
            endereco=usuario["endereco"],
            receber_whatsapp=usuario["receber_whatsapp"],
            detalhe="Usuario removido pelo painel administrativo",
        )

    cursor.execute("DELETE FROM usuarios WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    flash("Usuário removido com sucesso.")
    return redirect(url_for("admin.admin", _anchor="usuarios"))


@admin_routes.route("/admin/usuarios/<int:id>/editar", methods=["POST"])
@limiter.limit("30 per hour")
def editar_usuario(id):
    if not admin_autenticado():
        return redirect(url_for("admin.admin"))

    validar_csrf()

    nome = request.form.get("nome", "").strip()
    telefone = normalizar_telefone(request.form.get("telefone"))
    endereco = request.form.get("endereco", "").strip()
    receber_whatsapp = 1 if request.form.get("receber_whatsapp") == "1" else 0
    ativo = 1 if request.form.get("ativo") == "1" else 0

    if not nome or not telefone or not endereco:
        flash("Preencha nome, telefone e endereço antes de salvar.")
        return redirect(url_for("admin.admin", _anchor="usuarios"))

    conn = database.get_db()
    garantir_estruturas_admin(conn)

    usuario = conn.execute("SELECT id FROM usuarios WHERE id = ?", (id,)).fetchone()
    if not usuario:
        conn.close()
        flash("Usuário não encontrado.")
        return redirect(url_for("admin.admin", _anchor="usuarios"))

    try:
        conn.execute(
            """
            UPDATE usuarios
            SET nome = ?,
                telefone = ?,
                endereco = ?,
                receber_whatsapp = ?,
                ativo = ?
            WHERE id = ?
            """,
            (nome, telefone, endereco, receber_whatsapp, ativo, id),
        )
        registrar_evento_admin(
            conn,
            "edicao_admin",
            usuario_id=id,
            nome=nome,
            telefone=telefone,
            endereco=endereco,
            receber_whatsapp=receber_whatsapp,
            detalhe="Dados atualizados pelo painel administrativo",
        )
        conn.commit()
        flash("Usuário atualizado com sucesso.")
    except sqlite3.IntegrityError:
        conn.rollback()
        flash("Este telefone já está cadastrado para outro usuário.")
    finally:
        conn.close()

    return redirect(url_for("admin.admin", _anchor="usuarios"))


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
    garantir_estruturas_admin(conn)
    conn.commit()
    usuarios = conn.execute("SELECT * FROM usuarios ORDER BY id DESC").fetchall()
    resumo_usuarios = resumo_usuarios_admin(conn)
    saude_sistema = obter_saude_sistema_admin(conn)
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
        usuarios=preparar_usuarios_admin(usuarios),
        resumo_usuarios=resumo_usuarios,
        saude_sistema=saude_sistema,
        historico=preparar_historico_admin(historico),
        alertas=preparar_eventos_admin(alertas, assume_utc=True),
        eventos_cadastro=preparar_eventos_admin(eventos_cadastro, assume_utc=True),
        evolution_status=evolution_status,
    )
