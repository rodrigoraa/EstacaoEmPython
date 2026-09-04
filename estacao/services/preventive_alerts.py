"""Classificacao visual e conservadora de proximidade dos ecos do radar.

Este modulo nao possui integracao com WhatsApp nem efeitos de persistencia. Ele
apenas transforma dados observacionais ja validados em um estado explicavel
para as telas administrativas e para o snapshot do nowcasting.
"""

from __future__ import annotations

import math
from statistics import median

from time_utils import parse_datetime


CLUTTER_FORTE_LIMIAR = 0.75
ETA_BORDA_MAX_MINUTOS = 360.0
NIVEIS_CORES = {
    "NORMAL": "verde",
    "AMARELO": "amarelo",
    "LARANJA": "laranja",
    "VERMELHO": "vermelho",
}


def classificar_nivel_proximidade(distancia_km):
    """Classifica pela borda do eco, preservando os limites inclusivos."""
    if distancia_km is None:
        return "NORMAL"
    try:
        distancia = float(distancia_km)
    except (TypeError, ValueError):
        return "NORMAL"
    if not math.isfinite(distancia) or distancia < 0 or distancia > 100:
        return "NORMAL"
    if distancia <= 25:
        return "VERMELHO"
    if distancia <= 50:
        return "LARANJA"
    return "AMARELO"


def possui_clutter_forte(ameaca):
    indice = (ameaca or {}).get("indice_persistencia_clutter")
    try:
        return bool(indice is not None and float(indice) >= CLUTTER_FORTE_LIMIAR)
    except (TypeError, ValueError):
        return False


def qualidade_tracking(track, tracking_valido):
    if not tracking_valido:
        return "DADOS_INSUFICIENTES"
    frames = int((track or {}).get("quantidade_frames") or 0)
    duracao = float((track or {}).get("duracao_minutos") or 0)
    if frames >= 6 and duracao >= 25:
        return "BOA"
    return "SUFICIENTE"


def _observacoes_borda(historico):
    observacoes = {}
    for item in historico or []:
        momento = parse_datetime(item.get("data_frame"), assume_utc=True)
        try:
            distancia = float(item.get("distancia_borda_km"))
        except (TypeError, ValueError):
            continue
        if momento and math.isfinite(distancia) and distancia >= 0:
            observacoes[momento] = distancia
    return sorted(observacoes.items())[-8:]


def estimar_eta_borda(
    historico,
    *,
    tracking_valido,
    aproximando,
    trajetoria_compativel,
    min_frames=3,
    min_duration_minutes=10.0,
    max_speed_kmh=150.0,
):
    """Estima chegada da borda por uma tendência robusta do mesmo track.

    A mediana das taxas entre todos os pares reduz a influência de um único
    frame ruidoso. A estimativa é suprimida quando os passos recentes não são
    predominantemente convergentes ou quando sai da janela de curtíssimo prazo.
    """
    if not (tracking_valido and aproximando and trajetoria_compativel):
        return None
    observacoes = _observacoes_borda(historico)
    minimo = max(3, int(min_frames))
    if len(observacoes) < minimo:
        return None
    duracao_horas = (observacoes[-1][0] - observacoes[0][0]).total_seconds() / 3600
    if duracao_horas <= 0 or duracao_horas * 60 < float(min_duration_minutes):
        return None

    taxas_passos = []
    for (momento_a, distancia_a), (momento_b, distancia_b) in zip(
        observacoes, observacoes[1:]
    ):
        horas = (momento_b - momento_a).total_seconds() / 3600
        if horas > 0:
            taxas_passos.append((distancia_a - distancia_b) / horas)
    if len(taxas_passos) < minimo - 1:
        return None
    consistencia = sum(taxa >= 1.0 for taxa in taxas_passos) / len(taxas_passos)
    if consistencia < 0.75:
        return None

    taxas_pares = []
    for indice, (momento_a, distancia_a) in enumerate(observacoes[:-1]):
        for momento_b, distancia_b in observacoes[indice + 1 :]:
            horas = (momento_b - momento_a).total_seconds() / 3600
            if horas > 0:
                taxas_pares.append((distancia_a - distancia_b) / horas)
    taxa_robusta = median(taxas_pares) if taxas_pares else None
    if (
        taxa_robusta is None
        or taxa_robusta < 1.0
        or taxa_robusta > float(max_speed_kmh)
    ):
        return None

    desvio_mediano = median(abs(taxa - taxa_robusta) for taxa in taxas_passos)
    if desvio_mediano > max(10.0, taxa_robusta * 0.75):
        return None

    distancia_atual = observacoes[-1][1]
    eta_minutos = distancia_atual / taxa_robusta * 60
    if not math.isfinite(eta_minutos) or eta_minutos > ETA_BORDA_MAX_MINUTOS:
        return None
    return {
        "eta_minutes": round(max(0.0, eta_minutos), 1),
        "approach_rate_kmh": round(taxa_robusta, 1),
        "sample_count": len(observacoes),
        "duration_minutes": round(duracao_horas * 60, 1),
        "quality": "BOA" if consistencia >= 0.9 and len(observacoes) >= 5 else "MODERADA",
    }


