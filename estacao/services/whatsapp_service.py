import os
import requests
from dotenv import load_dotenv

load_dotenv()

EVOLUTION_URL = os.environ.get("EVOLUTION_URL")
EVOLUTION_API_KEY = os.environ.get("EVOLUTION_API_KEY")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE")


if not EVOLUTION_URL:
    raise RuntimeError("EVOLUTION_URL não configurado")

if not EVOLUTION_API_KEY:
    raise RuntimeError("EVOLUTION_API_KEY não configurado")

if not EVOLUTION_INSTANCE:
    raise RuntimeError("EVOLUTION_INSTANCE não configurado")


def enviar_whatsapp(numero, mensagem):

    url = f"{EVOLUTION_URL}/message/sendText/{EVOLUTION_INSTANCE}"

    numero = numero.replace("+", "").replace(" ", "")

    payload = {"number": numero, "text": mensagem}

    headers = {"Content-Type": "application/json", "apikey": EVOLUTION_API_KEY}

    try:

        response = requests.post(url, json=payload, headers=headers, timeout=15)

    except requests.exceptions.RequestException as e:
        raise Exception(f"Erro conexão Evolution API: {e}")

    if response.status_code != 200:

        raise Exception(f"Erro Evolution API {response.status_code}: {response.text}")

    print(f"WhatsApp enviado para {numero}")

    return True
