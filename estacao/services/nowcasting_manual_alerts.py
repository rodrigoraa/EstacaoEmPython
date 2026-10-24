"""Autorização administrativa de um alerta recebido, sem ligar o envio automático."""

import json
import logging
import re
import uuid

import database
from config import numero_alerta_valido
from services.alert_queue_service import enfileirar_alerta
from services.nowcasting_alert_evaluation import avaliar_alerta_preventivo_snapshot
from services.nowcasting_repository import obter_ultimo_snapshot
from services.preventive_alerts import ALERT_SEVERITY
from services.runtime_alert_controls import obter_controles
from time_utils import agora_utc, data_local, iso_local, iso_utc, parse_datetime

logger = logging.getLogger(__name__)
STATE_KEY = "nowcasting_manual_alert"
EVENT_TYPE = "nowcasting_radar_manual"
EVENT_PREFIX = "nowcasting-manual:"


def registrar_alerta_recebido(conn, snapshot, mensagem, *, now):
    """Participa da transação que confirma o envio ao administrador."""
    alerta = snapshot["alerta_preventivo"]
    recebido = {
        "id": uuid.uuid4().hex, "mensagem": mensagem, "snapshot": snapshot,
        "received_at": iso_utc(now), "alert_level": alerta["alert_level"],
    }
    conn.execute("""INSERT INTO estado_alertas (chave, valor_json, atualizado_em)
        VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(chave) DO UPDATE SET
        valor_json=excluded.valor_json, atualizado_em=CURRENT_TIMESTAMP""",
        (STATE_KEY, json.dumps(recebido, ensure_ascii=False)))


def _carregar(conn):
    row = conn.execute("SELECT valor_json FROM estado_alertas WHERE chave=?",
                       (STATE_KEY,)).fetchone()
    return _validar(json.loads(row["valor_json"])) if row else None


