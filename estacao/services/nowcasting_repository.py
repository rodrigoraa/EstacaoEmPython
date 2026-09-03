"""Agregacao persistida e snapshots do motor de nowcasting."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import database
from config import regional_stations_config
from services.radar_repository import obter_estado_radar
from services.regional_stations_repository import obter_estado_rede
from time_utils import minutos_desde


def _local_station(config):
    conn = database.get_db()
    try:
        row = conn.execute(
            """
            SELECT id, station_data_hora_utc, station_data_hora_local,
                   data_hora_utc, data_hora_local, data_hora,
                   temp, umidade, pressao, vento_vel, vento_rajada,
                   vento_dir, chuva_rate, chuva_hoje
            FROM historico_clima ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        timestamp_utc = row["station_data_hora_utc"] or row["data_hora_utc"]
        timestamp_local = (
            row["station_data_hora_local"]
            or row["data_hora_local"]
            or row["data_hora"]
        )
        age = minutos_desde(
            timestamp_utc or timestamp_local,
            assume_utc=bool(timestamp_utc),
        )
        return {
            "_id": row["id"],
            "measured_at": timestamp_local,
            "age_minutes": age,
            "stale": age is None or age > config["local_max_age_minutes"],
            "temperature": row["temp"],
            "humidity": row["umidade"],
            "pressure": row["pressao"],
            "wind_speed": row["vento_vel"],
            "wind_gust": row["vento_rajada"],
            "wind_direction": row["vento_dir"],
            "rain_rate": row["chuva_rate"],
            "rain_today": row["chuva_hoje"],
        }
    finally:
        conn.close()


def _tracks_frame_atual(radar):
    frame = radar.get("frame") or {}
    if not frame.get("id"):
        return []
    conn = database.get_db()
    try:
        rows = conn.execute(
            """
            SELECT t.*, c.id AS cluster_id, c.pixels_eco,
                   c.distancia_radar_km, c.direcao_relativa_escola,
                   c.intensidade_codigo, c.classe_predominante, c.classe_maxima,
                   c.pixels_refletividade_baixa, c.pixels_refletividade_media,
                   c.pixels_refletividade_alta, c.pixels_refletividade_muito_alta,
                   c.suspeito_clutter AS cluster_suspeito_clutter,
                   c.indice_persistencia_clutter AS cluster_indice_clutter,
                   c.clutter_amostras AS cluster_clutter_amostras
            FROM radar_track_points p
            JOIN radar_tracks t ON t.id=p.track_id
            JOIN radar_clusters c ON c.id=p.cluster_id
            WHERE p.frame_id=? AND t.ativo=1
            """,
            (frame["id"],),
        ).fetchall()
        if not rows:
            return []
        resultado = []
        for row in rows:
            resultado.append(
                {
                    "track": {
                        "track_id": row["id"],
                        "status": row["status"],
                        "quantidade_frames": row["quantidade_frames"],
                        "duracao_minutos": row["duracao_minutos"],
                        "velocidade_kmh": row["velocidade_media_kmh"],
                        "bearing_movimento": row["bearing_movimento"],
                        "direcao_movimento": row["direcao_movimento"],
                        "centro_lat": row["centro_lat_atual"],
                        "centro_lon": row["centro_lon_atual"],
                        "aproximando": (
                            None if row["aproximando"] is None else bool(row["aproximando"])
                        ),
                        "taxa_aproximacao_kmh": row["taxa_aproximacao_kmh"],
                        "trajetoria_compativel": bool(row["trajetoria_compativel"]),
                        "menor_aproximacao_km": row["menor_aproximacao_km"],
                        "eta_minutos": row["eta_minutos"],
                        "suspeito_clutter": bool(row["suspeito_clutter"]),
                        "indice_persistencia_clutter": row["indice_persistencia_clutter"],
                        "clutter_amostras": row["clutter_amostras"],
                    },
                    "cluster": {
                        "id": row["cluster_id"],
                        "pixels_eco": row["pixels_eco"],
                        "distancia_centro_escola_km": row["distancia_centro_escola_km"],
                        "distancia_borda_escola_km": row["distancia_borda_escola_km"],
                        "direcao_relativa": row["direcao_relativa_escola"],
                        "suspeito_clutter": bool(row["cluster_suspeito_clutter"]),
                        "indice_persistencia_clutter": row["cluster_indice_clutter"],
                        "clutter_amostras": row["cluster_clutter_amostras"],
                        "intensidade_codigo": row["intensidade_codigo"],
                        "classe_predominante": row["classe_predominante"],
                        "classe_maxima": row["classe_maxima"],
                        "pixels_refletividade_baixa": row["pixels_refletividade_baixa"],
                        "pixels_refletividade_media": row["pixels_refletividade_media"],
                        "pixels_refletividade_alta": row["pixels_refletividade_alta"],
                        "pixels_refletividade_muito_alta": row["pixels_refletividade_muito_alta"],
                    },
                }
            )
        return resultado
    finally:
        conn.close()


