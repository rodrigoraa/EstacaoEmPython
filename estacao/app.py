from flask import Flask
from dotenv import load_dotenv

load_dotenv(encoding="utf-8")
import os
from datetime import timedelta
from extensions import limiter

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
app.config["RATELIMIT_ENABLED"] = (
    os.environ.get("RATELIMIT_ENABLED", "true").strip().lower() == "true"
)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"
)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    minutes=int(os.environ.get("SESSION_TIMEOUT_MINUTES", "30"))
)

limiter.init_app(app)

from routes.webhook import webhook_routes
from routes.public import public_routes
from routes.api import api_routes
from routes.admin import admin_routes

app.register_blueprint(webhook_routes)
app.register_blueprint(public_routes)
app.register_blueprint(api_routes)
app.register_blueprint(admin_routes)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
