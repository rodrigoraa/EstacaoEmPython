"""Regras transparentes de fusao observacional de curtissimo prazo."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from time_utils import agora_utc, iso_local, iso_utc


NOWCASTING_ALGORITHM_VERSION = "1.1"
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


def analisar_ameaca(track, cluster, regional, config, radar_fresh=True):
    """Avalia uma célula isoladamente; scores de tracks diferentes nunca se somam."""
    track = track or {}
    cluster = cluster or {}
    evidence = []
    score = 0
    distance = cluster.get("distancia_borda_escola_km")
    track_valid = bool(
        radar_fresh
        and track.get("quantidade_frames", 0) >= config["track_min_frames"]
        and track.get("velocidade_kmh") is not None
        and track.get("bearing_movimento") is not None
    )

    if radar_fresh and cluster:
        score += 8
        evidence.append(
            "Eco de radar a "
            f"{distance:.1f} km da escola" if distance is not None else "Eco de radar identificado"
        )
    if track_valid:
        score += 12
        evidence.append("Tracking válido em múltiplos frames")
    if track_valid and track.get("aproximando"):
        score += 12
        evidence.append("Eco de radar em aproximação")
    if track_valid and track.get("trajetoria_compativel"):
        score += 15
        evidence.append("Trajetória compatível com a região da escola")
    if track_valid and track.get("aproximando") and distance is not None:
        score += 8 if distance <= 50 else 5 if distance <= 100 else 2 if distance <= 150 else 0

    relevant = []
    regional_score = 0
    regional_signal_count = 0
    stations_with_signals = []
    if track_valid:
        for station in regional.get("stations", []):
            geometry = classificar_estacao_montante(
                track.get("centro_lat"), track.get("centro_lon"),
                track.get("bearing_movimento"), station.get("latitude"),
                station.get("longitude"), config["upstream_corridor_km"],
                target_lat=config.get("target_lat"), target_lon=config.get("target_lon"),
            )
            if not geometry["upstream"]:
                continue
            station_score, station_evidence = _evidencias_estacao(
                station, config["regional_max_age_minutes"]
            )
            if station_evidence:
                stations_with_signals.append(station.get("code"))
                regional_signal_count += len(station_evidence)
            regional_score += station_score
            evidence.extend(station_evidence)
            relevant.append(
                {
                    "code": station.get("code"), "name": station.get("name"),
                    "distance_km": station.get("distance_km"), "upstream": True,
                    "status": station.get("status"), "age_minutes": station.get("age_minutes"),
                    "along_km": geometry["along_km"],
                    "cross_track_km": geometry["cross_track_km"],
                    "evidencias": station_evidence,
                }
            )
    score += regional_score

    structural = bool(
        track_valid and track.get("aproximando") and track.get("trajetoria_compativel")
    )
    confirmation = {
        "confirmada": bool(
            structural
            and regional_signal_count >= config.get("regional_confirm_min_signals", 2)
            and len(set(stations_with_signals)) >= config.get("regional_confirm_min_stations", 1)
        ),
        "stations": sorted(set(stations_with_signals)),
        "evidence_count": regional_signal_count,
    }

    clutter_index = track.get("indice_persistencia_clutter")
    if clutter_index is None:
        clutter_index = cluster.get("indice_persistencia_clutter")
    if clutter_index is not None and clutter_index >= 0.75:
        score -= 20
        evidence.append("Eco com índice elevado de persistência de clutter")
    elif cluster.get("suspeito_clutter"):
        score -= 5
        evidence.append("Eco próximo ao radar marcado para diagnóstico de clutter")

    score = max(0, min(100, int(round(score))))
    if not radar_fresh:
        score = min(score, 24)
    if confirmation["confirmada"] and score >= 70:
        status = "ATENCAO_PREVENTIVA"
    elif structural and regional_signal_count:
        status = "EVIDENCIA_REGIONAL"
    elif track_valid and track.get("trajetoria_compativel"):
        status = "TRAJETORIA_RELEVANTE"
    elif track_valid and track.get("aproximando"):
        status = "SISTEMA_SE_APROXIMANDO"
    elif track_valid:
        status = "SISTEMA_EM_MOVIMENTO"
    else:
        status = "ECO_EM_MONITORAMENTO"
    eta = (
        track.get("eta_minutos")
        if structural else None
    )
    relevant.sort(
        key=lambda item: (
            0 if item["evidencias"] else 1,
            item["cross_track_km"] if item["cross_track_km"] is not None else 9999,
        )
    )
    return {
        "track_id": track.get("track_id"),
        "status": status,
        "distance_km": distance,
        "faixa_distancia": _faixa_distancia(distance),
        "approaching": track.get("aproximando") if track_valid else None,
        "trajectory_compatible": bool(track_valid and track.get("trajetoria_compativel")),
        "tracking_valid": track_valid,
        "eta_minutes": eta,
        "direction": track.get("direcao_movimento"),
        "speed_kmh": track.get("velocidade_kmh") if track_valid else None,
        "frame_count": track.get("quantidade_frames"),
        "evidence_level": _nivel_evidencia(score),
        "evidence_index": score,
        "upstream_stations": relevant,
        "evidence": evidence or ["Sem evidência observacional relevante no momento"],
        "confirmacao_regional": confirmation,
        "radar_only": regional_signal_count == 0,
        "indice_persistencia_clutter": clutter_index,
        "suspeito_clutter": bool(cluster.get("suspeito_clutter")),
        "classe_predominante": cluster.get("classe_predominante"),
        "classe_maxima": cluster.get("classe_maxima"),
    }


def _prioridade_ameaca(ameaca):
    clutter = ameaca.get("indice_persistencia_clutter")
    return (
        0 if ameaca.get("trajectory_compatible") else 1,
        0 if ameaca.get("approaching") else 1,
        0 if ameaca.get("tracking_valid") else 1,
        ameaca.get("distance_km") if ameaca.get("distance_km") is not None else 99999,
        ameaca.get("eta_minutes") if ameaca.get("eta_minutes") is not None else 99999,
        clutter if clutter is not None else 0,
        ameaca.get("track_id") or 0,
    )


def analisar_nowcasting(radar, regional, local, config, now=None):
    now = (now or agora_utc()).astimezone(timezone.utc)
    radar = radar or {}
    regional = regional or {"stations": []}
    frame = radar.get("frame") or {}
    timestamp_suspect = frame.get("timestamp_status") == "suspect"
    radar_fresh = bool(
        radar.get("disponivel") and not radar.get("stale") and not timestamp_suspect
    )
    entradas = list(radar.get("tracks_atuais") or [])
    if not entradas and radar.get("cluster_mais_proximo"):
        entradas = [{
            "track": radar.get("tracking") or {},
            "cluster": radar.get("cluster_mais_proximo") or {},
        }]
    ameacas = sorted(
        [
            analisar_ameaca(
                item.get("track"), item.get("cluster"), regional, config, radar_fresh
            )
            for item in entradas
        ],
        key=_prioridade_ameaca,
    )
    principal = ameacas[0] if ameacas else None
    local_fresh = bool(local and not local.get("stale"))
    evento_local = bool(local_fresh and (local.get("rain_rate") or 0) > 0)
    regional_usable = any(
        station.get("status") == "OK"
        and station.get("age_minutes") is not None
        and station["age_minutes"] <= config["regional_max_age_minutes"]
        for station in regional.get("stations", [])
    )

    if principal:
        status = principal["status"]
        score = principal["evidence_index"]
        level = principal["evidence_level"]
        evidence = list(principal["evidence"])
        if evento_local:
            evidence.append("Evento já observado na estação local")
        if not radar_fresh and radar.get("disponivel"):
            evidence.append(
                "Timestamp do radar suspeito; tracking e ETA ignorados"
                if timestamp_suspect else "Radar desatualizado; evidência limitada"
            )
    elif not radar_fresh and not local_fresh and not regional_usable:
        status = "DADOS_INSUFICIENTES" if radar.get("disponivel") or regional.get("stations") else "SEM_DADOS"
        score, level = 0, "SEM_EVIDENCIA"
        evidence = ["Dados observacionais insuficientes no momento"]
    else:
        status, score, level = "NORMAL", 0, "SEM_EVIDENCIA"
        evidence = ["Sem evidência observacional relevante no momento"]
        if evento_local:
            evidence.append("Evento já observado na estação local")

    confirmation = (
        principal["confirmacao_regional"]
        if principal else {"confirmada": False, "stations": [], "evidence_count": 0}
    )
    radar_publico = {
        "disponivel": bool(radar.get("disponivel")),
        "stale": radar.get("stale"),
        "timestamp_status": frame.get("timestamp_status"),
        "frame_id": frame.get("id"),
        "track_id": principal.get("track_id") if principal else None,
        "distancia_borda_km": principal.get("distance_km") if principal else None,
        "faixa_distancia": principal.get("faixa_distancia") if principal else None,
        "direcao": principal.get("direction") if principal else None,
        "velocidade_kmh": principal.get("speed_kmh") if principal else None,
        "aproximando": principal.get("approaching") if principal else None,
        "trajetoria_compativel": bool(principal and principal.get("trajectory_compatible")),
        "eta_minutos": principal.get("eta_minutes") if principal else None,
        "quantidade_frames": principal.get("frame_count") if principal else None,
        "suspeito_clutter": bool(principal and principal.get("suspeito_clutter")),
        "indice_persistencia_clutter": principal.get("indice_persistencia_clutter") if principal else None,
        "imagem_disponivel": bool(frame.get("imagem_disponivel")),
    }
    return {
        "status": status,
        "nivel_evidencia": level,
        "indice_evidencia": score,
        "radar": radar_publico,
        "ameaca_principal": principal,
        "ameacas": ameacas,
        "confirmacao_regional": confirmation,
        "radar_only": bool(principal and principal.get("radar_only")),
        "estacoes_relevantes": principal.get("upstream_stations", []) if principal else [],
        "escola": local,
        "evento_local_observado": evento_local,
        "evidencias": evidence,
        "gerado_em": iso_local(now),
        "gerado_em_utc": iso_utc(now),
        "versao_algoritmo": config.get("algorithm_version", NOWCASTING_ALGORITHM_VERSION),
    }
