import requests
import os

WA_TOKEN = os.getenv("WA_TOKEN")
WA_PHONE_ID = os.getenv("WA_PHONE_ID")

URL = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"


def enviar_whatsapp(telefone, mensagem):

    if not WA_TOKEN or not WA_PHONE_ID:
        raise ValueError("Token ou Phone ID não configurados")

    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": telefone,
        "type": "text",
        "text": {"body": mensagem},
    }

    try:

        resp = requests.post(URL, headers=headers, json=payload, timeout=10)

        resp.raise_for_status()

        return resp.json()

    except requests.exceptions.RequestException as e:

        print("Erro ao enviar WhatsApp:", e)

        return None
