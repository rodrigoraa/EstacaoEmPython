"""Modo experimental de alerta preventivo enviado somente ao administrador.

Este modulo nunca consulta usuarios e nunca escreve em ``alertas_fila`` ou
``alertas_eventos``. O estado antispam usa uma chave propria da estrutura
generica ``health_check_estado``, ja existente no schema 8.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import timezone

import database
from services.admin_notification_service import (
    enviar_mensagem_admin,
    obter_admin_alert_phone,
)
from services.nowcasting_service import snapshot_operacionalmente_atual
from services.preventive_alerts import CLUTTER_FORTE_LIMIAR
from time_utils import agora_utc, iso_utc, parse_datetime


logger = logging.getLogger(__name__)

ESTADO_CHAVE = "nowcasting_test_alert"
ESTADO_CAMPOS = (
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


def estado_alerta_teste_padrao():
    return {
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
    return estado


def carregar_estado_alerta_teste(conn=None):
    proprio = conn is None
    conn = conn or database.get_db()
    try:
        row = conn.execute(
            "SELECT mensagem FROM health_check_estado WHERE chave = ?",
            (ESTADO_CHAVE,),
        ).fetchone()
        if not row or not row["mensagem"]:
            return estado_alerta_teste_padrao()
        try:
            return _normalizar_estado(json.loads(row["mensagem"]))
        except (TypeError, json.JSONDecodeError):
            logger.warning("Nowcasting teste admin: estado persistido inválido")
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


def _numero_finito(valor):
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return None
    return numero if math.isfinite(numero) else None


def _minutos_desde_utc(valor, agora):
    momento = parse_datetime(valor, assume_utc=True)
    if not momento:
        return None
    return max(
        0.0,
        (
            agora.astimezone(timezone.utc) - momento.astimezone(timezone.utc)
        ).total_seconds()
        / 60.0,
    )


def _evento_local_observado(snapshot):
    if snapshot.get("evento_local_observado") is True:
        return True
    chuva = _numero_finito((snapshot.get("escola") or {}).get("rain_rate"))
    return bool(chuva is not None and chuva > 0)


def _event_key(alerta):
    track_id = alerta.get("track_id")
    return f"track:{track_id}" if track_id is not None else "untracked_red_episode"


def avaliar_alerta_teste_admin(snapshot, config, *, admin_phone=None, now=None):
    """Avalia apenas os requisitos meteorologicos e de configuracao."""
    snapshot = snapshot or {}
    agora = (now or agora_utc()).astimezone(timezone.utc)
    habilitado = config.get("test_alerts_enabled") is True
    if not habilitado:
        return {"eligible": False, "reason": "disabled", "event_key": None}
    if not (admin_phone or "").strip():
        return {"eligible": False, "reason": "admin_phone_missing", "event_key": None}
    if not snapshot_operacionalmente_atual(snapshot, config, now=agora):
        return {"eligible": False, "reason": "snapshot_stale", "event_key": None}

    alerta = snapshot.get("alerta_preventivo") or {}
    radar = snapshot.get("radar") or {}
    if alerta.get("nivel") != "VERMELHO":
        return {"eligible": False, "reason": "level_not_red", "event_key": None}
    if alerta.get("would_send") is not True:
        return {"eligible": False, "reason": "not_candidate", "event_key": None}
    if radar.get("operacional") is not True or radar.get("stale") is True:
        return {"eligible": False, "reason": "radar_unavailable", "event_key": None}
    clutter_index = _numero_finito(alerta.get("clutter_index"))
    if (
        alerta.get("clutter") is True
        or alerta.get("low_confidence") is True
        or (
            clutter_index is not None
            and clutter_index >= CLUTTER_FORTE_LIMIAR
        )
    ):
        return {"eligible": False, "reason": "clutter", "event_key": None}
    if _evento_local_observado(snapshot):
        return {
            "eligible": False,
            "reason": "local_event_observed",
            "event_key": _event_key(alerta),
        }
    return {
        "eligible": True,
        "reason": "eligible",
        "event_key": _event_key(alerta),
    }


def montar_mensagem_alerta_teste(snapshot):
    snapshot = snapshot or {}
    alerta = snapshot.get("alerta_preventivo") or {}
    distancia = _numero_finito(alerta.get("distance_km"))
    linhas = [
        "🧪 ALERTA PREVENTIVO — TESTE",
        "",
        "Possível chuva próxima à EE São José.",
        "",
        (
            "Eco de radar com borda a aproximadamente "
            f"{distancia:.1f} km da escola."
            if distancia is not None
            else "Distância da borda do eco: dados insuficientes."
        ),
        "",
    ]

    tracking_valido = alerta.get("tracking_valid") is True
    if tracking_valido:
        aproximando = alerta.get("approaching")
        movimento = (
            "aproximando"
            if aproximando is True
            else "afastando"
            if aproximando is False
            else "dados insuficientes"
        )
        velocidade = _numero_finito(alerta.get("speed_kmh"))
        eta = _numero_finito(alerta.get("eta_minutes"))
        eta_borda = _numero_finito(alerta.get("eta_border_minutes"))
    else:
        movimento = "dados insuficientes"
        velocidade = eta = eta_borda = None

    linhas.append(f"Movimento: {movimento}")
    linhas.append(
        "Velocidade estimada do eco: "
        + (f"{velocidade:.0f} km/h" if velocidade is not None else "dados insuficientes")
    )
    linhas.append(
        "ETA da trajetória: "
        + (f"{eta:.0f} min" if eta is not None else "dados insuficientes")
    )
    linhas.append(
        "ETA estimado da borda: "
        + (f"{eta_borda:.0f} min" if eta_borda is not None else "dados insuficientes")
    )
    if alerta.get("regional_confirmation") is True:
        linhas.extend(
            [
                "",
                "Sinais compatíveis também foram observados em estações regionais.",
            ]
        )
    linhas.extend(
        [
            "",
            "Este é um alerta experimental enviado somente ao administrador para validação do sistema.",
            "",
            "Os dados de radar representam uma estimativa e não garantem ocorrência de chuva.",
        ]
    )
    return "\n".join(linhas)


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


def _atualizar_identidade(estado, snapshot, agora):
    alerta = snapshot.get("alerta_preventivo") or {}
    estado["last_level"] = alerta.get("nivel")
    estado["last_track_id"] = alerta.get("track_id")
    estado["last_cluster_id"] = alerta.get("cluster_id")
    estado["last_distance_km"] = _numero_finito(alerta.get("distance_km"))
    estado["last_seen_at"] = iso_utc(agora)


def _processar_saida_vermelho(estado, config, agora):
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
    if avaliacao["reason"] == "disabled":
        logger.info("Nowcasting teste admin: teste desabilitado")
        return obter_status_alerta_teste_admin(snapshot, config, now=agora)
    if avaliacao["reason"] == "admin_phone_missing":
        logger.warning(
            "Alerta preventivo de teste habilitado, mas ADMIN_ALERT_PHONE não está configurado."
        )
        return obter_status_alerta_teste_admin(snapshot, config, now=agora)

    conn = database.get_db()
    deve_enviar = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        estado = carregar_estado_alerta_teste(conn)
        _atualizar_identidade(estado, snapshot, agora)
        nivel_vermelho = (
            (snapshot.get("alerta_preventivo") or {}).get("nivel") == "VERMELHO"
        )

        if avaliacao["reason"] == "local_event_observed":
            if not estado["active"]:
                estado["active"] = True
                estado["event_key"] = avaliacao["event_key"]
            estado["clear_since"] = None
            estado["suppressed_for_current_episode"] = True
            estado["last_result"] = "local_event_observed"
            logger.info("Nowcasting teste admin: envio ignorado por evento local observado")
        elif not avaliacao["eligible"]:
            if nivel_vermelho and estado["active"]:
                estado["clear_since"] = None
            else:
                _processar_saida_vermelho(estado, config, agora)
            estado["last_result"] = (
                estado["last_result"]
                if estado["last_result"] in {"rearm_pending", "rearmed"}
                else avaliacao["reason"]
            )
            if avaliacao["reason"] == "snapshot_stale":
                logger.info("Nowcasting teste admin: envio ignorado por snapshot stale")
        else:
            logger.info(
                "Nowcasting teste admin: candidato vermelho detectado track=%s distancia=%s",
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
            elif estado["sent_for_current_episode"]:
                estado["last_result"] = "episode_already_notified"
                logger.info(
                    "Nowcasting teste admin: envio ignorado porque o episódio já foi notificado"
                )
            elif _cooldown_ativo(estado, config, agora):
                estado["last_result"] = "cooldown"
                logger.info("Nowcasting teste admin: envio ignorado por cooldown")
            else:
                estado["last_attempt_at"] = iso_utc(agora)
                estado["last_result"] = "sending"
                estado["last_error"] = None
                deve_enviar = True

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
            _finalizar_tentativa(enviado=True, erro=None, now=agora)

    return obter_status_alerta_teste_admin(snapshot, config, now=agora)


def _finalizar_tentativa(*, enviado, erro, now):
    conn = database.get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        estado = carregar_estado_alerta_teste(conn)
        if enviado:
            estado["last_sent_at"] = iso_utc(now)
            estado["last_result"] = "sent"
            estado["last_error"] = None
            estado["sent_for_current_episode"] = True
        else:
            estado["last_result"] = "send_failed"
            estado["last_error"] = erro
            estado["sent_for_current_episode"] = False
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
    agora = (now or agora_utc()).astimezone(timezone.utc)
    estado = carregar_estado_alerta_teste()
    avaliacao = avaliar_alerta_teste_admin(
        snapshot,
        config,
        admin_phone=obter_admin_alert_phone(),
        now=agora,
    )
    cooldown = _cooldown_ativo(estado, config, agora)
    rearm_pending = bool(estado["active"] and estado.get("clear_since"))
    eligible = bool(
        avaliacao["eligible"]
        and not estado["sent_for_current_episode"]
        and not estado["suppressed_for_current_episode"]
        and not cooldown
    )
    reason = estado["last_result"] if config.get("test_alerts_enabled") else "disabled"
    if not avaliacao["eligible"]:
        reason = avaliacao["reason"]
    return {
        "enabled": config.get("test_alerts_enabled") is True,
        "eligible": eligible,
        "sent_for_current_episode": bool(estado["sent_for_current_episode"]),
        "event_key": estado["event_key"] or avaliacao.get("event_key"),
        "last_sent_at": estado["last_sent_at"],
        "cooldown_active": cooldown,
        "rearm_pending": rearm_pending,
        "reason": reason,
    }
