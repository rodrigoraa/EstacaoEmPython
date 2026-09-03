"""Worker independente da rede regional PIN-MS."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env", encoding="utf-8")
load_dotenv(encoding="utf-8")

import database
from config import regional_stations_config
from logging_utils import configurar_logging, erro_externo_seguro
from services.regional_stations_catalog import REGIONAL_STATIONS, REGIONAL_STATION_CODES
from services.regional_stations_repository import (
    obter_estado_rede,
    registrar_status_estacao,
    salvar_observacao,
)
from services.regional_stations_service import (
    PinMsRegionalClient,
    RegionalStationsError,
    normalizar_registro,
)
from time_utils import agora_utc


logger = logging.getLogger(__name__)


def _attributes(payload):
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise RegionalStationsError("Resposta ArcGIS sem features")
    for feature in features:
        if isinstance(feature, dict) and isinstance(feature.get("attributes"), dict):
            yield feature["attributes"]


def enfileirar_alertas_regionais(config, _estado):
    """A rede e apenas observacional nesta fase e nunca cria alertas."""
    if config.get("alerts_enabled"):
        logger.warning(
            "REGIONAL_STATIONS_ALERTS_ENABLED=true foi ignorado: alertas regionais "
            "ainda nao foram habilitados"
        )
    return 0


def executar_ciclo(client=None, config=None):
    config = config or regional_stations_config()
    if not config["enabled"]:
        return {
            "disabled": True, "configured": 6, "found": 0, "with_current_data": 0,
            "new": 0, "duplicates": 0, "empty": 0, "errors": 0, "state": None,
            "time_diagnostics": [],
        }
    database.init_db()
    client = client or PinMsRegionalClient(
        timeout=config["timeout_seconds"],
        bootstrap_hours=config["bootstrap_hours"],
    )
    collected_at = agora_utc()
    atuais = {}
    diagnosticos_tempo = {}

    def registrar_diagnostico(observacao):
        if not observacao:
            return
        chave = (observacao.station_code, observacao.source_layer)
        atual = diagnosticos_tempo.get(chave)
        candidato = observacao.medido_em_utc or ""
        if atual is None or candidato > (atual.get("resolved_utc") or ""):
            diagnosticos_tempo[chave] = {
                "code": observacao.station_code,
                "layer": observacao.source_layer,
                "raw_date": observacao.source_dt_medicao_raw,
                "raw_hour": observacao.source_hr_medicao_raw,
                "resolved_utc": observacao.medido_em_utc,
                "resolved_local": observacao.medido_em_local,
                "timestamp_status": observacao.timestamp_status,
            }
    encontrados = set()
    erros = 0
    current_error = None
    try:
        payload_atual = client.obter_atuais()
        for raw in _attributes(payload_atual):
            code = str(raw.get("CD_ESTACAO") or "").strip().upper()
            if code not in REGIONAL_STATIONS:
                continue
            encontrados.add(code)
            try:
                atuais[code] = normalizar_registro(raw, 0, collected_at)
                registrar_diagnostico(atuais[code])
            except RegionalStationsError:
                atuais[code] = None
    except RegionalStationsError as erro:
        erros += 1
        current_error = erro_externo_seguro(erro)

    novos = duplicados = vazios = 0
    for code in REGIONAL_STATION_CODES:
        problema = None
        historico_valido = False
        atual = atuais.get(code)
        if atual:
            if salvar_observacao(atual):
                novos += 1
            else:
                duplicados += 1
        elif code in encontrados:
            vazios += 1

        try:
            historico = client.obter_historico(code)
            for raw in _attributes(historico):
                observacao = normalizar_registro(raw, 2, collected_at)
                if observacao is None:
                    vazios += 1
                    continue
                registrar_diagnostico(observacao)
                historico_valido = True
                if salvar_observacao(observacao):
                    novos += 1
                else:
                    duplicados += 1
        except (RegionalStationsError, ValueError) as erro:
            erros += 1
            problema = erro_externo_seguro(erro)

        if current_error:
            registrar_status_estacao(code, "ERRO_FONTE", current_error)
        elif code not in encontrados:
            registrar_status_estacao(code, "AUSENTE", "Estacao ausente na camada atual")
        elif problema:
            registrar_status_estacao(code, "ERRO_FONTE", problema)
        elif atual is None and not historico_valido:
            registrar_status_estacao(code, "SEM_DADOS", None, sucesso=True)
        else:
            registrar_status_estacao(code, "OK", None, sucesso=True)

    estado = obter_estado_rede(config)
    enfileirar_alertas_regionais(config, estado)
    resultado = {
        "disabled": False,
        "configured": len(REGIONAL_STATIONS),
        "found": len(encontrados),
        "with_current_data": sum(obs is not None for obs in atuais.values()),
        "new": novos,
        "duplicates": duplicados,
        "empty": vazios,
        "errors": erros,
        "state": estado,
        "time_diagnostics": [
            diagnosticos_tempo[chave] for chave in sorted(diagnosticos_tempo)
        ],
    }
    logger.info(
        "Coleta PIN-MS: configuradas=%s retornadas=%s atuais=%s novas=%s "
        "duplicadas=%s vazias=%s erros=%s",
        resultado["configured"], resultado["found"], resultado["with_current_data"],
        novos, duplicados, vazios, erros,
    )
    return resultado


def imprimir_resumo(resultado, verbose_time=False):
    print("Estações regionais PIN-MS")
    print("==========================")
    if resultado["disabled"]:
        print("Coleta desabilitada (REGIONAL_STATIONS_ENABLED=false).")
        return
    for station in (resultado.get("state") or {}).get("stations", []):
        obs = station.get("observation") or {}
        print(f"\n{station['name']} {station['code']}")
        print(f"  atual: {'OK' if obs else 'SEM DADOS'}")
        print(f"  temperatura: {_valor(obs.get('temperature'), '°C')}")
        print(f"  umidade: {_valor(obs.get('humidity'), '%')}")
        print(f"  pressão: {_valor(obs.get('pressure'), 'hPa')}")
        print(f"  vento: {_valor(obs.get('wind_speed_kmh'), 'km/h')}")
        print(f"  rajada: {_valor(obs.get('wind_gust_kmh'), 'km/h')}")
        print(f"  chuva: {_valor(obs.get('rain_mm'), 'mm')}")
        print(f"  último horário válido: {station.get('last_valid_hourly') or 'indefinido'}")
        print(f"  status: {station['status']}")
    print("\nResumo:")
    for key, label in (
        ("configured", "configuradas"), ("found", "encontradas"),
        ("with_current_data", "com dados atuais"), ("new", "novos registros"),
        ("duplicates", "duplicados"), ("empty", "slots vazios"),
        ("errors", "erros HTTP/fonte"),
    ):
        print(f"{resultado[key]} {label}")
    if verbose_time:
        print("\nDiagnóstico temporal:")
        for item in resultado.get("time_diagnostics", []):
            print(
                f"{item['code']} layer={item['layer']} "
                f"raw_date={item['raw_date'] or '-'} raw_hour={item['raw_hour'] or '-'} "
                f"utc={item['resolved_utc'] or '-'} "
                f"local={item['resolved_local'] or '-'} "
                f"status={item['timestamp_status']}"
            )


def _valor(value, unit):
    return "indisponível" if value is None else f"{value:.1f} {unit}"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Coleta estações regionais do PIN-MS")
    parser.add_argument("--once", action="store_true", help="Executa um ciclo e termina")
    parser.add_argument(
        "--verbose-time",
        action="store_true",
        help="Exibe campos temporais brutos e resolvidos sem alterar a coleta",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configurar_logging()
    config = regional_stations_config()
    if args.once:
        imprimir_resumo(executar_ciclo(config=config), args.verbose_time)
        return 0
    if not config["enabled"]:
        imprimir_resumo(executar_ciclo(config=config), args.verbose_time)
        return 0
    while True:
        try:
            imprimir_resumo(executar_ciclo(config=config), args.verbose_time)
        except Exception as erro:
            logger.error("Ciclo PIN-MS falhou: %s", erro_externo_seguro(erro))
        time.sleep(config["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
