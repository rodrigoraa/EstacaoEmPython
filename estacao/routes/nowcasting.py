"""Painel e API somente leitura do nowcasting observacional persistido."""

import logging

from flask import Blueprint, jsonify, redirect, render_template, url_for

from admin_auth import admin_api_required, admin_page_required
from config import nowcasting_config
from services.nowcasting_repository import obter_ultimo_snapshot
from services.nowcasting_service import (
    janela_snapshot_operacional_minutos,
    snapshot_operacionalmente_atual,
)
from services.preventive_alerts import criar_alerta_preventivo


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
    estado = _estado_seguro()
    config = nowcasting_config()
    monitoramento_atual = snapshot_operacionalmente_atual(
        estado, config
    )
    ultimo_nivel_calculado = None
    if estado and not monitoramento_atual:
        estado = dict(estado)
        alerta_anterior = estado.get("alerta_preventivo") or {}
        ultimo_nivel_calculado = alerta_anterior.get("nivel")
        alerta_indisponivel = criar_alerta_preventivo(
            None,
            radar_atualizado=False,
            evento_local=False,
        )
        alerta_indisponivel["message"] = (
            "Monitoramento desatualizado. O último alerta calculado não deve ser "
            "interpretado como situação atual."
        )
        estado["alerta_preventivo"] = alerta_indisponivel
    return render_template(
        "monitoramento.html",
        estado=estado,
        monitoramento_atual=monitoramento_atual,
        ultimo_nivel_calculado=ultimo_nivel_calculado,
        janela_snapshot_minutos=janela_snapshot_operacional_minutos(
            config
        ),
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