def _validar(alerta):
    if (not isinstance(alerta, dict)
            or not isinstance(alerta.get("id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", alerta["id"])
            or not isinstance(alerta.get("mensagem"), str) or not alerta["mensagem"]
            or not isinstance(alerta.get("snapshot"), dict)
            or not parse_datetime(alerta.get("received_at"))
            or alerta.get("alert_level") not in ALERT_SEVERITY):
        raise ValueError("Alerta recebido inválido")
    return alerta


def _motivo_bloqueio(alerta, config, agora):
    snapshot_original = alerta["snapshot"]
    observacao = parse_datetime((snapshot_original.get("radar") or {}).get("data_frame"))
    ttl = numero_alerta_valido(config.get("alert_delivery_max_age_minutes"), 15)
    idade = (agora - observacao).total_seconds() / 60 if observacao else None
    if idade is None or idade < -1 or idade > ttl:
        return "expired"
    if config.get("enabled") is not True:
        return "unavailable"
    atual = obter_ultimo_snapshot()
    avaliacao = avaliar_alerta_preventivo_snapshot(atual, config, now=agora)
    if not avaliacao["eligible"]:
        return "invalidated"
    origem = snapshot_original.get("alerta_preventivo") or {}
    decisao = avaliacao.get("decision") or {}
    if ((atual.get("alerta_preventivo") or {}).get("track_id") != origem.get("track_id")
            or ALERT_SEVERITY.get(decisao.get("alert_level"), 0)
            < ALERT_SEVERITY[alerta["alert_level"]]):
        return "invalidated"
    return None


def obter_status_alerta_manual(config, *, now=None):
    try:
        conn = database.get_db_readonly()
        try:
            alerta = _carregar(conn)
            if alerta is None:
                return {"available": False, "can_send": False, "alert": None,
                        "reason": "not_found"}
            evento = conn.execute("""SELECT criado_em, enfileirados FROM alertas_eventos
                WHERE evento_id=?""", (EVENT_PREFIX + alerta["id"],)).fetchone()
        finally:
            conn.close()
        controls = obter_controles(config)
        motivo = ("unavailable" if controls["public"]["source"] == "fail_safe" else
                  "public_alerts_enabled" if controls["public"]["enabled"] else
                  "duplicate" if evento else
                  _motivo_bloqueio(alerta, config, now or agora_utc()))
        visivel = {campo: alerta[campo] for campo in
                   ("id", "mensagem", "received_at", "alert_level")}
        visivel.update(enqueued_at=evento["criado_em"] if evento else None,
                       enfileirados=evento["enfileirados"] if evento else 0)
        return {"available": True, "can_send": motivo is None, "alert": visivel,
                "reason": motivo or "available"}
    except Exception as erro:
        logger.warning("Alerta manual indisponível (%s)", type(erro).__name__)
        return {"available": False, "can_send": False, "alert": None,
                "reason": "unavailable"}


def enfileirar_alerta_manual(alert_id, config, *, now=None):
    agora = now or agora_utc()
    conn = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        alerta = _carregar(conn)
        if alerta is None or alerta["id"] != alert_id:
            return {"reason": "not_found", "enfileirados": 0}
        controls = obter_controles(config)
        if controls["public"]["source"] == "fail_safe":
            return {"reason": "unavailable", "enfileirados": 0}
        if controls["public"]["enabled"]:
            return {"reason": "public_alerts_enabled", "enfileirados": 0}
        evento_id = EVENT_PREFIX + alerta["id"]
        if conn.execute("SELECT 1 FROM alertas_eventos WHERE evento_id=?",
                        (evento_id,)).fetchone():
            return {"reason": "duplicate", "enfileirados": 0, "duplicado": True}
        motivo = _motivo_bloqueio(alerta, config, agora)
        if motivo:
            return {"reason": motivo, "enfileirados": 0}
        original = alerta["snapshot"]
        momento = parse_datetime(original["radar"]["data_frame"])
        severity = ALERT_SEVERITY[alerta["alert_level"]]
        evento = {
            "evento_id": evento_id, "tipo": EVENT_TYPE, "nivel": severity,
            "data_referencia": data_local(momento),
            "valor": original["alerta_preventivo"].get("distance_km"),
            "unidade": "km", "ocorrido_em_local": iso_local(momento),
            "fonte": "admin_nowcasting_manual", "prioridade": {1: 50, 2: 80, 3: 100}[severity],
        }
        resultado = enfileirar_alerta(conn, alerta["mensagem"], evento,
                                     montar_mensagem_alerta=lambda _usuario, texto: texto)
        autorizacao = {**alerta, "authorized_at": iso_utc(agora)}
        conn.execute("UPDATE alertas_eventos SET detalhe=? WHERE evento_id=?",
                     (json.dumps(autorizacao, ensure_ascii=False), evento_id))
        conn.commit()
        return {**resultado, "reason": "enqueued" if resultado["enfileirados"] else "no_recipients"}
    except Exception:
        conn.rollback()
        raise
    finally:
        # Também encerra a transação nas recusas, sem alterar o banco.
        conn.close()


def revalidar_alerta_manual(conn, item, config, *, now=None):
    """Revalida a autorização persistida; meteo/TTL valem no enfileiramento."""
    try:
        if obter_controles(config)["public"]["source"] == "fail_safe":
            return "Alerta manual indisponível: controles de alertas inválidos"
        row = conn.execute("SELECT * FROM alertas_eventos WHERE evento_id=?",
                           (item["evento_id"],)).fetchone()
        if row is None or row["tipo"] != EVENT_TYPE or row["fonte"] != "admin_nowcasting_manual":
            return "Alerta manual sem autorização administrativa válida"
        alerta = _validar(json.loads(row["detalhe"]))
        if (row["evento_id"] != EVENT_PREFIX + alerta["id"]
                or row["nivel"] != ALERT_SEVERITY[alerta["alert_level"]]
                or row["mensagem"] != alerta["mensagem"]
                or item["mensagem"] != alerta["mensagem"]
                or not parse_datetime(alerta.get("authorized_at"))):
            return "Alerta manual com autorização inconsistente"
        return None
    except Exception as erro:
        return f"Revalidação do alerta manual indisponível ({type(erro).__name__})"
