"""Episódios públicos e fan-out atômico, sem envio ou acesso à rede."""
import json
import logging
import re
import uuid

import database
from services.alert_queue_service import enfileirar_alerta
from services.nowcasting_alert_evaluation import (
    avaliar_alerta_preventivo_snapshot, montar_mensagem_preventiva,
    _evento_local_observado, _minutos_desde_utc, _numero_finito,
)
from services.preventive_alerts import ALERT_SEVERITY
from time_utils import agora_utc, iso_utc, iso_local, data_local, parse_datetime

logger = logging.getLogger(__name__)
ESTADO_CHAVE = "nowcasting_public_alert"
AUSENCIA_CONFIRMADA = {
    "intensity_below_medium", "insufficient_pixels", "outside_proximity_range",
}


def estado_padrao():
    return dict(active=False, episode_id=None, episode_started_at=None,
                highest_enqueued_severity=0, last_enqueued_alert_level=None,
                last_enqueued_radar_intensity=None, last_enqueued_at=None,
                last_seen_at=None, last_distance_km=None, clear_since=None,
                last_result="never", suppressed_for_current_episode=False)


def carregar_estado(conn):
    row = conn.execute("SELECT mensagem FROM health_check_estado WHERE chave=?",
                       (ESTADO_CHAVE,)).fetchone()
    if row is None:
        return estado_padrao()
    estado = json.loads(row["mensagem"])
    if not isinstance(estado, dict) or not estado_padrao().keys() <= estado.keys():
        raise ValueError("Estado público inválido")
    severity = estado["highest_enqueued_severity"]
    if (type(estado["active"]) is not bool
            or type(estado["suppressed_for_current_episode"]) is not bool
            or type(severity) is not int or not 0 <= severity <= 3):
        raise ValueError("Estado público inválido")
    if estado["active"]:
        if not isinstance(estado["episode_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", estado["episode_id"]):
            raise ValueError("Identidade pública inválida")
        if not parse_datetime(estado["episode_started_at"]):
            raise ValueError("Início do episódio inválido")
    elif estado["episode_id"] is not None or severity:
        raise ValueError("Episódio inativo inconsistente")
    if not estado["active"] and (estado["episode_started_at"] is not None
                                or estado["suppressed_for_current_episode"]
                                or estado["clear_since"] is not None):
        raise ValueError("Episódio inativo inconsistente")
    for campo in ("last_enqueued_at", "last_seen_at", "clear_since", "episode_started_at"):
        if estado[campo] is not None and not parse_datetime(estado[campo]):
            raise ValueError("Horário público inválido")
    if severity and (not estado["last_enqueued_at"] or
                     ALERT_SEVERITY.get(estado["last_enqueued_alert_level"]) != severity):
        raise ValueError("Severidade pública inconsistente")
    if estado["last_enqueued_radar_intensity"] != {0: None, 1: "MEDIUM", 2: "HIGH", 3: "VERY_HIGH"}[severity]:
        raise ValueError("Intensidade pública inconsistente")
    if severity == 0 and estado["last_enqueued_alert_level"] is not None:
        raise ValueError("Nível público inconsistente")
    return estado


def salvar_estado(conn, estado):
    conn.execute("""
        INSERT INTO health_check_estado
            (chave, status, assinatura, mensagem, notificado_em, atualizado_em)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chave) DO UPDATE SET status=excluded.status,
            assinatura=excluded.assinatura, mensagem=excluded.mensagem,
            notificado_em=excluded.notificado_em, atualizado_em=CURRENT_TIMESTAMP
        """, (ESTADO_CHAVE, "active" if estado["active"] else "idle",
              estado["episode_id"], json.dumps(estado, ensure_ascii=False, sort_keys=True),
              estado["last_enqueued_at"]))


def _iniciar(estado, agora):
    estado.update(active=True, episode_id=uuid.uuid4().hex,
                  episode_started_at=iso_utc(agora), clear_since=None)


def _mensagem_usuario(usuario, mensagem):
    # O texto preventivo já contém local e link; aqui só entra a saudação.
    nome = (usuario["nome"] or "").strip()
    return (f"ATENÇÃO, {nome},\n" if nome else "") + mensagem


def processar_alerta_publico(snapshot, config, *, now=None):
    habilitado = config.get("alerts_enabled") is True
    logger.info("Nowcasting público: enabled=%s", habilitado)
    if not habilitado:
        return {"enabled": False, "enfileirados": 0, "reason": "disabled"}
    agora = now or agora_utc()
    snapshot = snapshot or {}
    avaliacao = avaliar_alerta_preventivo_snapshot(snapshot, config, now=agora)
    if avaliacao["reason"] == "invalid_snapshot":
        snapshot = {}
    decisao = avaliacao.get("decision") or {}
    alerta = {**(snapshot.get("alerta_preventivo") or {}), **decisao}
    severity = ALERT_SEVERITY.get(decisao.get("alert_level"), 0)
    resultado = {"total": 0, "enfileirados": 0, "falhas": 0, "duplicado": False}
    conn = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        estado = carregar_estado(conn)
        estado.update(last_seen_at=iso_utc(agora),
                      last_distance_km=_numero_finito(alerta.get("distance_km")),
                      last_result=avaliacao["reason"])
        if _evento_local_observado(snapshot) or avaliacao["reason"] == "local_event_observed":
            if not estado["active"]:
                _iniciar(estado, agora)
            estado.update(clear_since=None, suppressed_for_current_episode=True,
                          last_result="local_event_observed")
        elif not avaliacao["eligible"]:
            if estado["active"] and avaliacao["reason"] in AUSENCIA_CONFIRMADA:
                estado["clear_since"] = estado["clear_since"] or iso_utc(agora)
                minutos = _minutos_desde_utc(estado["clear_since"], agora)
                estado["last_result"] = "rearm_pending"
                if minutos >= config.get("alert_rearm_minutes", 30):
                    ultimo = estado["last_enqueued_at"]
                    estado = {**estado_padrao(), "last_enqueued_at": ultimo,
                              "last_seen_at": iso_utc(agora), "last_result": "rearmed"}
            else:
                estado["clear_since"] = None
        else:
            if not estado["active"]:
                _iniciar(estado, agora)
            estado["clear_since"] = None
            highest = estado["highest_enqueued_severity"]
            idade = _minutos_desde_utc(estado["last_enqueued_at"], agora)
            if estado["suppressed_for_current_episode"]:
                estado["last_result"] = "local_event_observed"
            elif severity <= highest:
                estado["last_result"] = "same_or_lower_severity"
            elif highest == 0 and idade is not None and idade < config.get("alert_cooldown_minutes", 60):
                estado["last_result"] = "cooldown"
            else:
                evento_id = f"nowcasting:{estado['episode_id']}:{decisao['alert_level'].lower()}"
                radar = snapshot.get("radar") or {}
                momento = (parse_datetime(radar.get("data_frame"))
                           or parse_datetime(snapshot.get("gerado_em_utc")))
                mensagem = montar_mensagem_preventiva({**snapshot, "alerta_preventivo": alerta})
                evento = dict(evento_id=evento_id, tipo="nowcasting_radar", nivel=severity,
                              data_referencia=data_local(momento), valor=estado["last_distance_km"],
                              unidade="km", fonte="redemet_jaraguari_nowcasting",
                              ocorrido_em_local=iso_local(momento),
                              prioridade={1: 50, 2: 80, 3: 100}[severity])
                resultado = enfileirar_alerta(conn, mensagem, evento,
                                             montar_mensagem_alerta=_mensagem_usuario)
                # Mesmo sem destinatários, o nível é consumido; não há backfill.
                estado.update(highest_enqueued_severity=severity,
                              last_enqueued_alert_level=decisao["alert_level"],
                              last_enqueued_radar_intensity=decisao["radar_intensity"],
                              last_enqueued_at=iso_utc(agora),
                              last_result="duplicate" if resultado.get("duplicado") else
                              "enqueued" if resultado["enfileirados"] else "no_recipients")
                logger.info("Nowcasting público: escalonamento=%s", highest > 0)
        salvar_estado(conn, estado)
        conn.commit()
    except Exception:
        conn.rollback()
        logger.warning("Nowcasting público: transação cancelada; nenhum envio autorizado")
        raise
    finally:
        conn.close()
    logger.info("Nowcasting público: episode_id=%s decisão=%s destinatarios=%s enfileirados=%s resultado=%s",
                estado["episode_id"], {k: alerta.get(k) for k in (
                    "alert_level", "radar_intensity", "authorization", "certainty", "urgency", "distance_km")},
                resultado["total"], resultado["enfileirados"], estado["last_result"])
    return {**resultado, "enabled": True, "reason": estado["last_result"], "state": estado}


def obter_status_alerta_publico(config):
    try:
        conn = database.get_db_readonly()
        try:
            estado = carregar_estado(conn)
        finally:
            conn.close()
        return {**estado, "enabled": config.get("alerts_enabled") is True}
    except Exception:
        return {**estado_padrao(), "enabled": config.get("alerts_enabled") is True,
                "last_result": "status_unavailable"}
