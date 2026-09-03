"""Cliente e normalizacao conservadora das estacoes publicas do PIN-MS."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import requests

from services.regional_stations_catalog import (
    REGIONAL_STATIONS,
    REGIONAL_STATION_CODES,
)
from time_utils import LOCAL_TZ, agora_utc, iso_local, iso_utc


PIN_MS_BASE_URL = (
    "https://www.pinms.ms.gov.br/arcgis/rest/services/publico/"
    "Estacoes_CEMADEN_INMET/MapServer"
)
METEOROLOGICAL_FIELDS = (
    "TEM_INS", "TEM_MIN", "TEM_MAX", "UMD_INS", "UMD_MIN", "UMD_MAX",
    "PRE_INS", "PRE_MIN", "PRE_MAX", "VEN_VEL", "VEN_RAJ", "CHUVA",
)
SOURCE_UNITS = {
    "temperature": "°C",
    "humidity": "%",
    "pressure": "hPa",
    "wind": "m/s",
    "wind_direction": "graus",
    "rain": "mm",
    "radiation": "kJ/m²",
}


class RegionalStationsError(RuntimeError):
    """Erro externo sanitizado da fonte regional."""


@dataclass(frozen=True)
class ParsedTimestamp:
    measured_utc: datetime | None
    measured_local: datetime | None
    status: str


@dataclass(frozen=True)
class RegionalObservation:
    station_code: str
    source_layer: int
    source_dt_medicao_raw: str | None
    source_hr_medicao_raw: str | None
    medido_em_utc: str | None
    medido_em_local: str | None
    timestamp_status: str
    coletado_em_utc: str
    coletado_em_local: str
    temperatura_atual: float | None
    temperatura_min: float | None
    temperatura_max: float | None
    umidade_atual: float | None
    umidade_min: float | None
    umidade_max: float | None
    pressao_atual: float | None
    pressao_min: float | None
    pressao_max: float | None
    vento_direcao_graus: float | None
    vento_velocidade_raw: float | None
    vento_velocidade_ms: float | None
    vento_velocidade_kmh: float | None
    rajada_raw: float | None
    rajada_ms: float | None
    rajada_kmh: float | None
    chuva_mm: float | None
    radiacao_raw: float | None
    radiacao_unidade: str | None
    latitude: float
    longitude: float
    latitude_fonte: float | None
    longitude_fonte: float | None
    nome_fonte: str | None
    orgao: str | None
    fingerprint: str
    payload_json: str
    qualidade: str


def normalizar_valor_meteorologico(valor: Any) -> float | None:
    if valor is None or isinstance(valor, bool):
        return None
    texto = str(valor).strip()
    if not texto or texto.lower() in {"null", "none", "nan", "n/a", "-"}:
        return None
    if "," in texto and "." not in texto:
        texto = texto.replace(",", ".")
    try:
        numero = float(texto)
    except (TypeError, ValueError):
        return None
    if abs(numero) >= 9999:
        return None
    return numero


def registro_tem_dados_meteorologicos(attributes: dict[str, Any]) -> bool:
    return any(
        normalizar_valor_meteorologico(attributes.get(campo)) is not None
        for campo in METEOROLOGICAL_FIELDS
    )


def _raw(valor: Any) -> str | None:
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def _parse_dt_fonte(valor: Any) -> datetime | None:
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)) or str(valor).strip().isdigit():
        try:
            numero = float(valor)
            if abs(numero) < 100_000_000_000:
                return None
            return datetime.fromtimestamp(numero / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    texto = str(valor).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", texto):
        return datetime.combine(date.fromisoformat(texto), time(), tzinfo=timezone.utc)
    if texto.endswith("Z"):
        texto = texto[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_hora(valor: Any) -> time | None:
    texto = _raw(valor)
    if not texto:
        return None
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?", texto)
    if not match:
        return None
    return time(int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def interpretar_timestamp(
    dt_raw: Any,
    hr_raw: Any,
    collected_at: datetime | None = None,
    max_future_minutes: float = 90.0,
    date_only_without_hour: bool = False,
) -> ParsedTimestamp:
    """Nunca interpreta formatos dia/mes ambiguos nem inventa hora de medicao."""
    base = _parse_dt_fonte(dt_raw)
    hora = _parse_hora(hr_raw)
    if date_only_without_hour and base and not _raw(hr_raw):
        return ParsedTimestamp(None, None, "date_only")
    if base and hora:
        base_hora = base.timetz().replace(tzinfo=None)
        if base_hora == hora:
            combinado = base
            status = "valid"
        elif base_hora == time():
            combinado = datetime.combine(base.date(), hora, tzinfo=timezone.utc)
            status = "reconciled"
        else:
            return ParsedTimestamp(None, None, "suspect")
        if collected_at is not None:
            coletado = collected_at
            if coletado.tzinfo is None:
                coletado = coletado.replace(tzinfo=timezone.utc)
            if combinado > coletado.astimezone(timezone.utc) + timedelta(
                minutes=max(0.0, float(max_future_minutes))
            ):
                return ParsedTimestamp(None, None, "suspect")
        return ParsedTimestamp(combinado, combinado.astimezone(LOCAL_TZ), status)
    if base and not _raw(hr_raw):
        if base.hour == base.minute == base.second == 0:
            return ParsedTimestamp(None, None, "date_only")
        if collected_at is not None:
            coletado = collected_at
            if coletado.tzinfo is None:
                coletado = coletado.replace(tzinfo=timezone.utc)
            if base > coletado.astimezone(timezone.utc) + timedelta(
                minutes=max(0.0, float(max_future_minutes))
            ):
                return ParsedTimestamp(None, None, "suspect")
        return ParsedTimestamp(base, base.astimezone(LOCAL_TZ), "valid")
    if base or _raw(hr_raw):
        return ParsedTimestamp(None, None, "suspect")
    return ParsedTimestamp(None, None, "unknown")


def _coordenada_fonte(valor: Any, latitude: bool) -> float | None:
    numero = normalizar_valor_meteorologico(valor)
    limite = 90 if latitude else 180
    return numero if numero is not None and -limite <= numero <= limite else None


def normalizar_registro(
    attributes: dict[str, Any], source_layer: int, collected_at: datetime | None = None
) -> RegionalObservation | None:
    if not isinstance(attributes, dict):
        raise RegionalStationsError("Registro ArcGIS invalido")
    code = str(attributes.get("CD_ESTACAO") or "").strip().upper()
    if code not in REGIONAL_STATIONS:
        raise RegionalStationsError("Codigo de estacao fora da allowlist")
    if source_layer not in {0, 2}:
        raise RegionalStationsError("Camada regional nao permitida")
    if not registro_tem_dados_meteorologicos(attributes):
        return None

    station = REGIONAL_STATIONS[code]
    collected_at = (collected_at or agora_utc()).astimezone(timezone.utc)
    timestamp = interpretar_timestamp(
        attributes.get("DT_MEDICAO"), attributes.get("HR_MEDICAO"), collected_at,
        date_only_without_hour=source_layer == 0,
    )
    vento_raw = normalizar_valor_meteorologico(attributes.get("VEN_VEL"))
    rajada_raw = normalizar_valor_meteorologico(attributes.get("VEN_RAJ"))
    radiacao = normalizar_valor_meteorologico(attributes.get("RAD_GLO"))
    campos = {
        "temperatura_atual": normalizar_valor_meteorologico(attributes.get("TEM_INS")),
        "temperatura_min": normalizar_valor_meteorologico(attributes.get("TEM_MIN")),
        "temperatura_max": normalizar_valor_meteorologico(attributes.get("TEM_MAX")),
        "umidade_atual": normalizar_valor_meteorologico(attributes.get("UMD_INS")),
        "umidade_min": normalizar_valor_meteorologico(attributes.get("UMD_MIN")),
        "umidade_max": normalizar_valor_meteorologico(attributes.get("UMD_MAX")),
        "pressao_atual": normalizar_valor_meteorologico(attributes.get("PRE_INS")),
        "pressao_min": normalizar_valor_meteorologico(attributes.get("PRE_MIN")),
        "pressao_max": normalizar_valor_meteorologico(attributes.get("PRE_MAX")),
        "vento_direcao_graus": normalizar_valor_meteorologico(attributes.get("VEN_DIR")),
        "vento_velocidade_raw": vento_raw,
        "vento_velocidade_ms": vento_raw,
        "vento_velocidade_kmh": vento_raw * 3.6 if vento_raw is not None else None,
        "rajada_raw": rajada_raw,
        "rajada_ms": rajada_raw,
        "rajada_kmh": rajada_raw * 3.6 if rajada_raw is not None else None,
        "chuva_mm": normalizar_valor_meteorologico(attributes.get("CHUVA")),
        "radiacao_raw": radiacao,
    }
    latitude_fonte = _coordenada_fonte(attributes.get("VL_LATITUDE"), True)
    longitude_fonte = _coordenada_fonte(attributes.get("VL_LONGITUDE"), False)
    latitude = latitude_fonte if latitude_fonte is not None else station.configured_lat
    longitude = longitude_fonte if longitude_fonte is not None else station.configured_lon
    source_dt = _raw(attributes.get("DT_MEDICAO"))
    source_hr = _raw(attributes.get("HR_MEDICAO"))
    fingerprint_body = {
        "station": code,
        "layer": source_layer,
        "dt": source_dt,
        "hr": source_hr,
        "values": campos,
        "latitude": latitude,
        "longitude": longitude,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    preenchidos = sum(
        normalizar_valor_meteorologico(attributes.get(campo)) is not None
        for campo in METEOROLOGICAL_FIELDS
    )
    return RegionalObservation(
        station_code=code,
        source_layer=source_layer,
        source_dt_medicao_raw=source_dt,
        source_hr_medicao_raw=source_hr,
        medido_em_utc=iso_utc(timestamp.measured_utc) if timestamp.measured_utc else None,
        medido_em_local=iso_local(timestamp.measured_utc) if timestamp.measured_utc else None,
        timestamp_status=timestamp.status,
        coletado_em_utc=iso_utc(collected_at),
        coletado_em_local=iso_local(collected_at),
        radiacao_unidade=SOURCE_UNITS["radiation"] if radiacao is not None else None,
        latitude=latitude,
        longitude=longitude,
        latitude_fonte=latitude_fonte,
        longitude_fonte=longitude_fonte,
        nome_fonte=_raw(attributes.get("DC_NOME")),
        orgao=_raw(attributes.get("ORGAO")),
        fingerprint=fingerprint,
        payload_json=json.dumps(attributes, ensure_ascii=False, sort_keys=True),
        qualidade="completa" if preenchidos >= 6 else "parcial",
        **campos,
    )


def normalizar_features(payload: Any, source_layer: int, collected_at=None):
    if not isinstance(payload, dict) or payload.get("error"):
        raise RegionalStationsError("ArcGIS retornou erro")
    features = payload.get("features")
    if not isinstance(features, list):
        raise RegionalStationsError("Resposta ArcGIS sem features")
    resultado = []
    for feature in features:
        attributes = feature.get("attributes") if isinstance(feature, dict) else None
        if not isinstance(attributes, dict):
            raise RegionalStationsError("Feature ArcGIS invalida")
        resultado.append(normalizar_registro(attributes, source_layer, collected_at))
    return resultado


class PinMsRegionalClient:
    def __init__(self, timeout=30, bootstrap_hours=24, session=None):
        self.timeout = timeout
        self.bootstrap_hours = min(168, max(6, int(bootstrap_hours)))
        self.session = session or requests.Session()

    def _query(self, layer: int, params: dict[str, Any]):
        if layer not in {0, 2}:
            raise RegionalStationsError("Camada regional nao permitida")
        try:
            response = self.session.get(
                f"{PIN_MS_BASE_URL}/{layer}/query",
                params={**params, "returnGeometry": "false", "f": "json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.Timeout as erro:
            raise RegionalStationsError("Timeout ao consultar o PIN-MS") from erro
        except requests.RequestException as erro:
            raise RegionalStationsError("Falha HTTP ao consultar o PIN-MS") from erro
        try:
            payload = response.json()
        except (TypeError, ValueError) as erro:
            raise RegionalStationsError("PIN-MS retornou JSON invalido") from erro
        if not isinstance(payload, dict) or payload.get("error"):
            raise RegionalStationsError("PIN-MS retornou erro ArcGIS")
        return payload

    def obter_atuais(self):
        codigos = ",".join(f"'{code}'" for code in REGIONAL_STATION_CODES)
        return self._query(0, {"where": f"CD_ESTACAO IN ({codigos})", "outFields": "*"})

    def obter_historico(self, station_code: str):
        code = str(station_code or "").strip().upper()
        if code not in REGIONAL_STATIONS:
            raise RegionalStationsError("Codigo de estacao fora da allowlist")
        return self._query(
            2,
            {
                "where": f"CD_ESTACAO='{code}'",
                "outFields": "*",
                "orderByFields": "DT_MEDICAO DESC, HR_MEDICAO DESC",
                "resultRecordCount": min(336, self.bootstrap_hours * 2),
            },
        )
