import requests
from datetime import datetime, timezone

BASE = (
    "https://www.pinms.ms.gov.br/arcgis/rest/services/"
    "publico/Estacoes_CEMADEN_INMET/MapServer"
)

ESTACOES = {
    "A721": "Dourados",
    "S706": "Caarapó",
    "A749": "Juti",
    "S735": "Naviraí",
    "A709": "Ivinhema",
    "S708": "Culturama",
}


def converter_timestamp(valor):
    if valor is None:
        return "None"

    try:
        return datetime.fromtimestamp(
            float(valor) / 1000,
            tz=timezone.utc
        ).isoformat()
    except Exception:
        return f"ERRO AO CONVERTER: {valor}"


def consultar(layer, codigo, ordem=None, quantidade=8):
    url = f"{BASE}/{layer}/query"

    params = {
        "where": f"CD_ESTACAO='{codigo}'",
        "outFields": "*",
        "returnGeometry": "false",
        "f": "json",
        "resultRecordCount": quantidade,
    }

    if ordem:
        params["orderByFields"] = ordem

    response = requests.get(url, params=params, timeout=30)

    print("URL:", response.url)
    print("HTTP:", response.status_code)

    response.raise_for_status()

    dados = response.json()

    if "error" in dados:
        print("ERRO ARCGIS:")
        print(dados["error"])
        return []

    return dados.get("features", [])


def imprimir(features):
    for i, feature in enumerate(features, start=1):
        a = feature.get("attributes", {})

        raw = a.get("DT_MEDICAO")

        print()
        print(f"REGISTRO {i}")
        print("OBJECTID      :", a.get("OBJECTID"))
        print("CD_ESTACAO    :", a.get("CD_ESTACAO"))
        print("DC_NOME       :", a.get("DC_NOME"))
        print("DT_MEDICAO RAW:", raw)
        print("DT como UTC   :", converter_timestamp(raw))
        print("HR_MEDICAO    :", a.get("HR_MEDICAO"))
        print("TEM_INS       :", a.get("TEM_INS"))
        print("TEM_MIN       :", a.get("TEM_MIN"))
        print("TEM_MAX       :", a.get("TEM_MAX"))
        print("UMD_INS       :", a.get("UMD_INS"))
        print("PRE_INS       :", a.get("PRE_INS"))
        print("VEN_VEL       :", a.get("VEN_VEL"))
        print("VEN_RAJ       :", a.get("VEN_RAJ"))
        print("CHUVA         :", a.get("CHUVA"))


for codigo, nome in ESTACOES.items():

    print()
    print("=" * 80)
    print(nome, "-", codigo)
    print("=" * 80)

    print()
    print("### CAMADA 0 - TEMPO REAL ###")

    atual = consultar(
        0,
        codigo,
        quantidade=1
    )

    imprimir(atual)

    print()
    print("### CAMADA 2 - ORDENADA POR DATA/HORA ###")

    por_data = consultar(
        2,
        codigo,
        ordem="DT_MEDICAO DESC, HR_MEDICAO DESC",
        quantidade=8
    )

    imprimir(por_data)

    print()
    print("### CAMADA 2 - ORDENADA POR OBJECTID ###")

    por_objectid = consultar(
        2,
        codigo,
        ordem="OBJECTID DESC",
        quantidade=8
    )

    imprimir(por_objectid)


print()
print("=" * 80)
print("FIM DO TESTE")
print("=" * 80)