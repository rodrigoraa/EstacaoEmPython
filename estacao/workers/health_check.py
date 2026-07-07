import argparse
import json
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

import database
from time_utils import agora_local, formatar_local, iso_utc, para_local
from unsubscribe_tokens import telefone_com_codigo_pais


ESTADO_CHAVE = "health_check"


def env_int(nome, padrao):
    valor = os.environ.get(nome)
    if valor in (None, ""):
        return padrao
    try:
        return int(valor)
    except ValueError:
        return padrao


def obter_config():
    atraso_padrao = env_int("ADMIN_UPDATER_ATRASO_MINUTOS", 5)
    return {
        "updater_atraso_minutos": env_int("HEALTH_UPDATER_ATRASO_MINUTOS", atraso_padrao),
        "fila_pendente_minutos": env_int("HEALTH_FILA_PENDENTE_MINUTOS", 30),
        "enviando_expirado_minutos": env_int("WHATSAPP_ENVIANDO_EXPIRADO_MINUTOS", 10),
        "falhas_minimas": env_int("HEALTH_FALHAS_MINIMAS", 1),
        "cooldown_minutos": env_int("HEALTH_ALERT_COOLDOWN_MINUTOS", 60),
        "admin_phone": os.environ.get("ADMIN_ALERT_PHONE", "").strip(),
    }


def log(mensagem):
    agora = agora_local().strftime("%d/%m %H:%M:%S")
    print(f"[{agora}] {mensagem}", flush=True)


def garantir_estruturas(conn):
    database.garantir_tabela_alertas_fila(conn)
    database.garantir_tabela_alertas_envios(conn)
    database.garantir_tabela_logs_persistencia(conn)
    database.garantir_tabela_health_check_estado(conn)
    conn.commit()


