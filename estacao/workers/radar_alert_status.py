"""Diagnostico somente leitura; consulta remota apenas com --check-source."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env", encoding="utf-8")

import database
from config import env_str, nowcasting_config, radar_config
from services.nowcasting_alert_evaluation import avaliar_alerta_preventivo_snapshot
from services.nowcasting_public_alerts import carregar_estado
from services.nowcasting_service import snapshot_operacionalmente_atual
from services.radar_service import RadarServiceError, RedemetRadarClient
from services.runtime_alert_controls import obter_controles
from time_utils import agora_utc, iso_utc, parse_datetime

REQUIRED_SCHEMA = {
    "estado_alertas": {"chave", "valor_json", "atualizado_em"},
    "health_check_estado": {"chave", "mensagem"},
    "radar_frames": {"id", "data_frame", "data_frame_utc", "timestamp_status", "status_processamento"},
    "radar_clusters": {"frente_relevante_json"},
    "radar_tracks": set(),
    "radar_track_points": set(),
    "nowcasting_snapshots": {"id", "calculado_em_utc", "estado_json"},
    "alertas_fila": {"status", "evento_id"},
    "alertas_eventos": set(),
    "usuarios": set(),
    "historico_clima": set(),
    "regional_stations": set(),
    "regional_station_observations": set(),
    "regional_station_samples": set(),
    "regional_station_state": set(),
}
CREDENTIALS = ("REDEMET_API_KEY", "EVOLUTION_URL", "EVOLUTION_API_KEY", "EVOLUTION_INSTANCE")
QUEUE_STATUSES = ("pendente", "enviando", "enviado", "falhou", "cancelado")
PUBLIC_RESULTS = {
    "never", "disabled", "eligible", "invalid_snapshot", "invalid_frame",
    "radar_stale", "radar_unavailable", "radar_frame_stale", "snapshot_stale",
    "invalid_frame_timestamp", "clutter", "local_event_observed", "inconsistent_data",
    "insufficient_pixels", "intensity_below_medium", "outside_proximity_range",
    "tracking_insufficient_for_early_warning", "not_approaching", "trajectory_incompatible",
    "public_trajectory_low_confidence", "public_projected_impact_absent",
    "projected_impact_eta_invalid", "rearm_pending", "rearmed", "same_or_lower_severity",
    "cooldown", "awaiting_projected_impact_confirmation", "duplicate", "enqueued", "no_recipients",
}
TIMING_LABELS = {
    "fresh": "Imagem dentro do limite de validade dos alertas.",
    "image_already_stale_at_collection": "A imagem ja estava vencida quando foi coletada; consulte a origem para distinguir atraso da fonte e do coletor.",
    "processing_delay": "A imagem estava valida na coleta e venceu antes da publicacao local.",
    "no_recent_local_publication": "A imagem estava valida na publicacao local, mas nao houve publicacao mais recente a tempo.",
    "timing_unavailable": "Faltam horarios de coleta/publicacao para localizar o atraso.",
    "inconsistent_timestamps": "Os horarios de imagem, coleta e publicacao sao inconsistentes.",
}
SOURCE_LABELS = {
    "source_delayed": "A imagem mais recente oferecida pela REDEMET ja excede o limite dos alertas.",
    "collector_behind_source": "A REDEMET oferece imagem mais recente que a ultima publicada localmente.",
    "local_matches_source": "A imagem local acompanha o horario mais recente consultado na REDEMET.",
    "remote_older_than_local": "A resposta atual da REDEMET tem horario anterior ao da imagem local.",
    "local_frame_missing": "Ha imagem na REDEMET, mas nenhuma imagem processada disponivel localmente.",
    "future_source_timestamp": "O horario da imagem remota esta no futuro em relacao ao relogio local.",
}


def _configured_by(name, fallback=None):
    if env_str(name):
        return name
    if fallback and env_str(fallback):
        return fallback
    return "default"


def _age(timestamp, now):
    value = parse_datetime(timestamp, assume_utc=True)
    return round((now - value).total_seconds() / 60, 2) if value else None


def _timestamp(timestamp):
    value = parse_datetime(timestamp, assume_utc=True)
    return iso_utc(value) if value else None


def _elapsed(start, end):
    first, last = parse_datetime(start, assume_utc=True), parse_datetime(end, assume_utc=True)
    return round((last - first).total_seconds() / 60, 2) if first and last else None


def _frame_timing(frame, now, limit):
    collected, published = frame["coletado_em_utc"], frame["processado_em"]
    collection_age = _elapsed(frame["timestamp"], collected)
    processing = _elapsed(collected, published)
    publication_age = _elapsed(frame["timestamp"], published)
    age = _age(frame["timestamp"], now)
    since_published = _age(published, now)
    metrics = (collection_age, processing, publication_age, since_published, age)
    if any(value is not None and value < -1 for value in metrics):
        diagnosis = "inconsistent_timestamps"
    elif age is not None and age <= limit:
        diagnosis = "fresh"
    elif collection_age is None or publication_age is None:
        diagnosis = "timing_unavailable"
    elif collection_age > limit:
        diagnosis = "image_already_stale_at_collection"
    elif publication_age > limit:
        diagnosis = "processing_delay"
    else:
        diagnosis = "no_recent_local_publication"
    return {
        "collected_at_utc": _timestamp(collected), "published_at_utc": _timestamp(published),
        "image_age_at_collection_minutes": collection_age,
        # A coleta e registrada antes do lote; esta duracao inclui a espera por outros frames.
        "collection_to_publication_minutes": processing,
        "image_age_at_publication_minutes": publication_age,
        "minutes_since_local_publication": since_published,
        "freshness_limit_minutes": limit, "delay_diagnosis": diagnosis,
    }


def _check_source(report, config, *, now=None):
    """Le somente metadados; nunca baixa PNG ou grava o resultado consultado."""
    result = {"requested": True, "status": "not_checked"}
    if not config["api_key"]:
        return {**result, "status": "api_key_missing"}
    try:
        with requests.Session() as session:
            client = RedemetRadarClient(config["api_key"], area=config["area"],
                                        anima=1, timeout=config["request_timeout_seconds"],
                                        produto=config["product"], session=session)
            response = client.obter_frames()
        checked_at = now or agora_utc()
        result["checked_at_utc"] = iso_utc(checked_at)
        if not response.frames:
            return {**result, "status": "no_frames"}
        remote = max(response.frames, key=lambda item: item.data_frame_utc)
        remote_timestamp = remote.data_utc_texto
        age = _age(remote_timestamp, checked_at)
        local = report.get("radar") or {}
        local_timestamp = local.get("timestamp_utc")
        lag = _elapsed(local_timestamp, remote_timestamp)
        limit = nowcasting_config()["radar_max_age_minutes"]
        if age < -1:
            diagnosis = "future_source_timestamp"
        elif lag is not None and lag < 0:
            diagnosis = "remote_older_than_local"
        elif lag is None:
            diagnosis = "local_frame_missing"
        elif lag > 0:
            diagnosis = "source_delayed" if age > limit else "collector_behind_source"
        else:
            diagnosis = "source_delayed" if age > limit else "local_matches_source"
        return {**result, "status": "ok", "latest_frame_utc": remote_timestamp,
                "age_minutes": age, "freshness_limit_minutes": limit,
                "fresh": -1 <= age <= limit, "remote_ahead_of_local_minutes": lag,
                "newer_than_local": lag > 0 if lag is not None else None,
                "delay_diagnosis": diagnosis}
    except RadarServiceError as error:
        # Nunca repasse mensagens de excecao, URLs ou a chave usada na consulta.
        status = {"Timeout ao consultar a REDEMET": "timeout",
                  "Falha HTTP ao consultar a REDEMET": "http_error",
                  "REDEMET retornou JSON invalido": "invalid_json",
                  "Produto de radar nao suportado": "unsupported_product"}.get(str(error), "metadata_request_failed")
        return {**result, "status": status}
    except (TypeError, ValueError, requests.RequestException):
        return {**result, "status": "metadata_request_failed"}


def _inspect_schema(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing_tables = sorted(REQUIRED_SCHEMA.keys() - tables)
    missing_columns = {}
    for table, required in REQUIRED_SCHEMA.items():
        if table in tables:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            missing = sorted(required - columns)
            if missing:
                missing_columns[table] = missing
    return tables, missing_tables, missing_columns


def _read_observations(conn, report, config, now):
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(radar_frames)")}
    timings = ", ".join(name if name in columns else f"NULL AS {name}"
                        for name in ("coletado_em_utc", "processado_em"))
    frame = conn.execute(
        "SELECT id, COALESCE(data_frame_utc, data_frame) AS timestamp, timestamp_status, " + timings + " "
        "FROM radar_frames WHERE status_processamento='processado' "
        "AND COALESCE(timestamp_status, 'legacy_unverified') <> 'suspect' "
        "ORDER BY COALESCE(data_frame_utc, data_frame) DESC, id DESC LIMIT 1"
    ).fetchone()
    if frame:
        age = _age(frame["timestamp"], now)
        valid = frame["timestamp_status"] != "suspect" and age is not None and -1 <= age <= config["radar_max_age_minutes"]
        report["radar"] = {"frame_id": frame["id"], "timestamp_utc": _timestamp(frame["timestamp"]),
                           "age_minutes": age, "fresh": valid,
                           **_frame_timing(frame, now, config["radar_max_age_minutes"])}
        if not valid:
            report["operational_blockers"].append("radar_frame_invalid_or_stale")
    else:
        report["operational_blockers"].append("radar_frames_missing")
    row = conn.execute(
        "SELECT id, calculado_em_utc, estado_json FROM nowcasting_snapshots "
        "ORDER BY calculado_em_utc DESC, id DESC LIMIT 1"
    ).fetchone()
    if row:
        try:
            snapshot = json.loads(row["estado_json"])
            if not isinstance(snapshot, dict):
                raise ValueError("snapshot")
            evaluation = avaliar_alerta_preventivo_snapshot(snapshot, config, now=now)
            fresh = snapshot_operacionalmente_atual(snapshot, config, now=now)
            current_frame = (snapshot.get("radar") or {}).get("frame_id")
            matches_latest = frame is not None and current_frame == frame["id"]
            report["snapshot"] = {
                "id": row["id"], "timestamp_utc": _timestamp(row["calculado_em_utc"]),
                "age_minutes": _age(row["calculado_em_utc"], now), "fresh": fresh,
                "uses_latest_frame": matches_latest,
                "weather_eligible": evaluation["eligible"], "weather_reason": evaluation["reason"],
            }
            if not fresh:
                report["operational_blockers"].append("snapshot_invalid_or_stale")
            if not matches_latest:
                report["operational_blockers"].append("snapshot_behind_latest_frame")
        except (TypeError, ValueError, AttributeError, OverflowError):
            report["operational_blockers"].append("snapshot_invalid")
    else:
        report["operational_blockers"].append("snapshot_missing")
    try:
        state = carregar_estado(conn)
        result = state["last_result"]
        report["public_episode"] = {
            "active": state["active"], "confirmation_frames": state["pending_tracking_count"],
            "last_result": result if result in PUBLIC_RESULTS else "unknown",
            "highest_enqueued_severity": state["highest_enqueued_severity"],
            "last_enqueued_at": _timestamp(state["last_enqueued_at"]),
        }
    except (TypeError, ValueError, KeyError):
        report["operational_blockers"].append("public_episode_invalid")
    for row in conn.execute(
        "SELECT status, COUNT(*) AS total FROM alertas_fila "
        "WHERE evento_id LIKE 'nowcasting:%' GROUP BY status"
    ):
        status = row["status"] if row["status"] in QUEUE_STATUSES else "outros"
        report["queue"][status] = report["queue"].get(status, 0) + row["total"]


def construir_diagnostico(*, now=None, check_source=False):
    """Config pode iniciar com schema ausente; operacional exige dados atuais."""
    fixed_now = now
    now = now or agora_utc()
    radar, config = radar_config(), nowcasting_config()
    controls = obter_controles(config)
    controls = {name: {**value, "updated_at": _timestamp(value["updated_at"])}
                for name, value in controls.items()}
    report = {
        "offline": not check_source, "checked_at_utc": iso_utc(now),
        "ready_to_start": False, "operational": False,
        "flags": {
            "radar": {"enabled": radar["enabled"], "source": "server", "configured_by": _configured_by("RADAR_ENABLED")},
            "nowcasting": {"enabled": config["enabled"], "source": "server", "configured_by": _configured_by("NOWCASTING_ENABLED", "RADAR_ALERTS_ENABLED")},
            "public_alerts": {**controls["public"], "configured_by": _configured_by("NOWCASTING_ALERTS_ENABLED", "RADAR_ALERTS_ENABLED")},
            "test_alerts": {**controls["test"], "configured_by": _configured_by("NOWCASTING_TEST_ALERTS_ENABLED")},
        },
        "credentials": {name: bool(env_str(name)) for name in CREDENTIALS},
        "database": {"path": str(Path(database.DATABASE).resolve()), "status": "unavailable", "missing_tables": [], "missing_columns": {}},
        "radar": None, "snapshot": None, "public_episode": None,
        "queue": dict.fromkeys(QUEUE_STATUSES, 0),
        "config_blockers": [], "operational_blockers": [],
        "worker_processes_checked": False,
        "source_check": {"requested": False, "status": "not_requested"},
    }
    if not radar["enabled"]:
        report["config_blockers"].append("RADAR_ENABLED=false")
    if not config["enabled"]:
        report["config_blockers"].append("NOWCASTING_ENABLED=false")
    report["config_blockers"].extend(name + " missing" for name, present in report["credentials"].items() if not present)
    pending_schema = False
    conn = None
    try:
        conn = database.get_db_readonly()
        tables, missing_tables, missing_columns = _inspect_schema(conn)
        pending_schema = "estado_alertas" not in tables
        report["database"].update(status="legacy" if missing_tables or missing_columns else "ok",
                                  missing_tables=missing_tables, missing_columns=missing_columns)
        if missing_tables or missing_columns:
            report["operational_blockers"].append("schema_initialization_required")
        else:
            _read_observations(conn, report, config, now)
    except (sqlite3.Error, OSError):
        missing = not Path(database.DATABASE).exists()
        pending_schema = missing
        report["database"]["status"] = "missing" if missing else "unreadable"
        report["operational_blockers"].append("database_missing" if missing else "database_unreadable")
        if not missing:
            report["config_blockers"].append("runtime_controls_unavailable")
    finally:
        if conn is not None:
            conn.close()
    public_after_init = config["alerts_enabled"] if pending_schema else controls["public"]["enabled"]
    report["public_alerts_after_schema_init"] = public_after_init
    if not public_after_init:
        report["config_blockers"].append("public_alerts_disabled")
    if not controls["public"]["enabled"]:
        report["operational_blockers"].append("public_alerts_disabled_or_fail_safe")
    report["ready_to_start"] = not report["config_blockers"]
    report["operational"] = report["ready_to_start"] and not report["operational_blockers"]
    if check_source:
        report["source_check"] = _check_source(report, radar, now=fixed_now)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Diagnostico somente leitura dos alertas de radar; offline por padrao")
    parser.add_argument("--json", action="store_true", help="Mostra resultado estruturado sem segredos ou dados de usuarios")
    parser.add_argument("--check-source", action="store_true", help="Consulta somente metadados REDEMET para localizar atraso da fonte/coletor")
    args = parser.parse_args(argv)
    report = construir_diagnostico(check_source=args.check_source)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("Alertas de radar: diagnostico somente leitura")
        print(f"Verificado em UTC: {report['checked_at_utc']}")
        print(f"Configuracao pronta para iniciar: {'SIM' if report['ready_to_start'] else 'NAO'}")
        print(f"Dados operacionais atuais: {'SIM' if report['operational'] else 'NAO'}")
        for name, flag in report["flags"].items():
            print(f"{name}: {'ATIVADO' if flag['enabled'] else 'DESATIVADO'} (origem: {flag['source']})")
        for name, present in report["credentials"].items():
            print(f"{name}: {'presente' if present else 'ausente'}")
        print(f"Banco: {report['database']['path']} ({report['database']['status']})")
        for name in ("radar", "snapshot", "public_episode", "queue"):
            print(f"{name}: {json.dumps(report[name], ensure_ascii=False)}")
        if report["radar"]:
            print(TIMING_LABELS[report["radar"]["delay_diagnosis"]])
        source = report["source_check"]
        if source["requested"]:
            print(f"source_check: {json.dumps(source, ensure_ascii=False)}")
            if source.get("delay_diagnosis"):
                print(SOURCE_LABELS[source["delay_diagnosis"]])
        for blocker in dict.fromkeys(report["config_blockers"] + report["operational_blockers"]):
            print(f"- {blocker}")
        print("Processos dos workers nao verificados neste diagnostico.")
        if report["offline"]:
            print("Sem consulta externa; --check-source compara somente metadados da REDEMET.")
    return 0 if report["operational"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
