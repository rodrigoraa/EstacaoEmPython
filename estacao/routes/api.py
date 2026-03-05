from flask import Blueprint, jsonify
import database

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