def minutos_desde(valor, assume_utc=True):
    dt = para_local(valor, assume_utc=assume_utc)
    if not dt:
        return None
    diferenca = agora_local() - dt
    return max(0, int(diferenca.total_seconds() // 60))


def registrar_log(conn, nivel, mensagem, detalhe=None):
    conn.execute(
        """
        INSERT INTO logs_persistencia (nivel, origem, mensagem, detalhe)
        VALUES (?, ?, ?, ?)
        """,
        (nivel, "health_check", mensagem, detalhe),
    )


def ultima_coleta(conn):
    return conn.execute(
        """
        SELECT id, data_hora_local, data_hora_utc, data_hora, temp
        FROM historico_clima
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


def avaliar_coleta(conn, config):
    row = ultima_coleta(conn)
    if not row:
        return [
            {
                "codigo": "sem_coleta",
                "titulo": "Sem coleta registrada",
                "detalhe": "Nenhuma leitura foi encontrada em historico_clima.",
            }
        ]

    valor_data = row["data_hora_local"] or row["data_hora_utc"] or row["data_hora"]
    assume_utc = bool(row["data_hora_utc"] and not row["data_hora_local"])
    minutos = minutos_desde(valor_data, assume_utc=assume_utc)
    limite = config["updater_atraso_minutos"]
    if minutos is None:
        return [
            {
                "codigo": "coleta_data_invalida",
                "titulo": "Data da ultima coleta invalida",
                "detalhe": f"Ultimo registro id {row['id']} tem data nao reconhecida.",
            }
        ]

    if minutos > limite:
        return [
            {
                "codigo": "coleta_atrasada",
                "titulo": "Coleta atrasada",
                "detalhe": (
                    f"Ultima leitura ha {minutos} min "
                    f"({formatar_local(valor_data, assume_utc=assume_utc)}). "
                    f"Limite configurado: {limite} min."
                ),
            }
        ]

    return []


def avaliar_fila_whatsapp(conn, config):
    problemas = []
    pendentes = conn.execute(
        "SELECT COUNT(*) FROM alertas_fila WHERE status = 'pendente'"
    ).fetchone()[0]
    falhas = conn.execute(
        "SELECT COUNT(*) FROM alertas_fila WHERE status = 'falhou'"
    ).fetchone()[0]
    enviando_antigo = conn.execute(
        """
        SELECT COUNT(*)
        FROM alertas_fila
        WHERE status = 'enviando'
        AND atualizado_em <= datetime('now', ?)
        """,
        (f"-{config['enviando_expirado_minutos']} minutes",),
    ).fetchone()[0]
    pendente_antigo = conn.execute(
        """
        SELECT criado_em
        FROM alertas_fila
        WHERE status = 'pendente'
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()

    if pendente_antigo:
        idade = minutos_desde(pendente_antigo["criado_em"], assume_utc=True)
        if idade is not None and idade > config["fila_pendente_minutos"]:
            problemas.append(
                {
                    "codigo": "fila_pendente_antiga",
                    "titulo": "Fila de WhatsApp parada",
                    "detalhe": (
                        f"{pendentes} alerta(s) pendente(s); "
                        f"mais antigo ha {idade} min. "
                        f"Limite configurado: {config['fila_pendente_minutos']} min."
                    ),
                }
            )

    if falhas >= config["falhas_minimas"]:
        problemas.append(
            {
                "codigo": "falhas_whatsapp",
                "titulo": "Falhas no WhatsApp",
                "detalhe": (
                    f"{falhas} envio(s) com status falhou. "
                    f"Limite configurado: {config['falhas_minimas']}."
                ),
            }
        )

    if enviando_antigo:
        problemas.append(
            {
                "codigo": "envio_travado",
                "titulo": "Envio possivelmente travado",
                "detalhe": (
                    f"{enviando_antigo} item(ns) em enviando por mais de "
                    f"{config['enviando_expirado_minutos']} min."
                ),
            }
        )

    return problemas


def avaliar_saude(conn, config=None):
    config = config or obter_config()
    garantir_estruturas(conn)
    problemas = []
    problemas.extend(avaliar_coleta(conn, config))
    problemas.extend(avaliar_fila_whatsapp(conn, config))
    return problemas


def assinatura_problemas(problemas):
    codigos = [problema["codigo"] for problema in problemas]
    return json.dumps(codigos, ensure_ascii=False, sort_keys=True)


def montar_mensagem(problemas):
    linhas = ["Alerta interno da estacao meteorologica:"]
    for problema in problemas:
        linhas.append(f"- {problema['titulo']}: {problema['detalhe']}")
    return "\n".join(linhas)


def obter_estado(conn):
    return conn.execute(
        """
        SELECT status, assinatura, notificado_em
        FROM health_check_estado
        WHERE chave = ?
        """,
        (ESTADO_CHAVE,),
    ).fetchone()


def deve_notificar(estado, assinatura, config):
    if not estado:
        return True
    if estado["status"] != "problema":
        return True
    if estado["assinatura"] != assinatura:
        return True

    minutos = minutos_desde(estado["notificado_em"], assume_utc=True)
    if minutos is None:
        return True
    return minutos >= config["cooldown_minutos"]


def salvar_estado(conn, status, assinatura=None, mensagem=None, notificou=False):
    notificado_em = iso_utc() if notificou else None
    conn.execute(
        """
        INSERT INTO health_check_estado (
            chave, status, assinatura, mensagem, notificado_em, atualizado_em
        ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chave) DO UPDATE SET
            status = excluded.status,
            assinatura = excluded.assinatura,
            mensagem = excluded.mensagem,
            notificado_em = COALESCE(excluded.notificado_em, health_check_estado.notificado_em),
            atualizado_em = CURRENT_TIMESTAMP
        """,
        (ESTADO_CHAVE, status, assinatura, mensagem, notificado_em),
    )


def enviar_mensagem_admin(telefone, mensagem):
    from services.whatsapp_service import enviar_whatsapp

    enviar_whatsapp(telefone_com_codigo_pais(telefone), mensagem)


def processar_resultado(conn, problemas, config, permitir_whatsapp=True):
    estado = obter_estado(conn)
    assinatura = assinatura_problemas(problemas)
    mensagem = montar_mensagem(problemas) if problemas else "Health check OK."
    notificou = False

    if not problemas:
        if estado and estado["status"] == "problema":
            registrar_log(conn, "INFO", "Health check voltou ao normal.", mensagem)
        salvar_estado(conn, "ok", assinatura="[]", mensagem=mensagem, notificou=False)
        conn.commit()
        return {"ok": True, "notificado": False, "mensagem": mensagem}

    registrar_log(conn, "WARNING", "Health check encontrou problema.", mensagem)
    if permitir_whatsapp and config["admin_phone"] and deve_notificar(estado, assinatura, config):
        enviar_mensagem_admin(config["admin_phone"], mensagem)
        notificou = True

    salvar_estado(
        conn,
        "problema",
        assinatura=assinatura,
        mensagem=mensagem,
        notificou=notificou,
    )
    conn.commit()
    return {"ok": False, "notificado": notificou, "mensagem": mensagem}


def executar_health_check(permitir_whatsapp=True):
    config = obter_config()
    conn = database.get_db()
    try:
        problemas = avaliar_saude(conn, config=config)
        return processar_resultado(
            conn,
            problemas,
            config=config,
            permitir_whatsapp=permitir_whatsapp,
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Verifica se a coleta e a fila de WhatsApp estao saudaveis."
    )
    parser.add_argument(
        "--no-whatsapp",
        action="store_true",
        help="Apenas registra o resultado, sem enviar alerta administrativo.",
    )
    parser.add_argument(
        "--fail-on-issues",
        action="store_true",
        help="Retorna codigo 1 quando encontrar problemas.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    resultado = executar_health_check(permitir_whatsapp=not args.no_whatsapp)
    log(resultado["mensagem"])
    if resultado["notificado"]:
        log("Alerta administrativo enviado.")
    elif not resultado["ok"]:
        log("Problema registrado sem envio administrativo.")

    if args.fail_on_issues and not resultado["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except sqlite3.Error as erro:
        log(f"Erro SQLite no health check: {erro}")
        raise
