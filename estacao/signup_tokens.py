import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import env_int, env_str


SALT = "estacao-signup-confirm-v2"


class TokenConfirmacaoInvalido(ValueError):
    pass


class TokenConfirmacaoExpirado(ValueError):
    pass


def _serializer():
    segredo = env_str("SIGNUP_CONFIRM_SECRET") or env_str("SECRET_KEY")
    if not segredo:
        raise RuntimeError("SIGNUP_CONFIRM_SECRET ou SECRET_KEY não configurado")
    return URLSafeTimedSerializer(segredo, salt=SALT)


def gerar_token_confirmacao(usuario_id, telefone):
    import database
    from services.subscriber_security import buscar_cadastro

    conn = database.get_db()
    try:
        usuario = buscar_cadastro(conn, telefone)
        if not usuario or usuario["id"] != int(usuario_id):
            raise ValueError("Cadastro inexistente para confirmar")
        return _serializer().dumps({
            "usuario_id": int(usuario_id),
            "telefone": usuario["telefone_canonico"],
            "cadastro_nonce": usuario["cadastro_nonce"],
            "nonce": secrets.token_urlsafe(8),
        })
    finally:
        conn.close()


def validar_token_confirmacao(token, *, conn=None):
    if not isinstance(token, str) or not token or len(token) > 2048:
        raise TokenConfirmacaoInvalido
    try:
        dados = _serializer().loads(
            token,
            max_age=min(72, max(1, env_int("SIGNUP_CONFIRM_TOKEN_MAX_AGE_HOURS", 24))) * 3600,
        )
    except SignatureExpired as erro:
        raise TokenConfirmacaoExpirado from erro
    except BadSignature as erro:
        raise TokenConfirmacaoInvalido from erro

    if (not isinstance(dados, dict) or not isinstance(dados.get("usuario_id"), int)
            or not isinstance(dados.get("cadastro_nonce"), str)
            or len(dados["cadastro_nonce"]) != 64):
        raise TokenConfirmacaoInvalido
    import database
    from services.subscriber_security import buscar_cadastro

    proprio = conn is None
    conn = conn or database.get_db()
    try:
        try:
            usuario = buscar_cadastro(conn, dados.get("telefone"))
        except ValueError as erro:
            raise TokenConfirmacaoInvalido from erro
        if (not usuario or usuario["id"] != dados["usuario_id"]
                or usuario["cadastro_nonce"] != dados["cadastro_nonce"]):
            raise TokenConfirmacaoInvalido
        return usuario["id"], usuario["telefone_canonico"]
    finally:
        if proprio:
            conn.close()
