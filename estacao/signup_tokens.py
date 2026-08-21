from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import env_int, env_str


SALT = "estacao-signup-confirm-v1"


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
    return _serializer().dumps({"usuario_id": int(usuario_id), "telefone": str(telefone)})


def validar_token_confirmacao(token):
    try:
        dados = _serializer().loads(
            token,
            max_age=max(1, env_int("SIGNUP_CONFIRM_TOKEN_MAX_AGE_HOURS", 24)) * 3600,
        )
    except SignatureExpired as erro:
        raise TokenConfirmacaoExpirado from erro
    except BadSignature as erro:
        raise TokenConfirmacaoInvalido from erro

    if not isinstance(dados, dict) or not dados.get("usuario_id") or not dados.get("telefone"):
        raise TokenConfirmacaoInvalido
    return int(dados["usuario_id"]), str(dados["telefone"])
