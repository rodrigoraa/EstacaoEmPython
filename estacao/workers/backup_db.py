import argparse
import logging
import os
import sqlite3
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
import sys

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import database
from logging_utils import configurar_logging


logger = logging.getLogger(__name__)


def progresso_backup(status, remaining, total):
    if remaining > 0:
        time.sleep(0.25)


def criar_backup(destino):
    origem = Path(database.DATABASE).resolve()
    destino = Path(destino).resolve()
    if not origem.is_file():
        raise FileNotFoundError(f"Banco de origem não encontrado: {origem}")
    if origem == destino:
        raise ValueError("O destino do backup não pode ser o banco de origem")
    if not destino.parent.is_dir():
        raise FileNotFoundError(f"Diretório de destino não existe: {destino.parent}")

    descritor = os.open(destino, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descritor)
    conn_origem = None
    conn_destino = None
    try:
        try:
            conn_origem = sqlite3.connect(origem, timeout=30)
            conn_destino = sqlite3.connect(destino, timeout=30)
            with conn_origem, conn_destino:
                conn_origem.backup(
                    conn_destino,
                    pages=50,
                    progress=progresso_backup,
                    sleep=0.25,
                )
                resultado = conn_destino.execute("PRAGMA quick_check").fetchone()[0]
                if resultado != "ok":
                    raise RuntimeError(f"Backup criado, mas quick_check retornou: {resultado}")
        finally:
            if conn_destino is not None:
                conn_destino.close()
            if conn_origem is not None:
                conn_origem.close()
        logger.info("Backup SQLite consistente criado em %s", destino)
        return destino
    except (Exception, KeyboardInterrupt):
        try:
            destino.unlink()
        except OSError:
            logger.error("Não foi possível remover backup parcial: %s", destino)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backup consistente do SQLite via API backup")
    parser.add_argument("destino", help="Arquivo novo que receberá o backup")
    args = parser.parse_args(argv)
    configurar_logging()
    criar_backup(args.destino)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
