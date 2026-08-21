import logging
import re

from config import env_str


def configurar_logging():
    nivel_nome = (env_str("LOG_LEVEL", "INFO") or "INFO").upper()
    nivel = getattr(logging, nivel_nome, logging.INFO)
    logging.basicConfig(
        level=nivel,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def mascarar_telefone(valor):
    digitos = re.sub(r"\D", "", str(valor or ""))
    if len(digitos) <= 6:
        return "***"
    return f"{digitos[:4]}{'*' * max(3, len(digitos) - 7)}{digitos[-3:]}"


def mascarar_nome(valor):
    nome = str(valor or "").strip()
    if not nome:
        return "-"
    return f"{nome[0]}***"


def erro_externo_seguro(erro, limite=240):
    texto = str(erro or "erro sem detalhe")
    texto = re.sub(
        r"(?i)(apikey|api[_-]?key|authorization|token|secret|password)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REMOVIDO]",
        texto,
    )
    texto = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REMOVIDO]", texto)
    return texto[:limite]

