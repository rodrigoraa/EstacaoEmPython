from flask import Blueprint, render_template, request, redirect, url_for, session, flash
import database

admin_routes = Blueprint("admin", __name__)

SENHA_ADMIN = "fera@123"


@admin_routes.route("/admin", methods=["GET", "POST"])
def admin():

    if request.method == "POST":

        if request.form.get("senha") == SENHA_ADMIN:
            session["logado"] = True
            return redirect(url_for("admin.admin"))
        else:
            flash("Senha incorreta!")

    if request.args.get("sair"):
        session.pop("logado", None)
        return redirect(url_for("admin.admin"))

    if not session.get("logado"):
        return render_template("admin_login.html")

    conn = database.get_db()

    usuarios = conn.execute("SELECT * FROM usuarios ORDER BY id DESC").fetchall()

    historico = conn.execute(
        "SELECT * FROM historico_clima ORDER BY id DESC LIMIT 5"
    ).fetchall()

    conn.close()

    return render_template("admin_painel.html", usuarios=usuarios, historico=historico)