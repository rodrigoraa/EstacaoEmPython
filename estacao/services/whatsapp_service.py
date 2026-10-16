import logging
import requests
from dotenv import load_dotenv

from config import env_str
from logging_utils import mascarar_telefone
from services.outbound_security import InvalidOutboundURL, validar_destino_evolution

load_dotenv(encoding="utf-8")

logger = logging.getLogger(__name__)


def obter_configuracao_evolution():
    valores = {
        "url": env_str("EVOLUTION_URL", "").rstrip("/"),
        "api_key": env_str("EVOLUTION_API_KEY"),
        "instance": env_str("EVOLUTION_INSTANCE"),
    }
    faltantes = [
        nome
        for nome, chave in (
            ("EVOLUTION_URL", "url"),
            ("EVOLUTION_API_KEY", "api_key"),
            ("EVOLUTION_INSTANCE", "instance"),
        )
        if not valores[chave]
    ]
    if faltantes:
        raise RuntimeError("Configuração Evolution incompleta: " + ", ".join(faltantes))
    try:
        valores["url"] = validar_destino_evolution(valores["url"], valores["instance"])
    except InvalidOutboundURL:
        raise RuntimeError("Configuração do destino Evolution inválida") from None
    return valores


def enviar_whatsapp(numero, mensagem):

    config = obter_configuracao_evolution()

    url = f"{config['url']}/message/sendText/{config['instance']}"

    numero = numero.replace("+", "").replace(" ", "")

    payload = {"number": numero, "text": mensagem, "linkPreview": False}

    headers = {"Content-Type": "application/json", "apikey": config["api_key"]}

    try:

        response = requests.post(
            url, json=payload, headers=headers, timeout=15, allow_redirects=False
        )

    except requests.Timeout:
        raise RuntimeError("Timeout na Evolution API") from None
    except requests.ConnectionError:
        raise RuntimeError("Falha de conexão/DNS com a Evolution API") from None
    except requests.RequestException:
        raise RuntimeError("Erro de conexão Evolution API") from None

    if not 200 <= response.status_code < 300:

        raise RuntimeError(f"Erro HTTP {response.status_code} na Evolution API")

    logger.info("WhatsApp enviado para %s", mascarar_telefone(numero))

    return True
