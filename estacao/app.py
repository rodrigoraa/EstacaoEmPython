import logging
import os
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from config import env_bool, env_int, env_str, validar_configuracao_web
from extensions import limiter
from logging_utils import configurar_logging


load_dotenv(encoding="utf-8")
logger = logging.getLogger(__name__)


def create_app(config_override=None):
    configurar_logging()
    validar_configuracao_web()

    aplicacao = Flask(__name__)
    app_env = env_str("APP_ENV", "development").lower()
    aplicacao.config["SECRET_KEY"] = env_str("SECRET_KEY") or "dev-only-change-me"
    aplicacao.config["RATELIMIT_ENABLED"] = env_bool("RATELIMIT_ENABLED", True)
    aplicacao.config["RATELIMIT_STORAGE_URI"] = env_str(
        "RATELIMIT_STORAGE_URI", "memory://"
    )
    aplicacao.config["RATELIMIT_KEY_PREFIX"] = env_str(
        "RATELIMIT_KEY_PREFIX", "estacao"
    )
    aplicacao.config["SESSION_COOKIE_HTTPONLY"] = True
    aplicacao.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    aplicacao.config["SESSION_COOKIE_SECURE"] = env_bool(
        "SESSION_COOKIE_SECURE", False
    )
    aplicacao.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=max(1, env_int("SESSION_TIMEOUT_MINUTES", 30))
    )
    if config_override:
        aplicacao.config.update(config_override)

    if env_bool("TRUST_PROXY", False):
        aplicacao.wsgi_app = ProxyFix(
            aplicacao.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_port=1,
        )

    limiter.init_app(aplicacao)
    if app_env == "production" and aplicacao.config["RATELIMIT_STORAGE_URI"] == "memory://":
        logger.warning(
            "RATELIMIT_STORAGE_URI=memory:// não compartilha limites entre workers"
        )
    if app_env == "production" and not aplicacao.config["SESSION_COOKIE_SECURE"]:
        logger.warning(
            "SESSION_COOKIE_SECURE=false em produção; use true quando o acesso público "
            "estiver protegido por HTTPS"
        )

    from routes.admin import admin_routes
    from routes.api import api_routes
    from routes.public import public_routes
    from routes.radar import radar_routes
    from routes.regional_stations import regional_stations_routes
    from routes.nowcasting import nowcasting_routes
    from routes.webhook import webhook_routes

    aplicacao.register_blueprint(webhook_routes)
    aplicacao.register_blueprint(public_routes)
    aplicacao.register_blueprint(radar_routes)
    aplicacao.register_blueprint(regional_stations_routes)
    aplicacao.register_blueprint(nowcasting_routes)
    aplicacao.register_blueprint(api_routes)
    aplicacao.register_blueprint(admin_routes)

    @aplicacao.after_request
    def adicionar_headers_seguranca(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'self'",
        )
        if env_bool("HSTS_ENABLED", False):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @aplicacao.route("/favicon.ico")
    def favicon():
        return send_from_directory(
            os.path.join(aplicacao.root_path, "static"),
            "logo.png",
            mimetype="image/png",
        )

    return aplicacao


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
