from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
    jsonify,
)
import database
import hashlib
import os
import sqlite3
from datetime import datetime, timedelta
import requests

app = Flask(__name__)

# ==========================================
# SECRET KEY SEGURA (produção)
# ==========================================
app.secret_key = os.environ.get("SECRET_KEY", "dev_key_trocar_em_producao")

SENHA_ADMIN = "fera@123"


# ==========================================
# PAGINA PRINCIPAL
# ==========================================


@app.route("/", methods=["GET", "POST"])
def index():

    mensagem = ""

    if request.method == "POST":

        nome = request.form.get("nome")
        telefone = request.form.get("telefone")
        endereco = request.form.get("endereco")

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

    # ======================================
    # dados chuva semana (placeholder)
    # ======================================

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


# ==========================================
# API CLIMA TEMPO REAL (API AMBIENT)
# ==========================================


@app.route("/api/clima")
def api_clima():

    try:

        PUBLIC_SLUG = "a535a0b6ff603c1d2376abc99e689f2f"

        url = f"https://lightning.ambientweather.net/devices?public.slug={PUBLIC_SLUG}"

        headers = {"Origin": "https://ambientweather.net", "User-Agent": "Mozilla/5.0"}

        resposta = requests.get(url, headers=headers, timeout=10)
        resposta.raise_for_status()

        dados_api = resposta.json()

        if not dados_api.get("data"):
            return jsonify({"erro": "Sem dados"})

        raw = dados_api["data"][0].get("lastData", dados_api["data"][0])
        info = dados_api["data"][0].get("info", {})

        dados = {
            "local": info.get("name", "EE São José"),
            "temp": round((raw.get("tempf", 32) - 32) * 5.0 / 9.0, 1),
            "sensacao": round(
                (raw.get("feelsLike", raw.get("tempf", 32)) - 32) * 5.0 / 9.0, 1
            ),
            "umidade": raw.get("humidity", 0),
            "pressao": round(raw.get("baromrelin", 0) * 33.8639, 1),
            "uv": raw.get("uv", 0),
            "radiacao": raw.get("solarradiation", 0),
            "vento_atual": round(raw.get("windspeedmph", 0) * 1.609, 1),
            "vento_rajada": round(raw.get("windgustmph", 0) * 1.609, 1),
            "vento_dir": raw.get("winddir", 0),
            "chuva_rate": round(raw.get("hourlyrainin", 0) * 25.4, 1),
            "chuva_evento": round(raw.get("eventrainin", 0) * 25.4, 1),
            "chuva_hoje": round(raw.get("dailyrainin", 0) * 25.4, 1),
            "hora_leitura": datetime.now().strftime("%H:%M:%S"),
        }

        return jsonify(dados)

    except Exception as e:
        return jsonify({"erro": str(e)})


# ==========================================
# API HISTÓRICO (PARA GRÁFICOS)
# ==========================================


@app.route("/api/historico")
def api_historico():

    conn = database.get_db()

    dados = conn.execute(
        """
        SELECT
            data_hora,
            temp,
            chuva,
            vento_vel
        FROM historico_clima
        ORDER BY data_hora ASC
        LIMIT 100
    """
    ).fetchall()

    conn.close()

    resultado = []

    for row in dados:
        resultado.append(
            {
                "timestamp": row["data_hora"],
                "temperatura": row["temp"],
                "chuva": row["chuva"],
                "vento": row["vento_vel"],
            }
        )

    return jsonify(resultado)


# ==========================================
# API ULTIMO REGISTRO
# ==========================================


@app.route("/api/ultimo")
def api_ultimo():

    try:

        conn = database.get_db()

        row = conn.execute(
            """
            SELECT
                data_hora,
                temp,
                chuva,
                vento_vel
            FROM historico_clima
            ORDER BY id DESC
            LIMIT 1
        """
        ).fetchone()

        conn.close()

        if not row:
            return jsonify({})

        return jsonify(
            {
                "timestamp": row["data_hora"],
                "temperatura": row["temp"],
                "vento": row["vento_vel"],
                "chuva": row["chuva"],
            }
        )

    except Exception as e:
        return jsonify({"erro": str(e)})


# ==========================================
# ADMIN
# ==========================================


@app.route("/admin", methods=["GET", "POST"])
def admin():

    if request.method == "POST":

        if request.form.get("senha") == SENHA_ADMIN:
            session["logado"] = True
            return redirect(url_for("admin"))
        else:
            flash("Senha incorreta!")

    if request.args.get("sair"):
        session.pop("logado", None)
        return redirect(url_for("admin"))

    if not session.get("logado"):
        return render_template("admin_login.html")

    conn = database.get_db()

    usuarios = conn.execute("SELECT * FROM usuarios ORDER BY id DESC").fetchall()

    historico = conn.execute(
        "SELECT * FROM historico_clima ORDER BY id DESC LIMIT 5"
    ).fetchall()

    conn.close()

    return render_template("admin_painel.html", usuarios=usuarios, historico=historico)


# ==========================================
# START (somente desenvolvimento)
# ==========================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
