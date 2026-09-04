"""Worker independente de fusao observacional; nao consulta a internet."""

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
from config import nowcasting_config
from logging_utils import configurar_logging, erro_externo_seguro
from services.nowcasting_repository import (
    carregar_entradas_nowcasting,
    salvar_snapshot,
)
from services.nowcasting_service import analisar_nowcasting


logger = logging.getLogger(__name__)


def enfileirar_alertas_nowcasting(config, _estado):
    """Trava deliberada: esta versao nunca cria fila ou envia mensagens."""
    if config.get("alerts_enabled"):
        logger.warning("alertas preventivos ainda não habilitados")
    return 0


def executar_ciclo(config=None):
    config = config or nowcasting_config()
    if not config["enabled"]:
        return {"disabled": True, "snapshot": None, "new": False}
    database.init_db()
    radar, regional, local, fingerprint = carregar_entradas_nowcasting(config)
    estado = analisar_nowcasting(radar, regional, local, config)
    snapshot_id = salvar_snapshot(estado, fingerprint)
    enfileirar_alertas_nowcasting(config, estado)
    codigos = ",".join(
        station["code"] for station in estado["estacoes_relevantes"]
    ) or "nenhuma"
    logger.info(
        "Nowcasting: track=%s distancia_borda=%s status=%s evidencia=%s "
        "alerta_visual=%s candidato_simulado=%s ameacas=%s "
        "estacoes_relevantes=%s snapshot=%s",
        estado["radar"].get("track_id"),
        estado["radar"].get("distancia_borda_km"),
        estado["status"],
        estado["nivel_evidencia"],
        estado["alerta_preventivo"]["nivel"],
        estado["alerta_preventivo"]["would_send"],
        len(estado.get("ameacas", [])),
        codigos,
        "novo" if snapshot_id else "inalterado",
    )
    return {
        "disabled": False,
        "snapshot": estado,
        "new": snapshot_id is not None,
        "snapshot_id": snapshot_id,
    }


def imprimir_resumo(result):
    print("Nowcasting observacional")
    if result["disabled"]:
        print("Desabilitado (NOWCASTING_ENABLED=false).")
        return
    state = result["snapshot"]
    print(f"Status: {state['status']}")
    print(f"Evidencia: {state['nivel_evidencia']} ({state['indice_evidencia']}/100)")
    print(
        "Alerta visual: "
        f"{state['alerta_preventivo']['nivel']} "
        "(envio preventivo desativado)"
    )
    print(f"Ameacas em monitoramento: {len(state.get('ameacas', []))}")
    print(f"Snapshot: {'novo' if result['new'] else 'entrada inalterada'}")
    for evidence in state["evidencias"]:
        print(f"- {evidence}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Motor observacional de nowcasting")
    parser.add_argument("--once", action="store_true", help="Executa um ciclo e termina")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configurar_logging()
    config = nowcasting_config()
    if args.once:
        imprimir_resumo(executar_ciclo(config))
        return 0
    if not config["enabled"]:
        imprimir_resumo(executar_ciclo(config))
        return 0
    while True:
        try:
            imprimir_resumo(executar_ciclo(config))
        except Exception as erro:
            logger.error("Ciclo nowcasting falhou: %s", erro_externo_seguro(erro))
        time.sleep(config["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
