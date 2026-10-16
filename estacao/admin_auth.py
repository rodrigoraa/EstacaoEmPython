"""Autorizacao compartilhada das superficies administrativas."""

import hashlib
import hmac
import secrets
import time
from contextlib import closing
from functools import wraps

from flask import current_app, flash, jsonify, redirect, session, url_for
from config import env_str


def garantir_tabela_sessoes_admin(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_sessoes (
            token_hash TEXT PRIMARY KEY,
            credencial_hash TEXT NOT NULL,
            criado_em REAL NOT NULL,
            ultimo_acesso REAL NOT NULL,
            expira_em REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_sessoes_expira ON admin_sessoes(expira_em)")


def _credencial_hash():
    credencial = env_str("ADMIN_PASSWORD_HASH") or env_str("ADMIN_PASSWORD") or ""
    chave = current_app.secret_key
    if isinstance(chave, str):
        chave = chave.encode("utf-8")
    return hmac.new(chave,
                    credencial.encode("utf-8"), hashlib.sha256).hexdigest()


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def iniciar_sessao_admin():
    """Chamado so depois de validar a senha; banco guarda apenas o hash do token."""
    import database

    encerrar_sessao_admin()
    token = secrets.token_urlsafe(48)
    agora = time.time()
    absoluto = current_app.config.get("ADMIN_SESSION_ABSOLUTE_SECONDS", 28800)
    with closing(database.get_db()) as conn, conn:
        garantir_tabela_sessoes_admin(conn)
        conn.execute("DELETE FROM admin_sessoes WHERE expira_em <= ?", (agora,))
        conn.execute("INSERT INTO admin_sessoes VALUES (?, ?, ?, ?, ?)",
                     (_token_hash(token), _credencial_hash(), agora, agora, agora + absoluto))
    session["admin_session"] = token
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True


def encerrar_sessao_admin():
    import database

    token = session.get("admin_session")
    if isinstance(token, str) and token.isascii() and len(token) == 64:
        with closing(database.get_db()) as conn, conn:
            garantir_tabela_sessoes_admin(conn)
            conn.execute("DELETE FROM admin_sessoes WHERE token_hash = ?", (_token_hash(token),))
    session.clear()


def admin_autenticado():
    import database

    token = session.get("admin_session")
    if not isinstance(token, str) or not token.isascii() or len(token) != 64:
        if session.get("logado") or token:
            session.clear()
        return False

    agora = time.time()
    limite_idle = agora - current_app.permanent_session_lifetime.total_seconds()
    with closing(database.get_db()) as conn, conn:
        garantir_tabela_sessoes_admin(conn)
        cursor = conn.execute("""
            UPDATE admin_sessoes SET ultimo_acesso = ?
            WHERE token_hash = ? AND credencial_hash = ?
              AND ultimo_acesso > ? AND expira_em > ? AND criado_em <= ?
        """, (agora, _token_hash(token), _credencial_hash(), limite_idle, agora, agora))
        valido = cursor.rowcount == 1
        if not valido:
            conn.execute("DELETE FROM admin_sessoes WHERE token_hash = ?", (_token_hash(token),))
    if not valido:
        session.clear()
        flash("Sessão expirada. Faça login novamente.")
        return False

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
