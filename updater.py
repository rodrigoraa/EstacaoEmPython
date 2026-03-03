import requests
import sqlite3
import time
import datetime
import json
import os

# ==========================================
# CONFIGURAÇÃO
# ==========================================

PUBLIC_SLUG = "a535a0b6ff603c1d2376abc99e689f2f"

URL = f"https://lightning.ambientweather.net/devices?public.slug={PUBLIC_SLUG}"

HEADERS = {"Origin": "https://ambientweather.net", "User-Agent": "Mozilla/5.0"}

DB = "estacao.db"
STATE_FILE = "alert_state.json"

INTERVALO = 15  # segundos

# ==========================================
# WHATSAPP CLOUD API
# ==========================================

WA_TOKEN = "SEU_TOKEN_AQUI"
WA_PHONE_ID = "SEU_PHONE_ID_AQUI"


# ==========================================
# CONVERSÕES
# ==========================================


def f_to_c(f):
    return round((f - 32) * 5 / 9, 1)


def mph_to_kmh(mph):
    return round(mph * 1.60934, 1)


def in_to_mm(i):
    return round(i * 25.4, 1)


# ==========================================
# LOG
# ==========================================


def log(msg):
    agora = datetime.datetime.now().strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {msg}")


# ==========================================
# SALVAR HISTÓRICO
# ==========================================


def salvar_leitura(temp, vento, chuva):

    conn = sqlite3.connect(DB)
    cursor = conn.cursor()

    agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        """
        INSERT INTO historico_clima
        (temp, umidade, chuva, vento_dir, vento_vel, data_hora)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (temp, 0, chuva, 0, vento, agora),
    )

    conn.commit()
    conn.close()

    log("💾 Histórico salvo")


# ==========================================
# ESTADO ALERTAS
# ==========================================


def carregar_estado():

    if not os.path.exists(STATE_FILE):
        return {"data": "", "chuva": 0, "vento": 0, "temp_alta": 0, "temp_baixa": 999}

    with open(STATE_FILE, "r") as f:
        return json.load(f)


def salvar_estado(estado):

    with open(STATE_FILE, "w") as f:
        json.dump(estado, f)


# ==========================================
# ENVIO WHATSAPP
# ==========================================


def enviar_alerta(mensagem):

    log(f"🚨 Disparando alerta: {mensagem}")

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    usuarios = cursor.execute(
        """
        SELECT nome, telefone
        FROM usuarios
        WHERE ativo = 1 OR ativo IS NULL
    """
    ).fetchall()

    conn.close()

    if not usuarios:
        log("⚠️ Nenhum usuário cadastrado")
        return

    url = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"

    headers = {
        "Authorization": f"Bearer {WA_TOKEN}",
        "Content-Type": "application/json",
    }

    for u in usuarios:

        telefone_limpo = "".join(filter(str.isdigit, u["telefone"]))

        payload = {
            "messaging_product": "whatsapp",
            "to": telefone_limpo,
            "type": "text",
            "text": {
                "body": (
                    f"⚠️ *Alerta Meteorológico EESJ*\n\n"
                    f"{mensagem}\n\n"
                    f"_Mensagem automática da estação._"
                )
            },
        }

        try:
            requests.post(url, headers=headers, json=payload, timeout=10)
            log(f"✅ Enviado para {u['nome']}")
        except Exception as e:
            log(f"❌ Erro ao enviar para {u['nome']}: {e}")


# ==========================================
# ALERTAS PROGRESSIVOS
# ==========================================


def verificar_alertas(temp, vento, chuva):

    estado = carregar_estado()
    hoje = datetime.date.today().isoformat()

    # reset diário
    if estado["data"] != hoje:
        estado = {
            "data": hoje,
            "chuva": 0,
            "vento": 0,
            "temp_alta": 0,
            "temp_baixa": 999,
        }

    # ================= CHUVA =================

    limite = estado["chuva"] + 10
    if limite == 10:
        limite = 45

    if chuva >= limite:
        enviar_alerta(f"🌧️ Chuva acumulada atingiu {chuva:.1f} mm hoje.")
        estado["chuva"] = limite

    # ================= VENTO =================

    limite = estado["vento"] + 10
    if limite == 10:
        limite = 60

    if vento >= limite:
        enviar_alerta(f"💨 Rajadas de vento atingiram {vento:.1f} km/h.")
        estado["vento"] = limite

    # ================= CALOR =================

    limite = estado["temp_alta"]
    if limite == 0:
        limite = 36
    else:
        limite += 3

    if temp >= limite:
        enviar_alerta(f"🔥 Temperatura elevada: {temp:.1f}°C.")
        estado["temp_alta"] = limite

    # ================= FRIO =================

    limite = estado["temp_baixa"]
    if limite == 999:
        limite = 10
    elif limite == 10:
        limite = 0
    else:
        limite -= 3

    if temp <= limite:
        enviar_alerta(f"🥶 Temperatura baixa: {temp:.1f}°C.")
        estado["temp_baixa"] = limite

    salvar_estado(estado)


# ==========================================
# EXECUÇÃO
# ==========================================


def executar():

    try:

        resposta = requests.get(URL, headers=HEADERS, timeout=20)
        resposta.raise_for_status()

        dados = resposta.json()

        if not dados.get("data"):
            log("⚠️ Sem dados da estação")
            return

        raw = dados["data"][0].get("lastData", dados["data"][0])

        temp = f_to_c(raw.get("tempf", 32))
        vento = mph_to_kmh(raw.get("windgustmph", 0))
        chuva = in_to_mm(raw.get("dailyrainin", 0))

        log(f"🌡️ Temp {temp}°C | 💨 Vento {vento} km/h | 🌧️ Chuva {chuva} mm")

        verificar_alertas(temp, vento, chuva)
        salvar_leitura(temp, vento, chuva)

    except Exception as e:
        log(f"❌ Erro: {e}")


# ==========================================
# LOOP PRINCIPAL
# ==========================================

if __name__ == "__main__":

    log("🚀 Updater iniciado")

    while True:
        executar()
        time.sleep(INTERVALO)
