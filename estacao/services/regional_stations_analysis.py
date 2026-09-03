"""Geografia, freshness e tendencias da rede regional de observacao."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Sequence

from time_utils import agora_utc, parse_datetime


EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_graus(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def direcao_cardinal(bearing):
    nomes = ("N", "NE", "L", "SE", "S", "SO", "O", "NO")
    return nomes[int((bearing + 22.5) // 45) % 8]


def diferenca_angular_graus(anterior, atual):
    if anterior is None or atual is None:
        return None
    diferenca = (float(atual) - float(anterior) + 180) % 360 - 180
    return 180.0 if diferenca == -180 else diferenca


def _dt(row):
    return parse_datetime(
        row.get("sample_time_utc")
        or row.get("tempo_referencia_utc")
        or row.get("medido_em_utc"),
        assume_utc=True,
    )


def _referencia(rows, atual_dt, horas):
    alvo = atual_dt - timedelta(hours=horas)
    candidatos = []
    for row in rows[1:]:
        momento = _dt(row)
        if momento and momento < atual_dt:
            intervalo = (atual_dt - momento).total_seconds()
            intervalo_minimo = max(45 * 60, horas * 3600 - 45 * 60)
            if intervalo < intervalo_minimo:
                continue
            diferenca = abs((momento - alvo).total_seconds())
            if diferenca <= 45 * 60:
                candidatos.append((diferenca, row))
    return min(candidatos, default=(None, None), key=lambda item: item[0])[1]


def _delta(atual, anterior, campo):
    a = atual.get(campo)
    b = anterior.get(campo) if anterior else None
    return None if a is None or b is None else float(a) - float(b)


def calcular_tendencias(observacoes: Sequence[dict]):
    validas = [dict(row) for row in observacoes if _dt(dict(row))]
    validas.sort(key=lambda row: _dt(row), reverse=True)
    vazio = {
        "temperatura_1h": None, "temperatura_3h": None,
        "umidade_1h": None, "umidade_3h": None,
        "pressao_1h": None, "pressao_3h": None,
        "vento_1h": None, "rajada_1h": None,
        "direcao_vento_1h": None,
        "chuva_1h": None, "chuva_3h": None, "chuva_6h": None,
    }
    if not validas:
        return vazio
    atual = validas[0]
    atual_dt = _dt(atual)
    refs = {horas: _referencia(validas, atual_dt, horas) for horas in (1, 3, 6)}
    resultado = dict(vazio)
    for campo_saida, campo in (
        ("temperatura", "temperatura_atual"),
        ("umidade", "umidade_atual"),
        ("pressao", "pressao_atual"),
    ):
        for horas in (1, 3):
            resultado[f"{campo_saida}_{horas}h"] = _delta(atual, refs[horas], campo)
    resultado["vento_1h"] = _delta(atual, refs[1], "vento_velocidade_kmh")
    resultado["rajada_1h"] = _delta(atual, refs[1], "rajada_kmh")
    resultado["direcao_vento_1h"] = diferenca_angular_graus(
        refs[1].get("vento_direcao_graus") if refs[1] else None,
        atual.get("vento_direcao_graus"),
    )
    for horas in (1, 3, 6):
        if refs[horas] is None:
            continue
        inicio = atual_dt - timedelta(hours=horas)
        chuvas = []
        origens_contadas = set()
        for row in validas:
            momento = _dt(row)
            if not (momento and inicio < momento <= atual_dt):
                continue
            chuva = row.get("chuva_mm")
            if chuva is None:
                continue
            # O mesmo registro da fonte pode permanecer visível em vários polls
            # ou buckets. Ele representa uma leitura, não novos acumulados.
            origem = (
                row.get("source_observation_id")
                or row.get("fingerprint")
                or row.get("id")
                or (momento.isoformat(), float(chuva))
            )
            if origem in origens_contadas:
                continue
            origens_contadas.add(origem)
            chuvas.append(float(chuva))
        resultado[f"chuva_{horas}h"] = sum(chuvas) if chuvas else None
    return resultado


def qualidade_tendencias(observacoes: Sequence[dict]):
    """Qualifica somente a cobertura temporal, sem fabricar valores ausentes."""
    validas = [dict(row) for row in observacoes if _dt(dict(row))]
    validas.sort(key=lambda row: _dt(row), reverse=True)
    if len(validas) < 2:
        return "INSUFFICIENT"
    atual_dt = _dt(validas[0])
    if _referencia(validas, atual_dt, 1) is not None:
        return "GOOD"
    return "PARTIAL"


def idade_minutos_observacao(observacao, now=None):
    if not observacao:
        return None
    medido = _dt(observacao)
    if not medido:
        return None
    now = (now or agora_utc()).astimezone(timezone.utc)
    segundos = (now - medido.astimezone(timezone.utc)).total_seconds()
    if segundos < -15 * 60:
        return None
    return max(0, int(segundos // 60))


def classificar_status(
    observacao,
    source_status="OK",
    stale_minutes=120,
    very_stale_minutes=240,
    now=None,
):
    if source_status in {"ERRO_FONTE", "AUSENTE"}:
        return "ERRO_FONTE"
    if not observacao:
        return "SEM_DADOS"
    idade = idade_minutos_observacao(observacao, now=now)
    if idade is None:
        return "TIMESTAMP_INDEFINIDO"
    if idade > very_stale_minutes:
        return "MUITO_ATRASADA"
    if idade > stale_minutes:
        return "ATRASADA"
    return "OK"
