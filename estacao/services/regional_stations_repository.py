"""Persistencia idempotente e estado publico das estacoes regionais."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import timezone

import database
from services.regional_stations_analysis import (
    bearing_graus,
    calcular_tendencias,
    classificar_status,
    direcao_cardinal,
    haversine_km,
    idade_minutos_observacao,
    qualidade_tendencias,
)
from services.regional_stations_catalog import REGIONAL_STATION_CODES
from services.regional_stations_service import RegionalObservation
from time_utils import LOCAL_TZ, agora_utc, iso_local, iso_utc, parse_datetime


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


def salvar_observacao(observacao: RegionalObservation, return_details=False):
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
        inserida = cursor.rowcount == 1
        amostra_atualizada = False
        if observacao.source_layer == 0:
            observation_row = conn.execute(
                "SELECT id FROM regional_station_observations WHERE fingerprint=?",
                (observacao.fingerprint,),
            ).fetchone()
            sample_utc = parse_datetime(observacao.coletado_em_utc, assume_utc=True)
            if observation_row is None or sample_utc is None:
                raise sqlite3.DatabaseError("Observacao layer 0 sem referencia persistida")
            sample_utc = sample_utc.astimezone(timezone.utc)
            sample_local = sample_utc.astimezone(LOCAL_TZ)
            bucket_utc = sample_utc.replace(minute=0, second=0, microsecond=0)
            bucket_local = sample_local.replace(minute=0, second=0, microsecond=0)
            sample_cursor = conn.execute(
                """
                INSERT INTO regional_station_samples (
                    station_code, source_observation_id,
                    sample_time_utc, sample_time_local, sample_time_type,
                    bucket_hour_utc, bucket_hour_local,
                    temperatura_atual, umidade_atual, pressao_atual,
                    vento_velocidade_kmh, rajada_kmh, vento_direcao_graus,
                    chuva_mm, source_layer, coletado_em_utc, coletado_em_local,
                    fingerprint
                ) VALUES (?, ?, ?, ?, 'collection_time_proxy', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT(station_code, bucket_hour_utc) DO UPDATE SET
                    source_observation_id=excluded.source_observation_id,
                    sample_time_utc=excluded.sample_time_utc,
                    sample_time_local=excluded.sample_time_local,
                    sample_time_type=excluded.sample_time_type,
                    temperatura_atual=excluded.temperatura_atual,
                    umidade_atual=excluded.umidade_atual,
                    pressao_atual=excluded.pressao_atual,
                    vento_velocidade_kmh=excluded.vento_velocidade_kmh,
                    rajada_kmh=excluded.rajada_kmh,
                    vento_direcao_graus=excluded.vento_direcao_graus,
                    chuva_mm=excluded.chuva_mm,
                    coletado_em_utc=excluded.coletado_em_utc,
                    coletado_em_local=excluded.coletado_em_local,
                    fingerprint=excluded.fingerprint,
                    atualizado_em=CURRENT_TIMESTAMP
                WHERE excluded.sample_time_utc >= regional_station_samples.sample_time_utc
                """,
                (
                    observacao.station_code, observation_row["id"],
                    iso_utc(sample_utc), iso_local(sample_utc),
                    iso_utc(bucket_utc), bucket_local.isoformat(timespec="seconds"),
                    observacao.temperatura_atual, observacao.umidade_atual,
                    observacao.pressao_atual, observacao.vento_velocidade_kmh,
                    observacao.rajada_kmh, observacao.vento_direcao_graus,
                    observacao.chuva_mm, iso_utc(sample_utc), iso_local(sample_utc),
                    observacao.fingerprint,
                ),
            )
            amostra_atualizada = sample_cursor.rowcount == 1
            sample_iso = iso_utc(sample_utc)
            estado = conn.execute(
                """
                SELECT current_fingerprint, current_fingerprint_first_seen,
                       current_fingerprint_last_seen
                FROM regional_station_state WHERE station_code=?
                """,
                (observacao.station_code,),
            ).fetchone()
            ultimo_visto = (
                parse_datetime(estado["current_fingerprint_last_seen"], assume_utc=True)
                if estado else None
            )
            if ultimo_visto is None or sample_utc >= ultimo_visto.astimezone(timezone.utc):
                mesmo_fingerprint = (
                    estado is not None
                    and estado["current_fingerprint"] == observacao.fingerprint
                )
                primeiro_visto = (
                    estado["current_fingerprint_first_seen"]
                    if mesmo_fingerprint
                    and estado["current_fingerprint_first_seen"]
                    else sample_iso
                )
                conn.execute(
                    """
                    UPDATE regional_station_state SET
                        current_fingerprint=?,
                        current_fingerprint_first_seen=?,
                        current_fingerprint_last_seen=?,
                        atualizado_em=CURRENT_TIMESTAMP
                    WHERE station_code=?
                    """,
                    (
                        observacao.fingerprint,
                        primeiro_visto,
                        sample_iso,
                        observacao.station_code,
                    ),
                )
        conn.commit()
        if return_details:
            return {"inserted": inserida, "sample_updated": amostra_atualizada}
        return inserida
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def registrar_status_estacao(
    code,
    status,
    erro=None,
    sucesso=False,
    external_history_status=None,
    layer2_polled_at=None,
    now=None,
):
    if code not in REGIONAL_STATION_CODES:
        raise ValueError("Codigo regional fora da allowlist")
    conn = database.get_db()
    try:
        agora = iso_utc(now)
        layer2_poll = iso_utc(layer2_polled_at) if layer2_polled_at else None
        conn.execute(
            """
            INSERT INTO regional_station_state (
                station_code, source_status, current_source_status,
                external_history_status, last_layer2_poll_utc,
                ultimo_erro, ultima_tentativa_em, ultimo_sucesso_em, atualizado_em
            ) VALUES (?, ?, ?, COALESCE(?, 'SEM_DADOS'), ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(station_code) DO UPDATE SET
                source_status=excluded.source_status,
                current_source_status=excluded.current_source_status,
                external_history_status=COALESCE(?, regional_station_state.external_history_status),
                last_layer2_poll_utc=COALESCE(excluded.last_layer2_poll_utc,
                                              regional_station_state.last_layer2_poll_utc),
                ultimo_erro=excluded.ultimo_erro,
                ultima_tentativa_em=excluded.ultima_tentativa_em,
                ultimo_sucesso_em=COALESCE(excluded.ultimo_sucesso_em,
                                           regional_station_state.ultimo_sucesso_em),
                atualizado_em=CURRENT_TIMESTAMP
            """,
            (
                code, status, status, external_history_status, layer2_poll,
                erro, agora, agora if sucesso else None, external_history_status,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def layer2_poll_devido(poll_seconds, now=None):
    """Consulta o estado persistido; reiniciar o processo nao antecipa a layer 2."""
    now = (now or agora_utc()).astimezone(timezone.utc)
    conn = database.get_db()
    try:
        rows = conn.execute(
            """
            SELECT st.last_layer2_poll_utc
            FROM regional_stations s
            LEFT JOIN regional_station_state st ON st.station_code=s.codigo
            WHERE s.ativo=1
            """
        ).fetchall()
        if not rows:
            return True
        polls = []
        for row in rows:
            poll = parse_datetime(row["last_layer2_poll_utc"], assume_utc=True)
            if poll is None:
                return True
            polls.append(poll.astimezone(timezone.utc))
        return (now - min(polls)).total_seconds() >= max(60, int(poll_seconds))
    finally:
        conn.close()


def _observation_publica(row):
    if not row:
        return None
    return {
        "source_layer": row["source_layer"],
        "measured_at": row["medido_em_local"],
        "collected_at": (
            row["tempo_referencia_local"]
            if "tempo_referencia_local" in row.keys()
            else row["coletado_em_local"]
        ),
        "reference_time": (
            row["tempo_referencia_local"]
            if "tempo_referencia_local" in row.keys()
            else row["medido_em_local"]
        ),
        "reference_time_type": (
            row["sample_time_type"]
            if "sample_time_type" in row.keys()
            else "source_measurement_time"
        ),
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


def _ultima_amostra_local(conn, code):
    return conn.execute(
        """
        SELECT o.*, s.sample_time_utc AS tempo_referencia_utc,
               s.sample_time_local AS tempo_referencia_local,
               s.sample_time_type, s.bucket_hour_local
        FROM regional_station_samples s
        JOIN regional_station_observations o ON o.id=s.source_observation_id
        WHERE s.station_code=?
        ORDER BY s.sample_time_utc DESC, s.id DESC LIMIT 1
        """,
        (code,),
    ).fetchone()


def _historico_layer2_usavel(rows, now, max_age_hours):
    usaveis = []
    for row in rows:
        momento = parse_datetime(row["medido_em_utc"], assume_utc=True)
        if momento is None:
            continue
        idade_horas = (now - momento.astimezone(timezone.utc)).total_seconds() / 3600
        if -1.5 <= idade_horas <= max_age_hours:
            usaveis.append(dict(row))
    return usaveis


def _tendencia_publica(tendencia_interna):
    return {
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


def obter_estado_rede(config, now=None):
    now = (now or agora_utc()).astimezone(timezone.utc)
    conn = database.get_db()
    try:
        stations = conn.execute(
            """
            SELECT s.*, st.source_status, st.current_source_status,
                   st.external_history_status, st.ultimo_erro,
                   st.ultima_tentativa_em, st.ultimo_sucesso_em,
                   st.current_fingerprint,
                   st.current_fingerprint_first_seen,
                   st.current_fingerprint_last_seen,
                   st.last_layer2_poll_utc
            FROM regional_stations s
            LEFT JOIN regional_station_state st ON st.station_code=s.codigo
            WHERE s.ativo=1 ORDER BY s.nome_exibicao
            """
        ).fetchall()
        resultado = []
        updated_at = None
        for station in stations:
            code = station["codigo"]
            atual = _ultima_amostra_local(conn, code)
            horaria = _ultima_observacao(conn, code, 2, exigir_timestamp=True)
            # A camada 2 pode servir às tendências, mas nunca se apresenta
            # como se fosse o estado atual da estação.
            observacao_exibida = atual
            historico_local = conn.execute(
                """
                SELECT * FROM regional_station_samples
                WHERE station_code=? ORDER BY sample_time_utc DESC LIMIT 24
                """,
                (code,),
            ).fetchall()
            historico_externo = conn.execute(
                """
                SELECT * FROM regional_station_observations
                WHERE station_code=? AND source_layer=2
                  AND medido_em_utc IS NOT NULL
                  AND timestamp_status IN ('valid', 'reconciled')
                ORDER BY medido_em_utc DESC LIMIT 48
                """,
                (code,),
            ).fetchall()
            local_rows = [dict(row) for row in historico_local]
            external_rows = _historico_layer2_usavel(
                historico_externo, now, config["layer2_max_age_hours"]
            )
            local_quality = qualidade_tendencias(local_rows)
            external_quality = qualidade_tendencias(external_rows)
            if local_quality == "GOOD":
                trend_rows = local_rows
                trend_source = "local_history_layer0"
                trend_quality = local_quality
            elif external_quality == "GOOD":
                trend_rows = external_rows
                trend_source = "external_layer2_bootstrap"
                trend_quality = external_quality
            elif local_rows:
                trend_rows = local_rows
                trend_source = "local_history_layer0"
                trend_quality = local_quality
            elif external_rows:
                trend_rows = external_rows
                trend_source = "external_layer2_bootstrap"
                trend_quality = external_quality
            else:
                trend_rows = []
                trend_source = "insufficient"
                trend_quality = "INSUFFICIENT"
            tendencia = _tendencia_publica(calcular_tendencias(trend_rows))
            current_source_status = (
                station["current_source_status"] or station["source_status"] or "SEM_DADOS"
            )
            fingerprint_first_seen = parse_datetime(
                station["current_fingerprint_first_seen"], assume_utc=True
            )
            fingerprint_last_seen = parse_datetime(
                station["current_fingerprint_last_seen"], assume_utc=True
            )
            same_values_minutes = None
            if fingerprint_first_seen and fingerprint_last_seen:
                same_values_minutes = round(
                    max(
                        0.0,
                        (
                            fingerprint_last_seen.astimezone(timezone.utc)
                            - fingerprint_first_seen.astimezone(timezone.utc)
                        ).total_seconds()
                        / 60,
                    ),
                    1,
                )
            stagnant = (
                same_values_minutes is not None
                and same_values_minutes >= config.get("stagnant_minutes", 180)
            )
            status = classificar_status(
                dict(atual) if atual else None,
                source_status=current_source_status,
                stale_minutes=config["stale_minutes"],
                very_stale_minutes=config["very_stale_minutes"],
                stagnant=stagnant,
                now=now,
            )
            external_age_hours = None
            if horaria:
                momento_externo = parse_datetime(horaria["medido_em_utc"], assume_utc=True)
                if momento_externo:
                    external_age_hours = max(
                        0.0,
                        (now - momento_externo.astimezone(timezone.utc)).total_seconds() / 3600,
                    )
            stored_external = station["external_history_status"] or "SEM_DADOS"
            if stored_external == "ERRO_FONTE":
                external_status = "ERRO_FONTE"
            elif external_rows:
                external_status = "OK"
            elif horaria:
                external_status = "STALE"
            else:
                external_status = stored_external
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
                    "source_status": current_source_status,
                    "current_source": {
                        "status": current_source_status,
                        "age_minutes": idade_minutos_observacao(dict(atual), now=now) if atual else None,
                        "last_collection": atual["tempo_referencia_local"] if atual else None,
                        "same_values_since": station["current_fingerprint_first_seen"],
                        "same_values_minutes": same_values_minutes,
                        "stagnant": stagnant,
                    },
                    "data_freshness": {
                        "status": status,
                        "stagnant": stagnant,
                    },
                    "external_hourly_source": {
                        "status": external_status,
                        "age_hours": round(external_age_hours, 1) if external_age_hours is not None else None,
                        "last_valid": horaria["medido_em_local"] if horaria else None,
                        "usable": bool(external_rows),
                        "last_checked_at": station["last_layer2_poll_utc"],
                    },
                    "latitude": lat,
                    "longitude": lon,
                    "configured_latitude": station["latitude_configurada"],
                    "configured_longitude": station["longitude_configurada"],
                    "source_latitude": station["latitude_fonte"],
                    "source_longitude": station["longitude_fonte"],
                    "distance_km": haversine_km(config["target_lat"], config["target_lon"], lat, lon),
                    "bearing_deg": bearing,
                    "relative_position": direcao_cardinal(bearing),
                    "age_minutes": idade_minutos_observacao(dict(atual), now=now) if atual else None,
                    "last_collection": atual["tempo_referencia_local"] if atual else None,
                    "current_bucket": atual["bucket_hour_local"] if atual else None,
                    "last_valid_hourly": horaria["medido_em_local"] if horaria else None,
                    "observation": _observation_publica(observacao_exibida),
                    "trend_source": trend_source,
                    "trend_quality": trend_quality,
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
