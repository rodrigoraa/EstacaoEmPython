"""Persistencia idempotente e estado publico das estacoes regionais."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict

import database
from services.regional_stations_analysis import (
    bearing_graus,
    calcular_tendencias,
    classificar_status,
    direcao_cardinal,
    haversine_km,
    idade_minutos_observacao,
)
from services.regional_stations_catalog import REGIONAL_STATION_CODES
from services.regional_stations_service import RegionalObservation
from time_utils import iso_utc


OBSERVATION_COLUMNS = (
    "station_code", "source_layer", "source_dt_medicao_raw",
    "source_hr_medicao_raw", "medido_em_utc", "medido_em_local",
    "timestamp_status", "coletado_em_utc", "coletado_em_local",
    "temperatura_atual", "temperatura_min", "temperatura_max",
    "umidade_atual", "umidade_min", "umidade_max",
    "pressao_atual", "pressao_min", "pressao_max",
    "vento_direcao_graus", "vento_velocidade_raw", "vento_velocidade_ms",
    "vento_velocidade_kmh", "rajada_raw", "rajada_ms", "rajada_kmh",
    "chuva_mm", "radiacao_raw", "radiacao_unidade", "latitude", "longitude",
    "latitude_fonte", "longitude_fonte",
    "fingerprint", "payload_json", "qualidade",
)


def salvar_observacao(observacao: RegionalObservation) -> bool:
    valores = asdict(observacao)
    conn = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE regional_stations SET
                nome_fonte=COALESCE(?, nome_fonte),
                latitude_fonte=COALESCE(?, latitude_fonte),
                longitude_fonte=COALESCE(?, longitude_fonte),
                atualizado_em=CURRENT_TIMESTAMP
            WHERE codigo=?
            """,
            (
                observacao.nome_fonte,
                observacao.latitude_fonte,
                observacao.longitude_fonte,
                observacao.station_code,
            ),
        )
        marcadores = ",".join("?" for _ in OBSERVATION_COLUMNS)
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO regional_station_observations "
            f"({','.join(OBSERVATION_COLUMNS)}) VALUES ({marcadores})",
            tuple(valores[coluna] for coluna in OBSERVATION_COLUMNS),
        )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def registrar_status_estacao(code, status, erro=None, sucesso=False):
    if code not in REGIONAL_STATION_CODES:
        raise ValueError("Codigo regional fora da allowlist")
    conn = database.get_db()
    try:
        agora = iso_utc()
        conn.execute(
            """
            INSERT INTO regional_station_state (
                station_code, source_status, ultimo_erro, ultima_tentativa_em,
                ultimo_sucesso_em, atualizado_em
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(station_code) DO UPDATE SET
                source_status=excluded.source_status,
                ultimo_erro=excluded.ultimo_erro,
                ultima_tentativa_em=excluded.ultima_tentativa_em,
                ultimo_sucesso_em=COALESCE(excluded.ultimo_sucesso_em,
                                           regional_station_state.ultimo_sucesso_em),
                atualizado_em=CURRENT_TIMESTAMP
            """,
            (code, status, erro, agora, agora if sucesso else None),
        )
        conn.commit()
    finally:
        conn.close()


def _observation_publica(row):
    if not row:
        return None
    return {
        "source_layer": row["source_layer"],
        "measured_at": row["medido_em_local"],
        "collected_at": row["coletado_em_local"],
        "timestamp_status": row["timestamp_status"],
        "temperature": row["temperatura_atual"],
        "temperature_min": row["temperatura_min"],
        "temperature_max": row["temperatura_max"],
        "humidity": row["umidade_atual"],
        "pressure": row["pressao_atual"],
        "wind_speed_ms": row["vento_velocidade_ms"],
        "wind_speed_kmh": row["vento_velocidade_kmh"],
        "wind_gust_ms": row["rajada_ms"],
        "wind_gust_kmh": row["rajada_kmh"],
        "wind_direction_deg": row["vento_direcao_graus"],
        "rain_mm": row["chuva_mm"],
        "radiation": row["radiacao_raw"],
        "radiation_unit": row["radiacao_unidade"],
        "quality": row["qualidade"],
    }


