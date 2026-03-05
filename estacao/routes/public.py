from flask import Blueprint, render_template, request
import database
import sqlite3
from datetime import datetime, timedelta

public_routes = Blueprint("public", __name__)


@public_routes.route("/", methods=["GET", "POST"])
def index():

    mensagem = ""

    if request.method == "POST":

        nome = request.form.get("nome", "").strip()
        telefone = request.form.get("telefone", "")
        endereco = request.form.get("endereco", "").strip()

        telefone = "".join(filter(str.isdigit, telefone))

        if not nome or not telefone or not endereco:
            mensagem = "❌ Preencha todos os campos!"
        else:

            conn = database.get_db()
            cursor = conn.cursor()

            try:

                cursor.execute(
                    "INSERT INTO usuarios (nome, telefone, endereco) VALUES (?, ?, ?)",
                    (nome, telefone, endereco),
                )

                conn.commit()
                mensagem = "✅ Cadastro realizado com sucesso!"

            except sqlite3.IntegrityError:
                mensagem = "⚠️ Número já cadastrado!"

            finally:
                conn.close()

    dias_chuva = []
    dias_semana = ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"]
    hoje = datetime.now()

    for i in range(6, -1, -1):

        data_alvo = hoje - timedelta(days=i)
        dia_str = dias_semana[int(data_alvo.strftime("%w"))]

        dias_chuva.append(
            {"label": dia_str, "valor": 0, "altura": 0, "is_hoje": (i == 0)}
        )

    return render_template("index.html", mensagem=mensagem, dias_chuva=dias_chuva)
