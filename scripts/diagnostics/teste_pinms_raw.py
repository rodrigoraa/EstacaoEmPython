"""Diagnostico manual dos payloads brutos das camadas 0 e 2 do PIN-MS."""

from datetime import datetime, timezone

import requests


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
            float(valor) / 1000, tz=timezone.utc
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
        atributos = feature.get("attributes", {})
        raw = atributos.get("DT_MEDICAO")
        print()
        print(f"REGISTRO {i}")
        print("OBJECTID      :", atributos.get("OBJECTID"))
        print("CD_ESTACAO    :", atributos.get("CD_ESTACAO"))
        print("DC_NOME       :", atributos.get("DC_NOME"))
        print("DT_MEDICAO RAW:", raw)
        print("DT como UTC   :", converter_timestamp(raw))
        for campo in (
            "HR_MEDICAO", "TEM_INS", "TEM_MIN", "TEM_MAX", "UMD_INS",
            "PRE_INS", "VEN_VEL", "VEN_RAJ", "CHUVA",
        ):
            print(f"{campo:<14}:", atributos.get(campo))


def main():
    for codigo, nome in ESTACOES.items():
        print()
        print("=" * 80)
        print(nome, "-", codigo)
        print("=" * 80)
        consultas = (
            ("CAMADA 0 - TEMPO REAL", 0, None, 1),
            ("CAMADA 2 - ORDENADA POR DATA/HORA", 2,
             "DT_MEDICAO DESC, HR_MEDICAO DESC", 8),
            ("CAMADA 2 - ORDENADA POR OBJECTID", 2, "OBJECTID DESC", 8),
        )
        for titulo, layer, ordem, quantidade in consultas:
            print(f"\n### {titulo} ###")
            imprimir(consultar(layer, codigo, ordem=ordem, quantidade=quantidade))
    print("\n" + "=" * 80)
    print("FIM DO TESTE")
    print("=" * 80)


if __name__ == "__main__":
    main()
