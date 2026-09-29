"""Dois overrides administrativos do nowcasting, separados do estado oficial."""

import json
import logging
from datetime import datetime, timezone

import database

logger = logging.getLogger(__name__)
STATE_KEY = "nowcasting_runtime_controls"
FIELDS = {"public": "public_alerts_enabled", "test": "test_alerts_enabled"}
TIMESTAMPS = {"public": "public_updated_at", "test": "test_updated_at"}


def _read(conn):
    row = conn.execute(
        "SELECT valor_json, atualizado_em FROM estado_alertas WHERE chave=?", (STATE_KEY,)
    ).fetchone()
    if row is None:
        return {}, None
    try:
        value = json.loads(row["valor_json"])
        if not isinstance(value, dict) or set(value) - {*FIELDS.values(), *TIMESTAMPS.values(), "updated_at", "source"}:
            raise ValueError("Campos de controle inválidos")
        if any(type(value[key]) is not bool for key in FIELDS.values() if key in value):
            raise ValueError("Valor de controle inválido")
        if value.get("source", "admin") != "admin":
            raise ValueError("Origem de controle inválida")
        return value, value.get("updated_at") or row["atualizado_em"]
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Controles do nowcasting inválidos; usando configuração do servidor")
        return {}, None


def obter_controles(config):
    """Falha de leitura usa exclusivamente os dois valores do ambiente."""
    try:
        conn = database.get_db_readonly()
        try:
            overrides, updated_at = _read(conn)
        finally:
            conn.close()
    except Exception as error:
        logger.warning("Controles do nowcasting indisponíveis (%s)", type(error).__name__)
        overrides, updated_at = {}, None
    result = {}
    for name, field in FIELDS.items():
        key = "alerts_enabled" if name == "public" else "test_alerts_enabled"
        overridden = field in overrides
        result[name] = {
            "enabled": overrides[field] if overridden else config.get(key) is True,
            "source": "admin" if overridden else "server",
            "updated_at": (overrides.get(TIMESTAMPS[name]) or updated_at) if overridden else None,
        }
    return result


def aplicar_controles(config):
    controls = obter_controles(config)
    return {**config, "alerts_enabled": controls["public"]["enabled"],
            "test_alerts_enabled": controls["test"]["enabled"]}


def salvar_controle(name, enabled):
    if name not in FIELDS or type(enabled) is not bool:
        raise ValueError("Controle inválido")
    conn = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        overrides, _ = _read(conn)
        overrides = {field: overrides[field] for field in (*FIELDS.values(), *TIMESTAMPS.values())
                     if field in overrides}
        overrides[FIELDS[name]] = enabled
        overrides[TIMESTAMPS[name]] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        overrides["source"] = "admin"
        conn.execute(
            """INSERT INTO estado_alertas (chave, valor_json, atualizado_em)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(chave) DO UPDATE SET
                 valor_json=excluded.valor_json, atualizado_em=CURRENT_TIMESTAMP""",
            (STATE_KEY, json.dumps(overrides, sort_keys=True)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
