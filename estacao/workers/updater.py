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
    padrao = {
        "data": "",
        "nivel_calor": 0,
        "nivel_frio": 0,
        "nivel_vento": 0,
        "nivel_chuva": 0,
        "nivel_umidade": 0,
        "nivel_uv": 0,
    }

    if not os.path.exists(STATE_FILE):
        return padrao

    try:
        with open(STATE_FILE, "r") as f:
            estado = json.load(f)
            for chave, valor in padrao.items():
                if chave not in estado:
                    estado[chave] = valor
            return estado
    except:
        return padrao


def salvar_estado(estado):
    with open(STATE_FILE, "w") as f:
        json.dump(estado, f)


def enviar_alerta(mensagem):
    log(f"🚨 Disparando Alerta Crítico para usuários...")
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

        # Estrutura base da mensagem para todos os alertas
        mensagem_final = f"{mensagem}\n\n📍 _Vicentina MS - Distrito de São José_\n🛑 Para cancelar alertas, acesse:\n{link_cancelar}"

        try:
            enviar_whatsapp(telefone, mensagem_final)
            log(f"✅ Enviado para {u['nome']}")
        except Exception as e:
            log(f"❌ Erro envio {u['nome']} ({telefone}) {e}")


def verificar_alertas(temp, rajada, chuva_hoje, umidade, uv):
    estado = carregar_estado()
    hoje = datetime.date.today().isoformat()

    # Mudança de dia: Salva o resumo de ontem no banco de dados e zera a memória de alertas
    if estado.get("data") != hoje:
        if estado.get("data") != "":
            salvar_resumo_diario_banco(estado["data"])

        estado = {
            "data": hoje,
            "nivel_calor": 0,
            "nivel_frio": 0,
            "nivel_vento": 0,
            "nivel_chuva": 0,
            "nivel_umidade": 0,
            "nivel_uv": 0,
        }
        salvar_estado(estado)

    # ================= REGRAS DE TEMPERATURA (CALOR) =================
    if temp >= 40 and estado["nivel_calor"] < 2:
        msg = f"🔥 *ALERTA CRÍTICO: Temperatura Muito Alta!*\nOs termômetros atingiram *{temp:.1f}°C*. Risco iminente de insolação. Evite exposição ao sol e hidrate-se imediatamente!"
        enviar_alerta(msg)
        estado["nivel_calor"] = 2
        salvar_estado(estado)

    elif temp >= 35 and estado["nivel_calor"] < 1:
        msg = f"🌡️ *ALERTA: Temperatura Alta!*\nRegistrados *{temp:.1f}°C*. Calor forte na região com risco de desconforto térmico. Beba bastante água."
        enviar_alerta(msg)
        estado["nivel_calor"] = 1
        salvar_estado(estado)

    # ================= REGRAS DE TEMPERATURA (FRIO) =================
    if temp <= 0 and estado["nivel_frio"] < 3:
        msg = f"🥶 *ALERTA MÁXIMO: Frio Congelante!*\nOs termômetros despencaram para *{temp:.1f}°C*. Condição extrema com alto risco de geada severa, danos às lavouras e hipotermia. Proteja pessoas vulneráveis, animais e plantas sensíveis imediatamente!"
        enviar_alerta(msg)
        estado["nivel_frio"] = 3
        salvar_estado(estado)

    elif temp <= 5 and estado["nivel_frio"] < 2:
        msg = f"🧊 *ALERTA CRÍTICO: Frio Extremo!*\nA temperatura caiu para *{temp:.1f}°C*. Risco grave à saúde humana e animal. Proteja-se do frio intenso."
        enviar_alerta(msg)
        estado["nivel_frio"] = 2
        salvar_estado(estado)

    elif temp <= 12 and estado["nivel_frio"] < 1:
        msg = f"❄️ *ALERTA: Temperatura Baixa!*\nRegistrados *{temp:.1f}°C*. Frio incomum para a região. Agasalhe-se bem."
        enviar_alerta(msg)
        estado["nivel_frio"] = 1
        salvar_estado(estado)

    # ================= REGRAS DE VENTO =================
    if rajada >= 100 and estado["nivel_vento"] < 3:
        msg = f"🌪️ *ALERTA CRÍTICO: Vento Extremo!*\nRajadas violentas de *{rajada:.1f} km/h*. Alto risco de destelhamentos e queda de árvores. Permaneça em local seguro!"
        enviar_alerta(msg)
        estado["nivel_vento"] = 3
        salvar_estado(estado)

    elif rajada >= 70 and estado["nivel_vento"] < 2:
        msg = f"🌪️ *ALERTA FORTE: Vento Muito Forte!*\nRajadas de *{rajada:.1f} km/h*. Possibilidade de danos na infraestrutura e rede elétrica. Atenção redobrada."
        enviar_alerta(msg)
        estado["nivel_vento"] = 2
        salvar_estado(estado)

    elif rajada >= 40 and estado["nivel_vento"] < 1:
        msg = f"🌬️ *ALERTA: Vento Forte!*\nRajadas de *{rajada:.1f} km/h*. Risco de queda de galhos e objetos soltos."
        enviar_alerta(msg)
        estado["nivel_vento"] = 1
        salvar_estado(estado)

    # ================= REGRAS DE CHUVA =================
    if chuva_hoje >= 100 and estado["nivel_chuva"] < 2:
        msg = f"🌧️ *ALERTA CRÍTICO: Chuva Muito Forte!*\nAcumulado de *{chuva_hoje:.1f} mm* hoje. Risco grave de enxurradas e transbordamentos. Evite áreas de risco!"
        enviar_alerta(msg)
        estado["nivel_chuva"] = 2
        salvar_estado(estado)

    elif chuva_hoje >= 50 and estado["nivel_chuva"] < 1:
        msg = f"🌧️ *ALERTA: Chuva Forte!*\nAcumulado de *{chuva_hoje:.1f} mm*. Risco de alagamentos em pontos isolados. Dirija com cuidado."
        enviar_alerta(msg)
        estado["nivel_chuva"] = 1
        salvar_estado(estado)

    # ================= REGRAS DE UMIDADE =================
    if umidade <= 20 and estado["nivel_umidade"] < 2:
        msg = f"🆘 *ALERTA CRÍTICO: Umidade Muito Baixa!*\nAr extremamente seco, registrando apenas *{umidade}%*. Grave risco à saúde e alto potencial de incêndios. Evite exercícios físicos e umidifique o ambiente."
        enviar_alerta(msg)
        estado["nivel_umidade"] = 2
        salvar_estado(estado)

    elif umidade <= 30 and estado["nivel_umidade"] < 1:
        msg = f"💧 *ALERTA: Umidade Baixa!*\nO ar está seco, na faixa de *{umidade}%*. Causa desconforto respiratório. Beba bastante água."
        enviar_alerta(msg)
        estado["nivel_umidade"] = 1
        salvar_estado(estado)

    # ================= REGRAS DE RADIAÇÃO UV =================
    if uv >= 11 and estado.get("nivel_uv", 0) < 2:
        msg = f"🟣 *ALERTA CRÍTICO: Radiação UV Extrema!*\nO Índice UV atingiu o nível máximo de *{uv:.1f}*. Risco extremo de queimaduras severas na pele em poucos minutos. Evite totalmente o sol, busque sombra e use proteção máxima!"
        enviar_alerta(msg)
        estado["nivel_uv"] = 2
        salvar_estado(estado)

    elif uv >= 8 and estado.get("nivel_uv", 0) < 1:
        msg = f"🔴 *ALERTA FORTE: Radiação UV Muito Alta!*\nO Índice UV está em *{uv:.1f}*. Risco alto de insolação e danos à pele. Se precisar sair, use chapéu, óculos escuros e bastante protetor solar."
        enviar_alerta(msg)
        estado["nivel_uv"] = 1
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

        log(
            f"🌡 {temp}°C | 💧 {umidade}% | 💨 {rajada} km/h (Rajada) | 🌧 {chuva_hoje} mm | ☀️ UV: {uv}"
        )

        verificar_alertas(temp, rajada, chuva_hoje, umidade, uv)

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
