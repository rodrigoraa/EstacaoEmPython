import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def env_str(nome, padrao=None):
    valor = os.environ.get(nome)
    if valor is None:
        return padrao
    return valor.strip()


def env_bool(nome, padrao=False):
    valor = env_str(nome)
    if valor is None or valor == "":
        return bool(padrao)
    return valor.lower() in {"1", "true", "yes", "sim", "on"}


def env_int(nome, padrao):
    try:
        return int(env_str(nome, str(padrao)))
    except (TypeError, ValueError):
        return int(padrao)


def env_float(nome, padrao):
    try:
        return float(env_str(nome, str(padrao)))
    except (TypeError, ValueError):
        return float(padrao)


def radar_config():
    """Retorna a configuracao do radar sem exigir a chave na aplicacao web."""
    base_dir = Path(__file__).resolve().parent
    data_dir = env_str("RADAR_DATA_DIR") or str(base_dir / "data" / "radar")
    return {
        "api_key": env_str("REDEMET_API_KEY", "") or "",
        "enabled": env_bool("RADAR_ENABLED", False),
        "area": env_str("RADAR_AREA", "jr") or "jr",
        "product": env_str("RADAR_PRODUCT", "maxcappi") or "maxcappi",
        "anima": max(1, env_int("RADAR_ANIMA", 15)),
        "target_lat": env_float("RADAR_TARGET_LAT", -22.4925326),
        "target_lon": env_float("RADAR_TARGET_LON", -54.4610352),
        "poll_seconds": max(30, env_int("RADAR_POLL_SECONDS", 300)),
        "request_timeout_seconds": max(
            1, env_int("RADAR_REQUEST_TIMEOUT_SECONDS", 30)
        ),
        "min_cluster_pixels": max(1, env_int("RADAR_MIN_CLUSTER_PIXELS", 100)),
        "morph_close_iterations": max(
            0, env_int("RADAR_MORPH_CLOSE_ITERATIONS", 2)
        ),
        "dilate_iterations": max(0, env_int("RADAR_DILATE_ITERATIONS", 1)),
        "clutter_radius_km": max(0.0, env_float("RADAR_CLUTTER_RADIUS_KM", 50)),
        "track_min_frames": max(2, env_int("RADAR_TRACK_MIN_FRAMES", 3)),
        "track_min_duration_minutes": max(
            1, env_int("RADAR_TRACK_MIN_DURATION_MINUTES", 10)
        ),
        "track_max_speed_kmh": max(
            1.0, env_float("RADAR_TRACK_MAX_SPEED_KMH", 150)
        ),
        "track_max_size_ratio": max(
            1.1, env_float("RADAR_TRACK_MAX_SIZE_RATIO", 4)
        ),
        "track_max_direction_change_deg": min(
            180.0,
            max(15.0, env_float("RADAR_TRACK_MAX_DIRECTION_CHANGE_DEG", 90)),
        ),
        "track_prediction_weight": min(
            1.0, max(0.0, env_float("RADAR_TRACK_PREDICTION_WEIGHT", 0.65))
        ),
        "track_timeout_minutes": max(
            30, env_int("RADAR_TRACK_TIMEOUT_MINUTES", 180)
        ),
        "intercept_radius_km": max(
            1.0, env_float("RADAR_INTERCEPT_RADIUS_KM", 25)
        ),
        "stale_minutes": max(1, env_int("RADAR_STALE_MINUTES", 45)),
        "max_future_minutes": max(
            0, env_int("RADAR_MAX_FUTURE_MINUTES", 30)
        ),
        "data_dir": str(Path(data_dir).expanduser().resolve()),
        "alerts_enabled": env_bool("RADAR_ALERTS_ENABLED", False),
        "retention_enabled": env_bool("RADAR_RETENCAO_AUTOMATICA", False),
        "retention_images_days": max(
            1, env_int("RADAR_RETENCAO_IMAGENS_DIAS", 7)
        ),
        "retention_frames_days": max(
            1, env_int("RADAR_RETENCAO_FRAMES_DIAS", 30)
        ),
    }


