import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import json
import database
from persistence import salvar_historico_clima
from time_utils import agora_local, data_local
from services.weather_service import obter_dados
from services.whatsapp_service import enviar_whatsapp


STATE_FILE = os.path.join(BASE_DIR, "alert_state.json")

INTERVALO = 15


def log(msg):
    agora = agora_local().strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {msg}", flush=True)


def carregar_estado():
    padrao = {
        "data": "",
        "nivel_calor": 0,
        "nivel_frio": 0,
        "nivel_vento": 0,
        "nivel_chuva": 0,
        "nivel_umidade": 0,
        "nivel_uv": 0,
        "rajada_max_nuvem": 0.0,
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
    except (OSError, json.JSONDecodeError) as erro:
        log(f"⚠️ Estado de alertas inválido, usando padrão: {erro}")
        return padrao


def salvar_estado(estado):
    with open(STATE_FILE, "w") as f:
        json.dump(estado, f)


def garantir_tabela_alertas(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alertas_envios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
            usuario_id INTEGER,
            nome TEXT,
            telefone TEXT,
            status TEXT NOT NULL,
            mensagem TEXT,
            erro TEXT
        )
        """
    )
    conn.commit()


def registrar_envio_alerta(conn, usuario, telefone, status, mensagem, erro=None):
    conn.execute(
        """
        INSERT INTO alertas_envios (
            usuario_id,
            nome,
            telefone,
            status,
            mensagem,
            erro
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            usuario["id"],
            usuario["nome"],
            telefone,
            status,
            mensagem,
            erro,
        ),
    )
    conn.commit()


def enviar_alerta(mensagem):
    log(f"🚨 Disparando Alerta Crítico para usuários...")
    conn = database.get_db()
    conn.row_factory = sqlite3.Row
    garantir_tabela_alertas(conn)
    enviados = 0
    falhas = 0

    usuarios = conn.execute(
        """
        SELECT id, nome, telefone
        FROM usuarios
        WHERE (ativo = 1 OR ativo IS NULL)
        AND receber_whatsapp = 1
        """
    ).fetchall()
    for u in usuarios:
        telefone = "".join(filter(str.isdigit, u["telefone"]))
        if not telefone.startswith("55"):
            telefone = "55" + telefone

        link_meteo = f"http://meteo.eesjv.com.br"

        # Estrutura base da mensagem para todos os alertas
        mensagem_final = f"{mensagem}\n\n📍 _Vicentina MS - Distrito de São José_\n Para mais informações, acesse:\n{link_meteo}"

        try:
            enviar_whatsapp(telefone, mensagem_final)
            registrar_envio_alerta(conn, u, telefone, "enviado", mensagem_final)
            enviados += 1
            log(f"✅ Enviado para {u['nome']}")
        except Exception as e:
            registrar_envio_alerta(conn, u, telefone, "falhou", mensagem_final, str(e))
            falhas += 1
            log(f"❌ Erro envio {u['nome']} ({telefone}) {e}")

    conn.close()
    return {"total": len(usuarios), "enviados": enviados, "falhas": falhas}


def marcar_alerta_enviado(estado, chave_nivel, nivel, mensagem):
    resultado = enviar_alerta(mensagem)

    if resultado["enviados"] > 0:
        estado[chave_nivel] = nivel
        salvar_estado(estado)
        return True

    log(
        f"Alerta nao marcado como enviado: {resultado['falhas']} falhas "
        f"de {resultado['total']} destinatarios."
    )
    return False


