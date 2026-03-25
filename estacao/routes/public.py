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
    # Pega o número de telefone enviado pelo site
    telefone = request.args.get("tel")

    # CENÁRIO 1: A pessoa chegou na página sem um número
    if not telefone:
        estado = {
            "cor": "#f59e0b",  # Amarelo (Tailwind Amber 500)
            "icone": '<i class="fa-solid fa-triangle-exclamation"></i>',
            "titulo": "Número não encontrado",
            "texto": "Não conseguimos identificar qual número você deseja cancelar. Por favor, tente novamente pelo botão no painel principal.",
        }
        return render_template("unsubscribe.html", estado=estado), 400

    try:
        # CENÁRIO 2: Tudo certo! Vamos apagar o número do banco
        conn = database.get_db()
        conn.execute("DELETE FROM usuarios WHERE telefone = ?", (telefone,))
        conn.commit()
        conn.close()

        estado = {
            "cor": "#10b981",  # Verde (Tailwind Emerald 500)
            "icone": '<i class="fa-solid fa-check"></i>',
            "titulo": "Cancelado com sucesso!",
            "texto": "O seu número foi apagado do nosso sistema com sucesso. Você não receberá mais os alertas da estação no WhatsApp.",
        }
        return render_template("unsubscribe.html", estado=estado)

    except Exception as e:
        # CENÁRIO 3: Deu algum erro técnico no servidor
        estado = {
            "cor": "#ef4444",  # Vermelho (Tailwind Red 500)
            "icone": '<i class="fa-solid fa-circle-xmark"></i>',
            "titulo": "Erro no sistema",
            "texto": "Ocorreu um erro técnico ao tentar cancelar a sua inscrição. Por favor, tente novamente mais tarde.",
        }
        print(f"Erro ao cancelar: {e}")  # Imprime o erro no console
        return render_template("unsubscribe.html", estado=estado), 500


@public_routes.route("/sobre")
def sobre():
    return render_template("sobre.html")


@public_routes.route("/historico")
def historico():
    return render_template("historico.html")
