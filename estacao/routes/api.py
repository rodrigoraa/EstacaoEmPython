from flask import Blueprint, jsonify, request
import acumulados
import database
import calendar
import os
import json
import logging
from config import env_float, env_int
from time_utils import agora_local, data_local, parse_datetime

api_routes = Blueprint("api", __name__)
logger = logging.getLogger(__name__)


@api_routes.route("/health")
def health():
    conn = None
    try:
        conn = database.get_db()
        conn.execute("SELECT 1").fetchone()
        leitura = conn.execute(
            """
            SELECT data_hora, data_hora_local
            FROM historico_clima
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        fila = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   MIN(CASE WHEN status IN ('pendente', 'enviando') THEN criado_em END)
                       AS mais_antigo
            FROM alertas_fila
            WHERE status IN ('pendente', 'enviando')
            """
        ).fetchone()

        idade = None
        if leitura:
            momento = parse_datetime(
                leitura["data_hora_local"] or leitura["data_hora"],
                assume_utc=False,
            )
            if momento:
                idade = max(0, int((agora_local() - momento).total_seconds()))

        limite = max(1, env_int("HEALTH_MAX_READING_AGE_SECONDS", 300))
        leitura_ok = idade is not None and idade <= limite
        status = "ok" if leitura_ok else "degraded"
        payload = {
            "status": status,
            "database": "ok",
            "last_reading_age_seconds": idade,
            "queue": "ok" if not fila["total"] else "pending",
        }
        return jsonify(payload), 200 if leitura_ok else 503
    except Exception as erro:
        logger.error("Health check HTTP falhou: %s", erro)
        return jsonify(
            {
                "status": "critical",
                "database": "error",
                "last_reading_age_seconds": None,
                "queue": "unknown",
            }
        ), 503
    finally:
        if conn:
            conn.close()


def maior_float(*valores):
    maior = 0.0
    for valor in valores:
        try:
            maior = max(maior, float(valor or 0))
        except (TypeError, ValueError):
            continue
    return maior


def rajada_maxima_estado_alertas():
    def extrair_rajada(estado):
        if not estado:
            return None
        try:
            rajada = float(estado.get("rajada_max_nuvem", 0) or 0)
        except (TypeError, ValueError):
            return None
        return rajada if rajada > 0 else None

    try:
        acumulado = acumulados.obter_acumulado_diario(data_local())
        rajada = extrair_rajada(
            {"rajada_max_nuvem": acumulado.get("rajada_max_corrigida")}
        )
        if rajada is not None:
            return rajada
    except Exception as e:
        logger.warning("Erro ao ler rajada corrigida no banco: %s", e)

    try:
        estado = database.obter_estado_alertas()
        rajada = extrair_rajada(estado)
        if rajada is not None:
            return rajada
    except Exception as e:
        logger.warning("Erro ao ler rajada no banco: %s", e)

    try:
        caminho_api = os.path.abspath(__file__)
        pasta_routes = os.path.dirname(caminho_api)
        pasta_raiz = os.path.dirname(pasta_routes)
        pasta_pai = os.path.dirname(pasta_raiz)

        locais_possiveis = [
            os.path.join(pasta_pai, "alert_state.json"),
            os.path.join(pasta_raiz, "alert_state.json"),
            "alert_state.json",
        ]

        for state_file in locais_possiveis:
            if os.path.exists(state_file):
                with open(state_file, "r", encoding="utf-8") as f:
                    estado = json.load(f)
                    rajada = extrair_rajada(estado)
                    if rajada is not None:
                        return rajada
                break
    except Exception as e:
        logger.warning("Erro ao ler rajada em arquivo: %s", e)

    return None


def possivel_tempestade(leitura_atual, leituras_recentes):
    chuva_min = env_float("TEMPESTADE_CHUVA_RATE_MIN", 10.0)
    rajada_min = env_float("TEMPESTADE_RAJADA_MIN", 60.0)
    janela_minutos = max(1, env_int("TEMPESTADE_JANELA_MINUTOS", 10))

    try:
        chuva_rate = float(leitura_atual["chuva_rate"])
    except (TypeError, ValueError):
        return False
    if chuva_rate < chuva_min:
        return False

    referencia = parse_datetime(
        leitura_atual["data_hora_local"] or leitura_atual["data_hora"],
        assume_utc=False,
    )
    for leitura in leituras_recentes:
        try:
            rajada = float(leitura["vento_rajada"])
        except (TypeError, ValueError):
            continue
        if rajada < rajada_min:
            continue
        momento = parse_datetime(
            leitura["data_hora_local"] or leitura["data_hora"],
            assume_utc=False,
        )
        if referencia is None or momento is None:
            if leitura is leitura_atual:
                return True
            continue
        diferenca = (referencia - momento).total_seconds()
        if 0 <= diferenca <= janela_minutos * 60:
            return True
    return False


