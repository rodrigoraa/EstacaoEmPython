"""Painel e API somente leitura do nowcasting observacional persistido."""

import logging

from flask import Blueprint, jsonify, redirect, render_template, url_for

from admin_auth import admin_api_required, admin_page_required
from config import nowcasting_config
from services.nowcasting_repository import obter_ultimo_snapshot
from services.nowcasting_service import (
    preparar_estado_nowcasting_admin,
)
from services.nowcasting_test_alerts import obter_status_alerta_teste_admin


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
    snapshot = _estado_seguro()
    config = nowcasting_config()
    preparado = preparar_estado_nowcasting_admin(snapshot, config)
    return render_template(
        "monitoramento.html",
        estado=preparado["estado"],
        monitoramento_atual=preparado["monitoramento_atual"],
        ultimo_nivel_calculado=preparado["ultimo_nivel_calculado"],
        janela_snapshot_minutos=preparado["janela_snapshot_minutos"],
        test_alert=obter_status_alerta_teste_admin(snapshot, config),
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
    snapshot = _estado_seguro()
    config = nowcasting_config()
    preparado = preparar_estado_nowcasting_admin(snapshot, config)
    estado = preparado["estado"]
    payload = dict(estado) if estado else {"status": "SEM_DADOS", "snapshot": None}
    for campo in (
        "monitoramento_atual",
        "snapshot_desatualizado",
        "janela_snapshot_minutos",
        "ultimo_nivel_calculado",
    ):
        payload[campo] = preparado[campo]
    payload["test_alert"] = obter_status_alerta_teste_admin(snapshot, config)
    return jsonify(payload)


@nowcasting_routes.route("/api/nowcasting/status")
@admin_api_required
def api_nowcasting_status_legacy():
    return redirect(url_for("nowcasting.api_nowcasting_status_admin"), code=308)
