from flask import Flask
from routes.webhook import webhook_routes
from routes.public import public_routes
from routes.api import api_routes
from routes.admin import admin_routes
import os
app = Flask(__name__)
app.register_blueprint(webhook_routes)

SECRET_KEY = os.environ.get("SECRET_KEY")

if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY não configurada")

app.secret_key = SECRET_KEY

app.register_blueprint(public_routes)
app.register_blueprint(api_routes)
app.register_blueprint(admin_routes)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