@api_routes.route("/api/clima")
def api_clima():
    conn = database.get_db()

    rows = conn.execute(
        """
        SELECT
            data_hora,
            data_hora_local,
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
            chuva_hoje
        FROM historico_clima
        ORDER BY id DESC
        LIMIT 50
        """
    ).fetchall()

    conn.close()

    if not rows:
        return jsonify({"erro": "Sem dados"})
    row = rows[0]

    acumulado = acumulados.obter_acumulado_diario(data_local())
    chuva_corrigida = acumulado.get("chuva_total_corrigida", 0) if acumulado else 0
    rajada_corrigida = acumulado.get("rajada_max_corrigida", 0) if acumulado else 0

    rajada_estado = rajada_maxima_estado_alertas()
    rajada_do_dia = maior_float(row["vento_rajada"], rajada_corrigida, rajada_estado)

    data_hora_exibicao = row["data_hora_local"] or row["data_hora"]
    hora = data_hora_exibicao[11:19]

    return jsonify(
        {
            "local": "Vicentina MS - Distrito de São José (EE São José)",
            "temp": row["temp"],
            "sensacao": row["sensacao"],
            "umidade": row["umidade"],
            "pressao": row["pressao"],
            "uv": row["uv"],
            "radiacao": row["radiacao"],
            "vento_atual": row["vento_vel"],
            "vento_rajada": row["vento_rajada"],
            "vento_rajada_max": rajada_do_dia,
            "vento_dir": row["vento_dir"],
            "chuva_rate": row["chuva_rate"],
            "chuva_evento": row["chuva_evento"],
            "chuva_hoje": maior_float(row["chuva_hoje"], chuva_corrigida),
            "possivel_tempestade": possivel_tempestade(row, rows),
            "hora_leitura": hora,
        }
    )

@api_routes.route("/api/historico")
def api_historico():
    data_hoje = data_local()
    conn = database.get_db()

    dados = conn.execute(
        """
        SELECT
            COALESCE(substr(data_hora_local, 12, 2), strftime('%H', data_hora)) || ':00' as hora,
            AVG(temp) as temp,
            MAX(chuva_hoje) as chuva_hoje,
            AVG(vento_vel) as vento_vel
        FROM historico_clima
        WHERE COALESCE(substr(data_hora_local, 1, 10), date(data_hora)) = ?
        GROUP BY hora
        ORDER BY hora ASC
        """
        ,
        (data_hoje,),
    ).fetchall()

    conn.close()

    resultado = []
    chuva_corrigida_por_hora = acumulados.serie_chuva_corrigida_por_hora(data_hoje)

    for row in dados:
        resultado.append(
            {
                "timestamp": row["hora"],
                "temperatura": row["temp"],
                "chuva": chuva_corrigida_por_hora.get(row["hora"], row["chuva_hoje"]),
                "vento": row["vento_vel"],
            }
        )

    return jsonify(resultado)

