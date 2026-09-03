"""Painel e API somente leitura do nowcasting observacional persistido."""

import logging

from flask import Blueprint, jsonify, render_template

from services.nowcasting_repository import obter_ultimo_snapshot


nowcasting_routes = Blueprint("nowcasting", __name__)
logger = logging.getLogger(__name__)


def _estado_seguro():
    try:
        return obter_ultimo_snapshot()
    except Exception as erro:
        logger.warning("Snapshot de nowcasting indisponivel: %s", type(erro).__name__)
        return None


@nowcasting_routes.route("/monitoramento")
def monitoramento():
    return render_template(
        "monitoramento.html",
        estado=_estado_seguro(),
        titulo="Monitoramento Regional",
    )


@nowcasting_routes.route("/api/nowcasting/status")
def api_nowcasting_status():
    estado = _estado_seguro()
    return jsonify(estado or {"status": "SEM_DADOS", "snapshot": None})
