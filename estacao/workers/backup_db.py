import argparse
import logging
import os
import signal
import sqlite3
import sys
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import database
from logging_utils import configurar_logging


logger = logging.getLogger(__name__)


PAGINAS_POR_LOTE = 1024  # 4 MiB com páginas SQLite de 4096 bytes.


def progresso_backup(max_segundos=7200):
    """Callback por execução, sem pausas; o prazo também limita retries BUSY."""
    inicio = ultimo_log = time.monotonic()
    marco = 0

    def progresso(status, remaining, total):
        nonlocal marco, ultimo_log
        agora = time.monotonic()
        if agora - inicio >= max_segundos:
            raise TimeoutError(f"Backup SQLite excedeu {max_segundos} segundos")
        percentual = int(100 * (total - remaining) / total) if total else 0
        if status == sqlite3.SQLITE_DONE:
            percentual = 100
        elif status != sqlite3.SQLITE_OK:
            percentual = 0
        novo_marco = percentual // 25
        if novo_marco > marco or agora - ultimo_log >= 60:
            logger.info("Backup SQLite: %s%% (status=%s)", percentual, status)
            marco = max(marco, novo_marco)
            ultimo_log = agora

    return progresso


def criar_backup(destino, origem=None, max_segundos=7200):
    origem = Path(origem if origem is not None else database.DATABASE).resolve()
    destino = Path(destino).absolute()
    if not origem.is_file():
        raise FileNotFoundError(f"Banco de origem não encontrado: {origem}")
    if origem == destino.resolve():
        raise ValueError("O destino do backup não pode ser o banco de origem")
    if not destino.parent.is_dir():
        raise FileNotFoundError(f"Diretório de destino não existe: {destino.parent}")

    def validar_destino_livre():
        for sufixo in ("", "-journal", "-wal", "-shm"):
            arquivo = Path(f"{destino}{sufixo}")
            if os.path.lexists(arquivo):
                raise FileExistsError(f"Destino ou auxiliar já existe: {arquivo}")

    validar_destino_livre()
    if max_segundos <= 0:
        raise ValueError("O prazo do backup deve ser positivo")

    try:
        # Tudo que o SQLite criar (inclusive journal/WAL) fica nesta pasta privada.
        # Nenhum resíduo de execuções anteriores é removido.
        with tempfile.TemporaryDirectory(prefix=".backup-sqlite-", dir=destino.parent) as tmp:
            parcial = Path(tmp) / "backup.db"
            descritor = os.open(parcial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descritor)
            with ExitStack() as conexoes:
                conn_origem = conexoes.enter_context(closing(sqlite3.connect(
                    f"{origem.as_uri()}?mode=ro", uri=True, timeout=30,
                )))
                conn_destino = conexoes.enter_context(closing(sqlite3.connect(parcial, timeout=30)))
                conn_origem.backup(
                    conn_destino,
                    pages=PAGINAS_POR_LOTE,
                    progress=progresso_backup(max_segundos),
                    sleep=0.1,  # Espera apenas quando SQLite retorna BUSY/LOCKED.
                )
                # Apenas no destino: deixa o artefato independente de arquivos WAL.
                conn_destino.execute("PRAGMA journal_mode = DELETE")
                resultado = conn_destino.execute("PRAGMA quick_check").fetchone()[0]
                if resultado != "ok":
                    raise RuntimeError(f"Backup criado, mas quick_check retornou: {resultado}")
            # Publicação atômica, no mesmo filesystem, sem sobrescrever concorrentes.
            validar_destino_livre()
            os.link(parcial, destino)
        logger.info("Backup SQLite consistente criado em %s", destino)
        return destino
    except (Exception, KeyboardInterrupt) as erro:
        logger.error("Falha no backup SQLite de %s para %s: %s", origem, destino, erro)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backup consistente do SQLite via API backup")
    parser.add_argument("destino", help="Arquivo novo que receberá o backup")
    parser.add_argument("--origem", help="SQLite de origem (padrão: ESTACAO_DB ou estacao.db)")
    parser.add_argument("--max-segundos", type=int, default=7200, help="Prazo da cópia online (padrão: 7200)")
    args = parser.parse_args(argv)
    configurar_logging()

    def interromper(signum, frame):
        raise KeyboardInterrupt(f"sinal {signum}")

    anterior = signal.signal(signal.SIGTERM, interromper)
    try:
        criar_backup(args.destino, origem=args.origem, max_segundos=args.max_segundos)
        return 0
    except (Exception, KeyboardInterrupt) as erro:
        logger.error("Backup SQLite não concluído: %s", erro)
        return 1
    finally:
        signal.signal(signal.SIGTERM, anterior)


if __name__ == "__main__":
    raise SystemExit(main())
