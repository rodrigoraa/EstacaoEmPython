"""Avaliação operacional e texto preventivo independentes do destinatário."""
import math
from datetime import timezone
from services.nowcasting_service import chuva_local_atual, snapshot_operacionalmente_atual
from services.preventive_alerts import decidir_alerta_preventivo, tracking_confirmado
from time_utils import agora_utc, parse_datetime

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
    return chuva_local_atual(snapshot.get("escola"))


def avaliar_alerta_preventivo_snapshot(snapshot, config, *, now=None):
    snapshot = snapshot or {}
    if not isinstance(snapshot, dict) or any(
        snapshot.get(campo) is not None and not isinstance(snapshot[campo], dict)
        for campo in ("radar", "alerta_preventivo", "escola")
    ):
        return {"eligible": False, "reason": "invalid_snapshot", "decision": None}
    agora = (now or agora_utc()).astimezone(timezone.utc)
    alerta = snapshot.get("alerta_preventivo") or {}
    radar = snapshot.get("radar") or {}
    if radar.get("stale") is True:
        return {"eligible": False, "reason": "radar_stale", "decision": None}
    if radar.get("operacional") is not True:
        return {"eligible": False, "reason": "radar_unavailable", "decision": None}
    if not snapshot_operacionalmente_atual(snapshot, config, now=agora):
        return {"eligible": False, "reason": "snapshot_stale", "decision": None}
    decisao = decidir_alerta_preventivo(
        alerta, config=config, radar_atualizado=True,
        frame_valido=radar.get("timestamp_status") != "suspect" and radar.get("frame_id") is not None,
        evento_local=_evento_local_observado(snapshot),
    )
    return {
        "eligible": decisao["would_send"], "reason": decisao["block_reason"] or "eligible",
        "decision": decisao,
    }


def montar_mensagem_preventiva(snapshot):
    """Texto público probabilístico, sem detalhes técnicos do diagnóstico."""
    alerta = (snapshot or {}).get("alerta_preventivo") or {}
    confirmado = tracking_confirmado(alerta)
    distancia = _numero_finito(alerta.get("distance_km"))
    movimento = "se aproximando da" if confirmado else "próxima da"
    nivel = alerta.get("alert_level")
    if nivel == "ALERTA":
        titulo = "🔴 Atenção para possibilidade de chuva forte no Distrito de São José."
        corpo = f"Uma área de chuva intensa está {movimento} região."
    elif nivel == "ATENCAO":
        titulo = "⚠️ Atenção para possível chuva no Distrito de São José."
        corpo = f"Uma área de chuva com maior intensidade está {movimento} região."
    else:
        titulo = ("🌧️ Possível chuva se aproximando do Distrito de São José." if confirmado
                  else "🌧️ Possível chuva próxima ao Distrito de São José.")
        corpo = (f"Uma área de chuva está a aproximadamente {distancia:.0f} km da região."
                 if distancia is not None else "Uma área de chuva está próxima da região.")
    partes = [titulo, corpo]
    eta = _numero_finito(alerta.get("eta_border_minutes"))
    if confirmado and eta is not None and 0 <= eta <= 360 and alerta.get("eta_border_quality") in {"BOA", "MODERADA"}:
        partes.append(f"Estimativa de chegada: {eta:.0f} min.")
    partes.append("Para mais informações acesse:\nhttps://meteo.eesjv.com.br")
    return "\n\n".join(partes)

