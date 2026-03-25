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

    # CENÁRIO 1: Sem número
    if not telefone:
        estado = {
            "titulo": "Número não encontrado",
            "texto": "Não foi possível identificar o número para cancelamento. Tente novamente pelo botão no painel.",
            "cor": "#f59e0b",
            "icone": "<i class='fa-solid fa-triangle-exclamation'></i>",
        }
        return render_template("unsubscribe.html", estado=estado), 400

    try:
        # Limpa tudo que não for número (tira espaços, traços, etc)
        telefone = "".join(filter(str.isdigit, telefone))

        # Cria as duas versões possíveis do número (com e sem o 55)
        if telefone.startswith("55"):
            telefone_sem_55 = telefone[2:]
            telefone_com_55 = telefone
        else:
            telefone_sem_55 = telefone
            telefone_com_55 = "55" + telefone

        # CENÁRIO 2: DELETAR DO BANCO DE VERDADE
        conn = database.get_db()

        # AGORA SIM! Apaga o número não importa se ele tem o 55 ou não:
        conn.execute(
            "DELETE FROM usuarios WHERE telefone = ? OR telefone = ?",
            (telefone_sem_55, telefone_com_55),
        )

        conn.commit()
        conn.close()

        estado = {
            "titulo": "Cancelado com sucesso!",
            "texto": "Você não receberá mais os alertas no WhatsApp.",
            "cor": "#10b981",
            "icone": "<i class='fa-solid fa-check'></i>",
        }

        return render_template("unsubscribe.html", estado=estado)

    except Exception as e:
        # CENÁRIO 3: Erro no servidor
        estado = {
            "titulo": "Erro no sistema",
            "texto": "Ocorreu um erro técnico ao tentar cancelar a sua inscrição. Por favor, tente novamente mais tarde.",
            "cor": "#ef4444",
            "icone": "<i class='fa-solid fa-circle-xmark'></i>",
        }
        print(f"Erro ao cancelar: {e}")
        return render_template("unsubscribe.html", estado=estado), 500


@public_routes.route("/sobre")
def sobre():
    return render_template("sobre.html")


@public_routes.route("/historico")
def historico():
    return render_template("historico.html")
