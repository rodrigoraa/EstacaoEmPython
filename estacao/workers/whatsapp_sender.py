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
from time_utils import agora_utc, parse_datetime
from config import env_int, nowcasting_config, numero_alerta_valido
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
    conn.commit()


def reivindicar_proximo_envio(conn, retry_failed=False):
    garantir_estruturas(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        condicoes = [
            "(alertas_fila.status = 'pendente' AND (proxima_tentativa_em IS NULL "
            "OR proxima_tentativa_em <= CURRENT_TIMESTAMP))",
            "(alertas_fila.status = 'enviando' "
            "AND alertas_fila.atualizado_em <= datetime('now', ?))",
        ]
        parametros = [f"-{ENVIANDO_EXPIRADO_MINUTOS} minutes"]
        if retry_failed:
            condicoes.append("alertas_fila.status = 'falhou'")

        row = conn.execute(
            f"""
            SELECT alertas_fila.*,
                   evento.tipo AS evento_tipo,
                   evento.nivel AS evento_nivel,
                   evento.ocorrido_em_local AS evento_ocorrido_em_local
            FROM alertas_fila
            LEFT JOIN alertas_eventos evento
              ON evento.evento_id = alertas_fila.evento_id
            WHERE {" OR ".join(condicoes)}
            ORDER BY COALESCE(prioridade, 50) DESC, alertas_fila.id
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
            SUM(CASE WHEN status = 'cancelado' THEN 1 ELSE 0 END) AS cancelados,
            SUM(CASE WHEN status IN ('pendente', 'enviando') THEN 1 ELSE 0 END) AS abertos
        FROM alertas_fila
        WHERE evento_id = ?
        """,
        (evento_id,),
    ).fetchone()
    enviados = int(totais["enviados"] or 0)
    falhas = int(totais["falhas"] or 0)
    cancelados = int(totais["cancelados"] or 0)
    abertos = int(totais["abertos"] or 0)
    if abertos:
        status = "processando"
    elif falhas:
        status = "concluido_com_falhas"
    elif cancelados:
        status = "concluido_com_cancelamentos" if enviados else "cancelado"
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


def revalidar_previsao_nowcasting(conn, item, *, now=None):
    """Cancela previsao invalida ou renova seu texto antes de enviar."""
    evento_id = item["evento_id"] or ""
    if (item["evento_tipo"] != "nowcasting_radar"
            and not evento_id.startswith("nowcasting:")):
        return None
    if item["evento_tipo"] != "nowcasting_radar":
        return "Previsao de radar sem metadados validos do evento"
    momento = parse_datetime(item["evento_ocorrido_em_local"], assume_utc=False)
    if momento is None:
        return "Previsao de radar sem horario de observacao valido"
    agora = now or agora_utc()
    idade_minutos = (agora - momento).total_seconds() / 60
    if idade_minutos < -1:
        return "Horario de observacao da previsao de radar esta no futuro"

    try:
        from services.runtime_alert_controls import aplicar_controles
        from services.nowcasting_repository import obter_ultimo_snapshot
        from services.nowcasting_alert_evaluation import (
            avaliar_alerta_preventivo_snapshot, montar_mensagem_preventiva,
        )
        from services.nowcasting_public_alerts import (
            _aplicar_politica_publica, _mensagem_usuario, carregar_estado,
        )
        from services.preventive_alerts import ALERT_SEVERITY

        config = aplicar_controles(nowcasting_config())
        ttl = numero_alerta_valido(config.get("alert_delivery_max_age_minutes"), 15)
        if idade_minutos > ttl:
            return f"Previsao de radar expirada: observacao com mais de {ttl:g} minutos"
        if config.get("enabled") is not True or config.get("alerts_enabled") is not True:
            return "Alertas publicos de previsao de radar desativados"

        estado = carregar_estado(conn)
        identidade = evento_id.split(":")
        if (len(identidade) != 3 or not estado["active"]
                or estado["episode_id"] != identidade[1]):
            return "Episodio da previsao de radar nao esta mais ativo"
        if estado["suppressed_for_current_episode"]:
            return "Previsao de radar suprimida por chuva ja observada no local"
        if estado["highest_enqueued_severity"] > item["evento_nivel"]:
            return "Previsao de radar substituida por alerta mais severo do mesmo episodio"
        snapshot = obter_ultimo_snapshot()
        avaliacao = _aplicar_politica_publica(
            avaliar_alerta_preventivo_snapshot(snapshot, config, now=agora),
            snapshot, config, agora,
        )
        if not avaliacao["eligible"]:
            return f"Previsao de radar nao confirmada na revalidacao: {avaliacao['reason']}"
        alerta = snapshot.get("alerta_preventivo") or {}
        confirmado_em = parse_datetime(estado["pending_tracking_observed_at"], assume_utc=True)
        imagem_em = parse_datetime((snapshot.get("radar") or {}).get("data_frame"), assume_utc=True)
        gap_confirmacao = ((imagem_em - confirmado_em).total_seconds() / 60
                          if imagem_em and confirmado_em else None)
        if (estado["pending_tracking_count"] < 2
                or estado["pending_tracking_track_id"] != alerta.get("track_id")
                or gap_confirmacao is None or gap_confirmacao < 0
                or gap_confirmacao > numero_alerta_valido(config.get("public_trajectory_max_gap_minutes"), 15)):
            return "Trajetoria atual da previsao de radar sem dupla confirmacao recente"
        decisao = avaliacao.get("decision") or {}
        nivel_atual = ALERT_SEVERITY.get(decisao.get("alert_level"), 0)
        if nivel_atual < item["evento_nivel"]:
            return "Intensidade atual inferior ao alerta de radar enfileirado"
        alerta_atual = {
            **alerta, **decisao,
            "alert_level": {1: "INFORMATIVO", 2: "ATENCAO", 3: "ALERTA"}[item["evento_nivel"]],
            "eta_border_minutes": alerta.get("projected_impact_eta_minutes"),
            "eta_border_quality": "BOA" if alerta.get("trajectory_confidence") == "ALTA" else "MODERADA",
        }
        item["mensagem"] = _mensagem_usuario(
            {"nome": item["nome"]},
            montar_mensagem_preventiva({**snapshot, "alerta_preventivo": alerta_atual}),
        )
        conn.execute("UPDATE alertas_fila SET mensagem=? WHERE id=?",
                     (item["mensagem"], item["id"]))
        conn.commit()
        return None
    except Exception as erro:
        # Indisponibilidade de dados nunca autoriza previsao antiga por omissao.
        return f"Revalidacao da previsao de radar indisponivel ({type(erro).__name__})"


def marcar_cancelado(conn, item, motivo):
    registrar_envio_alerta(conn, item, "cancelado", motivo)
    conn.execute(
        """
        UPDATE alertas_fila
        SET status = 'cancelado', erro = ?, erro_permanente = 1,
            proxima_tentativa_em = NULL, atualizado_em = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (motivo, item["id"]),
    )
    atualizar_evento(conn, item["evento_id"])
    conn.commit()


def processar_um_envio(retry_failed=False):
    conn = database.get_db()
    try:
        item = reivindicar_proximo_envio(conn, retry_failed=retry_failed)
        if item:
            item = dict(item)
            motivo = revalidar_previsao_nowcasting(conn, item)
            if motivo:
                marcar_cancelado(conn, item, motivo)
                log(f"Previsao de radar cancelada na fila (id={item['id']}): {motivo}")
                return "cancelado"
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

    while limite is None or processados < limite:
        resultado = processar_um_envio(retry_failed=retry_failed)
        if resultado is None:
            break

        processados += 1
        if resultado == "enviado":
            enviados += 1
        elif resultado == "falhou":
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
    configurar_logging()
    try:
        main()
    except sqlite3.Error as erro:
        log(f"❌ Erro SQLite no worker de WhatsApp: {erro}")
        raise
