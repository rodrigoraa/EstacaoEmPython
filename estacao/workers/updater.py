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
            "chuva_next": 20,
            "vento_next": 35,
            "temp_next": 36,
            "alerta_enviado_hoje": False,
            "resumo_enviado_hoje": False,
        }
    with open(STATE_FILE, "r") as f:
        estado = json.load(f)
        if "chuva_next" not in estado:
            estado["chuva_next"] = 20
            estado["vento_next"] = 35
            estado["temp_next"] = 36
        if "alerta_enviado_hoje" not in estado:
            estado["alerta_enviado_hoje"] = False
        if "resumo_enviado_hoje" not in estado:
            estado["resumo_enviado_hoje"] = False
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


def enviar_resumo_diario():
    log("📊 A gerar o resumo diário geral da estação (18h)...")
    try:
        conn = sqlite3.connect(DB, timeout=10)
        conn.row_factory = sqlite3.Row
        hoje_str = datetime.date.today().strftime("%Y-%m-%d")

        resumo = conn.execute(
            """
            SELECT 
                MIN(temp) as temp_min,
                MAX(temp) as temp_max,
                MIN(umidade) as umidade_min,
                MAX(umidade) as umidade_max,
                MAX(vento_rajada) as max_vento,
                MAX(chuva_hoje) as chuva_total,
                MAX(uv) as uv_max,
                MAX(pressao) as pressao_max,
                MIN(pressao) as pressao_min
            FROM historico_clima
            WHERE date(data_hora) = ?
        """,
            (hoje_str,),
        ).fetchone()
        conn.close()

        if resumo and resumo["temp_max"] is not None:
            mensagem = "🌅 *Resumo Meteorológico do Dia*\n\n"
            mensagem += "Nenhum alerta crítico foi registado hoje. Confira os dados da estação:\n\n"
            mensagem += f"🌡 *Temperatura*: {resumo['temp_min']:.1f}°C a {resumo['temp_max']:.1f}°C\n"
            mensagem += f"💧 *Humidade*: {resumo['umidade_min']:.0f}% a {resumo['umidade_max']:.0f}%\n"
            mensagem += f"💨 *Vento Máximo*: {resumo['max_vento']:.1f} km/h\n"
            mensagem += f"🌧 *Chuva Acumulada*: {resumo['chuva_total']:.1f} mm\n"
            mensagem += f"☀️ *Índice UV (Máx)*: {resumo['uv_max']:.1f}\n"
            mensagem += f"🧭 *Pressão Atmosférica*: {resumo['pressao_min']:.1f} a {resumo['pressao_max']:.1f} hPa\n\n"
            mensagem += "📍 _Vicentina MS - Distrito de São José_"

            enviar_alerta(mensagem)
    except Exception as e:
        log(f"❌ Erro ao gerar resumo diário: {e}")


def verificar_alertas(temp, rajada, chuva_hoje):
    estado = carregar_estado()
    hoje = datetime.date.today().isoformat()
    agora = datetime.datetime.now()

    if estado.get("data") != hoje:
        if estado.get("data") != "":
            salvar_resumo_diario_banco(estado["data"])
        estado = {
            "data": hoje,
            "chuva_next": 20,
            "vento_next": 35,
            "temp_next": 36,
            "alerta_enviado_hoje": False,
            "resumo_enviado_hoje": False,
        }

    alerta_acionado = False
    motivos = []

    if chuva_hoje >= estado["chuva_next"]:
        motivos.append(f"🌧️ *Chuva* atingiu {chuva_hoje:.1f} mm")
        while estado["chuva_next"] <= chuva_hoje:
            estado["chuva_next"] += 20
        alerta_acionado = True

    if rajada >= estado["vento_next"]:
        motivos.append(f"💨 *Rajadas de Vento* de {rajada:.1f} km/h")
        while estado["vento_next"] <= rajada:
            estado["vento_next"] += 10
        alerta_acionado = True

    if temp >= estado["temp_next"]:
        motivos.append(f"🌡️ *Temperatura* bateu {temp:.1f}°C")
        while estado["temp_next"] <= temp:
            estado["temp_next"] += 2
        alerta_acionado = True

    if alerta_acionado:
        estado["alerta_enviado_hoje"] = True

        mensagem = "*Motivos do Alerta:*\n- " + "\n- ".join(motivos) + "\n\n"
        mensagem += "*📊 Condições neste exato momento:*\n"
        mensagem += f"🌡 Temperatura: {temp:.1f}°C\n"
        mensagem += f"💨 Ventos: {rajada:.1f} km/h\n"
        mensagem += f"🌧 Chuva Hoje: {chuva_hoje:.1f} mm\n\n"
        mensagem += "📍 _Vicentina MS - Distrito de São José_"

        enviar_alerta(mensagem)

    if (
        agora.hour >= 18
        and not estado["alerta_enviado_hoje"]
        and not estado["resumo_enviado_hoje"]
    ):
        enviar_resumo_diario()
        estado["resumo_enviado_hoje"] = True

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

        verificar_alertas(temp, rajada, chuva_hoje)

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


def salvar_resumo_diario_banco(data_ontem_str):
    log(f"💾 Salvando resumo diário definitivo de {data_ontem_str}...")
    try:
        conn = sqlite3.connect(DB, timeout=10)

        # 1. Calcula os extremos do dia que acabou de passar
        resumo = conn.execute(
            """
            SELECT 
                MIN(temp) as temp_min,
                MAX(temp) as temp_max,
                AVG(temp) as temp_media,
                MIN(umidade) as umidade_min,
                MAX(umidade) as umidade_max,
                MAX(vento_rajada) as vento_rajada_max,
                MAX(chuva_hoje) as chuva_total,
                MIN(pressao) as pressao_min,
                MAX(pressao) as pressao_max,
                MAX(uv) as uv_max
            FROM historico_clima
            WHERE date(data_hora) = ?
        """,
            (data_ontem_str,),
        ).fetchone()

        # 2. Salva na nova tabela definitiva
        if resumo and resumo[0] is not None:
            conn.execute(
                """
                INSERT OR REPLACE INTO historico_diario (
                    data, temp_min, temp_max, temp_media, 
                    umidade_min, umidade_max, vento_rajada_max, 
                    chuva_total, pressao_min, pressao_max, uv_max
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    data_ontem_str,
                    resumo[0],
                    resumo[1],
                    round(resumo[2], 1),
                    resumo[3],
                    resumo[4],
                    resumo[5],
                    resumo[6],
                    resumo[7],
                    resumo[8],
                    resumo[9],
                ),
            )
            conn.commit()
            log("✅ Resumo diário salvo com sucesso no banco!")

        conn.close()
    except Exception as e:
        log(f"❌ Erro ao salvar resumo no banco: {e}")


if __name__ == "__main__":
    log("🚀 Updater iniciado")
    while True:
        executar()
        time.sleep(INTERVALO)
