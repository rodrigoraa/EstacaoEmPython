from flask import Blueprint, request
import subprocess

webhook_routes = Blueprint("webhook", __name__)


@webhook_routes.route("/deploy/python", methods=["POST"])
def deploy_python():

    subprocess.Popen(["/var/www/deploy/deploy-python.sh"])

    return "deploy python iniciado"


@webhook_routes.route("/deploy/php", methods=["POST"])
def deploy_php():

    subprocess.Popen(["/var/www/deploy/deploy-php.sh"])

    return "deploy php iniciado"