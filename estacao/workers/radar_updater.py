"""Worker independente para coleta, analise e persistencia do radar REDEMET."""

from __future__ import annotations

import argparse
import hashlib
import json
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
    diagnosticar_paleta,
    gerar_imagem_analisada,
)
from services.radar_repository import (
    atualizar_tracking,
    frame_existente,
    marcar_frame_processado,
    obter_estado_radar,
    salvar_resultado_frame,
)
from services.radar_service import (
    RadarServiceError,
    RedemetRadarClient,
    avaliar_timestamp_frame,
)
from time_utils import agora_utc


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


def processar_frame(client, frame, config, coletado_em_utc=None) -> tuple[int, int, bool]:
    coletado_em_utc = coletado_em_utc or agora_utc()
    frame = avaliar_timestamp_frame(
        frame, coletado_em_utc, config["max_future_minutes"]
    )
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
                valid_radius_km=frame.raio_km,
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
            coletado_em_utc=coletado_em_utc,
        )
        if alterado:
            if frame.timestamp_status != "suspect":
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
                coletado_em_utc=coletado_em_utc,
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
    coletado_em_utc = agora_utc()
    novos = falhas = 0
    for frame in resultado.frames:
        existente = frame_existente(frame.path_remoto)
        if existente and existente["status_processamento"] == "processado":
            continue
        try:
            _, clusters, alterado = processar_frame(
                client, frame, config, coletado_em_utc
            )
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
    parser.add_argument(
        "--diagnose-palette",
        nargs="?",
        const="latest",
        metavar="PNG",
        help="Analisa um PNG local; sem caminho usa o original mais recente",
    )
    parser.add_argument(
        "--diagnose-time",
        action="store_true",
        help="Consulta metadados atuais e mostra a hipótese temporal sem persistir",
    )
    return parser.parse_args(argv)


def _frame_metadata_for_image(path: Path, config):
    raiz = Path(config["data_dir"]).resolve()
    try:
        relativo = path.resolve().relative_to(raiz).as_posix()
    except ValueError as erro:
        raise ValueError("PNG de diagnóstico precisa estar dentro de RADAR_DATA_DIR") from erro
    conn = database.get_db()
    try:
        return conn.execute(
            """
            SELECT lat_center, lon_center, lat_min, lat_max, lon_min, lon_max, raio_km
            FROM radar_frames WHERE arquivo_local=? ORDER BY id DESC LIMIT 1
            """,
            (relativo,),
        ).fetchone()
    finally:
        conn.close()


def executar_diagnostico_paleta(caminho, config):
    database.init_db()
    raiz = Path(config["data_dir"]).resolve()
    if caminho == "latest":
        candidatos = list((raiz / "originais").glob("*.png"))
        if not candidatos:
            raise FileNotFoundError("Nenhum PNG original encontrado em RADAR_DATA_DIR")
        path = max(candidatos, key=lambda item: item.stat().st_mtime)
    else:
        path = Path(caminho).expanduser().resolve()
    metadata = _frame_metadata_for_image(path, config)
    if not metadata:
        raise ValueError("PNG não possui metadados de frame no SQLite")
    imagem = abrir_imagem_png(path.read_bytes())
    bounds = GeoBounds(
        metadata["lat_min"], metadata["lat_max"],
        metadata["lon_min"], metadata["lon_max"],
    )
    resumo, mascara_original, mascara_classes = diagnosticar_paleta(
        imagem,
        bounds,
        metadata["lat_center"],
        metadata["lon_center"],
        metadata["raio_km"],
    )
    destino = raiz / "diagnosticos" / path.stem
    _salvar_atomico(mascara_original, destino / "mask_original.png", "PNG")
    _salvar_atomico(mascara_classes, destino / "mask_classes.png", "PNG")
    print(json.dumps({**resumo, "principais_cores": resumo["principais_cores"][:20]}, ensure_ascii=False, indent=2))
    print(f"mask_original: {destino / 'mask_original.png'}")
    print(f"mask_classes: {destino / 'mask_classes.png'}")


def executar_diagnostico_tempo(config):
    if not config["api_key"]:
        raise RadarServiceError("REDEMET_API_KEY nao configurada")
    client = RedemetRadarClient(
        config["api_key"], area=config["area"], anima=config["anima"],
        timeout=config["request_timeout_seconds"], produto=config["product"],
    )
    resultado = client.obter_frames()
    agora = agora_utc()
    for frame in resultado.frames:
        avaliado = avaliar_timestamp_frame(
            frame, agora, config["max_future_minutes"]
        )
        diferenca = (avaliado.data_frame_utc - agora).total_seconds() / 60
        print(f"raw={avaliado.data_frame_raw}")
        print(f"utc={avaliado.data_utc_texto}")
        print(f"local={avaliado.data_local_texto}")
        print(f"agora_utc={agora.replace(microsecond=0).isoformat()}")
        print(f"diferenca_minutos={diferenca:.1f}")
        print(f"timestamp_status={avaliado.timestamp_status}\n")


def main(argv=None):
    args = parse_args(argv)
    configurar_logging()
    config = radar_config()
    if args.diagnose_palette:
        executar_diagnostico_paleta(args.diagnose_palette, config)
        return 0
    if args.diagnose_time:
        executar_diagnostico_tempo(config)
        return 0
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
