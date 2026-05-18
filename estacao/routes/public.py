from datetime import timedelta
import os
import sqlite3

from flask import Blueprint, render_template, request

import database
from extensions import limiter
from services.weather_service import obter_dados, obter_previsao
from time_utils import agora_local

public_routes = Blueprint("public", __name__)


def garantir_tabela_cadastro_eventos(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cadastro_eventos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT DEFAULT CURRENT_TIMESTAMP,
            acao TEXT NOT NULL,
            usuario_id INTEGER,
            nome TEXT,
            telefone TEXT,
            endereco TEXT,
            receber_whatsapp INTEGER,
            detalhe TEXT
        )
        """
    )


def registrar_evento_cadastro(
    conn,
    acao,
    usuario_id=None,
    nome=None,
    telefone=None,
    endereco=None,
    receber_whatsapp=None,
    detalhe=None,
):
    garantir_tabela_cadastro_eventos(conn)
    conn.execute(
        """
        INSERT INTO cadastro_eventos (
            acao,
            usuario_id,
            nome,
            telefone,
            endereco,
            receber_whatsapp,
            detalhe
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            acao,
            usuario_id,
            nome,
            telefone,
            endereco,
            receber_whatsapp,
            detalhe,
        ),
    )


def corrigir_texto_env(texto):
    if not texto:
        return texto

    if any(marcador in texto for marcador in ("\u00c3", "\u00c2")):
        try:
            texto = texto.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    return texto.replace("Sãõ", "São")


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
                registrar_evento_cadastro(
                    conn,
                    "cadastro",
                    usuario_id=cursor.lastrowid,
                    nome=nome,
                    telefone=telefone,
                    endereco=endereco,
                    receber_whatsapp=receber_whatsapp,
                    detalhe="Cadastro realizado pelo site",
                )
                conn.commit()
                mensagem = "✅ Cadastro realizado com sucesso!"
            except sqlite3.IntegrityError:
                registrar_evento_cadastro(
                    conn,
                    "cadastro_duplicado",
                    nome=nome,
                    telefone=telefone,
                    endereco=endereco,
                    receber_whatsapp=receber_whatsapp,
                    detalhe="Numero ja cadastrado",
                )
                conn.commit()
                mensagem = "⚠️ Número já cadastrado!"
            finally:
                conn.close()

    dias_chuva = []
    dias_semana = ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"]
    hoje = agora_local().replace(tzinfo=None)

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
            "titulo": "Número não encontrado",
            "texto": "Não foi possível identificar o número para cancelamento. Tente novamente pelo botão no painel.",
            "cor": "#f59e0b",
            "icone": "<i class='fa-solid fa-triangle-exclamation'></i>",
        }
        return render_template("unsubscribe.html", estado=estado), 400

    try:
        telefone = "".join(filter(str.isdigit, telefone))

        if telefone.startswith("55"):
            telefone_sem_55 = telefone[2:]
            telefone_com_55 = telefone
        else:
            telefone_sem_55 = telefone
            telefone_com_55 = "55" + telefone

        conn = database.get_db()
        usuario = conn.execute(
            """
            SELECT id, nome, telefone, endereco, receber_whatsapp
            FROM usuarios
            WHERE telefone = ? OR telefone = ?
            LIMIT 1
            """,
            (telefone_sem_55, telefone_com_55),
        ).fetchone()

        if usuario:
            registrar_evento_cadastro(
                conn,
                "cancelamento",
                usuario_id=usuario["id"],
                nome=usuario["nome"],
                telefone=usuario["telefone"],
                endereco=usuario["endereco"],
                receber_whatsapp=usuario["receber_whatsapp"],
                detalhe="Cancelamento realizado pelo link",
            )
        else:
            registrar_evento_cadastro(
                conn,
                "cancelamento_nao_encontrado",
                telefone=telefone,
                detalhe="Telefone nao encontrado no momento do cancelamento",
            )

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


@public_routes.route("/previsao")
def previsao():
    cidade = os.environ.get("FORECAST_CITY", "Vicentina")
    estado = os.environ.get("FORECAST_STATE", "Mato Grosso do Sul")
    pais = os.environ.get("FORECAST_COUNTRY", "Brasil")
    nome_exibicao = corrigir_texto_env(
        os.environ.get("FORECAST_LABEL", "Distrito de São José, Vicentina/MS")
    )
    latitude = os.environ.get("FORECAST_LAT")
    longitude = os.environ.get("FORECAST_LON")
    dados_estacao = obter_dados()

    if latitude and longitude:
        try:
            latitude = float(latitude)
            longitude = float(longitude)
        except ValueError:
            latitude = None
            longitude = None
    else:
        latitude = None
        longitude = None

    return render_template(
        "previsao.html",
        previsao=obter_previsao(
            cidade=cidade,
            estado=estado,
            pais=pais,
            latitude=latitude,
            longitude=longitude,
            nome_exibicao=nome_exibicao,
        ),
        dados_estacao=dados_estacao,
        cidade_exibicao=nome_exibicao,
    )
