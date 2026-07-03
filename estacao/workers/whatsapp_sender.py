import argparse
import os
import sqlite3
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import database
from services.whatsapp_service import enviar_whatsapp
from time_utils import agora_local


INTERVALO_ENVIO_USUARIOS = int(os.environ.get("INTERVALO_ENVIO_USUARIOS", "20"))
INTERVALO_SEM_FILA = int(os.environ.get("INTERVALO_WHATSAPP_SEM_FILA", "5"))
ENVIANDO_EXPIRADO_MINUTOS = int(os.environ.get("WHATSAPP_ENVIANDO_EXPIRADO_MINUTOS", "10"))


def log(mensagem):
    agora = agora_local().strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {mensagem}", flush=True)


def garantir_estruturas(conn):
    database.garantir_tabela_alertas_fila(conn)
    database.garantir_tabela_alertas_envios(conn)
    conn.commit()


def reivindicar_proximo_envio(conn, retry_failed=False):
    garantir_estruturas(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        condicoes = [
            "status = 'pendente'",
            "(status = 'enviando' AND atualizado_em <= datetime('now', ?))",
        ]
        parametros = [f"-{ENVIANDO_EXPIRADO_MINUTOS} minutes"]
        if retry_failed:
            condicoes.append("status = 'falhou'")

        row = conn.execute(
            f"""
            SELECT *
            FROM alertas_fila
            WHERE {" OR ".join(condicoes)}
            ORDER BY id
            LIMIT 1
            """,
            parametros,
        ).fetchone()

        if not row:
            conn.commit()
            return None

        conn.execute(
            """
            UPDATE alertas_fila
            SET status = 'enviando',
                tentativas = COALESCE(tentativas, 0) + 1,
                erro = NULL,
                atualizado_em = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (row["id"],),
        )
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise


def registrar_envio_alerta(conn, item, status, erro=None):
    conn.execute(
        """
        INSERT INTO alertas_envios (
            usuario_id,
            nome,
            telefone,
            status,
            mensagem,
            erro
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item["usuario_id"],
            item["nome"],
            item["telefone"],
            status,
            item["mensagem"],
            erro,
        ),
    )


def marcar_enviado(conn, item):
    registrar_envio_alerta(conn, item, "enviado")
    conn.execute(
        """
        UPDATE alertas_fila
        SET status = 'enviado',
            erro = NULL,
            enviado_em = CURRENT_TIMESTAMP,
            atualizado_em = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (item["id"],),
    )
    conn.commit()


def marcar_falhou(conn, item, erro):
    registrar_envio_alerta(conn, item, "falhou", erro)
    conn.execute(
        """
        UPDATE alertas_fila
        SET status = 'falhou',
            erro = ?,
            atualizado_em = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (erro, item["id"]),
    )
    conn.commit()


def processar_um_envio(retry_failed=False):
    conn = database.get_db()
    try:
        item = reivindicar_proximo_envio(conn, retry_failed=retry_failed)
    finally:
        conn.close()

    if not item:
        return None

    try:
        enviar_whatsapp(item["telefone"], item["mensagem"])
    except Exception as erro:
        conn = database.get_db()
        try:
            marcar_falhou(conn, item, str(erro))
        finally:
            conn.close()
        log(f"❌ Falha ao enviar alerta para {item['nome']} ({item['telefone']}): {erro}")
        return "falhou"

    conn = database.get_db()
    try:
        marcar_enviado(conn, item)
    finally:
        conn.close()
    log(f"✅ Alerta enviado para {item['nome']} ({item['telefone']})")
    return "enviado"


def processar_fila(limite=None, intervalo=INTERVALO_ENVIO_USUARIOS, retry_failed=False):
    enviados = 0
    falhas = 0
    processados = 0

    while limite is None or processados < limite:
        resultado = processar_um_envio(retry_failed=retry_failed)
        if resultado is None:
            break

        processados += 1
        if resultado == "enviado":
            enviados += 1
        else:
            falhas += 1

        if limite is None or processados < limite:
            time.sleep(intervalo)

    return {"processados": processados, "enviados": enviados, "falhas": falhas}


def rodar_continuamente(intervalo=INTERVALO_ENVIO_USUARIOS, retry_failed=False):
    log("🚀 Worker de WhatsApp iniciado")
    while True:
        resultado = processar_um_envio(retry_failed=retry_failed)
        if resultado is None:
            time.sleep(INTERVALO_SEM_FILA)
        else:
            time.sleep(intervalo)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Envia alertas pendentes da fila de WhatsApp."
    )
    parser.add_argument("--once", action="store_true", help="Processa apenas um envio pendente.")
    parser.add_argument("--limite", type=int, default=None, help="Processa ate N envios e encerra.")
    parser.add_argument("--intervalo", type=int, default=INTERVALO_ENVIO_USUARIOS)
    parser.add_argument("--retry-failed", action="store_true", help="Tenta reenviar itens com status falhou.")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.once:
        resultado = processar_fila(limite=1, intervalo=args.intervalo, retry_failed=args.retry_failed)
        log(
            "Resultado: "
            f"processados={resultado['processados']}, "
            f"enviados={resultado['enviados']}, "
            f"falhas={resultado['falhas']}"
        )
        return

    if args.limite is not None:
        resultado = processar_fila(
            limite=args.limite,
            intervalo=args.intervalo,
            retry_failed=args.retry_failed,
        )
        log(
            "Resultado: "
            f"processados={resultado['processados']}, "
            f"enviados={resultado['enviados']}, "
            f"falhas={resultado['falhas']}"
        )
        return

    rodar_continuamente(intervalo=args.intervalo, retry_failed=args.retry_failed)


if __name__ == "__main__":
    try:
        main()
    except sqlite3.Error as erro:
        log(f"❌ Erro SQLite no worker de WhatsApp: {erro}")
        raise
