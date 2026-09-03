"""Persistencia curta e consultas publicas do radar."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Sequence

import database
from services.radar_analysis import (
    RadarCluster,
    TrackPoint,
    analisar_track,
    custo_associacao,
    haversine_km,
)
from services.radar_service import RadarFrame
from time_utils import LOCAL_TZ, iso_local, iso_utc, minutos_desde, parse_datetime


def frame_existente(path_remoto: str):
    conn = database.get_db()
    try:
        return conn.execute(
            "SELECT id, status_processamento FROM radar_frames WHERE path_remoto = ?",
            (path_remoto,),
        ).fetchone()
    finally:
        conn.close()


def salvar_resultado_frame(
    frame: RadarFrame,
    arquivo_local: str | None,
    arquivo_analisado: str | None,
    largura: int | None,
    altura: int | None,
    clusters: Sequence[RadarCluster],
    status: str = "processado",
    erro: str | None = None,
) -> tuple[int, bool]:
    """Insere/reprocessa um frame numa unica transacao SQLite curta."""
    conn = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existente = conn.execute(
            "SELECT id, status_processamento FROM radar_frames WHERE path_remoto = ?",
            (frame.path_remoto,),
        ).fetchone()
        if existente and existente["status_processamento"] == "processado":
            conn.commit()
            return existente["id"], False

        if existente:
            frame_id = existente["id"]
            conn.execute("DELETE FROM radar_clusters WHERE frame_id = ?", (frame_id,))
            conn.execute(
                "DELETE FROM radar_tracks WHERE NOT EXISTS "
                "(SELECT 1 FROM radar_track_points p WHERE p.track_id=radar_tracks.id)"
            )
            conn.execute(
                """
                UPDATE radar_frames SET
                    radar_codigo=?, produto=?, data_frame=?, data_frame_utc=?,
                    data_frame_local=?, timestamp_status='utc_explicit', arquivo_local=?,
                    arquivo_analisado=?, largura=?, altura=?, lat_center=?, lon_center=?,
                    lat_min=?, lat_max=?, lon_min=?, lon_max=?, raio_km=?, tamanho=?,
                    baixado_em=CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP ELSE baixado_em END,
                    processado_em=CASE WHEN ? = 'processado' THEN CURRENT_TIMESTAMP ELSE NULL END,
                    status_processamento=?, erro_processamento=?
                WHERE id=?
                """,
                (
                    frame.radar_codigo,
                    frame.produto,
                    frame.data_texto,
                    frame.data_utc_texto,
                    frame.data_local_texto,
                    arquivo_local,
                    arquivo_analisado,
                    largura,
                    altura,
                    frame.lat_center,
                    frame.lon_center,
                    frame.lat_min,
                    frame.lat_max,
                    frame.lon_min,
                    frame.lon_max,
                    frame.raio_km,
                    frame.tamanho,
                    arquivo_local,
                    status,
                    status,
                    erro,
                    frame_id,
                ),
            )
        else:
            cursor = conn.execute(
                """
                INSERT INTO radar_frames (
                    radar_codigo, produto, data_frame, data_frame_utc,
                    data_frame_local, timestamp_status, path_remoto,
                    arquivo_local, arquivo_analisado, largura, altura,
                    lat_center, lon_center, lat_min, lat_max, lon_min, lon_max,
                    raio_km, tamanho, baixado_em, processado_em,
                    status_processamento, erro_processamento
                ) VALUES (
                    ?, ?, ?, ?, ?, 'utc_explicit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CASE WHEN ? IS NOT NULL THEN CURRENT_TIMESTAMP END,
                    CASE WHEN ? = 'processado' THEN CURRENT_TIMESTAMP END, ?, ?
                )
                """,
                (
                    frame.radar_codigo,
                    frame.produto,
                    frame.data_texto,
                    frame.data_utc_texto,
                    frame.data_local_texto,
                    frame.path_remoto,
                    arquivo_local,
                    arquivo_analisado,
                    largura,
                    altura,
                    frame.lat_center,
                    frame.lon_center,
                    frame.lat_min,
                    frame.lat_max,
                    frame.lon_min,
                    frame.lon_max,
                    frame.raio_km,
                    frame.tamanho,
                    arquivo_local,
                    status,
                    status,
                    erro,
                ),
            )
            frame_id = cursor.lastrowid

        for cluster in clusters:
            conn.execute(
                """
                INSERT INTO radar_clusters (
                    frame_id, cluster_numero, pixels_eco, centro_x, centro_y,
                    centro_lat, centro_lon, bbox_x, bbox_y, bbox_width, bbox_height,
                    distancia_centro_escola_km, distancia_borda_escola_km,
                    distancia_radar_km, direcao_relativa_escola,
                    suspeito_clutter, intensidade_codigo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    frame_id,
                    cluster.numero,
                    cluster.pixels_eco,
                    cluster.centro_x,
                    cluster.centro_y,
                    cluster.centro_lat,
                    cluster.centro_lon,
                    cluster.bbox_x,
                    cluster.bbox_y,
                    cluster.bbox_width,
                    cluster.bbox_height,
                    cluster.distancia_centro_escola_km,
                    cluster.distancia_borda_escola_km,
                    cluster.distancia_radar_km,
                    cluster.direcao_relativa_escola,
                    int(cluster.suspeito_clutter),
                    cluster.intensidade_codigo,
                ),
            )
        _atualizar_clutter_frame(conn, frame_id)
        conn.commit()
        return frame_id, True
    except sqlite3.IntegrityError:
        conn.rollback()
        row = conn.execute(
            "SELECT id FROM radar_frames WHERE path_remoto = ?", (frame.path_remoto,)
        ).fetchone()
        if row:
            return row["id"], False
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _point(row) -> TrackPoint:
    momento = parse_datetime(
        row["data_frame_utc"] if "data_frame_utc" in row.keys() else None,
        assume_utc=True,
    ) or parse_datetime(row["data_frame"], assume_utc=True)
    return TrackPoint(
        data_frame=momento,
        centro_lat=row["centro_lat"],
        centro_lon=row["centro_lon"],
        distancia_centro_escola_km=row["distancia_centro_escola_km"],
        distancia_borda_escola_km=row["distancia_borda_escola_km"],
        pixels_eco=row["pixels_eco"],
    )


