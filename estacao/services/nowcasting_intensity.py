"""Intensidade relativa dos ecos, sem conversão para dBZ ou precipitação."""

from config import numero_alerta_valido


RADAR_INTENSITY_NONE = "NONE"
RADAR_INTENSITY_LOW = "LOW"
RADAR_INTENSITY_MEDIUM = "MEDIUM"
RADAR_INTENSITY_HIGH = "HIGH"
RADAR_INTENSITY_VERY_HIGH = "VERY_HIGH"
FRONT_COUNT_FIELDS = tuple(f"front_pixels_{name}" for name in ("low", "medium", "high", "very_high"))
FRONT_FIELDS = (*FRONT_COUNT_FIELDS, "front_pixels_total", "front_percent_medium_or_higher",
                "front_percent_strong", "front_percent_very_high", "front_depth_km")


def classificar_intensidade_frente(frente, config=None):
    """Classificação única da frente original; nunca infere contagens de cores legadas."""
    frente, config = frente or {}, config or {}
    contagens = [frente.get(campo) for campo in FRONT_COUNT_FIELDS]
    valido = all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in contagens)
    baixa, media, alta, muito_alta = contagens if valido else (0, 0, 0, 0)
    total = baixa + media + alta + muito_alta
    if frente.get("front_pixels_total") not in (None, total):
        valido = False
    if frente.get("front_data_valid") is False:
        valido = False
    def percent(n):
        return n * 100 / total if total else 0.0
    intensidade = RADAR_INTENSITY_LOW if total else RADAR_INTENSITY_NONE
    if valido and total >= 2:
        for nome, quantidade, percentual, pixels, classe in (
            ("very_high", muito_alta, 2, 2, RADAR_INTENSITY_VERY_HIGH),
            ("strong", alta + muito_alta, 10, 2, RADAR_INTENSITY_HIGH),
            ("medium", media + alta + muito_alta, 10, 3, RADAR_INTENSITY_MEDIUM),
        ):
            minimo_pixels = numero_alerta_valido(config.get(f"alert_min_{nome}_reflectivity_pixels"), pixels, pixels=True)
            minimo_percent = numero_alerta_valido(config.get(f"alert_min_{nome}_reflectivity_percent"), percentual, percentual=True)
            if quantidade >= max(2, minimo_pixels) and percent(quantidade) >= minimo_percent:
                intensidade = classe
                break
    return {
        **{campo: frente.get(campo) for campo in FRONT_COUNT_FIELDS},
        "front_depth_km": frente.get("front_depth_km"),
        "front_pixels_total": total,
        "front_percent_medium_or_higher": percent(media + alta + muito_alta),
        "front_percent_strong": percent(alta + muito_alta),
        "front_percent_very_high": percent(muito_alta),
        "front_data_valid": valido,
        "radar_intensity": intensidade if valido else RADAR_INTENSITY_NONE,
        "intensidade_suficiente": valido and intensidade in {"MEDIUM", "HIGH", "VERY_HIGH"},
    }


CAMPOS_PIXELS = tuple(
    f"pixels_refletividade_{classe}"
    for classe in ("baixa", "media", "alta", "muito_alta")
)
CAMPOS_INTENSIDADE = (
    "classe_predominante", "classe_maxima", *CAMPOS_PIXELS,
    "total_pixels_refletividade", "percentual_refletividade_alta",
    "percentual_refletividade_forte", "percentual_refletividade_muito_alta",
    "intensidade_suficiente",
    *FRONT_FIELDS, "front_data_valid", "radar_intensity",
)


def analisar_intensidade_cluster(cluster, config=None):
    """Preserva estatísticas legadas do cluster; intensidade vem apenas da frente."""
    cluster = cluster or {}
    config = config or {}
    contagens = {}
    for campo in CAMPOS_PIXELS:
        valor = cluster.get(campo)
        # Contagens produzidas pelo radar/SQLite são inteiros não negativos.
        if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
            valor = None
        contagens[campo] = valor

    baixa, media, alta, muito_alta = (contagens[campo] or 0 for campo in CAMPOS_PIXELS)
    total = baixa + media + alta + muito_alta
    percentual_alta = alta / total * 100 if total else 0.0
    percentual_muito_alta = muito_alta / total * 100 if total else 0.0
    percentual_forte = (alta + muito_alta) / total * 100 if total else 0.0
    frente = classificar_intensidade_frente(cluster, config)
    return {
        "classe_predominante": cluster.get("classe_predominante"),
        "classe_maxima": cluster.get("classe_maxima"),
        **contagens,
        "total_pixels_refletividade": total,
        "percentual_refletividade_alta": percentual_alta,
        "percentual_refletividade_forte": percentual_forte,
        "percentual_refletividade_muito_alta": percentual_muito_alta,
        **frente,
    }
