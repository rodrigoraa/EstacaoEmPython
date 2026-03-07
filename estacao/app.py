from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
load_dotenv()
import os

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
limiter = Limiter(
    key_func=get_remote_address
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
