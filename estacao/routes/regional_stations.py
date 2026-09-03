"""Pagina e API somente leitura da rede regional PIN-MS."""

import logging

from flask import Blueprint, jsonify, render_template

from config import regional_stations_config
from services.regional_stations_repository import obter_estado_rede


regional_stations_routes = Blueprint("regional_stations", __name__)
logger = logging.getLogger(__name__)


def _estado_seguro():
    try:
        return obter_estado_rede(regional_stations_config())
    except Exception as erro:
        logger.warning("Estado regional indisponivel: %s", type(erro).__name__)
        return {"updated_at": None, "stations": []}


@regional_stations_routes.route("/estacoes-regionais")
def pagina_estacoes_regionais():
    return render_template(
        "regional_stations.html",
        estado=_estado_seguro(),
        titulo="Estações meteorológicas regionais",
    )


@regional_stations_routes.route("/api/regional-stations")
def api_estacoes_regionais():
    return jsonify(_estado_seguro())