def _ultima_observacao(conn, code, layer, exigir_timestamp=False):
    timestamp = (
        "AND medido_em_utc IS NOT NULL "
        "AND timestamp_status IN ('valid', 'reconciled')"
        if exigir_timestamp else ""
    )
    ordem = "medido_em_utc DESC, id DESC" if exigir_timestamp else "coletado_em_utc DESC, id DESC"
    return conn.execute(
        f"SELECT * FROM regional_station_observations "
        f"WHERE station_code=? AND source_layer=? {timestamp} ORDER BY {ordem} LIMIT 1",
        (code, layer),
    ).fetchone()


def obter_estado_rede(config):
    conn = database.get_db()
    try:
        stations = conn.execute(
            """
            SELECT s.*, st.source_status, st.ultimo_erro, st.ultima_tentativa_em,
                   st.ultimo_sucesso_em
            FROM regional_stations s
            LEFT JOIN regional_station_state st ON st.station_code=s.codigo
            WHERE s.ativo=1 ORDER BY s.nome_exibicao
            """
        ).fetchall()
        resultado = []
        updated_at = None
        for station in stations:
            code = station["codigo"]
            atual = _ultima_observacao(conn, code, 0)
            horaria = _ultima_observacao(conn, code, 2, exigir_timestamp=True)
            observacao_exibida = atual or horaria
            freshness = horaria or (atual if atual and atual["medido_em_utc"] else None)
            historico = conn.execute(
                """
                SELECT * FROM regional_station_observations
                WHERE station_code=? AND source_layer=2
                  AND medido_em_utc IS NOT NULL
                  AND timestamp_status IN ('valid', 'reconciled')
                ORDER BY medido_em_utc DESC LIMIT 12
                """,
                (code,),
            ).fetchall()
            tendencia_interna = calcular_tendencias([dict(row) for row in historico])
            tendencia = {
                "temperature_1h": tendencia_interna["temperatura_1h"],
                "temperature_3h": tendencia_interna["temperatura_3h"],
                "humidity_1h": tendencia_interna["umidade_1h"],
                "humidity_3h": tendencia_interna["umidade_3h"],
                "pressure_1h": tendencia_interna["pressao_1h"],
                "pressure_3h": tendencia_interna["pressao_3h"],
                "wind_1h": tendencia_interna["vento_1h"],
                "gust_1h": tendencia_interna["rajada_1h"],
                "wind_direction_change_1h": tendencia_interna["direcao_vento_1h"],
                "rain_1h": tendencia_interna["chuva_1h"],
                "rain_3h": tendencia_interna["chuva_3h"],
                "rain_6h": tendencia_interna["chuva_6h"],
            }
            source_status = station["source_status"] or "SEM_DADOS"
            status = classificar_status(
                dict(freshness) if freshness else None,
                source_status=source_status,
                stale_minutes=config["stale_minutes"],
                very_stale_minutes=config["very_stale_minutes"],
            )
            lat = station["latitude_fonte"]
            lon = station["longitude_fonte"]
            if lat is None or lon is None:
                lat, lon = station["latitude_configurada"], station["longitude_configurada"]
            bearing = bearing_graus(config["target_lat"], config["target_lon"], lat, lon)
            tentativa = station["ultima_tentativa_em"]
            if tentativa and (updated_at is None or tentativa > updated_at):
                updated_at = tentativa
            resultado.append(
                {
                    "code": code,
                    "name": station["nome_exibicao"],
                    "source_name": station["nome_fonte"],
                    "source": "PIN-MS / CEMADEN-INMET",
                    "status": status,
                    "source_status": source_status,
                    "latitude": lat,
                    "longitude": lon,
                    "configured_latitude": station["latitude_configurada"],
                    "configured_longitude": station["longitude_configurada"],
                    "source_latitude": station["latitude_fonte"],
                    "source_longitude": station["longitude_fonte"],
                    "distance_km": haversine_km(config["target_lat"], config["target_lon"], lat, lon),
                    "bearing_deg": bearing,
                    "relative_position": direcao_cardinal(bearing),
                    "age_minutes": idade_minutos_observacao(dict(freshness)) if freshness else None,
                    "last_valid_hourly": horaria["medido_em_local"] if horaria else None,
                    "observation": _observation_publica(observacao_exibida),
                    "trend": tendencia,
                }
            )
        return {"updated_at": updated_at, "stations": resultado}
    finally:
        conn.close()


def status_health_regional(config):
    if not config["enabled"]:
        return "disabled"
    estado = obter_estado_rede(config)
    if not estado["stations"]:
        return "warning"
    return "ok" if all(item["status"] == "OK" for item in estado["stations"]) else "warning"
