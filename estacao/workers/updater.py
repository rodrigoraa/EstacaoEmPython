import sys
import os
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
import time
import json
import acumulados
import database
from acumulados import valor_float
from persistence import salvar_historico_clima
from time_utils import agora_local, data_local
from services.weather_service import obter_dados
from unsubscribe_tokens import telefone_com_codigo_pais


STATE_FILE = os.path.join(BASE_DIR, "alert_state.json")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://meteo.eesjv.com.br").rstrip("/")

INTERVALO = 15


def env_float(nome, padrao):
    try:
        return float(os.environ.get(nome, padrao))
    except (TypeError, ValueError):
        return float(padrao)


def env_int(nome, padrao):
    try:
        return int(os.environ.get(nome, padrao))
    except (TypeError, ValueError):
        return int(padrao)


def configuracao_alertas():
    return {
        "calor_1": env_float("ALERTA_CALOR_NIVEL_1", 35),
        "calor_2": env_float("ALERTA_CALOR_NIVEL_2", 40),
        "calor_rearme": env_float("ALERTA_CALOR_REARME", 33),
        "frio_1": env_float("ALERTA_FRIO_NIVEL_1", 12.4),
        "frio_2": env_float("ALERTA_FRIO_NIVEL_2", 5),
        "frio_3": env_float("ALERTA_FRIO_NIVEL_3", 2),
        "frio_rearme": env_float("ALERTA_FRIO_REARME", 15),
        "vento_1": env_float("ALERTA_VENTO_NIVEL_1", 40),
        "vento_2": env_float("ALERTA_VENTO_NIVEL_2", 70),
        "vento_3": env_float("ALERTA_VENTO_NIVEL_3", 100),
        "chuva_1": env_float("ALERTA_CHUVA_NIVEL_1", 50),
        "chuva_2": env_float("ALERTA_CHUVA_NIVEL_2", 70),
        "umidade_1": env_float("ALERTA_UMIDADE_NIVEL_1", 30),
        "umidade_2": env_float("ALERTA_UMIDADE_NIVEL_2", 20),
        "umidade_rearme": env_float("ALERTA_UMIDADE_REARME", 35),
        "confirmacoes_nivel_1": max(
            1, env_int("ALERTA_CONFIRMACOES_NIVEL_1", 2)
        ),
    }


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
        "rajada_max_nuvem": 0.0,
        "chuva_ultima_nuvem": 0.0,
        "aguardando_reset_vento": False,
        "vento_max_dia_anterior": 0.0,
        "aguardando_reset_chuva": False,
        "chuva_base_virada": 0.0,
        "confirmacoes": {},
        "ciclo_calor": 0,
        "ciclo_frio": 0,
        "ciclo_vento": 0,
        "ciclo_chuva": 0,
        "ciclo_umidade": 0,
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


def enfileirar_alerta_usuario(
    conn,
    usuario,
    telefone,
    mensagem,
    evento_id=None,
    prioridade=50,
):
    conn.execute(
        """
        INSERT INTO alertas_fila (
            usuario_id,
            nome,
            telefone,
            mensagem,
            status,
            tentativas,
            evento_id,
            prioridade
        ) VALUES (?, ?, ?, ?, 'pendente', 0, ?, ?)
        """,
        (
            usuario["id"],
            usuario["nome"],
            telefone,
            mensagem,
            evento_id,
            prioridade,
        ),
    )


