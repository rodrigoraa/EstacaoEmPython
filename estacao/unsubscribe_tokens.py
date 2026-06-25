import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


TOKEN_SALT = "estacao-cancelamento-alertas"
TOKEN_MAX_AGE_DAYS = 90


class TokenCancelamentoInvalido(Exception):
    pass


class TokenCancelamentoExpirado(Exception):
    pass


def normalizar_telefone(valor):
    return "".join(filter(str.isdigit, valor or ""))


def telefone_com_codigo_pais(valor):
    telefone = normalizar_telefone(valor)
    if telefone and not telefone.startswith("55"):
        telefone = "55" + telefone
    return telefone


def obter_chave_token():
    chave = os.environ.get("UNSUBSCRIBE_SECRET") or os.environ.get("SECRET_KEY")
    if not chave:
        raise RuntimeError("SECRET_KEY ou UNSUBSCRIBE_SECRET não configurado")
    return chave


def token_max_age_seconds():
    dias = int(os.environ.get("UNSUBSCRIBE_TOKEN_MAX_AGE_DAYS", TOKEN_MAX_AGE_DAYS))
    return dias * 24 * 60 * 60


def serializer():
    return URLSafeTimedSerializer(obter_chave_token())


def gerar_token_cancelamento(telefone):
    telefone = telefone_com_codigo_pais(telefone)
    if not telefone:
        raise ValueError("Telefone obrigatório para gerar token de cancelamento")

    return serializer().dumps({"telefone": telefone}, salt=TOKEN_SALT)


def validar_token_cancelamento(token):
    if not token:
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

    telefone = telefone_com_codigo_pais(payload.get("telefone"))
    if not telefone:
        raise TokenCancelamentoInvalido("Telefone ausente no token")

    return telefone
