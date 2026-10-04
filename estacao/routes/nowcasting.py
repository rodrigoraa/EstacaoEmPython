"""Painel e API somente leitura do nowcasting observacional persistido."""

import logging

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for

from admin_auth import admin_api_required, admin_page_required
from config import nowcasting_config
from services.nowcasting_repository import obter_ultimo_snapshot
from services.nowcasting_service import (
    preparar_estado_nowcasting_admin,
)
from services.nowcasting_test_alerts import obter_status_alerta_teste_admin
from services.nowcasting_public_alerts import obter_status_alerta_publico
from services.runtime_alert_controls import obter_controles, salvar_controle
from services.regional_stations_catalog import REGIONAL_STATIONS
from routes.admin import validar_csrf
from extensions import limiter


nowcasting_routes = Blueprint("nowcasting", __name__)
logger = logging.getLogger(__name__)


def _estado_seguro():
    try:
        return obter_ultimo_snapshot()
    except Exception as erro:
        logger.warning("Snapshot de nowcasting indisponivel: %s", type(erro).__name__)
        return None


def _resumo_regional(estado):
    atuais = {item.get("code"): item for item in (estado or {}).get("estacoes_regionais", [])
              if isinstance(item, dict)}
    return [{"code": code, "name": station.display_name,
             "status": (atuais.get(code) or {}).get("status", "SEM_DADOS")}
            for code, station in REGIONAL_STATIONS.items()]


@nowcasting_routes.route("/admin/monitoramento")
@admin_page_required
def monitoramento_admin():
    snapshot = _estado_seguro()
    base_config = nowcasting_config()
    controls = obter_controles(base_config)
    config = {**base_config, "alerts_enabled": controls["public"]["enabled"],
              "test_alerts_enabled": controls["test"]["enabled"]}
    preparado = preparar_estado_nowcasting_admin(snapshot, config)
    return render_template(
        "monitoramento.html",
        estado=preparado["estado"],
        monitoramento_atual=preparado["monitoramento_atual"],
        snapshot_desatualizado=preparado["snapshot_desatualizado"],
        radar_atual=preparado["radar_atual"],
        estacao_atual=preparado["estacao_atual"],
        chuva_na_estacao=preparado["chuva_na_estacao"],
        frescor_fontes=preparado["frescor_fontes"],
        ultimo_nivel_calculado=preparado["ultimo_nivel_calculado"],
        janela_snapshot_minutos=preparado["janela_snapshot_minutos"],
        test_alert=obter_status_alerta_teste_admin(snapshot, config),
        public_alert=obter_status_alerta_publico(config),
        controls=controls,
        public_trajectory_min_frames=config.get("public_trajectory_min_frames", 4),
        regional_stations=_resumo_regional(preparado["estado"]),
        titulo="Monitoramento Meteorológico",
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
    base_config = nowcasting_config()
    controls = obter_controles(base_config)
    config = {**base_config, "alerts_enabled": controls["public"]["enabled"],
              "test_alerts_enabled": controls["test"]["enabled"]}
    preparado = preparar_estado_nowcasting_admin(snapshot, config)
    estado = preparado["estado"]
    payload = dict(estado) if estado else {"status": "SEM_DADOS", "snapshot": None}
    for campo in (
        "monitoramento_atual",
        "snapshot_desatualizado",
        "janela_snapshot_minutos",
        "ultimo_nivel_calculado",
        "analise_atual",
        "radar_atual",
        "estacao_atual",
        "chuva_na_estacao",
        "motivo_indisponibilidade",
        "frescor_fontes",
    ):
        payload[campo] = preparado[campo]
    payload["test_alert"] = obter_status_alerta_teste_admin(snapshot, config)
    payload["public_alert"] = obter_status_alerta_publico(config)
    payload["alert_controls"] = controls
    return jsonify(payload)


@nowcasting_routes.route("/admin/alert-controls/<name>", methods=["POST"])
@limiter.limit("12 per hour")
@admin_api_required
def atualizar_alert_control(name):
    if name not in ("public", "test"):
        abort(404)
    validar_csrf()
    if (set(request.form) != {"csrf_token", "enabled"}
            or len(request.form.getlist("csrf_token")) != 1
            or len(request.form.getlist("enabled")) != 1
            or request.form["enabled"] not in ("true", "false")):
        abort(400)
    salvar_controle(name, request.form["enabled"] == "true")
    flash("Controle de alertas atualizado.")
    return redirect(url_for("nowcasting.monitoramento_admin"), code=303)


@nowcasting_routes.route("/admin/api/alert-controls")
@admin_api_required
def api_alert_controls():
    return jsonify(obter_controles(nowcasting_config()))


@nowcasting_routes.route("/api/nowcasting/status")
@admin_api_required
def api_nowcasting_status_legacy():
    return redirect(url_for("nowcasting.api_nowcasting_status_admin"), code=308)
