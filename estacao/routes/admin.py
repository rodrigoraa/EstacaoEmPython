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
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import database
from extensions import limiter
from time_utils import agora_local as agora_local
from time_utils import formatar_local, minutos_desde
from unsubscribe_tokens import normalizar_telefone
from config import env_str
from admin_auth import admin_autenticado

admin_routes = Blueprint("admin", __name__)

ADMIN_ABAS = {
    "visao-geral",
    "usuarios",
    "cadastros",
    "eventos",
    "envios",
    "historico",
}
ADMIN_ITENS_POR_PAGINA = (10, 25, 50)


def obter_inteiro_query(nome, padrao=1):
    try:
        return max(1, int(request.args.get(nome, padrao)))
    except (TypeError, ValueError):
        return padrao


def obter_itens_por_pagina():
    valor = obter_inteiro_query("por_pagina", ADMIN_ITENS_POR_PAGINA[0])
    if valor not in ADMIN_ITENS_POR_PAGINA:
        return ADMIN_ITENS_POR_PAGINA[0]
    return valor


def termo_like(valor):
    escapado = valor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escapado}%"


def consultar_paginado(
    conn,
    tabela,
    pagina,
    por_pagina,
    where_sql="",
    parametros=(),
    order_by="id DESC",
):
    total = conn.execute(
        f"SELECT COUNT(*) FROM {tabela} {where_sql}", parametros
    ).fetchone()[0]
    total_paginas = max(1, (total + por_pagina - 1) // por_pagina)
    pagina = min(max(1, pagina), total_paginas)
    offset = (pagina - 1) * por_pagina
    linhas = conn.execute(
        f"SELECT * FROM {tabela} {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?",
        (*parametros, por_pagina, offset),
    ).fetchall()

    numeros = {1, total_paginas}
    numeros.update(range(max(1, pagina - 2), min(total_paginas, pagina + 2) + 1))
    itens = []
    anterior = None
    for numero in sorted(numeros):
        if anterior is not None and numero - anterior > 1:
            itens.append(None)
        itens.append(numero)
        anterior = numero

    return linhas, {
        "pagina": pagina,
        "por_pagina": por_pagina,
        "total": total,
        "total_paginas": total_paginas,
        "inicio": 0 if total == 0 else offset + 1,
        "fim": min(offset + por_pagina, total),
        "tem_anterior": pagina > 1,
        "tem_proxima": pagina < total_paginas,
        "itens": itens,
    }


def parametros_retorno_usuarios():
    parametros = {"aba": "usuarios"}
    for nome in ("pagina", "por_pagina", "busca", "filtro"):
        valor = request.form.get(f"retorno_{nome}", "").strip()
        if valor:
            parametros[nome] = valor
    return parametros


def redirecionar_usuarios():
    return redirect(url_for("admin.admin", **parametros_retorno_usuarios()))


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
    senha_admin = env_str("ADMIN_PASSWORD")
    senha_admin_hash = env_str("ADMIN_PASSWORD_HASH")

    if senha_admin_hash:
        try:
            return bcrypt.checkpw(senha.encode("utf-8"), senha_admin_hash.encode("utf-8"))
        except ValueError:
            return False

    if not senha_admin:
        return False

    return hmac.compare_digest(senha, senha_admin)


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
        valor_data = (
            item.get("data_hora")
            or item.get("ocorrido_em_local")
            or item.get("criado_em")
        )
        item["data_hora_exibicao"] = formatar_data_admin(
            valor_data,
            assume_utc=(assume_utc and not item.get("ocorrido_em_local")),
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
    database.garantir_tabela_alertas_eventos(conn)
    database.garantir_tabela_cadastro_eventos(conn)


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
            AND (status_cadastro = 'ativo' OR status_cadastro IS NULL)
            """
        ).fetchone()[0],
        "usuarios_pausados": conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE ativo = 0"
        ).fetchone()[0],
    }


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

    metricas_fila = conn.execute(
        """
        SELECT
            MIN(CASE WHEN status IN ('pendente', 'enviando') THEN criado_em END) AS mais_antigo,
            SUM(CASE WHEN status = 'falhou' AND atualizado_em >= datetime('now', '-1 day') THEN 1 ELSE 0 END) AS falhas_24h,
            AVG(CASE WHEN enviado_em IS NOT NULL
                THEN (julianday(enviado_em) - julianday(criado_em)) * 86400 END) AS media_entrega_segundos
        FROM alertas_fila
        """
    ).fetchone()
    estado_alertas = database.obter_estado_alertas() or {}
    minutos_item_antigo = minutos_desde(metricas_fila["mais_antigo"], assume_utc=True)

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
        "fila_mais_antiga": texto_tempo_decorrido(minutos_item_antigo),
        "falhas_24h": int(metricas_fila["falhas_24h"] or 0),
        "media_entrega_segundos": round(metricas_fila["media_entrega_segundos"] or 0),
        "aguardando_reset_vento": bool(estado_alertas.get("aguardando_reset_vento")),
        "aguardando_reset_chuva": bool(estado_alertas.get("aguardando_reset_chuva")),
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
        database.registrar_cadastro_evento(
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
    return redirecionar_usuarios()


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
        return redirecionar_usuarios()

    conn = database.get_db()
    garantir_estruturas_admin(conn)

    usuario = conn.execute("SELECT id FROM usuarios WHERE id = ?", (id,)).fetchone()
    if not usuario:
        conn.close()
        flash("Usuário não encontrado.")
        return redirecionar_usuarios()

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
        database.registrar_cadastro_evento(
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

    return redirecionar_usuarios()


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

    aba_ativa = request.args.get("aba", "visao-geral").strip().lower()
    if aba_ativa not in ADMIN_ABAS:
        aba_ativa = "visao-geral"

    pagina = obter_inteiro_query("pagina")
    por_pagina = obter_itens_por_pagina()
    busca = request.args.get("busca", "").strip()[:100]
    filtro = request.args.get("filtro", "todos").strip().lower()

    resumo_usuarios = resumo_usuarios_admin(conn)
    contagens = {
        "usuarios": resumo_usuarios["total_usuarios"],
        "cadastros": conn.execute("SELECT COUNT(*) FROM cadastro_eventos").fetchone()[0],
        "eventos": conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0],
        "envios": conn.execute("SELECT COUNT(*) FROM alertas_envios").fetchone()[0],
        "historico": conn.execute("SELECT COUNT(*) FROM historico_clima").fetchone()[0],
    }

    usuarios = []
    historico = []
    alertas = []
    eventos_alertas = []
    eventos_cadastro = []
    paginacao = None
    filtros = {"busca": busca, "filtro": filtro}
    saude_sistema = None

    if aba_ativa == "visao-geral":
        saude_sistema = obter_saude_sistema_admin(conn)
    elif aba_ativa == "usuarios":
        if filtro not in {"todos", "ativos", "pausados", "whatsapp", "pendentes"}:
            filtro = "todos"
        filtros["filtro"] = filtro
        condicoes = []
        parametros = []
        if busca:
            valor = termo_like(busca)
            condicoes.append(
                "(nome LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR telefone LIKE ? ESCAPE '\\' "
                "OR endereco LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            parametros.extend((valor, valor, valor))
        if filtro == "ativos":
            condicoes.append("(ativo = 1 OR ativo IS NULL)")
        elif filtro == "pausados":
            condicoes.append("ativo = 0")
        elif filtro == "whatsapp":
            condicoes.append(
                "(ativo = 1 OR ativo IS NULL) AND receber_whatsapp = 1 "
                "AND (status_cadastro = 'ativo' OR status_cadastro IS NULL)"
            )
        elif filtro == "pendentes":
            condicoes.append("status_cadastro = 'pendente'")
        where_sql = "WHERE " + " AND ".join(condicoes) if condicoes else ""
        usuarios, paginacao = consultar_paginado(
            conn, "usuarios", pagina, por_pagina, where_sql, tuple(parametros)
        )
    elif aba_ativa == "cadastros":
        if filtro not in {
            "todos",
            "cadastro",
            "cancelamento",
            "edicao_admin",
            "exclusao_admin",
        }:
            filtro = "todos"
        filtros["filtro"] = filtro
        condicoes = []
        parametros = []
        if busca:
            valor = termo_like(busca)
            condicoes.append(
                "(nome LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR telefone LIKE ? ESCAPE '\\' "
                "OR endereco LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR detalhe LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            parametros.extend((valor, valor, valor, valor))
        if filtro != "todos":
            condicoes.append("acao = ?")
            parametros.append(filtro)
        where_sql = "WHERE " + " AND ".join(condicoes) if condicoes else ""
        eventos_cadastro, paginacao = consultar_paginado(
            conn, "cadastro_eventos", pagina, por_pagina, where_sql, tuple(parametros)
        )
    elif aba_ativa == "eventos":
        if filtro not in {
            "todos",
            "detectado",
            "enfileirado",
            "sem_destinatarios",
            "concluido",
            "concluido_com_falhas",
        }:
            filtro = "todos"
        filtros["filtro"] = filtro
        condicoes = []
        parametros = []
        if busca:
            valor = termo_like(busca)
            condicoes.append(
                "(tipo LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR fonte LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR mensagem LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR detalhe LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            parametros.extend((valor, valor, valor, valor))
        if filtro != "todos":
            condicoes.append("status = ?")
            parametros.append(filtro)
        where_sql = "WHERE " + " AND ".join(condicoes) if condicoes else ""
        eventos_alertas, paginacao = consultar_paginado(
            conn, "alertas_eventos", pagina, por_pagina, where_sql, tuple(parametros)
        )
    elif aba_ativa == "envios":
        if filtro not in {"todos", "enviado", "falhou"}:
            filtro = "todos"
        filtros["filtro"] = filtro
        condicoes = []
        parametros = []
        if busca:
            valor = termo_like(busca)
            condicoes.append(
                "(nome LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR telefone LIKE ? ESCAPE '\\' "
                "OR mensagem LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR erro LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            parametros.extend((valor, valor, valor, valor))
        if filtro != "todos":
            condicoes.append("status = ?")
            parametros.append(filtro)
        where_sql = "WHERE " + " AND ".join(condicoes) if condicoes else ""
        alertas, paginacao = consultar_paginado(
            conn, "alertas_envios", pagina, por_pagina, where_sql, tuple(parametros)
        )
    elif aba_ativa == "historico":
        data_inicio = request.args.get("data_inicio", "").strip()[:10]
        data_fim = request.args.get("data_fim", "").strip()[:10]
        filtros.update({"data_inicio": data_inicio, "data_fim": data_fim})
        condicoes = []
        parametros = []
        if data_inicio:
            condicoes.append("COALESCE(data_hora_local, data_hora, '') >= ?")
            parametros.append(data_inicio)
        if data_fim:
            condicoes.append("COALESCE(data_hora_local, data_hora, '') < date(?, '+1 day')")
            parametros.append(data_fim)
        where_sql = "WHERE " + " AND ".join(condicoes) if condicoes else ""
        historico, paginacao = consultar_paginado(
            conn, "historico_clima", pagina, por_pagina, where_sql, tuple(parametros)
        )

    conn.close()

    evolution_status = obter_status_evolution() if aba_ativa == "visao-geral" else None

    return render_template(
        "admin_painel.html",
        aba_ativa=aba_ativa,
        usuarios=preparar_usuarios_admin(usuarios),
        resumo_usuarios=resumo_usuarios,
        saude_sistema=saude_sistema,
        historico=preparar_historico_admin(historico),
        alertas=preparar_eventos_admin(alertas, assume_utc=True),
        eventos_alertas=preparar_eventos_admin(eventos_alertas, assume_utc=True),
        eventos_cadastro=preparar_eventos_admin(eventos_cadastro, assume_utc=True),
        evolution_status=evolution_status,
        contagens=contagens,
        paginacao=paginacao,
        filtros=filtros,
        opcoes_por_pagina=ADMIN_ITENS_POR_PAGINA,
    )
