import os
from urllib.parse import urlsplit, urlunsplit


def env_str(nome, padrao=None):
    valor = os.environ.get(nome)
    if valor is None:
        return padrao
    return valor.strip()


def env_bool(nome, padrao=False):
    valor = env_str(nome)
    if valor is None or valor == "":
        return bool(padrao)
    return valor.lower() in {"1", "true", "yes", "sim", "on"}


def env_int(nome, padrao):
    try:
        return int(env_str(nome, str(padrao)))
    except (TypeError, ValueError):
        return int(padrao)


def env_float(nome, padrao):
    try:
        return float(env_str(nome, str(padrao)))
    except (TypeError, ValueError):
        return float(padrao)


def public_base_url():
    configurado = env_str("PUBLIC_BASE_URL")
    if env_str("APP_ENV", "development").lower() == "production" and not configurado:
        raise RuntimeError("PUBLIC_BASE_URL não configurada em produção")

    valor = (configurado or "http://meteo.eesjv.com.br").rstrip("/")
    partes = urlsplit(valor)
    if partes.scheme not in {"http", "https"} or not partes.netloc:
        raise RuntimeError("PUBLIC_BASE_URL deve ser uma URL http(s) absoluta")
    return urlunsplit((partes.scheme, partes.netloc, partes.path.rstrip("/"), "", ""))


def public_url(caminho):
    return f"{public_base_url()}/{str(caminho).lstrip('/')}"


def validar_configuracao_web():
    if env_str("APP_ENV", "development").lower() != "production":
        return

    faltantes = []
    if not env_str("SECRET_KEY"):
        faltantes.append("SECRET_KEY")
    if not (env_str("ADMIN_PASSWORD") or env_str("ADMIN_PASSWORD_HASH")):
        faltantes.append("ADMIN_PASSWORD ou ADMIN_PASSWORD_HASH")
    if not env_str("WEBHOOK_SECRET"):
        faltantes.append("WEBHOOK_SECRET")
    if not env_str("PUBLIC_BASE_URL"):
        faltantes.append("PUBLIC_BASE_URL")
    if faltantes:
        raise RuntimeError("Configuracao de producao incompleta: " + ", ".join(faltantes))