@api_routes.route("/api/ultimo")
def api_ultimo():
    conn = database.get_db()

    row = conn.execute(
        """
        SELECT
            data_hora,
            data_hora_local,
            temp,
            chuva_hoje as chuva,
            vento_vel
        FROM historico_clima
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    conn.close()

    if not row:
        return jsonify({})

    acumulado = acumulados.obter_acumulado_diario(data_local())
    chuva_corrigida = acumulado.get("chuva_total_corrigida", 0) if acumulado else 0

    return jsonify(
        {
            "timestamp": row["data_hora_local"] or row["data_hora"],
            "temperatura": row["temp"],
            "vento": row["vento_vel"],
            "chuva": maior_float(row["chuva"], chuva_corrigida),
        }
    )

@api_routes.route("/api/historico_semana")
def api_historico_semana():
    conn = database.get_db()
    database.garantir_tabela_acumulados_diarios(conn)
    conn.commit()

    dados = conn.execute(
        """
        SELECT
            strftime('%w', h.data) as dia_semana,
            COALESCE(a.chuva_total_corrigida, h.chuva) as chuva
        FROM (
            SELECT
                COALESCE(substr(data_hora_local, 1, 10), date(data_hora)) as data,
                MAX(chuva_hoje) as chuva
            FROM historico_clima
            WHERE strftime('%W', COALESCE(substr(data_hora_local, 1, 10), date(data_hora))) = strftime('%W', ?)
              AND strftime('%Y', COALESCE(substr(data_hora_local, 1, 10), date(data_hora))) = strftime('%Y', ?)
            GROUP BY data
        ) h
        LEFT JOIN acumulados_diarios a ON a.data = h.data
        ORDER BY h.data ASC
        """
        ,
        (data_local(), data_local()),
    ).fetchall()

    conn.close()

    resultado = []
    for row in dados:
        resultado.append({"dia_semana": row["dia_semana"], "chuva": row["chuva"]})

    return jsonify(resultado)

@api_routes.route("/api/historico_mes")
def historico_mes():
    ano = request.args.get("ano")
    mes = request.args.get("mes")

    if not ano or not mes:
        return jsonify([])

    conn = database.get_db()
    database.garantir_tabela_acumulados_diarios(conn)
    conn.commit()

    dados = conn.execute(
        """
        SELECT 
            strftime('%d', h.data) as dia,
            h.temperatura,
            COALESCE(a.chuva_total_corrigida, h.chuva) as chuva,
            h.vento
        FROM (
            SELECT
                COALESCE(substr(data_hora_local, 1, 10), date(data_hora)) as data,
                ROUND(AVG(temp), 1) as temperatura,
                MAX(chuva_hoje) as chuva,
                MAX(vento_vel) as vento
            FROM historico_clima
            WHERE strftime('%Y', COALESCE(substr(data_hora_local, 1, 10), date(data_hora))) = ?
              AND strftime('%m', COALESCE(substr(data_hora_local, 1, 10), date(data_hora))) = ?
            GROUP BY data
        ) h
        LEFT JOIN acumulados_diarios a ON a.data = h.data
        ORDER BY h.data
        """,
        (ano, mes),
    ).fetchall()

    conn.close()

    return jsonify([dict(d) for d in dados])

@api_routes.route("/api/recordes_mes")
def api_recordes_mes():
    ano = request.args.get("ano")
    mes = request.args.get("mes")

    if not ano or not mes:
        return jsonify({})

    conn = database.get_db()
    database.garantir_tabela_acumulados_diarios(conn)
    conn.commit()

    row = conn.execute(
        """
        SELECT
            MAX(h.max_temp) as max_temp,
            MAX(h.max_vento) as max_vento,
            MAX(COALESCE(a.chuva_total_corrigida, h.max_chuva)) as max_chuva
        FROM (
            SELECT
                COALESCE(substr(data_hora_local, 1, 10), date(data_hora)) as data,
                MAX(temp) as max_temp,
                MAX(vento_vel) as max_vento,
                MAX(chuva_hoje) as max_chuva
            FROM historico_clima
            WHERE strftime('%Y', COALESCE(substr(data_hora_local, 1, 10), date(data_hora))) = ?
              AND strftime('%m', COALESCE(substr(data_hora_local, 1, 10), date(data_hora))) = ?
            GROUP BY data
        ) h
        LEFT JOIN acumulados_diarios a ON a.data = h.data
        """,
        (ano, mes),
    ).fetchone()

    conn.close()

    if not row or row["max_temp"] is None:
        return jsonify({"max_temp": 0, "max_vento": 0, "max_chuva": 0})

    return jsonify(
        {
            "max_temp": row["max_temp"],
            "max_vento": row["max_vento"],
            "max_chuva": row["max_chuva"],
        }
    )

@api_routes.route("/api/historico_consulta")
def api_historico_consulta():
    ano = request.args.get("ano")
    mes = request.args.get("mes")

    if not ano or not mes:
        return jsonify({"erro": "Ano e mês são obrigatórios"}), 400

    try:
        ano_int = int(ano)
        mes_int = int(mes)
    except ValueError:
        return jsonify({"erro": "Ano e mês inválidos"}), 400

    _, num_dias = calendar.monthrange(ano_int, mes_int)

    dias_lista = list(range(1, num_dias + 1))
    chuva_lista = [0.0] * num_dias
    temp_max_lista = [0.0] * num_dias
    temp_min_lista = [0.0] * num_dias
    temp_media_lista = [0.0] * num_dias
    umidade_min_lista = [0.0] * num_dias
    umidade_max_lista = [0.0] * num_dias
    vento_lista = [0.0] * num_dias
    uv_lista = [0.0] * num_dias
    pressao_min_lista = [0.0] * num_dias
    pressao_max_lista = [0.0] * num_dias

    total_chuva = 0.0
    max_temp = 0.0
    min_temp = None
    max_vento = 0.0

    conn = database.get_db()
    linhas = conn.execute(
        """
        SELECT 
            strftime('%d', data) as dia,
            chuva_total, temp_min, temp_max, temp_media,
            umidade_min, umidade_max, vento_rajada_max, 
            uv_max, pressao_min, pressao_max
        FROM historico_diario
        WHERE strftime('%Y', data) = ? AND strftime('%m', data) = ?
        """,
        (ano, mes),
    ).fetchall()
    conn.close()

    for row in linhas:
        dia_idx = int(row["dia"]) - 1

        chuva_dia = 0.0 if row["chuva_total"] is None else row["chuva_total"]
        t_max = 0.0 if row["temp_max"] is None else row["temp_max"]
        t_min = 0.0 if row["temp_min"] is None else row["temp_min"]
        t_med = 0.0 if row["temp_media"] is None else row["temp_media"]
        u_min = 0.0 if row["umidade_min"] is None else row["umidade_min"]
        u_max = 0.0 if row["umidade_max"] is None else row["umidade_max"]
        vento_dia = 0.0 if row["vento_rajada_max"] is None else row["vento_rajada_max"]
        uv_dia = 0.0 if row["uv_max"] is None else row["uv_max"]
        p_min = 0.0 if row["pressao_min"] is None else row["pressao_min"]
        p_max = 0.0 if row["pressao_max"] is None else row["pressao_max"]

        chuva_lista[dia_idx] = chuva_dia
        temp_max_lista[dia_idx] = t_max
        temp_min_lista[dia_idx] = t_min
        temp_media_lista[dia_idx] = t_med
        umidade_min_lista[dia_idx] = u_min
        umidade_max_lista[dia_idx] = u_max
        vento_lista[dia_idx] = vento_dia
        uv_lista[dia_idx] = uv_dia
        pressao_min_lista[dia_idx] = p_min
        pressao_max_lista[dia_idx] = p_max

        total_chuva += chuva_dia
        if t_max > max_temp:
            max_temp = t_max
        if row["temp_min"] is not None and (min_temp is None or t_min < min_temp):
            min_temp = t_min
        if vento_dia > max_vento:
            max_vento = vento_dia

    if min_temp is None:
        min_temp = 0.0

    return jsonify(
        {
            "dias": dias_lista,
            "chuva": chuva_lista,
            "temp_max": temp_max_lista,
            "temp_min": temp_min_lista,
            "temp_media": temp_media_lista,
            "umidade_min": umidade_min_lista,
            "umidade_max": umidade_max_lista,
            "vento": vento_lista,
            "uv": uv_lista,
            "pressao_min": pressao_min_lista,
            "pressao_max": pressao_max_lista,
            "total_chuva": total_chuva,
            "max_temp": max_temp,
            "min_temp": min_temp,
            "max_vento": max_vento,
        }
    )


@api_routes.route("/api/anos_disponiveis")
def api_anos_disponiveis():
    conn = database.get_db()
    linhas = conn.execute(
        """
        SELECT DISTINCT strftime('%Y', data) as ano 
        FROM historico_diario 
        WHERE data IS NOT NULL 
        ORDER BY ano DESC
    """
    ).fetchall()
    conn.close()

    anos = [row["ano"] for row in linhas if row["ano"]]

    ano_atual = data_local()[:4]

    if not anos:
        anos = [ano_atual]
    elif ano_atual not in anos:
        anos.insert(0, ano_atual) 

    return jsonify(anos)
