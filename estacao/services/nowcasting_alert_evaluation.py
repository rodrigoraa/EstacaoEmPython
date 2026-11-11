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
    momento_frame = parse_datetime(radar.get("data_frame"), assume_utc=True)
    if momento_frame is None:
        return {"eligible": False, "reason": "invalid_frame_timestamp", "decision": None}
    idade_frame = (agora - momento_frame.astimezone(timezone.utc)).total_seconds() / 60.0
    limite_idade = _numero_finito(config.get("radar_max_age_minutes", 15))
    if limite_idade is None or limite_idade <= 0:
        limite_idade = 15
    if idade_frame < -1 or idade_frame > limite_idade:
        return {"eligible": False, "reason": "radar_frame_stale", "decision": None}
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
    """Distingue presença no raio monitorado de uma previsão de entrada futura."""
    alerta = (snapshot or {}).get("alerta_preventivo") or {}
    confirmado = tracking_confirmado(alerta)
    distancia = _medida_nao_negativa(alerta.get("distance_km"))
    eta = _medida_nao_negativa(alerta.get("projected_impact_eta_minutes"))
    projecao_confiavel = (
        confirmado and alerta.get("projected_impact") is True
        and eta is not None and eta <= 120
        and alerta.get("trajectory_confidence") in {"MODERADA", "ALTA"}
    )
    presente = projecao_confiavel and eta == 0
    futura = projecao_confiavel and eta > 0
    nivel = alerta.get("alert_level")
    if nivel == "ALERTA":
        titulo = "🔴 Atenção para possibilidade de chuva forte no Distrito de São José."
        area = "Uma área de chuva intensa"
    elif nivel == "ATENCAO":
        titulo = "⚠️ Atenção para possível chuva no Distrito de São José."
        area = "Uma área de chuva com maior intensidade"
    else:
        titulo = "🌧️ Possibilidade de chuva no Distrito de São José."
        area = "Uma área de chuva"
    if presente:
        corpo = f"{area} detectada pelo radar já está na área monitorada."
    elif futura:
        corpo = f"{area} detectada pelo radar está se aproximando da área monitorada."
    else:
        corpo = f"{area} foi detectada pelo radar."
    if distancia is None:
        localizacao = "Distância da área de chuva à estação indisponível."
    elif distancia == 0:
        localizacao = "A parte mais próxima da área de chuva detectada está sobre ou muito próxima da estação."
    elif distancia < 1:
        localizacao = "A parte mais próxima da área de chuva detectada está a menos de 1 km da estação."
    else:
        localizacao = f"A parte mais próxima da área de chuva detectada está a aproximadamente {distancia:.0f} km da estação."
    partes = [titulo, corpo, localizacao]
    if presente:
        partes.append("Isso não confirma chuva no solo na estação.")
    elif futura:
        tempo = "menos de 1 min" if eta < 1 else f"aproximadamente {eta:.0f} min"
        partes.append(f"Estimativa de entrada na área monitorada: {tempo}.")
        partes.append("Essa estimativa não prevê a chegada exata da chuva à estação.")
    else:
        partes.append("Ainda não há estimativa confiável de entrada na área monitorada.")
    partes.append("Para mais informações acesse:\nhttps://meteo.eesjv.com.br")
    return "\n\n".join(partes)


def _medida_nao_negativa(valor):
    """Não converte ausência, booleanos ou medidas inválidas em zero no texto."""
    numero = None if isinstance(valor, bool) else _numero_finito(valor)
    return numero if numero is not None and numero >= 0 else None

