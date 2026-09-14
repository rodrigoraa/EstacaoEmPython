"""Modo experimental de alerta preventivo enviado somente ao administrador.

Este modulo nunca consulta usuarios e nunca escreve em ``alertas_fila`` ou
``alertas_eventos``. O estado antispam usa uma chave propria da estrutura
generica ``health_check_estado``, ja existente no schema 8.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timezone

import database
from services.admin_notification_service import (
    enviar_mensagem_admin,
    obter_admin_alert_phone,
)
from services.preventive_alerts import ALERT_SEVERITY
from services.nowcasting_alert_evaluation import (
    avaliar_alerta_preventivo_snapshot,
    montar_mensagem_preventiva as montar_mensagem_alerta_teste,
    _numero_finito, _minutos_desde_utc, _evento_local_observado,
)
from time_utils import agora_utc, iso_utc


logger = logging.getLogger(__name__)

ESTADO_CHAVE = "nowcasting_test_alert"
ESTADO_CAMPOS = (
    "highest_sent_severity",
    "last_sent_alert_level",
    "last_sent_radar_intensity",
    "pending_severity",
    "active",
    "event_key",
    "last_level",
    "last_track_id",
    "last_cluster_id",
    "last_distance_km",
    "last_sent_at",
    "last_seen_at",
    "clear_since",
    "last_result",
    "last_error",
    "last_attempt_at",
    "sent_for_current_episode",
    "suppressed_for_current_episode",
)


class EstadoAlertaTesteInvalido(ValueError):
    """Indica que o estado opcional persistido nao pode ser interpretado."""


def estado_alerta_teste_padrao():
    return {
        "highest_sent_severity": 0,
        "last_sent_alert_level": None,
        "last_sent_radar_intensity": None,
        "pending_severity": 0,
        "active": False,
        "event_key": None,
        "last_level": None,
        "last_track_id": None,
        "last_cluster_id": None,
        "last_distance_km": None,
        "last_sent_at": None,
        "last_seen_at": None,
        "clear_since": None,
        "last_result": "never",
        "last_error": None,
        "last_attempt_at": None,
        "sent_for_current_episode": False,
        "suppressed_for_current_episode": False,
    }


def _normalizar_estado(valor):
    estado = estado_alerta_teste_padrao()
    if isinstance(valor, dict):
        for campo in ESTADO_CAMPOS:
            if campo in valor:
                estado[campo] = valor[campo]
    estado["active"] = bool(estado["active"])
    estado["sent_for_current_episode"] = bool(
        estado["sent_for_current_episode"]
    )
    estado["suppressed_for_current_episode"] = bool(
        estado["suppressed_for_current_episode"]
    )
    severity = estado.get("highest_sent_severity")
    if not isinstance(severity, int) or isinstance(severity, bool) or not 0 <= severity <= 3:
        severity = 3 if estado["sent_for_current_episode"] else 0
    # Uma notificação legada já enviada tinha o patamar máximo do sistema antigo.
    if isinstance(valor, dict) and "highest_sent_severity" not in valor and estado["sent_for_current_episode"]:
        severity = 3
    estado["highest_sent_severity"] = severity
    return estado


def carregar_estado_alerta_teste(
    conn=None, *, estrito=False, somente_leitura=False
):
    proprio = conn is None
    conn = conn or (
        database.get_db_readonly() if somente_leitura else database.get_db()
    )
    try:
        row = conn.execute(
            "SELECT mensagem FROM health_check_estado WHERE chave = ?",
            (ESTADO_CHAVE,),
        ).fetchone()
        if not row or not row["mensagem"]:
            return estado_alerta_teste_padrao()
        try:
            valor = json.loads(row["mensagem"])
            if not isinstance(valor, dict):
                raise EstadoAlertaTesteInvalido(
                    "Estado persistido do alerta de teste invalido"
                )
            return _normalizar_estado(valor)
        except (TypeError, json.JSONDecodeError, EstadoAlertaTesteInvalido):
            logger.warning("Nowcasting teste admin: estado persistido inválido")
            if estrito:
                raise EstadoAlertaTesteInvalido(
                    "Estado persistido do alerta de teste invalido"
                )
            return estado_alerta_teste_padrao()
    finally:
        if proprio:
            conn.close()


def salvar_estado_alerta_teste(conn, estado):
    estado = _normalizar_estado(estado)
    conteudo = json.dumps(estado, ensure_ascii=False, sort_keys=True)
    conn.execute(
        """
        INSERT INTO health_check_estado (
            chave, status, assinatura, mensagem, notificado_em, atualizado_em
        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chave) DO UPDATE SET
            status = excluded.status,
            assinatura = excluded.assinatura,
            mensagem = excluded.mensagem,
            notificado_em = excluded.notificado_em,
            atualizado_em = CURRENT_TIMESTAMP
        """,
        (
            ESTADO_CHAVE,
            "active" if estado["active"] else "idle",
            estado["event_key"],
            conteudo,
            estado["last_sent_at"],
        ),
    )


def _event_key(alerta):
    track_id = alerta.get("track_id")
    return f"track:{track_id}" if track_id is not None else "untracked_rain_episode"


def avaliar_alerta_teste_admin(snapshot, config, *, admin_phone=None, now=None):
    """Avalia apenas os requisitos meteorologicos e de configuracao."""
    snapshot = snapshot or {}
    agora = (now or agora_utc()).astimezone(timezone.utc)
    if config.get("alerts_enabled") is True:
        return {"eligible": False, "reason": "public_alerts_enabled", "event_key": None}
    habilitado = config.get("test_alerts_enabled") is True
    if not habilitado:
        return {"eligible": False, "reason": "disabled", "event_key": None}
    if not (admin_phone or "").strip():
        return {"eligible": False, "reason": "admin_phone_missing", "event_key": None}
    avaliacao = avaliar_alerta_preventivo_snapshot(snapshot, config, now=agora)
    alerta = snapshot.get("alerta_preventivo") or {}
    return {**avaliacao, "event_key": _event_key(alerta)
            if avaliacao["reason"] in {"eligible", "local_event_observed"} else None}


def _erro_resumido(erro):
    nome = type(erro).__name__ or "ErroExterno"
    return re.sub(r"[^A-Za-z0-9_.-]", "", nome)[:80] or "ErroExterno"


def _cooldown_ativo(estado, config, agora):
    idades = [
        minutos
        for minutos in (
            _minutos_desde_utc(estado.get("last_sent_at"), agora),
            _minutos_desde_utc(estado.get("last_attempt_at"), agora),
        )
        if minutos is not None
    ]
    minutos = min(idades) if idades else None
    return bool(
        minutos is not None
        and minutos < float(config.get("test_alert_cooldown_minutes", 60))
    )


def _cooldown_bloqueia(estado, config, agora, severity):
    # Escalonamento após sucesso é imediato; falhas e tentativas em curso
    # continuam respeitando cooldown para impedir duplicação concorrente.
    escalonamento = severity > estado["highest_sent_severity"] > 0
    tentativa_pendente = estado.get("last_attempt_at") and (
        not estado.get("last_sent_at") or estado["last_attempt_at"] > estado["last_sent_at"]
    )
    if escalonamento and not estado.get("pending_severity") and not tentativa_pendente and estado.get("last_result") not in {"sending", "send_failed"}:
        return False
    return _cooldown_ativo(estado, config, agora)


def _atualizar_identidade(estado, snapshot, agora):
    alerta = snapshot.get("alerta_preventivo") or {}
    estado["last_level"] = alerta.get("nivel")
    estado["last_track_id"] = alerta.get("track_id")
    estado["last_cluster_id"] = alerta.get("cluster_id")
    estado["last_distance_km"] = _numero_finito(alerta.get("distance_km"))
    estado["last_seen_at"] = iso_utc(agora)


def _processar_ausencia_evento(estado, config, agora):
    if not estado["active"]:
        return
    if not estado.get("clear_since"):
        estado["clear_since"] = iso_utc(agora)
        estado["last_result"] = "rearm_pending"
        return
    minutos = _minutos_desde_utc(estado["clear_since"], agora)
    if minutos is not None and minutos >= float(
        config.get("test_alert_rearm_minutes", 30)
    ):
        estado["active"] = False
        estado["event_key"] = None
        estado["sent_for_current_episode"] = False
        estado["suppressed_for_current_episode"] = False
        estado["highest_sent_severity"] = 0
        estado["last_sent_alert_level"] = None
        estado["last_sent_radar_intensity"] = None
        estado["pending_severity"] = 0
        estado["last_result"] = "rearmed"
    else:
        estado["last_result"] = "rearm_pending"


def processar_alerta_teste_admin(snapshot, config, *, now=None, sender=None):
    """Atualiza o episodio persistente e, quando seguro, envia diretamente."""
    snapshot = snapshot or {}
    agora = (now or agora_utc()).astimezone(timezone.utc)
    admin_phone = obter_admin_alert_phone()
    avaliacao = avaliar_alerta_teste_admin(
        snapshot, config, admin_phone=admin_phone, now=agora
    )
    if avaliacao["reason"] == "public_alerts_enabled":
        logger.info("Nowcasting teste admin: envio direto suprimido pelo modo público")
        return obter_status_alerta_teste_admin(snapshot, config, now=agora)
    if avaliacao["reason"] == "disabled":
        logger.info("Nowcasting teste admin: teste desabilitado")
        return obter_status_alerta_teste_admin(snapshot, config, now=agora)
    if avaliacao["reason"] == "admin_phone_missing":
        logger.warning(
            "Alerta preventivo de teste habilitado, mas ADMIN_ALERT_PHONE não está configurado."
        )
        return obter_status_alerta_teste_admin(snapshot, config, now=agora)

    decisao = avaliacao.get("decision") or {}
    alerta = {**(snapshot.get("alerta_preventivo") or {}), **decisao}
    snapshot = {**snapshot, "alerta_preventivo": alerta}
    severity = ALERT_SEVERITY.get(decisao.get("alert_level"), 0)
    campos_log = ("radar_intensity", "alert_level", "certainty", "urgency", "authorization",
                  "distance_km", "front_pixels_total", "front_percent_medium_or_higher",
                  "front_percent_strong", "front_percent_very_high", "tracking_valid",
                  "approaching", "trajectory_compatible")
    logger.info("Nowcasting teste admin: decisão=%s bloqueio=%s",
                {campo: alerta.get(campo) for campo in campos_log}, avaliacao["reason"])

    conn = database.get_db()
    deve_enviar = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        estado = carregar_estado_alerta_teste(conn)
        _atualizar_identidade(estado, snapshot, agora)
        # Ausência de dados ou perda de tracking não prova o fim de um episódio.
        ausencia_confirmada = avaliacao["reason"] in {
            "intensity_below_medium", "insufficient_pixels", "outside_proximity_range"
        }

        if _evento_local_observado(snapshot):
            if not estado["active"]:
                estado["active"] = True
                estado["event_key"] = avaliacao["event_key"]
            estado["clear_since"] = None
            estado["suppressed_for_current_episode"] = True
            estado["last_result"] = "local_event_observed"
        elif not avaliacao["eligible"]:
            if not ausencia_confirmada and estado["active"]:
                estado["clear_since"] = None
            else:
                _processar_ausencia_evento(estado, config, agora)
            estado["last_result"] = (
                estado["last_result"]
                if estado["last_result"] in {"rearm_pending", "rearmed"}
                else avaliacao["reason"]
            )
        else:
            logger.info(
                "Nowcasting teste admin: candidato preventivo detectado track=%s distancia=%s",
                (snapshot.get("alerta_preventivo") or {}).get("track_id"),
                (snapshot.get("alerta_preventivo") or {}).get("distance_km"),
            )
            if not estado["active"]:
                estado["active"] = True
                estado["event_key"] = avaliacao["event_key"]
                estado["sent_for_current_episode"] = False
                estado["suppressed_for_current_episode"] = False
            estado["clear_since"] = None

            if estado["suppressed_for_current_episode"]:
                estado["last_result"] = "local_event_observed"
            elif severity <= estado["highest_sent_severity"]:
                estado["last_result"] = "same_or_lower_severity"
                logger.info(
                    "Nowcasting teste admin: envio ignorado porque o episódio já foi notificado"
                )
            elif _cooldown_bloqueia(estado, config, agora, severity):
                estado["last_result"] = "cooldown"
                logger.info("Nowcasting teste admin: envio ignorado por cooldown")
            else:
                logger.info("Nowcasting teste admin: escalonamento=%s episodio_notificado=%s",
                            severity > estado["highest_sent_severity"] > 0, estado["sent_for_current_episode"])
                estado["last_attempt_at"] = iso_utc(agora)
                estado["pending_severity"] = severity
                estado["last_result"] = "sending"
                estado["last_error"] = None
                deve_enviar = True

        logger.info("Nowcasting teste admin: resultado=%s episodio_notificado=%s highest_sent_severity=%s",
                    estado["last_result"], estado["sent_for_current_episode"], estado["highest_sent_severity"])
        salvar_estado_alerta_teste(conn, estado)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if deve_enviar:
        enviar = sender or enviar_mensagem_admin
        try:
            enviar(admin_phone, montar_mensagem_alerta_teste(snapshot))
        except Exception as erro:
            erro_seguro = _erro_resumido(erro)
            logger.error(
                "Nowcasting teste admin: falha no WhatsApp (%s)", erro_seguro
            )
            _finalizar_tentativa(
                enviado=False, erro=erro_seguro, now=agora
            )
        else:
            alerta = snapshot.get("alerta_preventivo") or {}
            logger.info(
                "Nowcasting teste admin: alerta enviado track=%s distancia=%skm",
                alerta.get("track_id"),
                alerta.get("distance_km"),
            )
            _finalizar_tentativa(enviado=True, erro=None, now=agora, decisao=decisao)

    return obter_status_alerta_teste_admin(snapshot, config, now=agora)


def _finalizar_tentativa(*, enviado, erro, now, decisao=None):
    conn = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        estado = carregar_estado_alerta_teste(conn)
        if enviado:
            estado["last_sent_at"] = iso_utc(now)
            estado["last_result"] = "sent"
            estado["last_error"] = None
            estado["sent_for_current_episode"] = True
            estado["pending_severity"] = 0
            decisao = decisao or {}
            estado["highest_sent_severity"] = max(estado["highest_sent_severity"], ALERT_SEVERITY.get(decisao.get("alert_level"), 0))
            estado["last_sent_alert_level"] = decisao.get("alert_level")
            estado["last_sent_radar_intensity"] = decisao.get("radar_intensity")
        else:
            estado["last_result"] = "send_failed"
            estado["last_error"] = erro
        salvar_estado_alerta_teste(conn, estado)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def obter_status_alerta_teste_admin(snapshot=None, config=None, *, now=None):
    """Retorna somente metadados seguros para telas e API administrativas."""
    config = config or {}
    try:
        enabled = config.get("test_alerts_enabled") is True
    except Exception:
        enabled = False

    fallback = {
        "enabled": enabled,
        "eligible": False,
        "sent_for_current_episode": False,
        "event_key": None,
        "last_sent_at": None,
        "cooldown_active": False,
        "rearm_pending": False,
        "reason": "status_unavailable",
    }
    try:
        agora = (now or agora_utc()).astimezone(timezone.utc)
        estado = carregar_estado_alerta_teste(
            estrito=True, somente_leitura=True
        )
        avaliacao = avaliar_alerta_teste_admin(
            snapshot,
            config,
            admin_phone=obter_admin_alert_phone(),
            now=agora,
        )
        severity = ALERT_SEVERITY.get((avaliacao.get("decision") or {}).get("alert_level"), 0)
        cooldown = _cooldown_bloqueia(estado, config, agora, severity)
        rearm_pending = bool(estado["active"] and estado.get("clear_since"))
        eligible = bool(
            avaliacao["eligible"]
            and severity > estado["highest_sent_severity"]
            and not estado["suppressed_for_current_episode"]
            and not cooldown
        )
        reason = estado["last_result"] if enabled else "disabled"
        if not avaliacao["eligible"]:
            reason = avaliacao["reason"]
        return {
            "enabled": enabled,
            "eligible": eligible,
            "sent_for_current_episode": bool(
                estado["sent_for_current_episode"]
            ),
            "event_key": estado["event_key"] or avaliacao.get("event_key"),
            "last_sent_at": estado["last_sent_at"],
            "cooldown_active": cooldown,
            "rearm_pending": rearm_pending,
            "reason": reason,
            "highest_sent_severity": estado["highest_sent_severity"],
            "last_sent_alert_level": estado["last_sent_alert_level"],
            "last_sent_radar_intensity": estado["last_sent_radar_intensity"],
        }
    except Exception as erro:
        logger.warning(
            "Nowcasting teste admin: status indisponivel (%s)",
            _erro_resumido(erro),
        )
        return fallback