def regional_stations_config():
    """Configuracao da rede regional; nao interfere na estacao local."""
    return {
        "enabled": env_bool("REGIONAL_STATIONS_ENABLED", False),
        "poll_seconds": max(30, env_int("REGIONAL_STATIONS_POLL_SECONDS", 300)),
        "timeout_seconds": max(
            1, env_int("REGIONAL_STATIONS_TIMEOUT_SECONDS", 30)
        ),
        "bootstrap_hours": min(
            168, max(6, env_int("REGIONAL_STATIONS_BOOTSTRAP_HOURS", 24))
        ),
        "layer2_max_age_hours": max(
            1, env_int("REGIONAL_LAYER2_MAX_AGE_HOURS", 12)
        ),
        "layer2_poll_seconds": max(
            60, env_int("REGIONAL_LAYER2_POLL_SECONDS", 3600)
        ),
        "stale_minutes": max(
            60, env_int("REGIONAL_STATION_STALE_MINUTES", 120)
        ),
        "very_stale_minutes": max(
            120, env_int("REGIONAL_STATION_VERY_STALE_MINUTES", 240)
        ),
        "stagnant_minutes": max(
            60, env_int("REGIONAL_STATION_STAGNANT_MINUTES", 180)
        ),
        "alerts_enabled": env_bool("REGIONAL_STATIONS_ALERTS_ENABLED", False),
        "target_lat": env_float("REGIONAL_TARGET_LAT", -22.4925326),
        "target_lon": env_float("REGIONAL_TARGET_LON", -54.4610352),
        "retention_enabled": env_bool(
            "REGIONAL_STATIONS_RETENTION_ENABLED", False
        ),
        "retention_days": max(
            30, env_int("REGIONAL_STATIONS_RETENTION_DAYS", 730)
        ),
    }


def nowcasting_config():
    """Fusao observacional, sempre separada dos coletores e alertas."""
    return {
        "enabled": env_bool("NOWCASTING_ENABLED", False),
        "poll_seconds": max(60, env_int("NOWCASTING_POLL_SECONDS", 300)),
        "alerts_enabled": env_bool("NOWCASTING_ALERTS_ENABLED", False),
        "upstream_corridor_km": max(
            5.0, env_float("NOWCASTING_UPSTREAM_CORRIDOR_KM", 50)
        ),
        "radar_max_age_minutes": max(
            5, env_int("NOWCASTING_RADAR_MAX_AGE_MINUTES", 45)
        ),
        "regional_max_age_minutes": max(
            30, env_int("NOWCASTING_REGIONAL_MAX_AGE_MINUTES", 180)
        ),
        "regional_confirm_min_signals": max(
            1, env_int("NOWCASTING_REGIONAL_CONFIRM_MIN_SIGNALS", 2)
        ),
        "regional_confirm_min_stations": max(
            1, env_int("NOWCASTING_REGIONAL_CONFIRM_MIN_STATIONS", 1)
        ),
        "local_max_age_minutes": max(
            5, env_int("HEALTH_MAX_READING_AGE_SECONDS", 300) // 60
        ),
        "algorithm_version": env_str("NOWCASTING_ALGORITHM_VERSION", "1.2") or "1.2",
        "target_lat": env_float("RADAR_TARGET_LAT", -22.4925326),
        "target_lon": env_float("RADAR_TARGET_LON", -54.4610352),
        "track_min_frames": max(2, env_int("RADAR_TRACK_MIN_FRAMES", 3)),
    }


def public_base_url():
    configurado = env_str("PUBLIC_BASE_URL")
    if env_str("APP_ENV", "development").lower() == "production" and not configurado:
        raise RuntimeError("PUBLIC_BASE_URL não configurada em produção")

    valor = (configurado or "http://meteo.eesjv.com.br").rstrip("/")
    partes = urlsplit(valor)
    if partes.scheme not in {"http", "https"} or not partes.netloc:
        raise RuntimeError("PUBLIC_BASE_URL deve ser uma URL http(s) absoluta")
    return urlunsplit((partes.scheme, partes.netloc, partes.path.rstrip("/"), "", ""))


def public_url(caminho):
    return f"{public_base_url()}/{str(caminho).lstrip('/')}"


def validar_configuracao_web():
    if env_str("APP_ENV", "development").lower() != "production":
        return

    faltantes = []
    if not env_str("SECRET_KEY"):
        faltantes.append("SECRET_KEY")
    if not (env_str("ADMIN_PASSWORD") or env_str("ADMIN_PASSWORD_HASH")):
        faltantes.append("ADMIN_PASSWORD ou ADMIN_PASSWORD_HASH")
    if not env_str("WEBHOOK_SECRET"):
        faltantes.append("WEBHOOK_SECRET")
    if not env_str("PUBLIC_BASE_URL"):
        faltantes.append("PUBLIC_BASE_URL")
    if faltantes:
        raise RuntimeError("Configuracao de producao incompleta: " + ", ".join(faltantes))
