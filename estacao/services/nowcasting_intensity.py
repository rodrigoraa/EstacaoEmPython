"""Intensidade relativa dos ecos, sem conversão para dBZ ou precipitação."""

from config import percentual_positivo_valido


CAMPOS_PIXELS = tuple(
    f"pixels_refletividade_{classe}"
    for classe in ("baixa", "media", "alta", "muito_alta")
)
CAMPOS_INTENSIDADE = (
    "classe_predominante", "classe_maxima", *CAMPOS_PIXELS,
    "total_pixels_refletividade", "percentual_refletividade_alta",
    "percentual_refletividade_forte", "percentual_refletividade_muito_alta",
    "intensidade_suficiente",
)


def analisar_intensidade_cluster(cluster, config=None):
    """Calcula percentuais em 0–100; dados incompletos não habilitam envio.

    Exige mais de um pixel forte, além dos percentuais, para que um pixel
    isolado nunca baste mesmo em um cluster pequeno. Não usa classe_maxima
    como substituta das contagens. Limiares inválidos voltam a 10% e 2%.
    """
    cluster = cluster or {}
    config = config or {}
    contagens = {}
    dados_validos = True
    for campo in CAMPOS_PIXELS:
        valor = cluster.get(campo)
        # Contagens produzidas pelo radar/SQLite são inteiros não negativos.
        if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
            dados_validos = False
            valor = None
        contagens[campo] = valor

    baixa, media, alta, muito_alta = (contagens[campo] or 0 for campo in CAMPOS_PIXELS)
    total = baixa + media + alta + muito_alta
    percentual_alta = alta / total * 100 if total else 0.0
    percentual_muito_alta = muito_alta / total * 100 if total else 0.0
    percentual_forte = (alta + muito_alta) / total * 100 if total else 0.0
    minimo_forte = percentual_positivo_valido(
        config.get("alert_min_strong_reflectivity_percent"), 10
    )
    minimo_muito_alta = percentual_positivo_valido(
        config.get("alert_min_very_high_reflectivity_percent"), 2
    )
    return {
        "classe_predominante": cluster.get("classe_predominante"),
        "classe_maxima": cluster.get("classe_maxima"),
        **contagens,
        "total_pixels_refletividade": total,
        "percentual_refletividade_alta": percentual_alta,
        "percentual_refletividade_forte": percentual_forte,
        "percentual_refletividade_muito_alta": percentual_muito_alta,
        "intensidade_suficiente": bool(
            dados_validos and alta + muito_alta > 1
            and (percentual_muito_alta >= minimo_muito_alta
                 or percentual_forte >= minimo_forte)
        ),
    }
