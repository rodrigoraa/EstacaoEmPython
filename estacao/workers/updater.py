import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite3
import time
import datetime
import json
from services.weather_service import obter_dados
from services.whatsapp_service import enviar_whatsapp


DB = os.path.join(BASE_DIR, "estacao.db")
STATE_FILE = os.path.join(BASE_DIR, "alert_state.json")

INTERVALO = 15


def log(msg):
    agora = datetime.datetime.now().strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {msg}", flush=True)


def salvar_leitura(dados):
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
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
            "chuva_next": 40,
            "vento_next": 50,
            "temp_next": 38,
        }
    with open(STATE_FILE, "r") as f:
        estado = json.load(f)
        if "chuva_next" not in estado:
            estado["chuva_next"] = 40
            estado["vento_next"] = 50
            estado["temp_next"] = 38
        return estado


def salvar_estado(estado):
    with open(STATE_FILE, "w") as f:
        json.dump(estado, f)


def enviar_alerta(mensagem):
    log(f"🚨 Enviando Resumo de Alerta...")
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row

    usuarios = conn.execute(
        """
        SELECT nome, telefone
        FROM usuarios
        WHERE (ativo = 1 OR ativo IS NULL)
        AND receber_whatsapp = 1
        """
    ).fetchall()
    conn.close()

    for u in usuarios:
        telefone = "".join(filter(str.isdigit, u["telefone"]))
        if not telefone.startswith("55"):
            telefone = "55" + telefone

        link_cancelar = f"http://meteo.eesjv.com.br/unsubscribe?tel={telefone}"

        mensagem_final = f"⚠️ *Alerta Meteorológico*\n\n{mensagem}\n\n🛑 Para parar de receber alertas, acesse:\n{link_cancelar}"

        try:
            enviar_whatsapp(telefone, mensagem_final)
            log(f"✅ Enviado para {u['nome']}")
        except Exception as e:
            log(f"❌ Erro envio {u['nome']} ({telefone}) {e}")


def verificar_alertas(temp, vento, chuva_hoje):
    estado = carregar_estado()
    hoje = datetime.date.today().isoformat()

    if estado.get("data") != hoje:
        estado = {
            "data": hoje,
            "chuva_next": 40,
            "vento_next": 50,
            "temp_next": 38,
        }

    alerta_acionado = False
    motivos = []

    if chuva_hoje >= estado["chuva_next"]:
        motivos.append(f"🌧️ *Chuva* atingiu {chuva_hoje:.1f} mm")
        while estado["chuva_next"] <= chuva_hoje:
            estado["chuva_next"] += 20
        alerta_acionado = True

    if vento >= estado["vento_next"]:
        motivos.append(f"💨 *Rajadas de Vento* de {vento:.1f} km/h")
        while estado["vento_next"] <= vento:
            estado["vento_next"] += 10
        alerta_acionado = True

    if temp >= estado["temp_next"]:
        motivos.append(f"🌡️ *Temperatura* bateu {temp:.1f}°C")
        while estado["temp_next"] <= temp:
            estado["temp_next"] += 2
        alerta_acionado = True

    if alerta_acionado:
        mensagem = "*Motivos do Alerta:*\n- " + "\n- ".join(motivos) + "\n\n"
        mensagem += "*📊 Condições neste exato momento:*\n"
        mensagem += f"🌡 Temperatura: {temp:.1f}°C\n"
        mensagem += f"💨 Ventos: {vento:.1f} km/h\n"
        mensagem += f"🌧 Chuva Hoje: {chuva_hoje:.1f} mm\n\n"
        mensagem += "📍 _Vicentina MS - Distrito de São José_"

        enviar_alerta(mensagem)

    salvar_estado(estado)


def executar():
    log("🔄 Coletando dados da estação")
    try:
        dados = obter_dados()

        if not dados:
            log("⚠️ Sem dados")
            return

        temp = dados["temp"]
        sensacao = dados["sensacao"]
        umidade = dados["umidade"]
        pressao = dados["pressao"]

        uv = dados["uv"]
        radiacao = dados["radiacao"]

        vento = dados["vento"]
        rajada = dados["rajada"]
        vento_dir = dados["vento_dir"]

        chuva_rate = dados["chuva_rate"]
        chuva_evento = dados["chuva_evento"]
        chuva_hoje = dados["chuva_hoje"]

        log(f"🌡 {temp}°C | 💧 {umidade}% | 💨 {vento} km/h | 🌧 {chuva_hoje} mm")

        verificar_alertas(temp, vento, chuva_hoje)

        salvar_leitura(
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
            )
        )

    except Exception as e:
        log(f"❌ Erro {e}")


if __name__ == "__main__":
    log("🚀 Updater iniciado")
    while True:
        executar()
        time.sleep(INTERVALO)
