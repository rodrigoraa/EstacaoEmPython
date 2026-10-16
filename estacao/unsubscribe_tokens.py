import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from config import env_str


TOKEN_SALT = "estacao-cancelamento-alertas-v2"
TOKEN_MAX_AGE_DAYS = 7


class TokenCancelamentoInvalido(Exception):
    pass


class TokenCancelamentoExpirado(Exception):
    pass


def normalizar_telefone(valor):
    return "".join(filter(str.isdigit, valor or ""))


def telefone_com_codigo_pais(valor):
    telefone = normalizar_telefone(valor)
    if len(telefone) in (10, 11):
        telefone = "55" + telefone
    return telefone


def obter_chave_token():
    chave = env_str("UNSUBSCRIBE_SECRET") or env_str("SECRET_KEY")
    if not chave:
        raise RuntimeError("SECRET_KEY ou UNSUBSCRIBE_SECRET não configurado")
    return chave


def token_max_age_seconds():
    try:
        dias = int(os.environ.get("UNSUBSCRIBE_TOKEN_MAX_AGE_DAYS", TOKEN_MAX_AGE_DAYS))
    except (TypeError, ValueError):
        dias = TOKEN_MAX_AGE_DAYS
    dias = min(30, max(1, dias))
    return dias * 24 * 60 * 60


def serializer():
    return URLSafeTimedSerializer(obter_chave_token())


def gerar_token_cancelamento(telefone, *, conn=None):
    import database
    from services.subscriber_security import buscar_cadastro

    proprio = conn is None
    conn = conn or database.get_db()
    try:
        usuario = buscar_cadastro(conn, telefone)
        if not usuario or not usuario["cadastro_nonce"]:
            raise ValueError("Cadastro inexistente para gerar cancelamento")
        return serializer().dumps({
            "usuario_id": usuario["id"],
            "telefone": usuario["telefone_canonico"],
            "cadastro_nonce": usuario["cadastro_nonce"],
        }, salt=TOKEN_SALT)
    finally:
        if proprio:
            conn.close()


def validar_token_cancelamento(token, *, conn=None, retornar_cadastro=False):
    if not isinstance(token, str) or not token or len(token) > 2048:
        raise TokenCancelamentoInvalido("Token ausente")

    try:
        payload = serializer().loads(
            token,
            salt=TOKEN_SALT,
            max_age=token_max_age_seconds(),
        )
    except SignatureExpired as erro:
        raise TokenCancelamentoExpirado("Token expirado") from erro
    except BadSignature as erro:
        raise TokenCancelamentoInvalido("Token inválido") from erro

    if not isinstance(payload, dict) or not isinstance(payload.get("usuario_id"), int):
        raise TokenCancelamentoInvalido("Cadastro ausente no token")
    from services.subscriber_security import validar_telefone
    try:
        telefone = validar_telefone(payload.get("telefone"))
    except ValueError as erro:
        raise TokenCancelamentoInvalido("Telefone inválido no token") from erro
    nonce = payload.get("cadastro_nonce")
    if not isinstance(nonce, str) or len(nonce) != 64:
        raise TokenCancelamentoInvalido("Identidade de cadastro inválida")

    import database
    proprio = conn is None
    conn = conn or database.get_db()
    try:
        usuario = conn.execute("""
            SELECT id, nome, telefone, endereco, receber_whatsapp,
                   telefone_canonico, cadastro_nonce
            FROM usuarios WHERE id = ? AND cadastro_nonce = ?
              AND telefone_canonico = ? AND duplicado_de IS NULL
        """, (payload["usuario_id"], nonce, telefone)).fetchone()
        if not usuario:
            raise TokenCancelamentoInvalido("Cadastro removido ou alterado")
        return usuario if retornar_cadastro else telefone
    finally:
        if proprio:
            conn.close()
