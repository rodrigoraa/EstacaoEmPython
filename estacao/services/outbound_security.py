"""Regras de destino das integrações, sem expor URLs ou credenciais em erros."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit


# Host publicado no contrato da API de produtos RADAR do DECEA:
# https://ajuda.decea.mil.br/base-de-conhecimento/api-redemet-produtos-radar/
REDEMET_IMAGE_HOST = "estatico-redemet.decea.mil.br"
_INSTANCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


class InvalidOutboundURL(ValueError):
    """Destino inválido; a mensagem não inclui o valor recebido."""


def _partes_url(url: str):
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise InvalidOutboundURL("URL da integração inválida")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise InvalidOutboundURL("URL da integração inválida")
    try:
        partes = urlsplit(url)
        porta = partes.port
        if (
            not partes.hostname
            or partes.username is not None
            or partes.password is not None
            or partes.fragment
            or (porta is not None and not 1 <= porta <= 65535)
        ):
            raise ValueError("componentes inválidos")
    except ValueError as erro:
        raise InvalidOutboundURL("URL da integração inválida") from erro
    return partes


def validar_url_imagem_redemet(url: str) -> str:
    """Permite somente PNGs HTTPS no host de imagens oficial, sem redirects.

    O host é uma fronteira de confiança do fornecedor. Não há alegação de
    pinning de DNS: a resolução/TLS e a saída de rede devem continuar protegidas
    na infraestrutura. Não se aceita host configurável vindo dos metadados.
    """
    partes = _partes_url(url)
    caminho = unquote(partes.path)
    if (
        partes.scheme != "https"
        or partes.hostname != REDEMET_IMAGE_HOST
        or partes.port not in (None, 443)
        or partes.query
        or not caminho.startswith("/radar/")
        or not caminho.lower().endswith(".png")
        or "\\" in caminho
        or ".." in caminho.split("/")
        or any(ord(char) <= 32 or ord(char) == 127 for char in caminho)
    ):
        raise InvalidOutboundURL("URL da imagem fora da origem REDEMET permitida")
    return url


def validar_destino_evolution(url: str, instance: str) -> str:
    """Valida configuração do operador; a Evolution pode estar na rede local."""
    partes = _partes_url(url)
    caminho = unquote(partes.path)
    if (
        partes.scheme not in {"http", "https"}
        or partes.query
        or "\\" in caminho
        or ".." in caminho.split("/")
        or any(ord(char) <= 32 or ord(char) == 127 for char in caminho)
        or not isinstance(instance, str)
        or not _INSTANCE.fullmatch(instance)
    ):
        raise InvalidOutboundURL("Configuração do destino Evolution inválida")
    return url.rstrip("/")
