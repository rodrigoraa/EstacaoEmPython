import requests
from datetime import datetime

PUBLIC_SLUG = "a535a0b6ff603c1d2376abc99e689f2f"

URL = f"https://lightning.ambientweather.net/devices?public.slug={PUBLIC_SLUG}"

HEADERS = {"Origin": "https://ambientweather.net", "User-Agent": "Mozilla/5.0"}
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def f_to_c(f):
    return round((f - 32) * 5 / 9, 1)


def mph_to_kmh(mph):
    return round(mph * 1.60934, 1)


def in_to_mm(i):
    return round(i * 25.4, 1)


def obter_dados():

    try:
        resposta = requests.get(URL, headers=HEADERS, timeout=20)
        resposta.raise_for_status()
    except Exception:
        return None

    dados = resposta.json()

    if not dados.get("data"):
        return None

    raw = dados["data"][0].get("lastData", dados["data"][0])

    timestamp = raw.get("dateutc")

    if timestamp:
        import time

        agora = int(time.time() * 1000)

        if agora - timestamp > 600000:
            return None

    temp = f_to_c(raw.get("tempf", 32))
    sensacao = f_to_c(raw.get("feelsLike", raw.get("tempf", 32)))
    umidade = raw.get("humidity", 0)

    pressao = round(raw.get("baromrelin", 0) * 33.8639, 1)

    uv = raw.get("uv", 0)
    radiacao = raw.get("solarradiation", 0)

    vento = mph_to_kmh(raw.get("windspeedmph", 0))
    rajada = mph_to_kmh(raw.get("windgustmph", 0))

    # --- MUDANÇA AQUI: Lê a rajada máxima do dia direto do aparelho ---
    # (Se por acaso o aparelho não mandar o maxdailygust, ele usa a rajada atual para não dar erro)
    rajada_max = mph_to_kmh(raw.get("maxdailygust", raw.get("windgustmph", 0)))

    vento_dir = raw.get("winddir", 0)

    chuva_rate = in_to_mm(raw.get("hourlyrainin", 0))
    chuva_evento = in_to_mm(raw.get("eventrainin", 0))
    chuva_hoje = in_to_mm(raw.get("dailyrainin", 0))

    return {
        "temp": temp,
        "sensacao": sensacao,
        "umidade": umidade,
        "pressao": pressao,
        "uv": uv,
        "radiacao": radiacao,
        "vento": vento,
        "rajada": rajada,
        "rajada_max": rajada_max,  # <--- MUDANÇA AQUI: Envia para o updater
        "vento_dir": vento_dir,
        "chuva_rate": chuva_rate,
        "chuva_evento": chuva_evento,
        "chuva_hoje": chuva_hoje,
    }


def descricao_weather_code(code):
    descricoes = {
        0: "Ceu limpo",
        1: "Principalmente limpo",
        2: "Parcialmente nublado",
        3: "Nublado",
        45: "Neblina",
        48: "Neblina com geada",
        51: "Garoa fraca",
        53: "Garoa moderada",
        55: "Garoa intensa",
        61: "Chuva fraca",
        63: "Chuva moderada",
        65: "Chuva forte",
        71: "Neve fraca",
        73: "Neve moderada",
        75: "Neve forte",
        80: "Pancadas fracas",
        81: "Pancadas moderadas",
        82: "Pancadas fortes",
        95: "Trovoadas",
        96: "Trovoadas com granizo fraco",
        99: "Trovoadas com granizo forte",
    }
    return descricoes.get(code, "Condicao variavel")


def formatar_data_semana(data_iso):
    dias = [
        "Segunda-feira",
        "Terca-feira",
        "Quarta-feira",
        "Quinta-feira",
        "Sexta-feira",
        "Sabado",
        "Domingo",
    ]
    data_obj = datetime.strptime(data_iso, "%Y-%m-%d")
    return {
        "data_formatada": data_obj.strftime("%d/%m/%Y"),
        "dia_semana": dias[data_obj.weekday()],
    }


def obter_previsao(
    cidade="Vicentina",
    estado="MS",
    pais="Brasil",
    latitude=None,
    longitude=None,
    nome_exibicao=None,
):
    try:
        local = None

        if latitude is None or longitude is None:
            geo = requests.get(
                GEOCODING_URL,
                params={"name": cidade, "count": 10, "language": "pt", "format": "json"},
                timeout=20,
            )
            geo.raise_for_status()
            geo_data = geo.json()
            resultados = geo_data.get("results") or []

            if not resultados:
                return None

            estado_normalizado = (estado or "").strip().lower()
            pais_normalizado = (pais or "").strip().lower()

            def score_resultado(item):
                score = 0
                nome = (item.get("name") or "").strip().lower()
                admin1 = (item.get("admin1") or "").strip().lower()
                country = (item.get("country") or "").strip().lower()

                if nome == cidade.strip().lower():
                    score += 4
                if estado_normalizado and (
                    admin1 == estado_normalizado
                    or admin1.startswith(estado_normalizado)
                    or estado_normalizado.startswith(admin1)
                ):
                    score += 3
                if pais_normalizado and country == pais_normalizado:
                    score += 2
                return score

            local = max(resultados, key=score_resultado)
            latitude = local["latitude"]
            longitude = local["longitude"]

        forecast = requests.get(
            FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "timezone": "auto",
                "forecast_days": 16,
                "current": [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "apparent_temperature",
                    "weather_code",
                    "wind_speed_10m",
                ],
                "daily": [
                    "weather_code",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_probability_max",
                    "precipitation_sum",
                    "wind_speed_10m_max",
                ],
            },
            timeout=20,
        )
        forecast.raise_for_status()
        data = forecast.json()

        current = data.get("current", {})
        daily = data.get("daily", {})
        dias = []

        for i, data_iso in enumerate(daily.get("time", [])):
            data_info = formatar_data_semana(data_iso)
            dias.append(
                {
                    "data": data_iso,
                    "data_formatada": data_info["data_formatada"],
                    "dia_semana": data_info["dia_semana"],
                    "descricao": descricao_weather_code(
                        daily.get("weather_code", [None])[i]
                    ),
                    "temp_max": round(daily.get("temperature_2m_max", [0])[i], 1),
                    "temp_min": round(daily.get("temperature_2m_min", [0])[i], 1),
                    "chuva_prob": round(
                        daily.get("precipitation_probability_max", [0])[i], 1
                    ),
                    "chuva_total": round(daily.get("precipitation_sum", [0])[i], 1),
                    "vento_max": round(daily.get("wind_speed_10m_max", [0])[i], 1),
                }
            )

        return {
            "cidade": nome_exibicao or (local.get("name", cidade) if local else cidade),
            "estado": local.get("admin1", estado) if local else estado,
            "pais": local.get("country", pais) if local else pais,
            "latitude": latitude,
            "longitude": longitude,
            "atual": {
                "temperatura": round(current.get("temperature_2m", 0), 1),
                "sensacao": round(current.get("apparent_temperature", 0), 1),
                "umidade": round(current.get("relative_humidity_2m", 0), 1),
                "vento": round(current.get("wind_speed_10m", 0), 1),
                "descricao": descricao_weather_code(current.get("weather_code")),
            },
            "dias": dias,
        }
    except Exception:
        return None
