import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import database
from services.whatsapp_service import enviar_whatsapp
from time_utils import agora_local


INTERVALO_ENVIO_USUARIOS = int(os.environ.get("INTERVALO_ENVIO_USUARIOS", "20"))
INTERVALO_SEM_FILA = int(os.environ.get("INTERVALO_WHATSAPP_SEM_FILA", "5"))
ENVIANDO_EXPIRADO_MINUTOS = int(os.environ.get("WHATSAPP_ENVIANDO_EXPIRADO_MINUTOS", "10"))
WHATSAPP_WORKERS = max(1, int(os.environ.get("WHATSAPP_WORKERS", "3")))
MAX_TENTATIVAS = max(1, int(os.environ.get("WHATSAPP_MAX_TENTATIVAS", "4")))
ATRASOS_RETRY = (60, 300, 900, 1800)


def log(mensagem):
    agora = agora_local().strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {mensagem}", flush=True)


def garantir_estruturas(conn):
    database.garantir_tabela_alertas_fila(conn)
    database.garantir_tabela_alertas_envios(conn)
    database.garantir_tabela_alertas_eventos(conn)
    conn.commit()


def reivindicar_proximo_envio(conn, retry_failed=False):
    garantir_estruturas(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        condicoes = [
            "(status = 'pendente' AND (proxima_tentativa_em IS NULL "
            "OR proxima_tentativa_em <= CURRENT_TIMESTAMP))",
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
            ORDER BY COALESCE(prioridade, 50) DESC, id
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


def atualizar_evento(conn, evento_id):
    if not evento_id:
        return
    totais = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'enviado' THEN 1 ELSE 0 END) AS enviados,
            SUM(CASE WHEN status = 'falhou' THEN 1 ELSE 0 END) AS falhas,
            SUM(CASE WHEN status IN ('pendente', 'enviando') THEN 1 ELSE 0 END) AS abertos
        FROM alertas_fila
        WHERE evento_id = ?
        """,
        (evento_id,),
    ).fetchone()
    enviados = int(totais["enviados"] or 0)
    falhas = int(totais["falhas"] or 0)
    abertos = int(totais["abertos"] or 0)
    if abertos:
        status = "processando"
    elif falhas:
        status = "concluido_com_falhas"
    else:
        status = "concluido"
    conn.execute(
        """
        UPDATE alertas_eventos
        SET enviados = ?, falhas = ?, status = ?, atualizado_em = CURRENT_TIMESTAMP
        WHERE evento_id = ?
        """,
        (enviados, falhas, status, evento_id),
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


def processar_lote_paralelo(workers=WHATSAPP_WORKERS, retry_failed=False):
    with ThreadPoolExecutor(max_workers=workers) as executor:
        resultados = list(
            executor.map(
                lambda _: processar_um_envio(retry_failed=retry_failed),
                range(workers),
            )
        )
    return [resultado for resultado in resultados if resultado is not None]


def rodar_continuamente(intervalo=INTERVALO_ENVIO_USUARIOS, retry_failed=False):
    log("🚀 Worker de WhatsApp iniciado")
    while True:
        resultados = processar_lote_paralelo(retry_failed=retry_failed)
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
