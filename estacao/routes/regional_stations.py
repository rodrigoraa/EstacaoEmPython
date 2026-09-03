"""Pagina e API somente leitura da rede regional PIN-MS."""

import logging

from flask import Blueprint, jsonify, redirect, render_template, url_for

from admin_auth import admin_api_required, admin_page_required
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


@regional_stations_routes.route("/admin/estacoes-regionais")
@admin_page_required
def pagina_estacoes_regionais_admin():
    return render_template(
        "regional_stations.html",
        estado=_estado_seguro(),
        titulo="Estações meteorológicas regionais",
        aba_ativa="estacoes-regionais",
    )


@regional_stations_routes.route("/estacoes-regionais")
@admin_page_required
def pagina_estacoes_regionais_legacy():
    return redirect(url_for("regional_stations.pagina_estacoes_regionais_admin"))


@regional_stations_routes.route("/admin/api/regional-stations")
@admin_api_required
def api_estacoes_regionais_admin():
    return jsonify(_estado_seguro())


@regional_stations_routes.route("/api/regional-stations")
@admin_api_required
def api_estacoes_regionais_legacy():
    return redirect(url_for("regional_stations.api_estacoes_regionais_admin"), code=308)
