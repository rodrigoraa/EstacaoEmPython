import argparse
import os
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import database
from services.whatsapp_service import enviar_whatsapp
from services.alert_queue import atualizar_evento, substituir_envio_superado
from time_utils import agora_local
from config import env_int
from logging_utils import configurar_logging, mascarar_nome, mascarar_telefone


INTERVALO_ENVIO_USUARIOS = env_int("INTERVALO_ENVIO_USUARIOS", 20)
INTERVALO_SEM_FILA = env_int("INTERVALO_WHATSAPP_SEM_FILA", 5)
ENVIANDO_EXPIRADO_MINUTOS = env_int("WHATSAPP_ENVIANDO_EXPIRADO_MINUTOS", 10)
WHATSAPP_WORKERS = max(1, env_int("WHATSAPP_WORKERS", 3))
MAX_TENTATIVAS = max(1, env_int("WHATSAPP_MAX_TENTATIVAS", 4))
ATRASOS_RETRY = (60, 300, 900, 1800)
logger = logging.getLogger(__name__)


def log(mensagem):
    logger.info(mensagem)


def garantir_estruturas(conn):
    database.garantir_tabela_alertas_fila(conn)
    database.garantir_tabela_alertas_envios(conn)
    database.garantir_tabela_alertas_eventos(conn)


def reivindicar_proximo_envio(conn, retry_failed=False, ids_tentados=None):
    if ids_tentados is None:
        ids_tentados = set()
    id_adicionado = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Serializa também migrações aditivas na primeira execução dos workers.
        garantir_estruturas(conn)
        condicoes = [
            "(status = 'pendente' AND (proxima_tentativa_em IS NULL "
            "OR proxima_tentativa_em <= CURRENT_TIMESTAMP))",
            "(status = 'enviando' AND atualizado_em <= datetime('now', ?))",
        ]
        parametros = [f"-{ENVIANDO_EXPIRADO_MINUTOS} minutes"]
        if retry_failed:
            condicoes.append("status = 'falhou'")

        while True:
            candidatos = conn.execute(
                f"""
                SELECT *
                FROM alertas_fila
                WHERE {" OR ".join(condicoes)}
                ORDER BY COALESCE(prioridade, 50) DESC, id
                {"" if retry_failed else "LIMIT 1"}
                """,
                parametros,
            )
            row = next(
                (
                    candidato for candidato in candidatos
                    if candidato["status"] != "falhou"
                    or candidato["id"] not in ids_tentados
                ),
                None,
            )
            candidatos.close()

            if not row:
                conn.commit()
                return None
            if not substituir_envio_superado(conn, row["id"]):
                break

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
        # O BEGIN IMMEDIATE serializa também o conjunto compartilhado pelos
        # workers. Pendentes continuam elegíveis após o backoff normal, mas um
        # item que termina em 'falhou' não ganha outra tentativa nesta sessão.
        if retry_failed and row["id"] not in ids_tentados:
            ids_tentados.add(row["id"])
            id_adicionado = row["id"]
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        if id_adicionado is not None:
            ids_tentados.discard(id_adicionado)
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
            erro,
            evento_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item["usuario_id"],
            item["nome"],
            item["telefone"],
            status,
            item["mensagem"],
            erro,
            item["evento_id"],
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
    atualizar_evento(conn, item["evento_id"])
    conn.commit()


def erro_e_permanente(erro):
    texto = str(erro).lower()
    marcadores = (
        "erro evolution api 400",
        "erro evolution api 401",
        "erro evolution api 403",
        "erro evolution api 404",
        "telefone invalido",
        "telefone inválido",
    )
    return any(marcador in texto for marcador in marcadores)


def marcar_falhou(conn, item, erro):
    registrar_envio_alerta(conn, item, "falhou", erro)
    tentativas = int(item["tentativas"] or 0) + 1
    max_tentativas = int(item["max_tentativas"] or MAX_TENTATIVAS)
    permanente = erro_e_permanente(erro)
    if permanente or tentativas >= max_tentativas:
        conn.execute(
            """
            UPDATE alertas_fila
            SET status = 'falhou', erro = ?, erro_permanente = ?,
                proxima_tentativa_em = NULL, atualizado_em = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (erro, 1 if permanente else 0, item["id"]),
        )
    else:
        atraso = ATRASOS_RETRY[min(tentativas - 1, len(ATRASOS_RETRY) - 1)]
        conn.execute(
            """
            UPDATE alertas_fila
            SET status = 'pendente', erro = ?, erro_permanente = 0,
                proxima_tentativa_em = datetime('now', ?),
                atualizado_em = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (erro, f"+{atraso} seconds", item["id"]),
        )
    atualizar_evento(conn, item["evento_id"])
    conn.commit()


def processar_um_envio(retry_failed=False, ids_tentados=None):
    conn = database.get_db()
    try:
        item = reivindicar_proximo_envio(
            conn, retry_failed=retry_failed, ids_tentados=ids_tentados
        )
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
        log(
            "❌ Falha ao enviar alerta para "
            f"{mascarar_nome(item['nome'])} ({mascarar_telefone(item['telefone'])}): {erro}"
        )
        return "falhou"

    conn = database.get_db()
    try:
        marcar_enviado(conn, item)
    finally:
        conn.close()
    log(
        "✅ Alerta enviado para "
        f"{mascarar_nome(item['nome'])} ({mascarar_telefone(item['telefone'])})"
    )
    return "enviado"


def processar_fila(limite=None, intervalo=INTERVALO_ENVIO_USUARIOS, retry_failed=False):
    enviados = 0
    falhas = 0
    processados = 0
    ids_tentados = set()

    while limite is None or processados < limite:
        resultado = processar_um_envio(
            retry_failed=retry_failed, ids_tentados=ids_tentados
        )
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


def processar_lote_paralelo(
    workers=WHATSAPP_WORKERS, retry_failed=False, ids_tentados=None
):
    if ids_tentados is None:
        ids_tentados = set()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        resultados = list(
            executor.map(
                lambda _: processar_um_envio(
                    retry_failed=retry_failed, ids_tentados=ids_tentados
                ),
                range(workers),
            )
        )
    return [resultado for resultado in resultados if resultado is not None]


def rodar_continuamente(intervalo=INTERVALO_ENVIO_USUARIOS, retry_failed=False):
    log("🚀 Worker de WhatsApp iniciado")
    ids_tentados = set()
    while True:
        resultados = processar_lote_paralelo(
            retry_failed=retry_failed, ids_tentados=ids_tentados
        )
        if not resultados:
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
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Tenta reenviar itens com status falhou uma vez por execução/sessão.",
    )
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
    configurar_logging()
    try:
        main()
    except sqlite3.Error as erro:
        log(f"❌ Erro SQLite no worker de WhatsApp: {erro}")
        raise
