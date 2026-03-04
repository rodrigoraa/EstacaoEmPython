import requests

PUBLIC_SLUG = "a535a0b6ff603c1d2376abc99e689f2f"

URL = f"https://lightning.ambientweather.net/devices?public.slug={PUBLIC_SLUG}"

HEADERS = {
    "Origin": "https://ambientweather.net",
    "User-Agent": "Mozilla/5.0"
}


def f_to_c(f):
    return round((f - 32) * 5 / 9, 1)


def mph_to_kmh(mph):
    return round(mph * 1.60934, 1)


def in_to_mm(i):
    return round(i * 25.4, 1)


def obter_dados():

    resposta = requests.get(URL, headers=HEADERS, timeout=20)

    resposta.raise_for_status()

    dados = resposta.json()

    if not dados.get("data"):
        return None

    raw = dados["data"][0].get("lastData", dados["data"][0])

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

    return (
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
        chuva_hoje
    )