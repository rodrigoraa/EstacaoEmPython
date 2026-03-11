from flask import Blueprint, jsonify, request
import database
import calendar

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
            "vento_rajada": row["vento_rajada"],
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

    # Descobre quantos dias tem no mês solicitado (Ex: Fevereiro de 2024 = 29 dias)
    _, num_dias = calendar.monthrange(ano_int, mes_int)

    # Cria listas vazias para todos os dias do mês
    dias_lista = list(range(1, num_dias + 1))
    chuva_lista = [0.0] * num_dias
    temp_lista = [0.0] * num_dias

    # Variáveis para os cards de resumo
    total_chuva = 0.0
    max_temp = 0.0
    max_vento = 0.0

    # Conecta no banco (ajuste 'database.get_db()' para a forma que você conecta aí, caso seja diferente)
    conn = database.get_db()

    # Busca na tabela diária! Muito mais rápido e leve!
    linhas = conn.execute(
        """
        SELECT 
            strftime('%d', data) as dia,
            chuva_total,
            temp_max,
            vento_rajada_max
        FROM historico_diario
        WHERE strftime('%Y', data) = ? AND strftime('%m', data) = ?
        """,
        (ano, mes),
    ).fetchall()

    conn.close()

    # Povoa as listas zeradas com os dados reais que encontramos no banco
    for row in linhas:
        # Pega o dia e converte para índice (Dia 1 fica no índice 0 da lista)
        dia_idx = int(row["dia"]) - 1

        # Se for NULL no banco, consideramos 0.0
        chuva_dia = row["chuva_total"] or 0.0
        temp_dia = row["temp_max"] or 0.0
        vento_dia = row["vento_rajada_max"] or 0.0

        # Coloca o valor exato na posição correta do gráfico
        chuva_lista[dia_idx] = chuva_dia
        temp_lista[dia_idx] = temp_dia

        # Soma e calcula as máximas para os cards
        total_chuva += chuva_dia
        if temp_dia > max_temp:
            max_temp = temp_dia
        if vento_dia > max_vento:
            max_vento = vento_dia

    # Retorna o pacote completo e organizado para o JavaScript montar a tela
    return jsonify(
        {
            "dias": dias_lista,
            "chuva": chuva_lista,
            "temperatura": temp_lista,
            "total_chuva": total_chuva,
            "max_temp": max_temp,
            "max_vento": max_vento,
        }
    )
