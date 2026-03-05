from flask import Blueprint, jsonify
import database
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

api_routes = Blueprint("api", __name__)


def f_to_c(f):
    return round((f - 32) * 5 / 9, 1)


def mph_to_kmh(mph):
    return round(mph * 1.60934, 1)


def in_to_mm(i):
    return round(i * 25.4, 1)


@api_routes.route("/api/clima")
def api_clima():

    try:

        PUBLIC_SLUG = "a535a0b6ff603c1d2376abc99e689f2f"

        url = f"https://lightning.ambientweather.net/devices?public.slug={PUBLIC_SLUG}"

        headers = {"Origin": "https://ambientweather.net", "User-Agent": "Mozilla/5.0"}

        resposta = requests.get(url, headers=headers, timeout=10)
        resposta.raise_for_status()

        dados_api = resposta.json()

        if not dados_api.get("data"):
            return jsonify({"erro": "Sem dados"})

        raw = dados_api["data"][0]["lastData"]
        info = dados_api["data"][0].get("info", {})

        # ================= CONVERSÕES =================

        temp = f_to_c(raw.get("tempf", 32))
        sensacao = f_to_c(raw.get("feelsLike", raw.get("tempf", 32)))
        umidade = raw.get("humidity", 0)

        pressao = round(raw.get("baromrelin", 0) * 33.8639, 1)

        uv = raw.get("uv", 0)
        radiacao = raw.get("solarradiation", 0)

        vento = mph_to_kmh(raw.get("windspeedmph", 0))
        rajada = mph_to_kmh(raw.get("windgustmph", 0))
        vento_dir = raw.get("winddir", 0)

        chuva_rate = in_to_mm(raw.get("hourlyrainin", 0))
        chuva_evento = in_to_mm(raw.get("eventrainin", 0))
        chuva_hoje = in_to_mm(raw.get("dailyrainin", 0))

        # ================= HORA DA ESTAÇÃO =================

        timestamp = raw.get("dateutc")

        if timestamp:
            dt = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            dt = dt.astimezone(ZoneInfo("America/Campo_Grande"))
            hora_leitura = dt.strftime("%H:%M:%S")
        else:
            hora_leitura = "--:--:--"

        # ================= RESPOSTA =================

        dados = {
            "local": info.get("name", "EE São José"),
            "temp": temp,
            "sensacao": sensacao,
            "umidade": umidade,
            "pressao": pressao,
            "uv": uv,
            "radiacao": radiacao,
            "vento_atual": vento,
            "vento_rajada": rajada,
            "vento_dir": vento_dir,
            "chuva_rate": chuva_rate,
            "chuva_evento": chuva_evento,
            "chuva_hoje": chuva_hoje,
            "hora_leitura": hora_leitura,
        }

        return jsonify(dados)

    except Exception as e:
        return jsonify({"erro": str(e)})


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
