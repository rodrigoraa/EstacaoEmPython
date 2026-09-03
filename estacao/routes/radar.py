"""Pagina, API somente leitura e entrega controlada das imagens do radar."""

import logging
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, send_file

from config import radar_config
from services.radar_repository import obter_arquivo_frame, obter_estado_radar
from time_utils import formatar_local


radar_routes = Blueprint("radar", __name__)
logger = logging.getLogger(__name__)


def _estado_seguro():
    config = radar_config()
    try:
        return obter_estado_radar(config["stale_minutes"])
    except Exception as erro:
        logger.warning("Estado persistido do radar indisponivel: %s", type(erro).__name__)
        return {
            "disponivel": False,
            "stale": None,
            "frame": None,
            "cluster_mais_proximo": None,
            "tracking": None,
        }


@radar_routes.route("/radar")
def radar():
    estado = _estado_seguro()
    if estado.get("frame"):
        estado["frame"]["data_frame_formatada"] = formatar_local(
            estado["frame"]["data_frame"], assume_utc=False
        )
        estado["atualizado_em_formatada"] = formatar_local(
            estado.get("atualizado_em"), assume_utc=True
        )
    return render_template(
        "radar.html",
        estado=estado,
        titulo="Radar Meteorológico",
        radar_track_min_frames=radar_config()["track_min_frames"],
    )


@radar_routes.route("/api/radar/status")
def api_radar_status():
    return jsonify(_estado_seguro())


def _enviar_imagem(frame_id: int):
    config = radar_config()
    row = obter_arquivo_frame(frame_id, analisado=True)
    if not row or not row["arquivo"]:
        row = obter_arquivo_frame(frame_id, analisado=False)
    if not row or not row["arquivo"]:
        abort(404)
    raiz = Path(config["data_dir"]).resolve()
    if raiz == Path(raiz.anchor):
        logger.warning("RADAR_DATA_DIR inseguro; entrega de imagem bloqueada")
        abort(404)
    candidato = (raiz / row["arquivo"]).resolve()
    try:
        candidato.relative_to(raiz)
    except ValueError:
        logger.warning("Caminho de radar fora de RADAR_DATA_DIR bloqueado")
        abort(404)
    if candidato.suffix.lower() != ".png" or not candidato.is_file():
        abort(404)
    return send_file(
        candidato,
        mimetype="image/png",
        conditional=True,
        max_age=60,
    )


@radar_routes.route("/radar/imagem/<int:frame_id>")
def imagem_radar(frame_id):
    return _enviar_imagem(frame_id)


@radar_routes.route("/radar/imagem/atual")
def imagem_radar_atual():
    estado = _estado_seguro()
    frame = estado.get("frame")
    if not frame:
        abort(404)
    return _enviar_imagem(frame["id"])
