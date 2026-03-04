import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import time
import datetime
import json
import os

from services.weather_service import obter_dados
from services.whatsapp_service import enviar_whatsapp

DB = "estacao.db"

STATE_FILE = "alert_state.json"

INTERVALO = 15


def log(msg):

    agora = datetime.datetime.now().strftime("%d/%m %H:%M:%S")

    print(f"[{agora}] {msg}")


def salvar_leitura(dados):

    conn = sqlite3.connect(DB)

    cursor = conn.cursor()

    agora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cursor.execute(
        """
        INSERT INTO historico_clima (

        temp,
        sensacao,
        umidade,
        pressao,

        uv,
        radiacao,

        vento_vel,
        vento_rajada,
        vento_dir,

        chuva_rate,
        chuva_evento,
        chuva_hoje,

        data_hora

        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*dados, agora),
    )

    conn.commit()

    conn.close()

    log("💾 Histórico salvo")


def carregar_estado():

    if not os.path.exists(STATE_FILE):

        return {
            "data": "",
            "chuva": 0,
            "vento": 0,
        }

    with open(STATE_FILE, "r") as f:

        return json.load(f)


def salvar_estado(estado):

    with open(STATE_FILE, "w") as f:

        json.dump(estado, f)


def enviar_alerta(mensagem):

    log(f"🚨 {mensagem}")

    conn = sqlite3.connect(DB)

    conn.row_factory = sqlite3.Row

    usuarios = conn.execute(
        """
        SELECT nome, telefone
        FROM usuarios
        WHERE ativo = 1 OR ativo IS NULL
        """
    ).fetchall()

    conn.close()

    for u in usuarios:

        telefone = "".join(filter(str.isdigit, u["telefone"]))

        try:

            enviar_whatsapp(telefone, f"⚠️ Alerta Meteorológico\n\n{mensagem}")

            log(f"✅ Enviado para {u['nome']}")

        except Exception as e:

            log(f"❌ Erro envio {u['nome']} {e}")


def verificar_alertas(vento, chuva_hoje):

    estado = carregar_estado()

    hoje = datetime.date.today().isoformat()

    if estado["data"] != hoje:

        estado = {
            "data": hoje,
            "chuva": 0,
            "vento": 0,
        }

    if chuva_hoje >= estado["chuva"] + 10:

        enviar_alerta(f"🌧️ Chuva acumulada {chuva_hoje:.1f} mm")

        estado["chuva"] += 10

    if vento >= estado["vento"] + 10:

        enviar_alerta(f"💨 Rajadas {vento:.1f} km/h")

        estado["vento"] += 10

    salvar_estado(estado)


def executar():

    try:

        dados = obter_dados()

        if not dados:

            log("⚠️ Sem dados")

            return

        (
            temp,
            sensacao,
            umidade,
            pressao,
            uv,
            radiacao,
            vento,
            rajada,
            vento_dir,
            chuva_rate,
            chuva_evento,
            chuva_hoje,
        ) = dados

        log(f"🌡 {temp}°C | 💧 {umidade}% | 💨 {vento} km/h | 🌧 {chuva_hoje} mm")

        verificar_alertas(vento, chuva_hoje)

        salvar_leitura(dados)

    except Exception as e:

        log(f"❌ Erro {e}")


if __name__ == "__main__":

    log("🚀 Updater iniciado")

    while True:

        executar()

        time.sleep(INTERVALO)
