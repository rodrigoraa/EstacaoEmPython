from flask import Blueprint, jsonify, request
import database
import calendar
import sqlite3
import os
import json

api_routes = Blueprint("api", __name__)


# ================= CLIMA ATUAL (TEMPO REAL DO BANCO) =================


@api_routes.route("/api/clima")
def api_clima():
    conn = database.get_db()

    row = conn.execute(
        """
        SELECT
            data_hora,
            temp,
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
        LIMIT 1
        """
    ).fetchone()

    conn.close()

    if not row:
        return jsonify({"erro": "Sem dados"})

    rajada_do_dia = row["vento_rajada"]  # Valor padrão (se o arquivo falhar)
    try:
        # Busca inteligente: procura a pasta atual, a raiz e a pasta pai
        caminho_api = os.path.abspath(__file__)
        pasta_routes = os.path.dirname(caminho_api)
        pasta_raiz = os.path.dirname(pasta_routes)
        pasta_pai = os.path.dirname(pasta_raiz)

        # O updater pode ter salvo em um destes 3 lugares, vamos checar todos:
        locais_possiveis = [
            os.path.join(pasta_pai, "alert_state.json"),
            os.path.join(pasta_raiz, "alert_state.json"),
            "alert_state.json",
        ]

        for state_file in locais_possiveis:
            if os.path.exists(state_file):
                with open(state_file, "r") as f:
                    estado = json.load(f)
                    # Se tiver o recorde lá dentro e for maior que zero, ele atualiza!
                    if "rajada_max_nuvem" in estado and estado["rajada_max_nuvem"] > 0:
                        # Pega o maior valor entre o banco e a nuvem, por precaução
                        rajada_do_dia = max(
                            row["vento_rajada"], estado["rajada_max_nuvem"]
                        )
                break  # Achou o arquivo, pode parar de procurar
    except Exception as e:
        print(f"Erro ao ler rajada: {e}", flush=True)

    hora = row["data_hora"][11:19]

    return jsonify(
        {
            "local": "Vicentina MS - Distrito de São José (EE São José)",
            "temp": row["temp"],
            "sensacao": row["temp"],
            "umidade": row["umidade"],
            "pressao": row["pressao"],
            "uv": row["uv"],
            "radiacao": row["radiacao"],
            "vento_atual": row["vento_vel"],
            "vento_rajada": rajada_do_dia,  # <--- ENVIADO PARA A BÚSSOLA
            "vento_dir": row["vento_dir"],
            "chuva_rate": row["chuva_rate"],
            "chuva_evento": row["chuva_evento"],
            "chuva_hoje": row["chuva_hoje"],
            "hora_leitura": hora,
        }
    )


# ================= HISTÓRICO DO DIA (MÉDIA POR HORA) =================


@api_routes.route("/api/historico")
def api_historico():
    conn = database.get_db()

    dados = conn.execute(
        """
        SELECT
            strftime('%H:00', data_hora) as hora,
            AVG(temp) as temp,
            MAX(chuva_hoje) as chuva_hoje,
            AVG(vento_vel) as vento_vel
        FROM historico_clima
        WHERE date(data_hora) = date('now','localtime')
        GROUP BY hora
        ORDER BY hora ASC
        """
    ).fetchall()

    conn.close()

    resultado = []

    for row in dados:
        resultado.append(
            {
                "timestamp": row["hora"],
                "temperatura": row["temp"],
                "chuva": row["chuva_hoje"],
                "vento": row["vento_vel"],
            }
        )

    return jsonify(resultado)


# ================= ÚLTIMO REGISTRO =================