def _pontos_track(conn, track_id: int) -> list[TrackPoint]:
    rows = conn.execute(
        """
        SELECT data_frame, data_frame_utc, centro_lat, centro_lon,
               distancia_centro_escola_km, distancia_borda_escola_km, pixels_eco
        FROM radar_track_points WHERE track_id=? ORDER BY data_frame
        """,
        (track_id,),
    ).fetchall()
    return [_point(row) for row in rows]


def _atualizar_clutter_frame(conn, frame_id: int):
    """Calcula indice diagnostico; nunca remove ou reclassifica um eco."""
    frame = conn.execute(
        "SELECT COALESCE(data_frame_utc, data_frame) AS momento FROM radar_frames WHERE id=?",
        (frame_id,),
    ).fetchone()
    momento = parse_datetime(frame["momento"], assume_utc=True) if frame else None
    if not momento:
        return
    limite = momento - timedelta(days=30)
    frame_rows = conn.execute(
        """
        SELECT id, COALESCE(data_frame_utc, data_frame) AS momento
        FROM radar_frames
        WHERE id<>? AND status_processamento='processado'
        ORDER BY id DESC LIMIT 4000
        """,
        (frame_id,),
    ).fetchall()
    frames_periodo = {
        row["id"]
        for row in frame_rows
        if (
            (dt := parse_datetime(row["momento"], assume_utc=True))
            and limite <= dt < momento
        )
    }
    rows = conn.execute(
        """
        SELECT c.frame_id, c.centro_lat, c.centro_lon,
               COALESCE(f.data_frame_utc, f.data_frame) AS momento
        FROM radar_clusters c
        JOIN radar_frames f ON f.id=c.frame_id
        WHERE c.frame_id<>?
        ORDER BY f.id DESC LIMIT 4000
        """,
        (frame_id,),
    ).fetchall()
    historico = [row for row in rows if row["frame_id"] in frames_periodo]
    total_frames = len(frames_periodo)
    atuais = conn.execute(
        "SELECT * FROM radar_clusters WHERE frame_id=?", (frame_id,)
    ).fetchall()
    for atual in atuais:
        proximos = [
            row
            for row in historico
            if haversine_km(
                atual["centro_lat"], atual["centro_lon"],
                row["centro_lat"], row["centro_lon"],
            ) <= 12.0
        ]
        amostras = len({row["frame_id"] for row in proximos})
        indice = None
        if total_frames >= 12 and amostras >= 4:
            frequencia = min(1.0, amostras / total_frames)
            persistencia = min(1.0, amostras / 12.0)
            deslocamentos = []
            ordenados = sorted(
                proximos,
                key=lambda row: parse_datetime(row["momento"], assume_utc=True),
            )
            for a, b in zip(ordenados, ordenados[1:]):
                if a["frame_id"] != b["frame_id"]:
                    deslocamentos.append(
                        haversine_km(
                            a["centro_lat"], a["centro_lon"],
                            b["centro_lat"], b["centro_lon"],
                        )
                    )
            medio = sum(deslocamentos) / len(deslocamentos) if deslocamentos else 0.0
            estacionario = max(0.0, 1.0 - medio / 12.0)
            perto_centro = 1.0 if atual["distancia_radar_km"] <= 50 else 0.0
            indice = round(
                min(
                    1.0,
                    0.45 * frequencia
                    + 0.25 * persistencia
                    + 0.20 * estacionario
                    + 0.10 * perto_centro,
                ),
                3,
            )
        conn.execute(
            "UPDATE radar_clusters SET indice_persistencia_clutter=?, clutter_amostras=? WHERE id=?",
            (indice, amostras, atual["id"]),
        )


