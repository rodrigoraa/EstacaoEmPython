import sys
import os
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import time
import json
import acumulados
import database
from persistence import salvar_historico_clima
from time_utils import agora_local, data_local
from services.weather_service import obter_dados


STATE_FILE = os.path.join(BASE_DIR, "alert_state.json")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://meteo.eesjv.com.br").rstrip("/")

INTERVALO = 15
FRIO_REARME_TEMP = 15.0


def log(msg):
    agora = agora_local().strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {msg}", flush=True)


def estado_alertas_padrao(data=""):
    return {
        "data": data,
        "nivel_calor": 0,
        "nivel_frio": 0,
        "nivel_vento": 0,
        "nivel_chuva": 0,
        "nivel_umidade": 0,
        "nivel_uv": 0,
        "rajada_max_nuvem": 0.0,
        "frio_rearmado": False,
        "temp_max_apos_alerta_frio": None,
    }


def normalizar_estado_alertas(estado):
    padrao = estado_alertas_padrao()
    if not isinstance(estado, dict):
        return padrao

    for chave, valor in padrao.items():
        if chave not in estado:
            estado[chave] = valor
    return estado


def carregar_estado_arquivo():
    padrao = estado_alertas_padrao()

    if not os.path.exists(STATE_FILE):
        return None

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return normalizar_estado_alertas(json.load(f))
    except (OSError, json.JSONDecodeError) as erro:
        log(f"⚠️ Estado de alertas inválido, usando padrão: {erro}")
        return padrao


def carregar_estado():
    try:
        estado_banco = database.obter_estado_alertas()
        if estado_banco is not None:
            return normalizar_estado_alertas(estado_banco)
    except (sqlite3.Error, json.JSONDecodeError, TypeError) as erro:
        log(f"⚠️ Estado de alertas no banco indisponível, usando arquivo: {erro}")

    estado_arquivo = carregar_estado_arquivo()
    if estado_arquivo is not None:
        salvar_estado(estado_arquivo)
        return estado_arquivo

    return estado_alertas_padrao()


def salvar_estado(estado):
    try:
        database.salvar_estado_alertas(estado)
    except (sqlite3.Error, TypeError, ValueError) as erro:
        log(f"⚠️ Não foi possível salvar estado de alertas no banco: {erro}")

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f, ensure_ascii=False, sort_keys=True)
    except OSError as erro:
        log(f"⚠️ Não foi possível salvar estado de alertas em arquivo: {erro}")


def telefone_alerta(telefone):
    telefone = "".join(filter(str.isdigit, telefone or ""))
    if telefone and not telefone.startswith("55"):
        telefone = "55" + telefone
    return telefone


def montar_mensagem_alerta(usuario, mensagem):
    link_meteo = f"{PUBLIC_BASE_URL}"
    nome_usuario = (usuario["nome"] or "").strip()
    saudacao = f"ATENÇÃO, {nome_usuario}," if nome_usuario else ""

    return (
        f"{saudacao}\n"
        f"{mensagem}\n\n"
        f"📍 _Vicentina MS - Distrito de São José_\n"
        f" Para mais informações, acesse:\n"
        f"{link_meteo}"
    )


def enfileirar_alerta_usuario(conn, usuario, telefone, mensagem):
    conn.execute(
        """
        INSERT INTO alertas_fila (
            usuario_id,
            nome,
            telefone,
            mensagem,
            status,
            tentativas
        ) VALUES (?, ?, ?, ?, 'pendente', 0)
        """,
        (
            usuario["id"],
            usuario["nome"],
            telefone,
            mensagem,
        ),
    )


def enviar_alerta(mensagem):
    log(f"🚨 Enfileirando alerta para usuários...")
    conn = database.get_db()
    conn.row_factory = sqlite3.Row
    database.garantir_tabela_alertas_fila(conn)
    conn.commit()
    enfileirados = 0
    falhas = 0

    try:
        usuarios = conn.execute(
            """
            SELECT id, nome, telefone
            FROM usuarios
            WHERE (ativo = 1 OR ativo IS NULL)
            AND receber_whatsapp = 1
            ORDER BY id
            """
        ).fetchall()

        for usuario in usuarios:
            telefone = telefone_alerta(usuario["telefone"])
            mensagem_final = montar_mensagem_alerta(usuario, mensagem)

            try:
                enfileirar_alerta_usuario(conn, usuario, telefone, mensagem_final)
                enfileirados += 1
                log(f"✅ Alerta enfileirado para {usuario['nome']}")
            except Exception as e:
                falhas += 1
                log(f"❌ Erro ao enfileirar {usuario['nome']} ({telefone}) {e}")

        conn.commit()
        return {"total": len(usuarios), "enfileirados": enfileirados, "falhas": falhas}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def marcar_alerta_enviado(estado, chave_nivel, nivel, mensagem, atualizacoes=None):
    resultado = enviar_alerta(mensagem)

    if resultado["enfileirados"] > 0:
        estado[chave_nivel] = nivel
        if atualizacoes:
            estado.update(atualizacoes)
        salvar_estado(estado)
        return True

    log(
        f"Alerta nao marcado como enfileirado: {resultado['falhas']} falhas "
        f"de {resultado['total']} destinatarios."
    )
    return False


