from flask import Blueprint, render_template, request
from datetime import datetime, timedelta
from app import limiter
import database
import sqlite3

public_routes = Blueprint("public", __name__)


@public_routes.route("/", methods=["GET", "POST"])
@limiter.limit("5 per hour")
def index():

    mensagem = ""

    if request.method == "POST":

        nome = request.form.get("nome", "").strip()
        telefone = request.form.get("telefone", "")
        endereco = request.form.get("endereco", "").strip()
        whatsapp = request.form.get("whatsapp")
        receber_whatsapp = 1 if whatsapp else 0

        telefone = "".join(filter(str.isdigit, telefone))

        if not nome or not telefone or not endereco:
            mensagem = "❌ Preencha todos os campos!"
        else:

            conn = database.get_db()
            cursor = conn.cursor()

            try:

                cursor.execute(
                    "INSERT INTO usuarios (nome, telefone, endereco, receber_whatsapp) VALUES (?, ?, ?, ?)",
                    (nome, telefone, endereco, receber_whatsapp),
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


@public_routes.route("/unsubscribe")
def unsubscribe():

    telefone = request.args.get("tel")

    if not telefone:

        estado = {
            "titulo": "Link inválido",
            "texto": "Não foi possível identificar o número para cancelamento.",
            "cor": "#ef4444",
            "icone": "<i class='fa-solid fa-circle-xmark'></i>",
        }

        return render_template("unsubscribe.html", estado=estado)

    conn = database.get_db()

    conn.execute("UPDATE usuarios SET ativo = 0 WHERE telefone = ?", (telefone,))

    conn.commit()
    conn.close()

    estado = {
        "titulo": "Alertas cancelados",
        "texto": "Você não receberá mais alertas meteorológicos desta estação.",
        "cor": "#22c55e",
        "icone": "<i class='fa-solid fa-circle-check'></i>",
    }

    return render_template("unsubscribe.html", estado=estado)

@public_routes.route("/sobre")
def sobre():
    return render_template("sobre.html")