import argparse
import logging
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import database
from config import env_bool, env_int, radar_config, regional_stations_config
from logging_utils import configurar_logging
from time_utils import agora_utc


logger = logging.getLogger(__name__)

RETENCOES = (
    ("leituras_brutas", "recebido_em", "RETENCAO_LEITURAS_BRUTAS_DIAS", 365),
    ("logs_persistencia", "data_hora", "RETENCAO_LOGS_DIAS", 90),
    ("alertas_envios", "data_hora", "RETENCAO_ALERTAS_ENVIOS_DIAS", 365),
    ("cadastro_eventos", "data_hora", "RETENCAO_CADASTRO_EVENTOS_DIAS", 730),
)
TABELAS_CONTAGEM = (
    "historico_clima",
    "leituras_brutas",
    "historico_diario",
    "usuarios",
    "alertas_envios",
    "alertas_fila",
    "alertas_eventos",
    "logs_persistencia",
    "cadastro_eventos",
    "campanhas_whatsapp_envios",
    "radar_frames",
    "radar_clusters",
    "radar_tracks",
    "radar_track_points",
    "regional_stations",
    "regional_station_observations",
    "regional_station_state",
)
INDICES_MANUAIS = (
    (
        "idx_leituras_brutas_origem_station_ts",
        "CREATE INDEX IF NOT EXISTS idx_leituras_brutas_origem_station_ts "
        "ON leituras_brutas(origem, station_timestamp_ms)",
    ),
    (
        "idx_leituras_brutas_recebido_em",
        "CREATE INDEX IF NOT EXISTS idx_leituras_brutas_recebido_em "
        "ON leituras_brutas(recebido_em)",
    ),
    (
        "idx_historico_clima_data_hora_local",
        "CREATE INDEX IF NOT EXISTS idx_historico_clima_data_hora_local "
        "ON historico_clima(data_hora_local)",
    ),
)


def data_limite(dias):
    return (agora_utc() - timedelta(days=max(1, dias))).strftime("%Y-%m-%d %H:%M:%S")


def plano_retencao(conn):
    plano = []
    for tabela, coluna, variavel, padrao in RETENCOES:
        dias = max(1, env_int(variavel, padrao))
        limite = data_limite(dias)
        total = conn.execute(
            f"SELECT COUNT(*) FROM {tabela} WHERE {coluna} IS NOT NULL AND {coluna} < ?",
            (limite,),
        ).fetchone()[0]
        plano.append(
            {
                "tabela": tabela,
                "coluna": coluna,
                "variavel": variavel,
                "dias": dias,
                "limite": limite,
                "total": total,
            }
        )
    return plano


def executar_cleanup(conn, lote=None):
    if not env_bool("RETENCAO_AUTOMATICA", False):
        raise RuntimeError(
            "Cleanup bloqueado: defina RETENCAO_AUTOMATICA=true e use --cleanup explicitamente"
        )
    lote = max(1, lote or env_int("RETENCAO_DELETE_BATCH_SIZE", 1000))
    removidos = {}
    for item in plano_retencao(conn):
        total = 0
        while True:
            ids = conn.execute(
                f"SELECT id FROM {item['tabela']} "
                f"WHERE {item['coluna']} IS NOT NULL AND {item['coluna']} < ? "
                "ORDER BY id LIMIT ?",
                (item["limite"], lote),
            ).fetchall()
            if not ids:
                break
            marcadores = ",".join("?" for _ in ids)
            conn.execute(
                f"DELETE FROM {item['tabela']} WHERE id IN ({marcadores})",
                tuple(row[0] for row in ids),
            )
            conn.commit()
            total += len(ids)
            logger.info("Cleanup %s: %s removidos", item["tabela"], total)
        removidos[item["tabela"]] = total
    return removidos


def integrity_check(conn):
    return conn.execute("PRAGMA integrity_check").fetchone()[0]


def contagens(conn):
    return {tabela: conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0] for tabela in TABELAS_CONTAGEM}


def criar_indices(conn):
    for nome, sql in INDICES_MANUAIS:
        logger.info("Criando/verificando índice manual: %s", nome)
        conn.execute(sql)
        conn.commit()


