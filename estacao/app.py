import logging
import os
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from dotenv import load_dotenv
from flask import Flask, abort, g, request, send_from_directory

from config import env_bool, env_int, env_str, validar_configuracao_web
from extensions import limiter
from logging_utils import configurar_logging
from csrf import gerar_csrf_token, validar_csrf
from trusted_proxy import TrustedProxyFix


load_dotenv(encoding="utf-8")
logger = logging.getLogger(__name__)


def create_app(config_override=None):
    configurar_logging()
    validar_configuracao_web()

    aplicacao = Flask(__name__)
    app_env = env_str("APP_ENV", "development").lower()
    aplicacao.config["SECRET_KEY"] = env_str("SECRET_KEY") or secrets.token_urlsafe(48)
    aplicacao.config["RATELIMIT_ENABLED"] = env_bool("RATELIMIT_ENABLED", True)
    aplicacao.config["RATELIMIT_STORAGE_URI"] = env_str(
        "RATELIMIT_STORAGE_URI", "memory://"
    )
    aplicacao.config["RATELIMIT_KEY_PREFIX"] = env_str(
        "RATELIMIT_KEY_PREFIX", "estacao"
    )
    aplicacao.config["RATELIMIT_DEFAULT"] = "120 per minute;1200 per hour"
    aplicacao.config["RATELIMIT_SWALLOW_ERRORS"] = False
    aplicacao.config["RATELIMIT_IN_MEMORY_FALLBACK_ENABLED"] = False
    aplicacao.config["SESSION_COOKIE_HTTPONLY"] = True
    aplicacao.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    aplicacao.config["SESSION_COOKIE_SECURE"] = env_bool(
        "SESSION_COOKIE_SECURE", app_env == "production"
    )
    aplicacao.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=min(120, max(1, env_int("SESSION_TIMEOUT_MINUTES", 30)))
    )
    aplicacao.config["ADMIN_SESSION_ABSOLUTE_SECONDS"] = min(
        86400, max(60, env_int("ADMIN_SESSION_ABSOLUTE_SECONDS", 28800))
    )
    aplicacao.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    aplicacao.config["MAX_FORM_MEMORY_SIZE"] = 64 * 1024
    aplicacao.config["MAX_FORM_PARTS"] = 20
    if app_env == "production":
        aplicacao.config["TRUSTED_HOSTS"] = [urlsplit(env_str("PUBLIC_BASE_URL")).hostname]
    if config_override:
        aplicacao.config.update(config_override)

    if env_bool("TRUST_PROXY", False):
        aplicacao.wsgi_app = TrustedProxyFix(
            aplicacao.wsgi_app,
            (env_str("TRUSTED_PROXY_IPS", "127.0.0.1,::1") or "").split(","),
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

    @aplicacao.before_request
    def proteger_requisicao():
        g.csp_nonce = secrets.token_urlsafe(24)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.files:
                abort(415)
            if request.blueprint != "webhook":
                validar_csrf()

    @aplicacao.context_processor
    def contexto_seguranca():
        return {"csrf_token": gerar_csrf_token, "csp_nonce": getattr(g, "csp_nonce", "")}

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
            f"default-src 'self'; script-src 'self' 'nonce-{getattr(g, 'csp_nonce', '')}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'self'; "
            "base-uri 'self'; object-src 'none'; form-action 'self'",
        )
        if (response.mimetype == "text/html" or request.blueprint in {
                "admin", "radar", "regional_stations", "nowcasting"
            } or request.path.startswith("/unsubscribe") or request.args.get("token")
                or request.method != "GET"):
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
        if env_bool("HSTS_ENABLED", app_env == "production"):
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
    app.run(host="127.0.0.1", port=8080)
