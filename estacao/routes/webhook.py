from flask import Blueprint, request, abort
import subprocess
import hmac
import hashlib
import os
import logging
logging.warning("Webhook recebido: deploy python")
webhook_routes = Blueprint("webhook", __name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

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


@webhook_routes.route("/deploy/python", methods=["POST"])
def deploy_python():

    if request.headers.get("X-GitHub-Event") != "push":
        return "evento ignorado"

    if not verificar_github(request):
        abort(403)

    payload = request.get_json(silent=True)

    if not payload:
        return "payload inválido"

    if payload.get("ref") != "refs/heads/main":
        return "branch ignorada"

    subprocess.Popen(["sudo", "-u", "servidor", "/bin/bash", "/var/www/deploy/deploy-python.sh"])

    return "deploy python iniciado"


@webhook_routes.route("/deploy/php", methods=["POST"])
def deploy_php():

    if not verificar_github(request):
        abort(403)

    subprocess.Popen(["sudo", "-u", "servidor", "/bin/bash", "/var/www/deploy/deploy-php.sh"])

    return "deploy php iniciado"