def auditar_duplicidades(conn):
    return conn.execute(
        """
        SELECT origem, station_timestamp_ms, COUNT(*) AS total
        FROM leituras_brutas
        WHERE station_timestamp_ms IS NOT NULL
        GROUP BY origem, station_timestamp_ms
        HAVING COUNT(*) > 1
        ORDER BY total DESC
        """
    ).fetchall()


def plano_retencao_radar(conn):
    config = radar_config()
    limite_imagens = data_limite(config["retention_images_days"])
    limite_frames = data_limite(config["retention_frames_days"])
    imagens = conn.execute(
        """
        SELECT COUNT(*) FROM radar_frames
        WHERE data_frame < ? AND (arquivo_local IS NOT NULL OR arquivo_analisado IS NOT NULL)
        """,
        (limite_imagens,),
    ).fetchone()[0]
    frames = conn.execute(
        "SELECT COUNT(*) FROM radar_frames WHERE data_frame < ?", (limite_frames,)
    ).fetchone()[0]
    return {
        "imagens_dias": config["retention_images_days"],
        "frames_dias": config["retention_frames_days"],
        "limite_imagens": limite_imagens,
        "limite_frames": limite_frames,
        "imagens": imagens,
        "frames": frames,
    }


def _arquivo_radar_seguro(raiz: Path, relativo: str | None):
    if not relativo:
        return None
    candidato = (raiz / relativo).resolve()
    try:
        candidato.relative_to(raiz)
    except ValueError:
        logger.warning("Arquivo fora de RADAR_DATA_DIR ignorado na retencao")
        return None
    return candidato if candidato.suffix.lower() == ".png" else None


