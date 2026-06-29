from datetime import timedelta
import os
import sqlite3

from flask import Blueprint, render_template, request, url_for

import database
from extensions import limiter
from services.weather_service import obter_dados, obter_previsao
from time_utils import agora_local
from unsubscribe_tokens import (
    TokenCancelamentoExpirado,
    TokenCancelamentoInvalido,
    gerar_token_cancelamento,
    normalizar_telefone,
    telefone_com_codigo_pais,
    validar_token_cancelamento,
)

public_routes = Blueprint("public", __name__)
PUBLIC_CADASTRO_RATE_LIMIT = os.environ.get("PUBLIC_CADASTRO_RATE_LIMIT", "60 per hour")


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
    database.registrar_cadastro_evento(
        conn,
        acao,
        usuario_id=usuario_id,
        nome=nome,
        telefone=telefone,
        endereco=endereco,
        receber_whatsapp=receber_whatsapp,
        detalhe=detalhe,
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


def estado_cancelamento(titulo, texto, cor, icone, **extra):
    estado = {
        "titulo": titulo,
        "texto": texto,
        "cor": cor,
        "icone": icone,
    }
    estado.update(extra)
    return estado


def variantes_telefone(telefone):
    telefone_com_55 = telefone_com_codigo_pais(telefone)
    telefone_sem_55 = telefone_com_55[2:] if telefone_com_55.startswith("55") else telefone_com_55
    return telefone_sem_55, telefone_com_55


def buscar_usuario_por_telefone(conn, telefone):
    telefone_sem_55, telefone_com_55 = variantes_telefone(telefone)
    usuario = conn.execute(
        """
        SELECT id, nome, telefone, endereco, receber_whatsapp
        FROM usuarios
        WHERE telefone = ? OR telefone = ?
        LIMIT 1
        """,
        (telefone_sem_55, telefone_com_55),
    ).fetchone()
    return usuario, telefone_sem_55, telefone_com_55


def enviar_link_cancelamento_whatsapp(numero, link_cancelamento):
    from services.whatsapp_service import enviar_whatsapp

    mensagem = (
        "Você solicitou o cancelamento dos alertas meteorológicos da EE São José.\n\n"
        "Para confirmar, acesse o link abaixo:\n"
        f"{link_cancelamento}\n\n"
        "Se você não solicitou, ignore esta mensagem."
    )
    enviar_whatsapp(numero, mensagem)


def enviar_confirmacao_cadastro_whatsapp(numero, nome):
    from services.whatsapp_service import enviar_whatsapp

    nome = (nome or "").strip()
    saudacao = f"Olá, {nome}!" if nome else "Olá!"
    mensagem = (
        f"{saudacao}\n\n"
        "Seu cadastro para receber alertas meteorológicos da EE São José foi confirmado.\n\n"
        "A partir de agora, você receberá avisos pelo WhatsApp quando a estação "
        "registrar uma condição de atenção ou agravamento: frio de 12°C ou menos, "
        "calor de 35°C ou mais, rajadas de vento a partir de 40 km/h, chuva "
        "acumulada a partir de 50 mm no dia ou umidade de 30% ou menos.\n\n"
        "Os alertas são enviados somente quando esses limites forem atingidos, "
        "quando o nível ficar mais crítico ou, no caso do frio, quando a "
        "temperatura subir e cair novamente.\n\n"
        "Para continuar recebendo os alertas meteorológicos com mais estabilidade, salve este número nos seus contatos como _Alertas EE São José_.\n"
        "Isso ajuda o WhatsApp a reconhecer que você quer receber nossos avisos e reduz o risco de restrição da nossa conta.\n\n"
        "Os alertas são enviados somente para quem solicitou receber. Para cancelar, acesse: http://meteo.eesjv.com.br"
    )
    enviar_whatsapp(numero, mensagem)


def tentar_enviar_confirmacao_cadastro(
    conn,
    usuario_id,
    nome,
    telefone,
    endereco,
    receber_whatsapp,
):
    if not receber_whatsapp:
        return

    telefone_envio = telefone_com_codigo_pais(telefone)
    try:
        enviar_confirmacao_cadastro_whatsapp(telefone_envio, nome)
        registrar_evento_cadastro(
            conn,
            "confirmacao_whatsapp",
            usuario_id=usuario_id,
            nome=nome,
            telefone=telefone,
            endereco=endereco,
            receber_whatsapp=receber_whatsapp,
            detalhe="Mensagem de confirmacao enviada por WhatsApp",
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Erro ao enviar confirmação de cadastro: {e}")
        try:
            registrar_evento_cadastro(
                conn,
                "confirmacao_whatsapp_falhou",
                usuario_id=usuario_id,
                nome=nome,
                telefone=telefone,
                endereco=endereco,
                receber_whatsapp=receber_whatsapp,
                detalhe=f"Falha ao enviar confirmacao por WhatsApp: {e}",
            )
            conn.commit()
        except Exception as erro_evento:
            conn.rollback()
            print(f"Erro ao registrar falha de confirmação: {erro_evento}")


@public_routes.route("/", methods=["GET", "POST"])
@limiter.limit(PUBLIC_CADASTRO_RATE_LIMIT, methods=["POST"])
def index():
    mensagem = ""

    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        telefone = request.form.get("telefone", "")
        endereco = request.form.get("endereco", "").strip()
        whatsapp = request.form.get("whatsapp")
        receber_whatsapp = 1 if whatsapp else 0

        telefone = normalizar_telefone(telefone)

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
                usuario_id = cursor.lastrowid
                registrar_evento_cadastro(
                    conn,
                    "cadastro",
                    usuario_id=usuario_id,
                    nome=nome,
                    telefone=telefone,
                    endereco=endereco,
                    receber_whatsapp=receber_whatsapp,
                    detalhe="Cadastro realizado pelo site",
                )
                conn.commit()
                tentar_enviar_confirmacao_cadastro(
                    conn,
                    usuario_id,
                    nome,
                    telefone,
                    endereco,
                    receber_whatsapp,
                )
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


@public_routes.route("/unsubscribe/request", methods=["POST"])
@limiter.limit("10 per hour")
def solicitar_cancelamento():
    telefone = normalizar_telefone(request.form.get("telefone"))

    if len(telefone) < 10:
        estado = estado_cancelamento(
            "Número inválido",
            "Informe um número de WhatsApp válido com DDD para receber o link de confirmação.",
            "#f59e0b",
            "<i class='fa-solid fa-triangle-exclamation'></i>",
        )
        return render_template("unsubscribe.html", estado=estado), 400

    conn = None
    try:
        conn = database.get_db()
        usuario, _, telefone_com_55 = buscar_usuario_por_telefone(conn, telefone)

        if usuario:
            token = gerar_token_cancelamento(usuario["telefone"])
            link_cancelamento = url_for("public.unsubscribe", token=token, _external=True)
            enviar_link_cancelamento_whatsapp(telefone_com_55, link_cancelamento)
            registrar_evento_cadastro(
                conn,
                "cancelamento_solicitado",
                usuario_id=usuario["id"],
                nome=usuario["nome"],
                telefone=usuario["telefone"],
                endereco=usuario["endereco"],
                receber_whatsapp=usuario["receber_whatsapp"],
                detalhe="Link seguro de cancelamento enviado por WhatsApp",
            )
        else:
            registrar_evento_cadastro(
                conn,
                "cancelamento_solicitado_nao_encontrado",
                telefone=telefone,
                detalhe="Solicitacao de cancelamento para telefone nao cadastrado",
            )

        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        estado = estado_cancelamento(
            "Erro ao enviar confirmação",
            "Não foi possível enviar o link de confirmação agora. Tente novamente mais tarde.",
            "#ef4444",
            "<i class='fa-solid fa-circle-xmark'></i>",
        )
        print(f"Erro ao solicitar cancelamento: {e}")
        return render_template("unsubscribe.html", estado=estado), 500
    finally:
        if conn:
            conn.close()

    estado = estado_cancelamento(
        "Confira seu WhatsApp",
        "Se o número estiver cadastrado, enviamos um link de confirmação para concluir o cancelamento.",
        "#10b981",
        "<i class='fa-solid fa-paper-plane'></i>",
    )
    return render_template("unsubscribe.html", estado=estado)


@public_routes.route("/unsubscribe", methods=["GET", "POST"])
@limiter.limit("60 per hour")
def unsubscribe():
    token = request.values.get("token")
    conn = None

    if not token:
        estado = estado_cancelamento(
            "Confirmação necessária",
            "Para sua segurança, o cancelamento agora precisa de um link de confirmação enviado pelo WhatsApp.",
            "#f59e0b",
            "<i class='fa-solid fa-triangle-exclamation'></i>",
        )
        return render_template("unsubscribe.html", estado=estado), 400

    try:
        telefone = validar_token_cancelamento(token)
        conn = database.get_db()
        usuario, telefone_sem_55, telefone_com_55 = buscar_usuario_por_telefone(conn, telefone)

        if request.method == "GET":
            conn.close()
            conn = None
            estado = estado_cancelamento(
                "Confirmar cancelamento",
                "Confirme abaixo para parar de receber os alertas meteorológicos no WhatsApp.",
                "#f59e0b",
                "<i class='fa-solid fa-bell-slash'></i>",
                token=token,
                mostrar_formulario=True,
            )
            return render_template("unsubscribe.html", estado=estado)

        if usuario:
            registrar_evento_cadastro(
                conn,
                "cancelamento",
                usuario_id=usuario["id"],
                nome=usuario["nome"],
                telefone=usuario["telefone"],
                endereco=usuario["endereco"],
                receber_whatsapp=usuario["receber_whatsapp"],
                detalhe="Cancelamento confirmado por token assinado",
            )
        else:
            registrar_evento_cadastro(
                conn,
                "cancelamento_nao_encontrado",
                telefone=telefone,
                detalhe="Telefone nao encontrado no momento do cancelamento por token",
            )

        conn.execute(
            "DELETE FROM usuarios WHERE telefone = ? OR telefone = ?",
            (telefone_sem_55, telefone_com_55),
        )
        conn.commit()
        conn.close()
        conn = None

        estado = estado_cancelamento(
            "Cancelado com sucesso!",
            "Você não receberá mais os alertas no WhatsApp.",
            "#10b981",
            "<i class='fa-solid fa-check'></i>",
        )
        return render_template("unsubscribe.html", estado=estado)

    except TokenCancelamentoExpirado:
        estado = estado_cancelamento(
            "Link expirado",
            "Solicite um novo link de cancelamento pelo painel da estação.",
            "#f59e0b",
            "<i class='fa-solid fa-clock'></i>",
        )
        return render_template("unsubscribe.html", estado=estado), 400
    except TokenCancelamentoInvalido:
        estado = estado_cancelamento(
            "Link inválido",
            "Não foi possível validar este link de cancelamento.",
            "#f59e0b",
            "<i class='fa-solid fa-triangle-exclamation'></i>",
        )
        return render_template("unsubscribe.html", estado=estado), 400
    except Exception as e:
        if conn:
            conn.rollback()
        estado = estado_cancelamento(
            "Erro no sistema",
            "Ocorreu um erro técnico ao tentar cancelar a sua inscrição. Por favor, tente novamente mais tarde.",
            "#ef4444",
            "<i class='fa-solid fa-circle-xmark'></i>",
        )
        print(f"Erro ao cancelar: {e}")
        return render_template("unsubscribe.html", estado=estado), 500
    finally:
        if conn:
            conn.close()


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
