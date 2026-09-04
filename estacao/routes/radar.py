"""Pagina, API somente leitura e entrega controlada das imagens do radar."""

import logging
from pathlib import Path

from flask import Blueprint, abort, jsonify, redirect, render_template, send_file, url_for

from admin_auth import admin_api_required, admin_page_required
from config import nowcasting_config, radar_config
from services.nowcasting_repository import obter_ultimo_snapshot
from services.nowcasting_service import snapshot_operacionalmente_atual
from services.nowcasting_test_alerts import obter_status_alerta_teste_admin
from services.preventive_alerts import criar_alerta_preventivo
from services.radar_repository import obter_arquivo_frame, obter_estado_radar
from time_utils import formatar_local


radar_routes = Blueprint("radar", __name__)
logger = logging.getLogger(__name__)


def _alerta_indisponivel(mensagem):
    alerta = criar_alerta_preventivo(
        None,
        radar_atualizado=False,
        evento_local=False,
    )
    alerta["message"] = mensagem
    return alerta


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


@radar_routes.route("/admin/radar")
@admin_page_required
def radar_admin():
    estado = _estado_seguro()
    config_nowcasting = nowcasting_config()
    snapshot_contextual = None
    alerta_preventivo = (
        None
        if estado.get("frame")
        else _alerta_indisponivel("Dados operacionais do radar indisponíveis.")
    )
    if estado.get("frame") and estado.get("stale"):
        alerta_preventivo = _alerta_indisponivel(
            "Dados operacionais do radar desatualizados. O último alerta "
            "calculado não deve ser interpretado como situação atual."
        )
    ameaca_principal = None
    ultimo_nivel_calculado = None
    if estado.get("frame"):
        estado["frame"]["data_frame_formatada"] = formatar_local(
            estado["frame"].get("data_frame_local")
            or estado["frame"].get("data_frame_utc"),
            assume_utc=not bool(estado["frame"].get("data_frame_local")),
        )
        estado["atualizado_em_formatada"] = formatar_local(
            estado.get("atualizado_em"), assume_utc=True
        )
        try:
            snapshot = obter_ultimo_snapshot()
            if (
                snapshot
                and (snapshot.get("radar") or {}).get("frame_id")
                == estado["frame"]["id"]
            ):
                snapshot_contextual = snapshot
                alerta_snapshot = snapshot.get("alerta_preventivo") or {}
                if (
                    not estado.get("stale")
                    and snapshot_operacionalmente_atual(
                        snapshot, config_nowcasting
                    )
                ):
                    alerta_preventivo = snapshot.get("alerta_preventivo")
                    ameaca_principal = snapshot.get("ameaca_principal")
                else:
                    ultimo_nivel_calculado = alerta_snapshot.get("nivel")
                    mensagem = (
                        "Dados operacionais do radar desatualizados. O último "
                        "alerta calculado não deve ser interpretado como situação atual."
                        if estado.get("stale")
                        else "O último alerta calculado está desatualizado e não deve "
                        "ser interpretado como situação atual."
                    )
                    alerta_preventivo = _alerta_indisponivel(mensagem)
        except Exception as erro:
            logger.warning(
                "Alerta preventivo persistido indisponivel no radar: %s",
                type(erro).__name__,
            )
    return render_template(
        "radar.html",
        estado=estado,
        alerta_preventivo=alerta_preventivo,
        ameaca_principal=ameaca_principal,
        ultimo_nivel_calculado=ultimo_nivel_calculado,
        test_alert=obter_status_alerta_teste_admin(
            snapshot_contextual, config_nowcasting
        ),
        titulo="Radar Meteorológico",
        radar_track_min_frames=radar_config()["track_min_frames"],
        aba_ativa="radar",
    )


@radar_routes.route("/radar")
@admin_page_required
def radar_legacy():
    return redirect(url_for("radar.radar_admin"))


@radar_routes.route("/admin/api/radar/status")
@admin_api_required
def api_radar_status_admin():
    return jsonify(_estado_seguro())


@radar_routes.route("/api/radar/status")
@admin_api_required
def api_radar_status_legacy():
    return redirect(url_for("radar.api_radar_status_admin"), code=308)


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


@radar_routes.route("/admin/radar/imagem/<int:frame_id>")
@admin_api_required
def imagem_radar_admin(frame_id):
    return _enviar_imagem(frame_id)


@radar_routes.route("/admin/radar/imagem/atual")
@admin_api_required
def imagem_radar_atual_admin():
    estado = _estado_seguro()
    frame = estado.get("frame")
    if not frame:
        abort(404)
    return _enviar_imagem(frame["id"])


@radar_routes.route("/radar/imagem/<int:frame_id>")
@admin_api_required
def imagem_radar_legacy(frame_id):
    return redirect(url_for("radar.imagem_radar_admin", frame_id=frame_id), code=308)


@radar_routes.route("/radar/imagem/atual")
@admin_api_required
def imagem_radar_atual_legacy():
    return redirect(url_for("radar.imagem_radar_atual_admin"), code=308)
