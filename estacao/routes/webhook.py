from flask import Blueprint, request, abort
import subprocess
import hmac
import hashlib
import os
import logging
import threading

from config import env_str

webhook_routes = Blueprint("webhook", __name__)
logger = logging.getLogger(__name__)
_deploy_lock = threading.Lock()
_deploy_processos = {}

def verificar_github(req):
    webhook_secret = env_str("WEBHOOK_SECRET")
    if not webhook_secret:
        return False
    assinatura = req.headers.get("X-Hub-Signature-256")

    if assinatura is None:
        return False

    try:
        sha_name, assinatura = assinatura.split("=")
    except ValueError:
        return False

    if sha_name != "sha256":
        return False

    mac = hmac.new(webhook_secret.encode(), msg=req.data, digestmod=hashlib.sha256)
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
    if repo != env_str("ALLOWED_DEPLOY_REPO", "rodrigoraa/EstacaoEmPython"):
        return None, "repo ignorado"

    if payload.get("ref") != env_str("ALLOWED_DEPLOY_BRANCH", "refs/heads/master"):
        return None, "branch ignorada"

    return payload, None


def iniciar_deploy(tipo, script):
    lock_path = f"/tmp/estacao-deploy-{tipo}.lock"
    with _deploy_lock:
        anterior = _deploy_processos.get(tipo)
        if anterior is not None and anterior.poll() is None:
            return None

        processo = subprocess.Popen(
            [
                "flock",
                "--nonblock",
                lock_path,
                "sudo",
                "-u",
                "servidor",
                "/bin/bash",
                script,
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _deploy_processos[tipo] = processo
    logger.warning("Deploy %s iniciado; pid=%s", tipo, processo.pid)
    return processo


@webhook_routes.route("/deploy/python", methods=["POST"])
def deploy_python():
    logger.warning("Webhook recebido: deploy python")

    _, erro = validar_payload_deploy()
    if erro:
        return erro

    if iniciar_deploy("python", "/var/www/deploy/deploy-python.sh") is None:
        return "deploy python já está em execução", 409

    return "deploy python iniciado"


@webhook_routes.route("/deploy/php", methods=["POST"])
def deploy_php():
    logger.warning("Webhook recebido: deploy php")

    _, erro = validar_payload_deploy()
    if erro:
        return erro

    if iniciar_deploy("php", "/var/www/deploy/deploy-php.sh") is None:
        return "deploy php já está em execução", 409

    return "deploy php iniciado"