def enviar_alerta(mensagem, evento=None):
    log(f"🚨 Enfileirando alerta para usuários...")
    conn = database.get_db()
    database.garantir_tabela_alertas_fila(conn)
    database.garantir_tabela_alertas_eventos(conn)
    conn.commit()
    enfileirados = 0
    falhas = 0

    try:
        evento_id = evento.get("evento_id") if evento else None
        if evento:
            cursor_evento = conn.execute(
                """
                INSERT OR IGNORE INTO alertas_eventos (
                    evento_id, data_referencia, tipo, nivel, valor, unidade,
                    ocorrido_em_local, fonte, mensagem
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evento_id,
                    evento["data_referencia"],
                    evento["tipo"],
                    evento["nivel"],
                    evento.get("valor"),
                    evento.get("unidade"),
                    evento.get("ocorrido_em_local"),
                    evento.get("fonte"),
                    mensagem,
                ),
            )
            if cursor_evento.rowcount == 0:
                conn.commit()
                return {
                    "total": 0,
                    "enfileirados": 0,
                    "falhas": 0,
                    "duplicado": True,
                }

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
            telefone = telefone_com_codigo_pais(usuario["telefone"])
            mensagem_final = montar_mensagem_alerta(usuario, mensagem)

            try:
                enfileirar_alerta_usuario(
                    conn,
                    usuario,
                    telefone,
                    mensagem_final,
                    evento_id=evento_id,
                    prioridade=evento.get("prioridade", 50) if evento else 50,
                )
                enfileirados += 1
                log(f"✅ Alerta enfileirado para {usuario['nome']}")
            except Exception as e:
                falhas += 1
                log(f"❌ Erro ao enfileirar {usuario['nome']} ({telefone}) {e}")

        if evento:
            status = "enfileirado" if enfileirados else "sem_destinatarios"
            conn.execute(
                """
                UPDATE alertas_eventos
                SET status = ?, destinatarios = ?, enfileirados = ?,
                    falhas = ?, atualizado_em = CURRENT_TIMESTAMP
                WHERE evento_id = ?
                """,
                (status, len(usuarios), enfileirados, falhas, evento_id),
            )
        conn.commit()
        return {"total": len(usuarios), "enfileirados": enfileirados, "falhas": falhas}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def marcar_alerta_enviado(
    estado,
    chave_nivel,
    nivel,
    mensagem,
    atualizacoes=None,
    valor=None,
    unidade=None,
    ocorrido_em_local=None,
    fonte="ambientweather",
):
    tipo = chave_nivel.replace("nivel_", "")
    ciclo = int(estado.get(f"ciclo_{tipo}", 0) or 0)
    data_referencia = estado.get("data") or data_local()
    evento_id = f"{data_referencia}:{tipo}:{nivel}:{ciclo}"
    prioridade = 100 if nivel >= 3 else 80 if nivel == 2 else 50

    resultado = enviar_alerta(
        mensagem,
        evento={
            "evento_id": evento_id,
            "data_referencia": data_referencia,
            "tipo": tipo,
            "nivel": nivel,
            "valor": valor,
            "unidade": unidade,
            "ocorrido_em_local": ocorrido_em_local,
            "fonte": fonte,
            "prioridade": prioridade,
        },
    )

    if resultado["enfileirados"] > 0 or resultado.get("duplicado"):
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

    if estado["nivel_frio"] > 0 and temp_max >= configuracao_alertas()["frio_rearme"]:
        estado["nivel_frio"] = 0
        estado["frio_rearmado"] = True
        estado["ciclo_frio"] = int(estado.get("ciclo_frio", 0) or 0) + 1
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

    # Condicoes instantaneas nao devem gerar outro alerta apenas porque a
    # data mudou. O nivel sera rearmado quando a condicao normalizar.
    estado["nivel_calor"] = estado_anterior.get("nivel_calor", 0)
    estado["nivel_umidade"] = estado_anterior.get("nivel_umidade", 0)
    for tipo in ("calor", "frio", "umidade"):
        estado[f"ciclo_{tipo}"] = int(
            estado_anterior.get(f"ciclo_{tipo}", 0) or 0
        )

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


def data_da_leitura(dados):
    data_hora = (dados or {}).get("station_data_hora_local")
    if data_hora:
        return str(data_hora)[:10]
    return data_local()


def preparar_dados_novo_dia(dados):
    """Separa contadores antigos antes de atualizar os acumulados do dia."""
    dados = dict(dados)
    hoje = data_da_leitura(dados)
    estado = carregar_estado()

    rajada_atual = valor_float(dados.get("rajada"))
    rajada_max_nuvem = valor_float(dados.get("rajada_max"), rajada_atual)
    chuva_nuvem = max(valor_float(dados.get("chuva_hoje")), 0.0)

    if estado.get("data") != hoje:
        if estado.get("data"):
            salvar_resumo_diario_banco(estado["data"])

        vento_anterior = max(valor_float(estado.get("rajada_max_nuvem")), 0.0)
        chuva_anterior = max(valor_float(estado.get("chuva_ultima_nuvem")), 0.0)
        estado = estado_alertas_novo_dia(estado, hoje)

        estado["vento_max_dia_anterior"] = vento_anterior
        estado["aguardando_reset_vento"] = (
            vento_anterior > 0 and rajada_max_nuvem >= vento_anterior
        )
        estado["chuva_base_virada"] = chuva_anterior
        estado["aguardando_reset_chuva"] = (
            chuva_anterior > 0 and chuva_nuvem >= chuva_anterior
        )

        log(
            "Novo dia da estacao iniciado; aguardando a virada dos "
            "contadores diarios de vento e chuva."
        )

    if estado.get("aguardando_reset_vento"):
        referencia = valor_float(estado.get("vento_max_dia_anterior"))
        if rajada_max_nuvem < referencia:
            estado["aguardando_reset_vento"] = False
            estado["vento_max_dia_anterior"] = 0.0
            log("Reset diario da rajada maxima confirmado pela estacao.")
        elif rajada_max_nuvem > referencia:
            # Mesmo sem observar o zero, um valor acima do fechamento de ontem
            # representa uma nova maxima registrada depois da virada.
            dados["rajada_max"] = rajada_max_nuvem
        else:
            # maxdailygust ainda pertence ao dia anterior. Enquanto isso,
            # conserva apenas a maior rajada realmente lida no novo dia.
            dados["rajada_max"] = rajada_atual

    if estado.get("aguardando_reset_chuva"):
        base = valor_float(estado.get("chuva_base_virada"))
        if chuva_nuvem < base:
            estado["aguardando_reset_chuva"] = False
            estado["chuva_base_virada"] = 0.0
            log("Reset diario da chuva acumulada confirmado pela estacao.")
        else:
            # Se chover antes do reset fisico, contabiliza apenas o incremento
            # posterior a meia-noite, sem carregar o total do dia anterior.
            dados["chuva_hoje"] = round(max(chuva_nuvem - base, 0.0), 1)

    estado["chuva_ultima_nuvem"] = chuva_nuvem
    salvar_estado(estado)
    return dados, hoje


def atualizar_rearme_condicoes_instantaneas(
    estado, temp, umidade, temp_valida=True, umidade_valida=True
):
    config = configuracao_alertas()
    alterado = False

    if temp_valida and estado.get("nivel_calor", 0) > 0 and temp < config["calor_rearme"]:
        estado["nivel_calor"] = 0
        estado["ciclo_calor"] = int(estado.get("ciclo_calor", 0) or 0) + 1
        alterado = True

    if umidade_valida and estado.get("nivel_umidade", 0) > 0 and umidade > config["umidade_rearme"]:
        estado["nivel_umidade"] = 0
        estado["ciclo_umidade"] = int(estado.get("ciclo_umidade", 0) or 0) + 1
        alterado = True

    if alterado:
        salvar_estado(estado)


def confirmar_nivel_1(estado, tipo, condicao):
    confirmacoes = estado.setdefault("confirmacoes", {})
    anterior = int(confirmacoes.get(tipo, 0) or 0)
    necessarias = configuracao_alertas()["confirmacoes_nivel_1"]
    atual = min(anterior + 1, necessarias) if condicao else 0
    confirmacoes[tipo] = atual
    if atual != anterior:
        salvar_estado(estado)
    return condicao and atual >= necessarias


def verificar_alertas(
    temp,
    sensacao,
    rajada,
    chuva_hoje,
    umidade,
    uv,
    data_referencia=None,
    ocorrido_em_local=None,
    validade_alertas=None,
):
    estado = carregar_estado()
    hoje = data_referencia or data_local()
    config = configuracao_alertas()
    validade = validade_alertas or {
        "temperatura": True,
        "sensacao": True,
        "vento": True,
        "chuva": True,
        "umidade": True,
        "uv": True,
    }

    # Mudança de dia: salva o resumo e inicia o novo dia preservando frio ativo.
    if estado.get("data") != hoje:
        if estado.get("data") != "":
            salvar_resumo_diario_banco(estado["data"])

        estado = estado_alertas_novo_dia(estado, hoje)
        salvar_estado(estado)

    if validade.get("temperatura"):
        atualizar_estado_rearme_frio(estado, temp)
    atualizar_rearme_condicoes_instantaneas(
        estado,
        temp,
        umidade,
        temp_valida=validade.get("temperatura", False),
        umidade_valida=validade.get("umidade", False),
    )

    calor_confirmado = confirmar_nivel_1(
        estado, "calor", validade.get("temperatura") and temp >= config["calor_1"]
    )
    frio_confirmado = confirmar_nivel_1(
        estado,
        "frio",
        (validade.get("temperatura") and temp <= config["frio_1"])
        or (validade.get("sensacao") and sensacao <= config["frio_1"]),
    )
    vento_confirmado = confirmar_nivel_1(
        estado, "vento", validade.get("vento") and rajada >= config["vento_1"]
    )
    chuva_confirmada = confirmar_nivel_1(
        estado, "chuva", validade.get("chuva") and chuva_hoje >= config["chuva_1"]
    )
    umidade_confirmada = confirmar_nivel_1(
        estado, "umidade", validade.get("umidade") and umidade <= config["umidade_1"]
    )

    if validade.get("temperatura") and temp >= config["calor_2"] and estado["nivel_calor"] < 2:
        msg = f"🔥 *ALERTA CRÍTICO: Temperatura Muito Alta!*\nOs termômetros atingiram *{formatar_temperatura_alerta(temp)}*\nSensação térmica de *{formatar_temperatura_alerta(sensacao)}*.\nRisco iminente de insolação. Evite exposição ao sol!"
        marcar_alerta_enviado(estado, "nivel_calor", 2, msg, valor=temp, unidade="°C", ocorrido_em_local=ocorrido_em_local)

    elif calor_confirmado and estado["nivel_calor"] < 1:
        msg = f"🌡️ *ALERTA: Temperatura Alta!*\nRegistrados *{formatar_temperatura_alerta(temp)}*\nSensação térmica de *{formatar_temperatura_alerta(sensacao)}*.\nCalor forte na região."
        marcar_alerta_enviado(estado, "nivel_calor", 1, msg, valor=temp, unidade="°C", ocorrido_em_local=ocorrido_em_local)

    if validade.get("temperatura") and temp <= config["frio_3"] and estado["nivel_frio"] < 3:
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
            valor=temp,
            unidade="°C",
            ocorrido_em_local=ocorrido_em_local,
        )

    elif validade.get("temperatura") and temp <= config["frio_2"] and estado["nivel_frio"] < 2:
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
            valor=temp,
            unidade="°C",
            ocorrido_em_local=ocorrido_em_local,
        )

    elif frio_confirmado and estado["nivel_frio"] < 1:
        msg = mensagem_frio(
            "❄️ *ALERTA: Temperatura Baixa",
            "Cuide-se e agasalhe-se.",
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
            valor=min(temp, sensacao),
            unidade="°C",
            ocorrido_em_local=ocorrido_em_local,
        )

    if validade.get("vento") and rajada >= config["vento_3"] and estado["nivel_vento"] < 3:
        msg = f"🌪️ *ALERTA CRÍTICO: Vento Extremo!*\nRajadas violentas de *{rajada:.1f} km/h*."
        marcar_alerta_enviado(estado, "nivel_vento", 3, msg, valor=rajada, unidade="km/h", ocorrido_em_local=ocorrido_em_local)

    elif validade.get("vento") and rajada >= config["vento_2"] and estado["nivel_vento"] < 2:
        msg = f"🌪️ *ALERTA FORTE: Vento Muito Forte!*\nRajadas de *{rajada:.1f} km/h*."
        marcar_alerta_enviado(estado, "nivel_vento", 2, msg, valor=rajada, unidade="km/h", ocorrido_em_local=ocorrido_em_local)

    elif vento_confirmado and estado["nivel_vento"] < 1:
        msg = f"🌬️ *ALERTA: Vento Forte!*\nRajadas de *{rajada:.1f} km/h*."
        marcar_alerta_enviado(estado, "nivel_vento", 1, msg, valor=rajada, unidade="km/h", ocorrido_em_local=ocorrido_em_local)

    if validade.get("chuva") and chuva_hoje >= config["chuva_2"] and estado["nivel_chuva"] < 2:
        msg = f"🌧️ *ALERTA CRÍTICO: Chuva Muito Forte!*\nAcumulado de *{chuva_hoje:.1f} mm* hoje."
        marcar_alerta_enviado(estado, "nivel_chuva", 2, msg, valor=chuva_hoje, unidade="mm", ocorrido_em_local=ocorrido_em_local)

    elif chuva_confirmada and estado["nivel_chuva"] < 1:
        msg = f"🌧️ *ALERTA: Chuva Forte!*\nAcumulado de *{chuva_hoje:.1f} mm*.\nDirija com cuidado."
        marcar_alerta_enviado(estado, "nivel_chuva", 1, msg, valor=chuva_hoje, unidade="mm", ocorrido_em_local=ocorrido_em_local)

    if validade.get("umidade") and umidade <= config["umidade_2"] and estado["nivel_umidade"] < 2:
        msg = f"🆘 *ALERTA CRÍTICO: Umidade Muito Baixa!*\nAr extremamente seco, registrando apenas *{umidade}%*.\nGrave risco à saúde e alto potencial de incêndios."
        marcar_alerta_enviado(estado, "nivel_umidade", 2, msg, valor=umidade, unidade="%", ocorrido_em_local=ocorrido_em_local)

    elif umidade_confirmada and estado["nivel_umidade"] < 1:
        msg = f"💧 *ALERTA: Umidade Baixa!*\nO ar está seco, na faixa de *{umidade}%*."
        marcar_alerta_enviado(estado, "nivel_umidade", 1, msg, valor=umidade, unidade="%", ocorrido_em_local=ocorrido_em_local)

def executar():
    log("🔄 Coletando dados da estação")
    try:
        dados = obter_dados()

        if not dados:
            log("⚠️ Sem dados")
            return

        leitura_bruta_id = dados.get("leitura_bruta_id")
        dados, data_leitura = preparar_dados_novo_dia(dados)

        temp = dados["temp"]
        sensacao = dados["sensacao"]
        umidade = dados["umidade"]
        uv = dados["uv"]
        rajada = dados["rajada"]
        rajada_max = dados.get("rajada_max", rajada)
        chuva_hoje = dados["chuva_hoje"]

        log(
            f"🌡 {temp}°C | 💧 {umidade}% | 💨 {rajada} km/h (Rajada) | 🌧 {chuva_hoje} mm | ☀️ UV: {uv}"
        )

        log(f"Rajada atual: {rajada} km/h | Rajada máxima do dia: {rajada_max} km/h")

        salvar_historico_clima(dados, leitura_bruta_id=leitura_bruta_id)
        log("💾 Histórico salvo antes de alertas e processamentos")

        acumulado = acumulados.atualizar_acumulado_diario(dados, data_leitura)
        rajada_corrigida = acumulado["rajada_max_corrigida"]
        chuva_corrigida = acumulado["chuva_total_corrigida"]
        log(
            "Acumulados corrigidos do dia: "
            f"rajada máxima {rajada_corrigida} km/h | chuva {chuva_corrigida} mm"
        )

        rajada_alerta = max(rajada, rajada_max, rajada_corrigida)
        if rajada_alerta != rajada:
            log(f"Usando rajada máxima do dia para alertas: {rajada_alerta} km/h")
        verificar_alertas(
            temp,
            sensacao,
            rajada_alerta,
            chuva_corrigida,
            umidade,
            uv,
            data_referencia=data_leitura,
            ocorrido_em_local=dados.get("station_data_hora_local"),
            validade_alertas=dados.get("validade_alertas"),
        )

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