def _mensagem_nivel(nivel, distancia):
    if nivel == "AMARELO":
        return f"ATENÇÃO: eco meteorológico detectado a {distancia:.1f} km da escola."
    if nivel == "LARANJA":
        return f"ATENÇÃO ELEVADA: eco meteorológico a {distancia:.1f} km da escola."
    if nivel == "VERMELHO":
        return (
            "ALERTA PREVENTIVO: possível chuva próxima. "
            f"Eco a {distancia:.1f} km da escola."
        )
    return "Sem eco relevante dentro de 100 km."


def criar_alerta_preventivo(
    ameaca_principal,
    *,
    radar_atualizado,
    evento_local,
    confirmacao_regional=None,
):
    """Monta o estado visual; nunca enfileira ou envia mensagens."""
    ameaca = ameaca_principal or {}
    confirmacao = confirmacao_regional or {
        "confirmada": False,
        "stations": [],
        "evidence_count": 0,
    }
    distancia = ameaca.get("distance_km")
    try:
        distancia = float(distancia) if distancia is not None else None
        if distancia is not None and not math.isfinite(distancia):
            distancia = None
    except (TypeError, ValueError):
        distancia = None
    nivel_base = (
        classificar_nivel_proximidade(distancia) if radar_atualizado else "NORMAL"
    )
    clutter_forte = possui_clutter_forte(ameaca)
    nivel = nivel_base
    if clutter_forte and nivel_base in {"LARANJA", "VERMELHO"}:
        nivel = "AMARELO"

    if evento_local:
        mensagem = "Chuva já observada na EE São José."
    elif not radar_atualizado and ameaca:
        mensagem = "Radar desatualizado; nível preventivo indisponível."
    elif clutter_forte and nivel_base != "NORMAL":
        mensagem = (
            f"Eco a {distancia:.1f} km com baixa confiabilidade por "
            "persistência de clutter; mantido em monitoramento diagnóstico."
        )
    else:
        mensagem = _mensagem_nivel(nivel, distancia or 0)
        if nivel == "VERMELHO":
            mensagem += (
                " Sinais também observados em estações regionais."
                if confirmacao.get("confirmada")
                else " Confirmação regional ainda não disponível."
            )

    would_send = bool(
        radar_atualizado and nivel == "VERMELHO" and not clutter_forte
    )
    return {
        "nivel": nivel,
        "nivel_base": nivel_base,
        "cor": NIVEIS_CORES[nivel],
        "distance_km": distancia,
        "track_id": ameaca.get("track_id"),
        "tracking_valid": bool(ameaca.get("tracking_valid")),
        "tracking_quality": ameaca.get("tracking_quality", "DADOS_INSUFICIENTES"),
        "frame_count": ameaca.get("frame_count"),
        "duration_minutes": ameaca.get("duration_minutes"),
        "approaching": ameaca.get("approaching"),
        "trajectory_compatible": bool(ameaca.get("trajectory_compatible")),
        "direction": ameaca.get("direction"),
        "speed_kmh": ameaca.get("speed_kmh"),
        "eta_minutes": ameaca.get("eta_minutes"),
        "eta_border_minutes": ameaca.get("eta_border_minutes"),
        "border_approach_rate_kmh": ameaca.get("border_approach_rate_kmh"),
        "clutter": bool(ameaca.get("suspeito_clutter")),
        "clutter_index": ameaca.get("indice_persistencia_clutter"),
        "low_confidence": clutter_forte,
        "regional_confirmation": bool(confirmacao.get("confirmada")),
        "regional_stations": list(confirmacao.get("stations") or []),
        "local_event": bool(evento_local),
        "message": mensagem,
        "would_send": would_send,
        "preventive_sending": "DESATIVADO",
        "simulation_message": (
            "Este evento seria candidato a alerta por WhatsApp, mas o envio está desativado."
            if would_send
            else "Nenhum alerta preventivo será enviado por esta versão."
        ),
    }
