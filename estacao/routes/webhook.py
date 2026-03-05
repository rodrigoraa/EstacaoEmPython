from flask import Blueprint, request, abort
import subprocess
import hmac
import hashlib
import os

webhook_routes = Blueprint("webhook", __name__)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "trocar_esse_segredo")


def verificar_github(req):

    assinatura = req.headers.get("X-Hub-Signature-256")

    if assinatura is None:
        return False

    sha_name, assinatura = assinatura.split("=")

    if sha_name != "sha256":
        return False

    mac = hmac.new(WEBHOOK_SECRET.encode(), msg=req.data, digestmod=hashlib.sha256)

    return hmac.compare_digest(mac.hexdigest(), assinatura)


@webhook_routes.route("/deploy/python", methods=["POST"])
def deploy_python():

    if not verificar_github(request):
        abort(403)

    subprocess.Popen(["/var/www/deploy/deploy-python.sh"])

    return "deploy python iniciado"


@webhook_routes.route("/deploy/php", methods=["POST"])
def deploy_php():

    if not verificar_github(request):
        abort(403)

    subprocess.Popen(["/var/www/deploy/deploy-php.sh"])

    return "deploy php iniciado"