def carregar_entradas_nowcasting(config):
    radar = obter_estado_radar(config["radar_max_age_minutes"])
    tracks_atuais = _tracks_frame_atual(radar)
    radar["tracks_atuais"] = tracks_atuais
    if tracks_atuais:
        preliminar = min(
            tracks_atuais,
            key=lambda item: (
                0 if item["track"].get("trajetoria_compativel") else 1,
                0 if item["track"].get("aproximando") else 1,
                0 if item["track"].get("velocidade_kmh") is not None else 1,
                item["cluster"].get("distancia_borda_escola_km")
                if item["cluster"].get("distancia_borda_escola_km") is not None
                else 99999,
                item["track"].get("eta_minutos")
                if item["track"].get("eta_minutos") is not None else 99999,
                item["track"].get("indice_persistencia_clutter")
                if item["track"].get("indice_persistencia_clutter") is not None else 0,
            ),
        )
        radar["tracking"] = preliminar["track"]
        radar["cluster_mais_proximo"] = preliminar["cluster"]

    regional_config = regional_stations_config()
    regional_config["stale_minutes"] = config["regional_max_age_minutes"]
    regional_config["very_stale_minutes"] = max(
        config["regional_max_age_minutes"] * 2,
        regional_config["very_stale_minutes"],
    )
    regional = obter_estado_rede(regional_config)
    local = _local_station(config)
    fingerprint_body = {
        "algorithm": config["algorithm_version"],
        "radar_frame": (radar.get("frame") or {}).get("id"),
        "radar_stale": radar.get("stale"),
        "radar_tracks": [
            (
                item["track"].get("track_id"),
                item["track"].get("quantidade_frames"),
                item["track"].get("trajetoria_compativel"),
                item["track"].get("aproximando"),
                item["cluster"].get("distancia_borda_escola_km"),
                item["track"].get("indice_persistencia_clutter"),
            )
            for item in tracks_atuais
        ],
        "regional": [
            (
                station.get("code"),
                station.get("last_collection"),
                station.get("status"),
                station.get("trend_source"),
                station.get("trend_quality"),
                station.get("observation"),
                station.get("trend"),
            )
            for station in regional.get("stations", [])
        ],
        "local": local.get("_id") if local else None,
        "local_stale": local.get("stale") if local else None,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if local:
        local = dict(local)
        local.pop("_id", None)
    return radar, regional, local, fingerprint


def salvar_snapshot(estado, input_fingerprint):
    radar = estado.get("radar") or {}
    conn = database.get_db()
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO nowcasting_snapshots (
                calculado_em_utc, calculado_em_local, radar_frame_id,
                radar_track_id, status, nivel_evidencia, indice_evidencia,
                distancia_borda_km, velocidade_kmh, direcao_movimento,
                aproximando, trajetoria_compativel, eta_minutos,
                estacoes_relevantes_json, evidencias_json, dados_escola_json,
                estado_json, input_fingerprint, versao_algoritmo
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                estado["gerado_em_utc"],
                estado["gerado_em"],
                radar.get("frame_id"),
                radar.get("track_id"),
                estado["status"],
                estado["nivel_evidencia"],
                estado["indice_evidencia"],
                radar.get("distancia_borda_km"),
                radar.get("velocidade_kmh"),
                radar.get("direcao"),
                None if radar.get("aproximando") is None else int(radar["aproximando"]),
                int(bool(radar.get("trajetoria_compativel"))),
                radar.get("eta_minutos"),
                json.dumps(estado["estacoes_relevantes"], ensure_ascii=False),
                json.dumps(estado["evidencias"], ensure_ascii=False),
                json.dumps(estado.get("escola"), ensure_ascii=False),
                json.dumps(estado, ensure_ascii=False, sort_keys=True),
                input_fingerprint,
                estado["versao_algoritmo"],
            ),
        )
        conn.commit()
        return cursor.lastrowid if cursor.rowcount == 1 else None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def obter_ultimo_snapshot():
    conn = database.get_db()
    try:
        row = conn.execute(
            "SELECT id, estado_json FROM nowcasting_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        try:
            estado = json.loads(row["estado_json"])
        except (TypeError, json.JSONDecodeError) as erro:
            raise sqlite3.DatabaseError("Snapshot de nowcasting invalido") from erro
        estado["snapshot_id"] = row["id"]
        return estado
    finally:
        conn.close()


def status_health_nowcasting(config):
    if not config["enabled"]:
        return "disabled"
    snapshot = obter_ultimo_snapshot()
    if not snapshot:
        return "warning"
    if snapshot.get("status") in {"SEM_DADOS", "DADOS_INSUFICIENTES"}:
        return "warning"
    age = minutos_desde(snapshot.get("gerado_em_utc"), assume_utc=True)
    return "ok" if age is not None and age <= max(10, config["poll_seconds"] // 30) else "warning"