def atualizar_tracking(
    frame_id: int,
    target_lat: float,
    target_lon: float,
    min_frames: int,
    min_duration_minutes: float,
    max_speed_kmh: float,
    intercept_radius_km: float,
    max_size_ratio: float = 4.0,
    max_direction_change_deg: float = 90.0,
    prediction_weight: float = 0.65,
    track_timeout_minutes: float = 180.0,
) -> list[int]:
    conn = database.get_db()
    atualizados: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        frame = conn.execute(
            """
            SELECT data_frame, data_frame_utc, data_frame_local
            FROM radar_frames WHERE id=?
            """,
            (frame_id,),
        ).fetchone()
        if not frame:
            raise ValueError("Frame inexistente para tracking")
        momento = parse_datetime(frame["data_frame_utc"], assume_utc=True) or parse_datetime(
            frame["data_frame"], assume_utc=True
        )
        if not momento:
            raise ValueError("Frame sem timestamp valido para tracking")
        momento_utc = iso_utc(momento)
        momento_local = iso_local(momento)
        clusters = conn.execute(
            "SELECT * FROM radar_clusters WHERE frame_id=? ORDER BY distancia_borda_escola_km",
            (frame_id,),
        ).fetchall()
        track_rows = conn.execute(
            """
            SELECT id FROM radar_tracks
            WHERE ativo=1
            """,
        ).fetchall()
        historicos = {}
        for track in track_rows:
            pontos = _pontos_track(conn, track["id"])
            if pontos and pontos[-1].data_frame < momento:
                historicos[track["id"]] = pontos
        pontos_atuais = {
            cluster["id"]: TrackPoint(
                data_frame=momento,
                centro_lat=cluster["centro_lat"],
                centro_lon=cluster["centro_lon"],
                distancia_centro_escola_km=cluster["distancia_centro_escola_km"],
                distancia_borda_escola_km=cluster["distancia_borda_escola_km"],
                pixels_eco=cluster["pixels_eco"],
            )
            for cluster in clusters
        }
        arestas = []
        for cluster in clusters:
            for track_id, pontos in historicos.items():
                valido, custo, _detalhes = custo_associacao(
                    pontos,
                    pontos_atuais[cluster["id"]],
                    max_speed_kmh,
                    max_size_ratio=max_size_ratio,
                    max_direction_change_deg=max_direction_change_deg,
                    prediction_weight=prediction_weight,
                    max_gap_minutes=track_timeout_minutes,
                )
                if valido:
                    arestas.append((custo, track_id, cluster["id"]))
        associacoes = {}
        clusters_usados: set[int] = set()
        tracks_usados: set[int] = set()
        for _, track_id, cluster_id in sorted(arestas):
            if cluster_id in clusters_usados or track_id in tracks_usados:
                continue
            associacoes[cluster_id] = track_id
            clusters_usados.add(cluster_id)
            tracks_usados.add(track_id)

        for cluster in clusters:
            track_id = associacoes.get(cluster["id"])
            if not track_id:
                codigo = f"radar-{frame_id}-{cluster['id']}"
                cursor = conn.execute(
                    """
                    INSERT INTO radar_tracks (
                        track_codigo, primeiro_frame_em, ultimo_frame_em,
                        primeiro_frame_em_utc, primeiro_frame_em_local,
                        ultimo_frame_em_utc, ultimo_frame_em_local,
                        centro_lat_atual, centro_lon_atual,
                        distancia_centro_escola_km, distancia_borda_escola_km,
                        suspeito_clutter, indice_persistencia_clutter,
                        clutter_amostras, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'DADOS_INSUFICIENTES')
                    """,
                    (
                        codigo,
                        momento.strftime("%Y-%m-%d %H:%M:%S"),
                        momento.strftime("%Y-%m-%d %H:%M:%S"),
                        momento_utc,
                        momento_local,
                        momento_utc,
                        momento_local,
                        cluster["centro_lat"],
                        cluster["centro_lon"],
                        cluster["distancia_centro_escola_km"],
                        cluster["distancia_borda_escola_km"],
                        cluster["suspeito_clutter"],
                        cluster["indice_persistencia_clutter"],
                        cluster["clutter_amostras"],
                    ),
                )
                track_id = cursor.lastrowid
            conn.execute(
                """
                INSERT OR IGNORE INTO radar_track_points (
                    track_id, frame_id, cluster_id, data_frame,
                    data_frame_utc, data_frame_local, centro_lat, centro_lon,
                    distancia_centro_escola_km, distancia_borda_escola_km, pixels_eco
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    track_id,
                    frame_id,
                    cluster["id"],
                    momento.strftime("%Y-%m-%d %H:%M:%S"),
                    momento_utc,
                    momento_local,
                    cluster["centro_lat"],
                    cluster["centro_lon"],
                    cluster["distancia_centro_escola_km"],
                    cluster["distancia_borda_escola_km"],
                    cluster["pixels_eco"],
                ),
            )
            analise = analisar_track(
                _pontos_track(conn, track_id),
                target_lat,
                target_lon,
                min_frames,
                min_duration_minutes,
                max_speed_kmh,
                intercept_radius_km,
            )
            conn.execute(
                """
                UPDATE radar_tracks SET
                    primeiro_frame_em=?, ultimo_frame_em=?, quantidade_frames=?,
                    primeiro_frame_em_utc=?, primeiro_frame_em_local=?,
                    ultimo_frame_em_utc=?, ultimo_frame_em_local=?,
                    duracao_minutos=?, deslocamento_total_km=?, velocidade_media_kmh=?,
                    bearing_movimento=?, direcao_movimento=?, centro_lat_atual=?,
                    centro_lon_atual=?, distancia_centro_escola_km=?,
                    distancia_borda_escola_km=?, aproximando=?, taxa_aproximacao_kmh=?,
                    trajetoria_compativel=?, menor_aproximacao_km=?, eta_minutos=?,
                    suspeito_clutter=?, indice_persistencia_clutter=?,
                    clutter_amostras=?, status=?, ativo=1,
                    atualizado_em=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    analise.primeiro_frame_em.strftime("%Y-%m-%d %H:%M:%S"),
                    analise.ultimo_frame_em.strftime("%Y-%m-%d %H:%M:%S"),
                    analise.quantidade_frames,
                    iso_utc(analise.primeiro_frame_em),
                    iso_local(analise.primeiro_frame_em),
                    iso_utc(analise.ultimo_frame_em),
                    iso_local(analise.ultimo_frame_em),
                    analise.duracao_minutos,
                    analise.deslocamento_total_km,
                    analise.velocidade_media_kmh,
                    analise.bearing_movimento,
                    analise.direcao_movimento,
                    cluster["centro_lat"],
                    cluster["centro_lon"],
                    analise.distancia_centro_escola_km,
                    analise.distancia_borda_escola_km,
                    None if analise.aproximando is None else int(analise.aproximando),
                    analise.taxa_aproximacao_kmh,
                    int(analise.trajetoria_compativel),
                    analise.menor_aproximacao_km,
                    analise.eta_minutos,
                    cluster["suspeito_clutter"],
                    cluster["indice_persistencia_clutter"],
                    cluster["clutter_amostras"],
                    analise.status,
                    track_id,
                ),
            )
            atualizados.append(track_id)

        limite_dt = momento - timedelta(minutes=track_timeout_minutes)
        limite = limite_dt.strftime("%Y-%m-%d %H:%M:%S")
        limite_utc = iso_utc(limite_dt)
        conn.execute(
            """
            UPDATE radar_tracks SET ativo=0
            WHERE ativo=1 AND (
                (ultimo_frame_em_utc IS NOT NULL AND ultimo_frame_em_utc < ?)
                OR (ultimo_frame_em_utc IS NULL AND ultimo_frame_em < ?)
            )
            """,
            (limite_utc, limite),
        )
        conn.commit()
        return atualizados
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def marcar_frame_processado(frame_id: int):
    conn = database.get_db()
    try:
        conn.execute(
            """
            UPDATE radar_frames
            SET status_processamento='processado', erro_processamento=NULL,
                processado_em=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (frame_id,),
        )
        conn.commit()
    finally:
        conn.close()


def obter_arquivo_frame(frame_id: int, analisado: bool = True):
    coluna = "arquivo_analisado" if analisado else "arquivo_local"
    conn = database.get_db()
    try:
        return conn.execute(
            f"SELECT id, {coluna} AS arquivo FROM radar_frames "
            "WHERE id=? AND status_processamento='processado'",
            (frame_id,),
        ).fetchone()
    finally:
        conn.close()


def listar_frames_recentes(limite: int = 15) -> list[dict]:
    limite = min(100, max(1, int(limite)))
    conn = database.get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, radar_codigo, produto, data_frame, data_frame_utc,
                   data_frame_local, timestamp_status, largura, altura,
                   status_processamento, processado_em
            FROM radar_frames
            ORDER BY COALESCE(data_frame_utc, data_frame) DESC LIMIT ?
            """,
            (limite,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def listar_clusters_frame(frame_id: int) -> list[dict]:
    conn = database.get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, frame_id, cluster_numero, pixels_eco, centro_lat, centro_lon,
                   distancia_centro_escola_km, distancia_borda_escola_km,
                   distancia_radar_km, direcao_relativa_escola, suspeito_clutter,
                   indice_persistencia_clutter, clutter_amostras,
                   intensidade_codigo
            FROM radar_clusters WHERE frame_id=?
            ORDER BY distancia_borda_escola_km
            """,
            (frame_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def listar_tracks_ativos(limite: int = 50) -> list[dict]:
    limite = min(100, max(1, int(limite)))
    conn = database.get_db()
    try:
        rows = conn.execute(
            """
            SELECT id, track_codigo, primeiro_frame_em, ultimo_frame_em,
                   primeiro_frame_em_utc, primeiro_frame_em_local,
                   ultimo_frame_em_utc, ultimo_frame_em_local,
                   quantidade_frames, velocidade_media_kmh, direcao_movimento,
                   bearing_movimento, centro_lat_atual, centro_lon_atual,
                   distancia_centro_escola_km, distancia_borda_escola_km,
                   aproximando, trajetoria_compativel, eta_minutos,
                   suspeito_clutter, indice_persistencia_clutter,
                   clutter_amostras, status
            FROM radar_tracks WHERE ativo=1
            ORDER BY distancia_borda_escola_km, ultimo_frame_em DESC LIMIT ?
            """,
            (limite,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def obter_estado_radar(stale_minutes: int) -> dict:
    conn = database.get_db()
    try:
        frame = conn.execute(
            """
            SELECT * FROM radar_frames
            WHERE status_processamento='processado'
            ORDER BY COALESCE(data_frame_utc, data_frame) DESC LIMIT 1
            """
        ).fetchone()
        if not frame:
            return {"disponivel": False, "stale": None, "frame": None,
                    "cluster_mais_proximo": None, "tracking": None}
        cluster = conn.execute(
            """
            SELECT * FROM radar_clusters WHERE frame_id=?
            ORDER BY distancia_borda_escola_km LIMIT 1
            """,
            (frame["id"],),
        ).fetchone()
        total_clusters = conn.execute(
            "SELECT COUNT(*) FROM radar_clusters WHERE frame_id=?", (frame["id"],)
        ).fetchone()[0]
        track = None
        if cluster:
            track = conn.execute(
                """
                SELECT t.* FROM radar_tracks t
                JOIN radar_track_points p ON p.track_id=t.id
                WHERE p.cluster_id=? LIMIT 1
                """,
                (cluster["id"],),
            ).fetchone()
        frame_utc = frame["data_frame_utc"] or frame["data_frame"]
        frame_local = frame["data_frame_local"]
        if not frame_local:
            dt_legado = parse_datetime(frame_utc, assume_utc=True)
            frame_local = iso_local(dt_legado) if dt_legado else None
        idade = minutos_desde(frame_utc, assume_utc=True)
        frame_publico = {
            "id": frame["id"],
            "radar_codigo": frame["radar_codigo"],
            "produto": frame["produto"],
            "data_frame": frame["data_frame"],
            "data_frame_utc": frame_utc,
            "data_frame_local": frame_local,
            "timestamp_status": frame["timestamp_status"],
            "largura": frame["largura"],
            "altura": frame["altura"],
            "idade_minutos": idade,
            "imagem_disponivel": bool(frame["arquivo_analisado"] or frame["arquivo_local"]),
            "clusters_significativos": total_clusters,
        }
        cluster_publico = None
        if cluster:
            cluster_publico = {
                "id": cluster["id"],
                "cluster_numero": cluster["cluster_numero"],
                "pixels_eco": cluster["pixels_eco"],
                "centro_lat": cluster["centro_lat"],
                "centro_lon": cluster["centro_lon"],
                "distancia_centro_escola_km": cluster["distancia_centro_escola_km"],
                "distancia_borda_escola_km": cluster["distancia_borda_escola_km"],
                "direcao_relativa": cluster["direcao_relativa_escola"],
                "suspeito_clutter": bool(cluster["suspeito_clutter"]),
                "indice_persistencia_clutter": cluster["indice_persistencia_clutter"],
                "clutter_amostras": cluster["clutter_amostras"],
                "intensidade_codigo": cluster["intensidade_codigo"],
            }
        tracking = None
        if track:
            tracking = {
                "track_id": track["id"],
                "status": track["status"],
                "quantidade_frames": track["quantidade_frames"],
                "duracao_minutos": track["duracao_minutos"],
                "velocidade_kmh": track["velocidade_media_kmh"],
                "direcao_movimento": track["direcao_movimento"],
                "bearing_movimento": track["bearing_movimento"],
                "centro_lat": track["centro_lat_atual"],
                "centro_lon": track["centro_lon_atual"],
                "aproximando": None if track["aproximando"] is None else bool(track["aproximando"]),
                "taxa_aproximacao_kmh": track["taxa_aproximacao_kmh"],
                "trajetoria_compativel": bool(track["trajetoria_compativel"]),
                "menor_aproximacao_km": track["menor_aproximacao_km"],
                "eta_minutos": track["eta_minutos"],
                "suspeito_clutter": bool(track["suspeito_clutter"]),
                "indice_persistencia_clutter": track["indice_persistencia_clutter"],
                "clutter_amostras": track["clutter_amostras"],
            }
        return {
            "disponivel": True,
            "atualizado_em": frame["processado_em"],
            "stale": idade is None or idade > stale_minutes,
            "frame": frame_publico,
            "cluster_mais_proximo": cluster_publico,
            "tracking": tracking,
        }
    finally:
        conn.close()