def atualizar_estado_rearme_frio(estado, temp):
    if estado["nivel_frio"] <= 0 and not estado.get("frio_rearmado"):
        return

    temp_max = estado.get("temp_max_apos_alerta_frio")
    alterado = False

    if temp_max is None or temp > temp_max:
        estado["temp_max_apos_alerta_frio"] = temp
        temp_max = temp
        alterado = True

    if estado["nivel_frio"] > 0 and temp_max >= FRIO_REARME_TEMP:
        estado["nivel_frio"] = 0
        estado["frio_rearmado"] = True
        alterado = True
        log(
            "Alerta de frio rearmado apos temperatura subir "
            f"para {temp_max:.1f}°C."
        )

    if alterado:
        salvar_estado(estado)


def formatar_temperatura_alerta(valor):
    try:
        temperatura = Decimal(str(valor)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    except (InvalidOperation, TypeError, ValueError):
        return f"{valor}°C"

    return f"{temperatura}°C"


def mensagem_frio(titulo, texto_base, temp, sensacao, estado):
    temp_max = estado.get("temp_max_apos_alerta_frio")
    if estado.get("frio_rearmado") and temp_max is not None and temp_max > temp:
        return (
            f"{titulo} novamente!*\n"
            f"A temperatura subiu até *{formatar_temperatura_alerta(temp_max)}* "
            f"e caiu novamente para *{formatar_temperatura_alerta(temp)}*.\n"
            f"Sensação térmica de *{formatar_temperatura_alerta(sensacao)}*.\n"
            f"{texto_base}"
        )

    return (
        f"{titulo}!*\n"
        f"Registrados *{formatar_temperatura_alerta(temp)}*\n"
        f"Sensação térmica de *{formatar_temperatura_alerta(sensacao)}*.\n"
        f"{texto_base}"
    )


def estado_alertas_novo_dia(estado_anterior, hoje):
    estado = estado_alertas_padrao(hoje)

    if (
        estado_anterior.get("nivel_frio", 0) > 0
        or estado_anterior.get("frio_rearmado")
    ):
        estado["nivel_frio"] = estado_anterior.get("nivel_frio", 0)
        estado["frio_rearmado"] = estado_anterior.get("frio_rearmado", False)
        estado["temp_max_apos_alerta_frio"] = estado_anterior.get(
            "temp_max_apos_alerta_frio"
        )

    return estado


def verificar_alertas(temp, sensacao, rajada, chuva_hoje, umidade, uv):
    estado = carregar_estado()
    hoje = data_local()

    # Mudança de dia: salva o resumo e inicia o novo dia preservando frio ativo.
    if estado.get("data") != hoje:
        if estado.get("data") != "":
            salvar_resumo_diario_banco(estado["data"])

        estado = estado_alertas_novo_dia(estado, hoje)
        salvar_estado(estado)

    atualizar_estado_rearme_frio(estado, temp)

    if temp >= 40 and estado["nivel_calor"] < 2:
        msg = f"🔥 *ALERTA CRÍTICO: Temperatura Muito Alta!*\nOs termômetros atingiram *{formatar_temperatura_alerta(temp)}*\nSensação térmica de *{formatar_temperatura_alerta(sensacao)}*.\nRisco iminente de insolação. Evite exposição ao sol!"
        marcar_alerta_enviado(estado, "nivel_calor", 2, msg)

    elif temp >= 35 and estado["nivel_calor"] < 1:
        msg = f"🌡️ *ALERTA: Temperatura Alta!*\nRegistrados *{formatar_temperatura_alerta(temp)}*\nSensação térmica de *{formatar_temperatura_alerta(sensacao)}*.\nCalor forte na região."
        marcar_alerta_enviado(estado, "nivel_calor", 1, msg)

    if temp <= 2 and estado["nivel_frio"] < 3:
        msg = mensagem_frio(
            "🥶 *ALERTA MÁXIMO: Frio Congelante",
            "Alto risco de geada!",
            temp,
            sensacao,
            estado,
        )
        marcar_alerta_enviado(
            estado,
            "nivel_frio",
            3,
            msg,
            {"frio_rearmado": False, "temp_max_apos_alerta_frio": temp},
        )

    elif temp <= 5 and estado["nivel_frio"] < 2:
        msg = mensagem_frio(
            "🧊 *ALERTA CRÍTICO: Frio Extremo",
            "Proteja-se do frio.",
            temp,
            sensacao,
            estado,
        )
        marcar_alerta_enviado(
            estado,
            "nivel_frio",
            2,
            msg,
            {"frio_rearmado": False, "temp_max_apos_alerta_frio": temp},
        )

    elif temp <= 12.4 and estado["nivel_frio"] < 1:
        msg = mensagem_frio(
            "❄️ *ALERTA: Temperatura Baixa",
            temp,
            sensacao,
            estado,
        )
        marcar_alerta_enviado(
            estado,
            "nivel_frio",
            1,
            msg,
            {"frio_rearmado": False, "temp_max_apos_alerta_frio": temp},
        )

    if rajada >= 100 and estado["nivel_vento"] < 3:
        msg = f"🌪️ *ALERTA CRÍTICO: Vento Extremo!*\nRajadas violentas de *{rajada:.1f} km/h*.\nAlto risco de destelhamentos e queda de árvores!"
        marcar_alerta_enviado(estado, "nivel_vento", 3, msg)

    elif rajada >= 70 and estado["nivel_vento"] < 2:
        msg = f"🌪️ *ALERTA FORTE: Vento Muito Forte!*\nRajadas de *{rajada:.1f} km/h*.\nPossibilidade de danos. Atenção redobrada."
        marcar_alerta_enviado(estado, "nivel_vento", 2, msg)

    elif rajada >= 40 and estado["nivel_vento"] < 1:
        msg = f"🌬️ *ALERTA: Vento Forte!*\nRajadas de *{rajada:.1f} km/h*.\nRisco de queda de galhos."
        marcar_alerta_enviado(estado, "nivel_vento", 1, msg)

    if chuva_hoje >= 70 and estado["nivel_chuva"] < 2:
        msg = f"🌧️ *ALERTA CRÍTICO: Chuva Muito Forte!*\nAcumulado de *{chuva_hoje:.1f} mm* hoje.\nRisco de enxurradas!"
        marcar_alerta_enviado(estado, "nivel_chuva", 2, msg)

    elif chuva_hoje >= 50 and estado["nivel_chuva"] < 1:
        msg = f"🌧️ *ALERTA: Chuva Forte!*\nAcumulado de *{chuva_hoje:.1f} mm*.\nDirija com cuidado."
        marcar_alerta_enviado(estado, "nivel_chuva", 1, msg)

    if umidade <= 20 and estado["nivel_umidade"] < 2:
        msg = f"🆘 *ALERTA CRÍTICO: Umidade Muito Baixa!*\nAr extremamente seco, registrando apenas *{umidade}%*.\nGrave risco à saúde e alto potencial de incêndios."
        marcar_alerta_enviado(estado, "nivel_umidade", 2, msg)

    elif umidade <= 30 and estado["nivel_umidade"] < 1:
        msg = f"💧 *ALERTA: Umidade Baixa!*\nO ar está seco, na faixa de *{umidade}%*.\nCausa desconforto respiratório."
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

        acumulado = acumulados.atualizar_acumulado_diario(dados, data_local())
        rajada_corrigida = acumulado["rajada_max_corrigida"]
        chuva_corrigida = acumulado["chuva_total_corrigida"]
        log(
            "Acumulados corrigidos do dia: "
            f"rajada máxima {rajada_corrigida} km/h | chuva {chuva_corrigida} mm"
        )

        rajada_alerta = max(rajada, rajada_max, rajada_corrigida)
        if rajada_alerta != rajada:
            log(f"Usando rajada máxima do dia para alertas: {rajada_alerta} km/h")
        verificar_alertas(temp, sensacao, rajada_alerta, chuva_corrigida, umidade, uv)

        estado = carregar_estado()
        if estado.get("rajada_max_nuvem") != rajada_corrigida:
            estado["rajada_max_nuvem"] = rajada_corrigida
            salvar_estado(estado)

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
        acumulado = acumulados.obter_acumulado_diario(data_ontem_str)

        if resumo and resumo[0] is not None:
            vento_rajada_max = max(
                resumo[5] or 0,
                acumulado.get("rajada_max_corrigida", 0) if acumulado else 0,
            )
            chuva_total = max(
                resumo[6] or 0,
                acumulado.get("chuva_total_corrigida", 0) if acumulado else 0,
            )
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
                    vento_rajada_max,
                    chuva_total,
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
