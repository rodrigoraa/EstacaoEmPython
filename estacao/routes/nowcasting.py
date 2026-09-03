"""Painel e API somente leitura do nowcasting observacional persistido."""

import logging

from flask import Blueprint, jsonify, redirect, render_template, url_for

from admin_auth import admin_api_required, admin_page_required
from services.nowcasting_repository import obter_ultimo_snapshot


nowcasting_routes = Blueprint("nowcasting", __name__)
logger = logging.getLogger(__name__)


def _estado_seguro():
    try:
        return obter_ultimo_snapshot()
    except Exception as erro:
        logger.warning("Snapshot de nowcasting indisponivel: %s", type(erro).__name__)
        return None


@nowcasting_routes.route("/admin/monitoramento")
@admin_page_required
def monitoramento_admin():
    return render_template(
        "monitoramento.html",
        estado=_estado_seguro(),
        titulo="Monitoramento Regional",
        aba_ativa="monitoramento",
    )


@nowcasting_routes.route("/monitoramento")
@admin_page_required
def monitoramento_legacy():
    return redirect(url_for("nowcasting.monitoramento_admin"))


@nowcasting_routes.route("/admin/api/nowcasting/status")
@admin_api_required
def api_nowcasting_status_admin():
    estado = _estado_seguro()
    return jsonify(estado or {"status": "SEM_DADOS", "snapshot": None})


@nowcasting_routes.route("/api/nowcasting/status")
@admin_api_required
def api_nowcasting_status_legacy():
    return redirect(url_for("nowcasting.api_nowcasting_status_admin"), code=308)
