"""Worker independente para coleta, analise e persistencia do radar REDEMET."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

load_dotenv(BASE_DIR / ".env", encoding="utf-8")
load_dotenv(encoding="utf-8")

import database
from config import radar_config
from logging_utils import configurar_logging, erro_externo_seguro
from services.radar_analysis import (
    DetectionConfig,
    GeoBounds,
    abrir_imagem_png,
    detectar_clusters,
    gerar_imagem_analisada,
)
from services.radar_repository import (
    atualizar_tracking,
    frame_existente,
    marcar_frame_processado,
    obter_estado_radar,
    salvar_resultado_frame,
)
from services.radar_service import RadarServiceError, RedemetRadarClient


logger = logging.getLogger(__name__)


def _nome_base(frame) -> str:
    digest = hashlib.sha256(frame.path_remoto.encode("utf-8")).hexdigest()[:12]
    return f"{frame.data_frame.strftime('%Y%m%d_%H%M%S')}_{digest}"


def _salvar_atomico(imagem_ou_bytes, destino: Path, formato: str | None = None):
    destino.parent.mkdir(parents=True, exist_ok=True)
    descritor, temporario = tempfile.mkstemp(
        prefix=f".{destino.name}.", suffix=".tmp", dir=destino.parent
    )
    os.close(descritor)
    try:
        if isinstance(imagem_ou_bytes, bytes):
            Path(temporario).write_bytes(imagem_ou_bytes)
        else:
            imagem_ou_bytes.save(temporario, format=formato or "PNG")
        os.replace(temporario, destino)
    finally:
        if os.path.exists(temporario):
            os.unlink(temporario)


def _relativo_seguro(caminho: Path, raiz: Path) -> str:
    return caminho.resolve().relative_to(raiz.resolve()).as_posix()


def processar_frame(client, frame, config) -> tuple[int, int, bool]:
    raiz = Path(config["data_dir"]).resolve()
    base = _nome_base(frame)
    original = raiz / "originais" / f"{base}.png"
    analisada = raiz / "analisadas" / f"{base}.png"
    try:
        conteudo = client.baixar_imagem(frame)
        imagem = abrir_imagem_png(conteudo)
        bounds = GeoBounds(frame.lat_min, frame.lat_max, frame.lon_min, frame.lon_max)
        clusters = detectar_clusters(
            imagem,
            bounds,
            config["target_lat"],
            config["target_lon"],
            frame.lat_center,
            frame.lon_center,
            DetectionConfig(
                min_cluster_pixels=config["min_cluster_pixels"],
                close_iterations=config["morph_close_iterations"],
                dilate_iterations=config["dilate_iterations"],
                clutter_radius_km=config["clutter_radius_km"],
            ),
        )
        derivada = gerar_imagem_analisada(
            imagem, bounds, config["target_lat"], config["target_lon"], clusters
        )
        _salvar_atomico(conteudo, original)
        _salvar_atomico(derivada, analisada, "PNG")
        frame_id, alterado = salvar_resultado_frame(
            frame,
            _relativo_seguro(original, raiz),
            _relativo_seguro(analisada, raiz),
            imagem.width,
            imagem.height,
            clusters,
            status="analisado",
        )
        if alterado:
            atualizar_tracking(
                frame_id,
                config["target_lat"],
                config["target_lon"],
                config["track_min_frames"],
                config["track_min_duration_minutes"],
                config["track_max_speed_kmh"],
                config["intercept_radius_km"],
                config["track_max_size_ratio"],
                config["track_max_direction_change_deg"],
                config["track_prediction_weight"],
                config["track_timeout_minutes"],
            )
            marcar_frame_processado(frame_id)
        return frame_id, len(clusters), alterado
    except Exception as erro:
        detalhe = erro_externo_seguro(erro, limite=500)
        try:
            salvar_resultado_frame(
                frame,
                _relativo_seguro(original, raiz) if original.exists() else None,
                None,
                None,
                None,
                (),
                status="erro",
                erro=detalhe,
            )
        except Exception as erro_banco:
            logger.error(
                "Falha ao registrar erro do frame %s: %s",
                frame.data_texto,
                erro_externo_seguro(erro_banco),
            )
        raise


def enfileirar_alertas_radar(config, _estado) -> int:
    """Ponto de extensao futuro; nesta fase nunca cria alertas automaticamente."""
    if not config.get("alerts_enabled", False):
        return 0
    logger.warning(
        "RADAR_ALERTS_ENABLED=true, mas o envio preventivo ainda nao foi habilitado "
        "sem validacao meteorologica"
    )
    return 0


def executar_ciclo(client=None, config=None) -> dict:
    config = config or radar_config()
    if not config["enabled"]:
        return {"desabilitado": True, "recebidos": 0, "unicos": 0, "novos": 0,
                "falhas": 0, "clusters": 0, "estado": None}
    if not config["api_key"]:
        raise RadarServiceError("REDEMET_API_KEY nao configurada")

    database.init_db()
    client = client or RedemetRadarClient(
        config["api_key"],
        area=config["area"],
        anima=config["anima"],
        timeout=config["request_timeout_seconds"],
        produto=config["product"],
    )
    resultado = client.obter_frames()
    novos = falhas = 0
    for frame in resultado.frames:
        existente = frame_existente(frame.path_remoto)
        if existente and existente["status_processamento"] == "processado":
            continue
        try:
            _, clusters, alterado = processar_frame(client, frame, config)
            novos += int(alterado)
        except Exception as erro:
            falhas += 1
            logger.warning(
                "Frame de radar %s nao processado: %s",
                frame.data_texto,
                erro_externo_seguro(erro),
            )
    estado = obter_estado_radar(config["stale_minutes"])
    enfileirar_alertas_radar(config, estado)
    return {
        "desabilitado": False,
        "recebidos": resultado.recebidos,
        "unicos": resultado.unicos,
        "novos": novos,
        "falhas": falhas,
        "clusters": (estado.get("frame") or {}).get("clusters_significativos", 0),
        "estado": estado,
    }


def imprimir_resumo(resultado: dict):
    print("Radar Jaraguari")
    if resultado["desabilitado"]:
        print("Radar desabilitado (RADAR_ENABLED=false).")
        return
    print(f"Frames recebidos: {resultado['recebidos']}")
    print(f"Frames unicos: {resultado['unicos']}")
    print(f"Novos frames: {resultado['novos']}")
    print(f"Falhas: {resultado['falhas']}")
    estado = resultado.get("estado") or {}
    frame = estado.get("frame") or {}
    cluster = estado.get("cluster_mais_proximo") or {}
    tracking = estado.get("tracking") or {}
    if frame:
        print(f"\nUltimo frame:\n{frame['data_frame']}")
    print(f"\nClusters significativos:\n{resultado['clusters']}")
    if cluster:
        print("\nCluster mais proximo:")
        print(f"Borda: {cluster['distancia_borda_escola_km']:.1f} km")
        print(f"Centro: {cluster['distancia_centro_escola_km']:.1f} km")
        print(f"Posicao relativa: {cluster['direcao_relativa']}")
        print(f"Suspeito de clutter: {'SIM' if cluster['suspeito_clutter'] else 'NAO'}")
    print(f"\nTracking:\n{tracking.get('status', 'dados insuficientes')}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Worker do radar REDEMET Jaraguari")
    parser.add_argument("--once", action="store_true", help="Executa um ciclo e termina")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configurar_logging()
    config = radar_config()
    if args.once:
        imprimir_resumo(executar_ciclo(config=config))
        return 0
    if not config["enabled"]:
        imprimir_resumo(executar_ciclo(config=config))
        return 0
    while True:
        try:
            imprimir_resumo(executar_ciclo(config=config))
        except Exception as erro:
            logger.error("Ciclo do radar falhou: %s", erro_externo_seguro(erro))
        time.sleep(config["poll_seconds"])


if __name__ == "__main__":
    raise SystemExit(main())