def verificar_alertas(temp, sensacao, rajada, chuva_hoje, umidade, uv):
    estado = carregar_estado()
    hoje = data_local()

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

    if temp >= 40 and estado["nivel_calor"] < 2:
        msg = f"🔥 *ALERTA CRÍTICO: Temperatura Muito Alta!*\nOs termômetros atingiram *{temp:.1f}°C* (Sensação térmica de *{sensacao:.1f}°C*). Risco iminente de insolação. Evite exposição ao sol!"
        marcar_alerta_enviado(estado, "nivel_calor", 2, msg)

    elif temp >= 35 and estado["nivel_calor"] < 1:
        msg = f"🌡️ *ALERTA: Temperatura Alta!*\nRegistrados *{temp:.1f}°C* (Sensação térmica de *{sensacao:.1f}°C*). Calor forte na região."
        marcar_alerta_enviado(estado, "nivel_calor", 1, msg)

    if temp <= 0 and estado["nivel_frio"] < 3:
        msg = f"🥶 *ALERTA MÁXIMO: Frio Congelante!*\nOs termômetros despencaram para *{temp:.1f}°C* (Sensação térmica de *{sensacao:.1f}°C*). Condição extrema com alto risco de geada!"
        marcar_alerta_enviado(estado, "nivel_frio", 3, msg)

    elif temp <= 5 and estado["nivel_frio"] < 2:
        msg = f"🧊 *ALERTA CRÍTICO: Frio Extremo!*\nA temperatura caiu para *{temp:.1f}°C* (Sensação térmica de *{sensacao:.1f}°C*). Proteja-se do frio."
        marcar_alerta_enviado(estado, "nivel_frio", 2, msg)

    elif temp <= 12 and estado["nivel_frio"] < 1:
        msg = f"❄️ *ALERTA: Temperatura Baixa!*\nRegistrados *{temp:.1f}°C* (Sensação térmica de *{sensacao:.1f}°C*). Frio incomum para a região."
        marcar_alerta_enviado(estado, "nivel_frio", 1, msg)

    if rajada >= 100 and estado["nivel_vento"] < 3:
        msg = f"🌪️ *ALERTA CRÍTICO: Vento Extremo!*\nRajadas violentas de *{rajada:.1f} km/h*. Alto risco de destelhamentos e queda de árvores!"
        marcar_alerta_enviado(estado, "nivel_vento", 3, msg)

    elif rajada >= 70 and estado["nivel_vento"] < 2:
        msg = f"🌪️ *ALERTA FORTE: Vento Muito Forte!*\nRajadas de *{rajada:.1f} km/h*. Possibilidade de danos. Atenção redobrada."
        marcar_alerta_enviado(estado, "nivel_vento", 2, msg)

    elif rajada >= 40 and estado["nivel_vento"] < 1:
        msg = f"🌬️ *ALERTA: Vento Forte!*\nRajadas de *{rajada:.1f} km/h*. Risco de queda de galhos."
        marcar_alerta_enviado(estado, "nivel_vento", 1, msg)

    if chuva_hoje >= 70 and estado["nivel_chuva"] < 2:
        msg = f"🌧️ *ALERTA CRÍTICO: Chuva Muito Forte!*\nAcumulado de *{chuva_hoje:.1f} mm* hoje. Risco de enxurradas!"
        marcar_alerta_enviado(estado, "nivel_chuva", 2, msg)

    elif chuva_hoje >= 50 and estado["nivel_chuva"] < 1:
        msg = f"🌧️ *ALERTA: Chuva Forte!*\nAcumulado de *{chuva_hoje:.1f} mm*. Risco de alagamentos em pontos isolados. Dirija com cuidado."
        marcar_alerta_enviado(estado, "nivel_chuva", 1, msg)

    if umidade <= 20 and estado["nivel_umidade"] < 2:
        msg = f"🆘 *ALERTA CRÍTICO: Umidade Muito Baixa!*\nAr extremamente seco, registrando apenas *{umidade}%*. Grave risco à saúde e alto potencial de incêndios."
        marcar_alerta_enviado(estado, "nivel_umidade", 2, msg)

    elif umidade <= 30 and estado["nivel_umidade"] < 1:
        msg = f"💧 *ALERTA: Umidade Baixa!*\nO ar está seco, na faixa de *{umidade}%*. Causa desconforto respiratório."
        marcar_alerta_enviado(estado, "nivel_umidade", 1, msg)


def executar():
    log("🔄 Coletando dados da estação")
    try:
        dados = obter_dados()

        if not dados:
            log("⚠️ Sem dados")
            return

        leitura_bruta_id = dados.get("leitura_bruta_id")

        temp = dados["temp"]
        sensacao = dados["sensacao"]
        umidade = dados["umidade"]
        pressao = dados["pressao"]

        uv = dados["uv"]
        radiacao = dados["radiacao"]

        vento = dados["vento"]
        rajada = dados["rajada"]
        rajada_max = dados.get("rajada_max", rajada)
        vento_dir = dados["vento_dir"]

        chuva_rate = dados["chuva_rate"]
        chuva_evento = dados["chuva_evento"]
        chuva_hoje = dados["chuva_hoje"]

        log(
            f"🌡 {temp}°C | 💧 {umidade}% | 💨 {rajada} km/h (Rajada) | 🌧 {chuva_hoje} mm | ☀️ UV: {uv}"
        )

        log(f"Rajada atual: {rajada} km/h | Rajada máxima do dia: {rajada_max} km/h")

        salvar_historico_clima(dados, leitura_bruta_id=leitura_bruta_id)
        log("💾 Histórico salvo antes de alertas e processamentos")

        estado = carregar_estado()
        if estado.get("rajada_max_nuvem") != rajada_max:
            estado["rajada_max_nuvem"] = rajada_max
            salvar_estado(estado)

        rajada_alerta = max(rajada, rajada_max)
        if rajada_alerta != rajada:
            log(f"Usando rajada máxima do dia para alertas: {rajada_alerta} km/h")
        verificar_alertas(temp, sensacao, rajada_alerta, chuva_hoje, umidade, uv)

    except Exception as e:
        log(f"❌ Erro {e}")


def salvar_resumo_diario_banco(data_ontem_str):
    log(f"💾 Salvando resumo diário definitivo de {data_ontem_str}...")
    conn = None
    try:
        conn = database.get_db()

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
            WHERE COALESCE(substr(data_hora_local, 1, 10), date(data_hora)) = ?
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

    except Exception as e:
        if conn:
            conn.rollback()
        log(f"❌ Erro ao salvar resumo no banco: {e}")
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    log("🚀 Updater iniciado")
    while True:
        executar()
        time.sleep(INTERVALO)