@api_routes.route("/api/ultimo")
def api_ultimo():
    conn = database.get_db()

    row = conn.execute(
        """
        SELECT
            data_hora,
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

    return jsonify(
        {
            "timestamp": row["data_hora"],
            "temperatura": row["temp"],
            "vento": row["vento_vel"],
            "chuva": row["chuva"],
        }
    )


# ================= HISTÓRICO DA SEMANA =================


@api_routes.route("/api/historico_semana")
def api_historico_semana():
    conn = database.get_db()

    # strftime('%W') = Agrupa pela semana atual do ano
    # strftime('%w') = Pega o dia da semana (0=Dom, 1=Seg, 2=Ter, etc)
    dados = conn.execute(
        """
        SELECT
            strftime('%w', data_hora) as dia_semana,
            MAX(chuva_hoje) as chuva
        FROM historico_clima
        WHERE strftime('%W', data_hora) = strftime('%W', 'now', 'localtime')
          AND strftime('%Y', data_hora) = strftime('%Y', 'now', 'localtime')
        GROUP BY dia_semana
        ORDER BY dia_semana ASC
        """
    ).fetchall()

    conn.close()

    resultado = []
    for row in dados:
        resultado.append({"dia_semana": row["dia_semana"], "chuva": row["chuva"]})

    return jsonify(resultado)


# ================= HISTÓRICO DO MÊS =================


@api_routes.route("/api/historico_mes")
def historico_mes():
    ano = request.args.get("ano")
    mes = request.args.get("mes")

    if not ano or not mes:
        return jsonify([])

    conn = database.get_db()

    dados = conn.execute(
        """
        SELECT 
            strftime('%d', data_hora) as dia,
            ROUND(AVG(temp), 1) as temperatura,
            MAX(chuva_hoje) as chuva,
            MAX(vento_vel) as vento
        FROM historico_clima
        WHERE strftime('%Y', data_hora) = ? 
          AND strftime('%m', data_hora) = ?
        GROUP BY dia
        ORDER BY dia
        """,
        (ano, mes),
    ).fetchall()

    conn.close()

    return jsonify([dict(d) for d in dados])


# ================= RECORDES DO MÊS (MÁXIMAS) =================


@api_routes.route("/api/recordes_mes")
def api_recordes_mes():
    ano = request.args.get("ano")
    mes = request.args.get("mes")

    if not ano or not mes:
        return jsonify({})

    conn = database.get_db()

    row = conn.execute(
        """
        SELECT
            MAX(temp) as max_temp,
            MAX(vento_vel) as max_vento,
            MAX(chuva_hoje) as max_chuva
        FROM historico_clima
        WHERE strftime('%Y', data_hora) = ?
          AND strftime('%m', data_hora) = ?
        """,
        (ano, mes),
    ).fetchone()

    conn.close()

    # Se não houver dados ainda, retorna tudo zerado
    if not row or row["max_temp"] is None:
        return jsonify({"max_temp": 0, "max_vento": 0, "max_chuva": 0})

    return jsonify(
        {
            "max_temp": row["max_temp"],
            "max_vento": row["max_vento"],
            "max_chuva": row["max_chuva"],
        }
    )


# ================= HISTÓRICO MENSAL PROFISSIONAL =================


# ================= HISTÓRICO MENSAL PROFISSIONAL =================


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

    # Cria listas vazias para todos os dias do mês para TODOS os dados
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
    min_temp = 99.0
    max_vento = 0.0

    conn = database.get_db()
    conn.row_factory = sqlite3.Row

    # Busca TODOS os dados da tabela historico_diario
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

        chuva_dia = row["chuva_total"] or 0.0
        t_max = row["temp_max"] or 0.0
        t_min = row["temp_min"] or 0.0
        t_med = row["temp_media"] or 0.0
        u_min = row["umidade_min"] or 0.0
        u_max = row["umidade_max"] or 0.0
        vento_dia = row["vento_rajada_max"] or 0.0
        uv_dia = row["uv_max"] or 0.0
        p_min = row["pressao_min"] or 0.0
        p_max = row["pressao_max"] or 0.0

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
        if t_min > 0 and t_min < min_temp:
            min_temp = t_min
        if vento_dia > max_vento:
            max_vento = vento_dia

    if min_temp == 99.0:
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
    # Pega apenas os anos de forma única (sem repetir) e em ordem decrescente
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

    # Se o banco for novo e estiver vazio, mostra pelo menos o ano atual
    import datetime

    ano_atual = str(datetime.datetime.now().year)

    if not anos:
        anos = [ano_atual]
    elif ano_atual not in anos:
        anos.insert(0, ano_atual)  # Garante que o ano atual sempre esteja na lista

    return jsonify(anos)