def executar_cleanup_radar(conn, lote=None):
    config = radar_config()
    if not config["retention_enabled"]:
        raise RuntimeError(
            "Cleanup do radar bloqueado: defina RADAR_RETENCAO_AUTOMATICA=true "
            "e use --radar-cleanup explicitamente"
        )
    lote = max(1, lote or env_int("RETENCAO_DELETE_BATCH_SIZE", 1000))
    raiz = Path(config["data_dir"]).resolve()
    if raiz == Path(raiz.anchor):
        raise RuntimeError("RADAR_DATA_DIR nao pode ser a raiz do sistema de arquivos")
    plano = plano_retencao_radar(conn)
    arquivos_removidos = frames_removidos = 0

    while True:
        rows = conn.execute(
            """
            SELECT id, arquivo_local, arquivo_analisado FROM radar_frames
            WHERE data_frame < ? AND (arquivo_local IS NOT NULL OR arquivo_analisado IS NOT NULL)
            ORDER BY id LIMIT ?
            """,
            (plano["limite_imagens"], lote),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            for coluna in ("arquivo_local", "arquivo_analisado"):
                caminho = _arquivo_radar_seguro(raiz, row[coluna])
                if caminho and caminho.is_file():
                    caminho.unlink()
                    arquivos_removidos += 1
            conn.execute(
                "UPDATE radar_frames SET arquivo_local=NULL, arquivo_analisado=NULL WHERE id=?",
                (row["id"],),
            )
        conn.commit()

    while True:
        rows = conn.execute(
            "SELECT id, arquivo_local, arquivo_analisado FROM radar_frames "
            "WHERE data_frame < ? ORDER BY id LIMIT ?",
            (plano["limite_frames"], lote),
        ).fetchall()
        if not rows:
            break
        for row in rows:
            for coluna in ("arquivo_local", "arquivo_analisado"):
                caminho = _arquivo_radar_seguro(raiz, row[coluna])
                if caminho and caminho.is_file():
                    caminho.unlink()
                    arquivos_removidos += 1
        ids = tuple(row["id"] for row in rows)
        marcadores = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM radar_frames WHERE id IN ({marcadores})", ids)
        conn.execute(
            "DELETE FROM radar_tracks WHERE NOT EXISTS "
            "(SELECT 1 FROM radar_track_points p WHERE p.track_id=radar_tracks.id)"
        )
        conn.commit()
        frames_removidos += len(ids)
    return {"arquivos": arquivos_removidos, "frames": frames_removidos}


def plano_retencao_regional(conn):
    config = regional_stations_config()
    limite = data_limite(config["retention_days"])
    total = conn.execute(
        "SELECT COUNT(*) FROM regional_station_observations "
        "WHERE coletado_em_utc < ?",
        (limite,),
    ).fetchone()[0]
    return {"dias": config["retention_days"], "limite": limite, "total": total}


def executar_cleanup_regional(conn, lote=None):
    config = regional_stations_config()
    if not config["retention_enabled"]:
        raise RuntimeError(
            "Cleanup regional bloqueado: defina "
            "REGIONAL_STATIONS_RETENTION_ENABLED=true e use --regional-cleanup"
        )
    lote = max(1, lote or env_int("RETENCAO_DELETE_BATCH_SIZE", 1000))
    plano = plano_retencao_regional(conn)
    removidos = 0
    while True:
        ids = conn.execute(
            "SELECT id FROM regional_station_observations "
            "WHERE coletado_em_utc < ? ORDER BY id LIMIT ?",
            (plano["limite"], lote),
        ).fetchall()
        if not ids:
            break
        marcadores = ",".join("?" for _ in ids)
        conn.execute(
            f"DELETE FROM regional_station_observations WHERE id IN ({marcadores})",
            tuple(row[0] for row in ids),
        )
        conn.commit()
        removidos += len(ids)
    return removidos


def main(argv=None):
    parser = argparse.ArgumentParser(description="Auditoria e manutenção explícita do SQLite")
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--dry-run", action="store_true")
    grupo.add_argument("--cleanup", action="store_true")
    grupo.add_argument("--integrity-check", action="store_true")
    grupo.add_argument("--counts", action="store_true")
    grupo.add_argument("--create-indexes", action="store_true")
    grupo.add_argument("--audit-duplicates", action="store_true")
    grupo.add_argument("--radar-dry-run", action="store_true")
    grupo.add_argument("--radar-cleanup", action="store_true")
    grupo.add_argument("--regional-dry-run", action="store_true")
    grupo.add_argument("--regional-cleanup", action="store_true")
    parser.add_argument("--batch-size", type=int)
    args = parser.parse_args(argv)
    configurar_logging()

    conn = database.get_db()
    try:
        if args.dry_run:
            for item in plano_retencao(conn):
                print(
                    f"{item['tabela']}: criterio {item['coluna']} < {item['limite']}; "
                    f"estimativa={item['total']} ({item['variavel']}={item['dias']})"
                )
        elif args.cleanup:
            for tabela, total in executar_cleanup(conn, args.batch_size).items():
                print(f"{tabela}: removidos={total}")
        elif args.integrity_check:
            resultado = integrity_check(conn)
            print(resultado)
            return 0 if resultado == "ok" else 1
        elif args.counts:
            for tabela, total in contagens(conn).items():
                print(f"{tabela}: {total}")
        elif args.create_indexes:
            criar_indices(conn)
        elif args.audit_duplicates:
            linhas = auditar_duplicidades(conn)
            for row in linhas:
                print(f"{row['origem']} {row['station_timestamp_ms']}: {row['total']}")
            print(f"grupos_duplicados={len(linhas)}")
        elif args.radar_dry_run:
            plano = plano_retencao_radar(conn)
            print(
                f"radar imagens: criterio data_frame < {plano['limite_imagens']}; "
                f"frames_com_arquivos={plano['imagens']} "
                f"(RADAR_RETENCAO_IMAGENS_DIAS={plano['imagens_dias']})"
            )
            print(
                f"radar frames: criterio data_frame < {plano['limite_frames']}; "
                f"frames={plano['frames']} "
                f"(RADAR_RETENCAO_FRAMES_DIAS={plano['frames_dias']})"
            )
        elif args.radar_cleanup:
            for item, total in executar_cleanup_radar(conn, args.batch_size).items():
                print(f"radar {item}: removidos={total}")
        elif args.regional_dry_run:
            plano = plano_retencao_regional(conn)
            print(
                f"regional_station_observations: criterio coletado_em_utc < "
                f"{plano['limite']}; estimativa={plano['total']} "
                f"(REGIONAL_STATIONS_RETENTION_DAYS={plano['dias']})"
            )
        elif args.regional_cleanup:
            total = executar_cleanup_regional(conn, args.batch_size)
            print(f"regional_station_observations: removidos={total}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
