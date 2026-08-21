import os
import logging
import sqlite3

from flask import Blueprint, render_template, request

import database
from extensions import limiter
from config import env_str, public_url
from logging_utils import erro_externo_seguro, mascarar_telefone
from services.weather_service import obter_previsao
from signup_tokens import (
    TokenConfirmacaoExpirado,
    TokenConfirmacaoInvalido,
    gerar_token_confirmacao,
    validar_token_confirmacao,
)
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
logger = logging.getLogger(__name__)


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


def enviar_link_confirmacao_whatsapp(numero, link_confirmacao):
    from services.whatsapp_service import enviar_whatsapp

    mensagem = (
        "Confirme seu cadastro nos alertas meteorológicos da EE São José.\n\n"
        f"Acesse: {link_confirmacao}\n\n"
        "Se você não solicitou, ignore esta mensagem."
    )
    enviar_whatsapp(numero, mensagem)


def obter_ultima_leitura_persistida():
    conn = database.get_db()
    try:
        row = conn.execute(
            """
            SELECT temp, sensacao, umidade, pressao, uv, radiacao,
                   vento_vel, vento_rajada, vento_dir,
                   chuva_rate, chuva_evento, chuva_hoje,
                   station_timestamp_ms, station_data_hora_local
            FROM historico_clima
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            return None
        dados = dict(row)
        dados["vento"] = dados.pop("vento_vel")
        dados["rajada"] = dados.pop("vento_rajada")
        return dados
    finally:
        conn.close()


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

        if not nome or not endereco or not 10 <= len(telefone) <= 13:
            mensagem = "❌ Preencha todos os campos!"
        else:
            conn = database.get_db()
            cursor = conn.cursor()

            try:
                status_cadastro = "pendente" if receber_whatsapp else "ativo"
                receber_inicial = 0
                cursor.execute(
                    """
                    INSERT INTO usuarios (
                        nome, telefone, endereco, receber_whatsapp, status_cadastro
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (nome, telefone, endereco, receber_inicial, status_cadastro),
                )
                usuario_id = cursor.lastrowid
                database.registrar_cadastro_evento(
                    conn,
                    "cadastro",
                    usuario_id=usuario_id,
                    nome=nome,
                    telefone=telefone,
                    endereco=endereco,
                    receber_whatsapp=receber_inicial,
                    detalhe=f"Cadastro realizado pelo site; status={status_cadastro}",
                )
                conn.commit()
                if receber_whatsapp:
                    token = gerar_token_confirmacao(usuario_id, telefone)
                    link = public_url(f"signup/confirm?token={token}")
                    try:
                        enviar_link_confirmacao_whatsapp(
                            telefone_com_codigo_pais(telefone), link
                        )
                        mensagem = "✅ Cadastro pendente. Confira seu WhatsApp para confirmar."
                    except Exception as erro:
                        logger.error(
                            "Falha ao enviar confirmacao para %s: %s",
                            mascarar_telefone(telefone),
                            erro_externo_seguro(erro),
                        )
                        mensagem = (
                            "⚠️ Cadastro salvo como pendente, mas não foi possível enviar "
                            "a confirmação agora. Tente novamente mais tarde."
                        )
                else:
                    mensagem = "✅ Cadastro realizado com sucesso!"
            except sqlite3.IntegrityError:
                database.registrar_cadastro_evento(
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

    return render_template("index.html", mensagem=mensagem)


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
            link_cancelamento = public_url(f"unsubscribe?token={token}")
            enviar_link_cancelamento_whatsapp(telefone_com_55, link_cancelamento)
            database.registrar_cadastro_evento(
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
            database.registrar_cadastro_evento(
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
        logger.error("Erro ao solicitar cancelamento: %s", erro_externo_seguro(e))
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
            database.registrar_cadastro_evento(
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
            database.registrar_cadastro_evento(
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
        logger.error("Erro ao cancelar: %s", erro_externo_seguro(e))
        return render_template("unsubscribe.html", estado=estado), 500
    finally:
        if conn:
            conn.close()


@public_routes.route("/signup/confirm")
@limiter.limit("30 per hour")
def confirmar_cadastro():
    token = request.args.get("token", "")
    try:
        usuario_id, telefone = validar_token_confirmacao(token)
    except TokenConfirmacaoExpirado:
        estado = estado_cancelamento(
            "Link expirado",
            "O link de confirmação expirou. Faça um novo cadastro ou contate a escola.",
            "#f59e0b",
            "<i class='fa-solid fa-clock'></i>",
        )
        return render_template("unsubscribe.html", estado=estado), 400
    except (TokenConfirmacaoInvalido, RuntimeError):
        estado = estado_cancelamento(
            "Link inválido",
            "Não foi possível validar esta confirmação.",
            "#f59e0b",
            "<i class='fa-solid fa-triangle-exclamation'></i>",
        )
        return render_template("unsubscribe.html", estado=estado), 400

    conn = database.get_db()
    try:
        usuario = conn.execute(
            "SELECT id, telefone, status_cadastro FROM usuarios WHERE id = ?",
            (usuario_id,),
        ).fetchone()
        if not usuario or normalizar_telefone(usuario["telefone"]) != normalizar_telefone(telefone):
            estado = estado_cancelamento(
                "Cadastro não encontrado",
                "Este cadastro não existe mais.",
                "#f59e0b",
                "<i class='fa-solid fa-triangle-exclamation'></i>",
            )
            return render_template("unsubscribe.html", estado=estado), 404

        if usuario["status_cadastro"] == "pendente":
            conn.execute(
                """
                UPDATE usuarios
                SET status_cadastro = 'ativo', receber_whatsapp = 1,
                    confirmado_em = CURRENT_TIMESTAMP
                WHERE id = ? AND status_cadastro = 'pendente'
                """,
                (usuario_id,),
            )
            database.registrar_cadastro_evento(
                conn,
                "cadastro_confirmado",
                usuario_id=usuario_id,
                telefone=usuario["telefone"],
                receber_whatsapp=1,
                detalhe="Double opt-in confirmado por token assinado",
            )
            conn.commit()

        estado = estado_cancelamento(
            "Cadastro confirmado!",
            "Seu número está ativo para receber alertas meteorológicos.",
            "#10b981",
            "<i class='fa-solid fa-check'></i>",
        )
        return render_template("unsubscribe.html", estado=estado)
    finally:
        conn.close()


@public_routes.route("/sobre")
def sobre():
    return render_template("sobre.html")


@public_routes.route("/historico")
def historico():
    return render_template("historico.html")


@public_routes.route("/previsao")
def previsao():
    cidade = env_str("FORECAST_CITY", "Vicentina")
    estado = env_str("FORECAST_STATE", "Mato Grosso do Sul")
    pais = env_str("FORECAST_COUNTRY", "Brasil")
    nome_exibicao = corrigir_texto_env(
        env_str("FORECAST_LABEL", "Distrito de São José, Vicentina/MS")
    )
    latitude = os.environ.get("FORECAST_LAT")
    longitude = os.environ.get("FORECAST_LON")
    dados_estacao = obter_ultima_leitura_persistida()

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
