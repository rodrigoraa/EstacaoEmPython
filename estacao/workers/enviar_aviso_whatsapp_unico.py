import argparse
import os
import sqlite3
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import database
from services.whatsapp_service import enviar_whatsapp
from unsubscribe_tokens import telefone_com_codigo_pais


PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://meteo.eesjv.com.br").rstrip("/")
DEFAULT_CAMPAIGN_ID = "adicionar_contato_whatsapp_2026_06"
DEFAULT_INTERVALO = int(os.environ.get("INTERVALO_CAMPANHA_WHATSAPP", "10"))

DEFAULT_MESSAGE = """Olá, {nome}!

Aqui é a Estação Meteorológica da EE São José.

Para continuar recebendo os alertas meteorológicos com mais estabilidade, salve este número nos seus contatos como "Alertas EE São José".

Isso ajuda o WhatsApp a reconhecer que você quer receber nossos avisos e reduz o risco de restrição da conta de envio.

Os alertas são enviados somente para quem solicitou receber. Para cancelar, acesse:
{public_base_url}
"""


def log(mensagem):
    print(mensagem, flush=True)


def garantir_estruturas(conn):
    database.garantir_tabela_usuarios(conn)
    database.garantir_tabela_campanhas_whatsapp_envios(conn)
    conn.commit()


def carregar_template(caminho=None):
    if not caminho:
        return DEFAULT_MESSAGE

    with open(caminho, "r", encoding="utf-8") as arquivo:
        return arquivo.read()


def montar_mensagem(template, usuario):
    nome = (usuario["nome"] or "").strip() or "morador(a)"
    return template.format(nome=nome, public_base_url=PUBLIC_BASE_URL)


def listar_destinatarios(conn):
    return conn.execute(
        """
        SELECT id, nome, telefone
        FROM usuarios
        WHERE (ativo = 1 OR ativo IS NULL)
        AND receber_whatsapp = 1
        ORDER BY id
        """
    ).fetchall()


def obter_envio(conn, campanha_id, usuario_id):
    return conn.execute(
        """
        SELECT status
        FROM campanhas_whatsapp_envios
        WHERE campanha_id = ?
        AND usuario_id = ?
        """,
        (campanha_id, usuario_id),
    ).fetchone()


def salvar_envio(conn, campanha_id, usuario, telefone, status, mensagem, erro=None):
    cursor = conn.execute(
        """
        UPDATE campanhas_whatsapp_envios
        SET data_hora = CURRENT_TIMESTAMP,
            nome = ?,
            telefone = ?,
            status = ?,
            mensagem = ?,
            erro = ?
        WHERE campanha_id = ?
        AND usuario_id = ?
        """,
        (
            usuario["nome"],
            telefone,
            status,
            mensagem,
            erro,
            campanha_id,
            usuario["id"],
        ),
    )

    if cursor.rowcount == 0:
        conn.execute(
            """
            INSERT INTO campanhas_whatsapp_envios (
                campanha_id,
                usuario_id,
                nome,
                telefone,
                status,
                mensagem,
                erro
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campanha_id,
                usuario["id"],
                usuario["nome"],
                telefone,
                status,
                mensagem,
                erro,
            ),
        )

    conn.commit()


def deve_pular_envio(registro, retry_failed=False):
    if not registro:
        return False

    status = registro["status"]
    if status == "falhou" and retry_failed:
        return False

    return True


def enviar_campanha(campanha_id, template, intervalo, confirmar=False, limite=None, retry_failed=False):
    conn = database.get_db()
    conn.row_factory = sqlite3.Row
    garantir_estruturas(conn)

    usuarios = listar_destinatarios(conn)
    if limite:
        usuarios = usuarios[:limite]

    total = len(usuarios)
    enviados = 0
    falhas = 0
    pulados = 0

    if confirmar:
        log(f"Enviando campanha {campanha_id} para ate {total} usuario(s).")
    else:
        log(f"Simulacao da campanha {campanha_id}: {total} usuario(s) seriam avaliados.")

    for indice, usuario in enumerate(usuarios):
        telefone = telefone_com_codigo_pais(usuario["telefone"])
        mensagem = montar_mensagem(template, usuario)
        registro = obter_envio(conn, campanha_id, usuario["id"])

        if deve_pular_envio(registro, retry_failed=retry_failed):
            pulados += 1
            log(f"Pulando {usuario['nome']} ({telefone}): campanha ja registrada.")
            continue

        if not confirmar:
            log(f"[SIMULACAO] Enviaria para {usuario['nome']} ({telefone})")
            continue

        try:
            enviar_whatsapp(telefone, mensagem)
            salvar_envio(conn, campanha_id, usuario, telefone, "enviado", mensagem)
            enviados += 1
            log(f"Enviado para {usuario['nome']} ({telefone})")
        except Exception as erro:
            salvar_envio(conn, campanha_id, usuario, telefone, "falhou", mensagem, str(erro))
            falhas += 1
            log(f"Falha para {usuario['nome']} ({telefone}): {erro}")

        if indice < total - 1:
            time.sleep(intervalo)

    conn.close()
    return {
        "total": total,
        "enviados": enviados,
        "falhas": falhas,
        "pulados": pulados,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Envia uma campanha unica de WhatsApp para usuarios inscritos."
    )
    parser.add_argument("--campaign-id", default=os.environ.get("WHATSAPP_CAMPAIGN_ID", DEFAULT_CAMPAIGN_ID))
    parser.add_argument("--intervalo", type=int, default=DEFAULT_INTERVALO)
    parser.add_argument("--limite", type=int, default=None)
    parser.add_argument("--mensagem-arquivo")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--confirmar",
        action="store_true",
        help="Sem esta opcao o script apenas simula e nao envia mensagens.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    template = carregar_template(args.mensagem_arquivo)
    resultado = enviar_campanha(
        campanha_id=args.campaign_id,
        template=template,
        intervalo=args.intervalo,
        confirmar=args.confirmar,
        limite=args.limite,
        retry_failed=args.retry_failed,
    )
    log(
        "Resultado: "
        f"total={resultado['total']}, "
        f"enviados={resultado['enviados']}, "
        f"falhas={resultado['falhas']}, "
        f"pulados={resultado['pulados']}"
    )


if __name__ == "__main__":
    main()
