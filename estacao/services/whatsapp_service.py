import requests

WA_TOKEN = "SEU_TOKEN_AQUI"
WA_PHONE_ID = "SEU_PHONE_ID_AQUI"


def enviar_whatsapp(telefone, mensagem):

    url = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"

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

    requests.post(url, headers=headers, json=payload, timeout=10)