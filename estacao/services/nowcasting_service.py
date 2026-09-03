"""Regras transparentes de fusao observacional de curtissimo prazo."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from time_utils import agora_utc, iso_local, iso_utc


NOWCASTING_ALGORITHM_VERSION = "1.0"
EVIDENCE_LEVELS = (
    (70, "MUITO_ELEVADA"),
    (50, "ELEVADA"),
    (30, "MODERADA"),
    (10, "BAIXA"),
    (0, "SEM_EVIDENCIA"),
)


def _xy_local(lat, lon, ref_lat, ref_lon):
    norte = (float(lat) - float(ref_lat)) * 111.195
    leste = (
        (float(lon) - float(ref_lon))
        * 111.195
        * math.cos(math.radians(float(ref_lat)))
    )
    return leste, norte


def classificar_estacao_montante(
    track_lat,
    track_lon,
    movement_bearing_deg,
    station_lat,
    station_lon,
    corridor_km=50.0,
    max_upstream_km=300.0,
    target_lat=None,
    target_lon=None,
):
    """Projeta a estacao no eixo inverso ao movimento atual do track.

    ``along_km`` negativo significa que a estacao esta atras do eco no sentido
    do movimento. ``cross_track_km`` e a distancia perpendicular ao eixo.
    """
    if None in (
        track_lat,
        track_lon,
        movement_bearing_deg,
        station_lat,
        station_lon,
    ):
        return {"upstream": False, "along_km": None, "cross_track_km": None}
    x, y = _xy_local(station_lat, station_lon, track_lat, track_lon)
    bearing = math.radians(float(movement_bearing_deg))
    eixo_x, eixo_y = math.sin(bearing), math.cos(bearing)
    along = x * eixo_x + y * eixo_y
    cross = abs(x * eixo_y - y * eixo_x)
    toward_target = True
    if target_lat is not None and target_lon is not None:
        target_x, target_y = _xy_local(target_lat, target_lon, track_lat, track_lon)
        toward_target = target_x * eixo_x + target_y * eixo_y > 0
    upstream = (
        -float(max_upstream_km) <= along <= 10.0
        and cross <= float(corridor_km)
        and toward_target
    )
    return {
        "upstream": upstream,
        "along_km": round(along, 1),
        "cross_track_km": round(cross, 1),
        "movement_toward_target": toward_target,
    }


def _evidencias_estacao(station, regional_max_age_minutes):
    if station.get("status") != "OK":
        return 0, []
    age = station.get("age_minutes")
    if age is None or age > regional_max_age_minutes:
        return 0, []
    trend = station.get("trend") or {}
    score = 0
    evidence = []
    name = station.get("name") or station.get("code") or "Estacao regional"

    rain_1h = trend.get("rain_1h")
    if rain_1h is not None and rain_1h >= 0.2:
        score += 12 if rain_1h < 5 else 16
        evidence.append(f"Chuva observada em {name} na ultima hora")
    gust_delta = trend.get("gust_1h")
    if gust_delta is not None and gust_delta >= 15:
        score += 10
        evidence.append(f"Aumento importante de rajada observado em {name}")
    wind_delta = trend.get("wind_1h")
    if wind_delta is not None and wind_delta >= 10:
        score += 6
        evidence.append(f"Vento de superficie aumentou em {name}")
    temperature_delta = trend.get("temperature_1h")
    if temperature_delta is not None and temperature_delta <= -2:
        score += 8
        evidence.append(f"Queda de temperatura observada em {name}")
    humidity_delta = trend.get("humidity_1h")
    if humidity_delta is not None and humidity_delta >= 10:
        score += 5
        evidence.append(f"Aumento de umidade observado em {name}")
    pressure_delta = trend.get("pressure_1h")
    if pressure_delta is not None and pressure_delta <= -1:
        score += 7
        evidence.append(f"Queda de pressao na propria estacao {name}")
    return min(25, score), evidence


def _nivel_evidencia(score):
    for minimum, level in EVIDENCE_LEVELS:
        if score >= minimum:
            return level
    return "SEM_EVIDENCIA"


def _faixa_distancia(distance):
    if distance is None:
        return None
    if distance > 150:
        return "LONGE"
    if distance > 100:
        return "MONITORAMENTO"
    if distance > 50:
        return "APROXIMANDO"
    return "PROXIMO"


def analisar_nowcasting(radar, regional, local, config, now=None):
    now = (now or agora_utc()).astimezone(timezone.utc)
    radar = radar or {}
    regional = regional or {"stations": []}
    local = local or None
    frame = radar.get("frame") or {}
    cluster = radar.get("cluster_mais_proximo") or {}
    track = radar.get("tracking") or {}
    radar_fresh = bool(radar.get("disponivel") and not radar.get("stale"))
    track_valid = bool(
        radar_fresh
        and track.get("quantidade_frames", 0) >= config["track_min_frames"]
        and track.get("velocidade_kmh") is not None
        and track.get("bearing_movimento") is not None
    )
    evidence = []
    score = 0

    if radar_fresh and cluster:
        score += 10
        evidence.append(
            "Eco significativo a "
            f"{cluster.get('distancia_borda_escola_km', 0):.1f} km da escola"
        )
    if track_valid and track.get("trajetoria_compativel"):
        score += 18
        evidence.append("Movimento do eco confirmado por multiplos frames")
    if track_valid and track.get("aproximando"):
        score += 14
        evidence.append("Eco significativo em aproximacao")
    if track_valid and track.get("trajetoria_compativel"):
        score += 20
        evidence.append("Trajetoria compativel com a regiao da escola")
    distance = cluster.get("distancia_borda_escola_km")
    if track_valid and track.get("aproximando") and distance is not None:
        score += 8 if distance <= 50 else 5 if distance <= 100 else 2

    relevant = []
    if track_valid:
        for station in regional.get("stations", []):
            geometry = classificar_estacao_montante(
                track.get("centro_lat"),
                track.get("centro_lon"),
                track.get("bearing_movimento"),
                station.get("latitude"),
                station.get("longitude"),
                config["upstream_corridor_km"],
                target_lat=config.get("target_lat"),
                target_lon=config.get("target_lon"),
            )
            if not geometry["upstream"]:
                continue
            station_score, station_evidence = _evidencias_estacao(
                station, config["regional_max_age_minutes"]
            )
            relevant.append(
                {
                    "code": station.get("code"),
                    "name": station.get("name"),
                    "distance_km": station.get("distance_km"),
                    "upstream": True,
                    "status": station.get("status"),
                    "age_minutes": station.get("age_minutes"),
                    "along_km": geometry["along_km"],
                    "cross_track_km": geometry["cross_track_km"],
                    "evidencias": station_evidence,
                }
            )
            score += station_score
            evidence.extend(station_evidence)

    clutter_index = track.get("indice_persistencia_clutter")
    if clutter_index is None:
        clutter_index = cluster.get("indice_persistencia_clutter")
    if clutter_index is not None and clutter_index >= 0.75:
        score -= 20
        evidence.append("Eco com indice elevado de persistencia de clutter")
    elif cluster.get("suspeito_clutter"):
        score -= 5
        evidence.append("Eco proximo ao radar marcado para diagnostico de clutter")

    local_fresh = bool(local and not local.get("stale"))
    regional_usable = any(
        station.get("status") == "OK"
        and station.get("age_minutes") is not None
        and station["age_minutes"] <= config["regional_max_age_minutes"]
        for station in regional.get("stations", [])
    )
    if local_fresh and (local.get("rain_rate") or 0) > 0:
        score += 8
        evidence.append("Chuva ja observada na EE Sao Jose")
    if local_fresh and (local.get("wind_gust") or 0) >= 40:
        score += 5
        evidence.append("Rajada relevante observada na EE Sao Jose")
    if (
        track_valid
        and track.get("aproximando")
        and any(item["evidencias"] for item in relevant)
        and local_fresh
        and (local.get("rain_rate") or 0) <= 0
    ):
        evidence.append("Escola ainda sem chuva durante a aproximacao")

    score = max(0, min(100, int(round(score))))
    if not radar_fresh:
        score = min(score, 24)
        if radar.get("disponivel"):
            evidence.append("Radar desatualizado; evidencia limitada")
    level = _nivel_evidencia(score)

    if not radar_fresh and not local_fresh and not regional_usable:
        status = (
            "DADOS_INSUFICIENTES"
            if radar.get("disponivel") or regional.get("stations")
            else "SEM_DADOS"
        )
    elif score >= 70:
        status = "ATENCAO_PREVENTIVA"
    elif any(item["evidencias"] for item in relevant) and track_valid:
        status = "EVIDENCIA_REGIONAL"
    elif track_valid and track.get("trajetoria_compativel"):
        status = "TRAJETORIA_RELEVANTE"
    elif track_valid and track.get("aproximando"):
        status = "SISTEMA_SE_APROXIMANDO"
    elif track_valid:
        status = "SISTEMA_EM_MOVIMENTO"
    elif radar_fresh and cluster:
        status = "ECO_EM_MONITORAMENTO"
    else:
        status = "NORMAL"

    eta = (
        track.get("eta_minutos")
        if track_valid
        and track.get("aproximando")
        and track.get("trajetoria_compativel")
        else None
    )
    return {
        "status": status,
        "nivel_evidencia": level,
        "indice_evidencia": score,
        "radar": {
            "disponivel": bool(radar.get("disponivel")),
            "stale": radar.get("stale"),
            "frame_id": frame.get("id"),
            "track_id": track.get("track_id"),
            "distancia_borda_km": distance,
            "faixa_distancia": _faixa_distancia(distance),
            "direcao": track.get("direcao_movimento"),
            "velocidade_kmh": track.get("velocidade_kmh") if track_valid else None,
            "aproximando": track.get("aproximando") if track_valid else None,
            "trajetoria_compativel": bool(
                track_valid and track.get("trajetoria_compativel")
            ),
            "eta_minutos": eta,
            "quantidade_frames": track.get("quantidade_frames"),
            "suspeito_clutter": bool(cluster.get("suspeito_clutter")),
            "indice_persistencia_clutter": clutter_index,
            "imagem_disponivel": bool(frame.get("imagem_disponivel")),
        },
        "estacoes_relevantes": sorted(
            relevant,
            key=lambda item: (
                0 if item["evidencias"] else 1,
                item["cross_track_km"] if item["cross_track_km"] is not None else 9999,
            ),
        ),
        "escola": local,
        "evidencias": evidence or ["Sem evidencia observacional relevante no momento"],
        "gerado_em": iso_local(now),
        "gerado_em_utc": iso_utc(now),
        "versao_algoritmo": config.get(
            "algorithm_version", NOWCASTING_ALGORITHM_VERSION
        ),
    }
