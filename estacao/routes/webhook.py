from flask import Blueprint, request, abort
import subprocess
import hmac
import hashlib
import os
import logging

webhook_routes = Blueprint("webhook", __name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
ALLOWED_DEPLOY_REPO = os.environ.get("ALLOWED_DEPLOY_REPO", "rodrigoraa/EstacaoEmPython")
ALLOWED_DEPLOY_BRANCH = os.environ.get("ALLOWED_DEPLOY_BRANCH", "refs/heads/main")

if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET não configurado")


def verificar_github(req):
    assinatura = req.headers.get("X-Hub-Signature-256")

    if assinatura is None:
        return False

    try:
        sha_name, assinatura = assinatura.split("=")
    except ValueError:
        return False

    if sha_name != "sha256":
        return False

    mac = hmac.new(WEBHOOK_SECRET.encode(), msg=req.data, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), assinatura)


def validar_payload_deploy():
    if request.headers.get("X-GitHub-Event") != "push":
        return None, "evento ignorado"

    if not verificar_github(request):
        abort(403)

    payload = request.get_json(silent=True)
    if not payload:
        return None, "payload inválido"

    repo = payload.get("repository", {}).get("full_name")
    if repo != ALLOWED_DEPLOY_REPO:
        return None, "repo ignorado"

    if payload.get("ref") != ALLOWED_DEPLOY_BRANCH:
        return None, "branch ignorada"

    return payload, None


@webhook_routes.route("/deploy/python", methods=["POST"])
def deploy_python():
    logging.warning("Webhook recebido: deploy python")

    _, erro = validar_payload_deploy()
    if erro:
        return erro

    subprocess.Popen(
        ["sudo", "-u", "servidor", "/bin/bash", "/var/www/deploy/deploy-python.sh"],
        start_new_session=True,
    )

    return "deploy python iniciado"


@webhook_routes.route("/deploy/php", methods=["POST"])
def deploy_php():
    logging.warning("Webhook recebido: deploy php")

    _, erro = validar_payload_deploy()
    if erro:
        return erro

    subprocess.Popen(
        ["sudo", "-u", "servidor", "/bin/bash", "/var/www/deploy/deploy-php.sh"],
        start_new_session=True,
    )

    return "deploy php iniciado"
