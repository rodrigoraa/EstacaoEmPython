import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class MaintenanceBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "teste.db"
        os.environ["ESTACAO_DB"] = str(self.db_path)
        import database
        import workers.backup_db
        import workers.maintenance

        self.database = importlib.reload(database)
        self.maintenance = importlib.reload(workers.maintenance)
        self.backup = importlib.reload(workers.backup_db)
        self.database.init_db()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("ESTACAO_DB", None)
        os.environ.pop("RETENCAO_AUTOMATICA", None)

    def test_dry_run_nao_remove_e_cleanup_remove_em_lotes(self):
        conn = self.database.get_db()
        antigos = [
            ("2020-01-01 00:00:00", "{}")
            for _ in range(2105)
        ]
        conn.executemany(
            "INSERT INTO leituras_brutas (recebido_em, payload_json) VALUES (?, ?)",
            antigos,
        )
        conn.execute(
            "INSERT INTO leituras_brutas (recebido_em, payload_json) VALUES ('2099-01-01 00:00:00', '{}')"
        )
        conn.commit()
        plano = self.maintenance.plano_retencao(conn)
        self.assertEqual(
            next(item for item in plano if item["tabela"] == "leituras_brutas")["total"],
            2105,
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM leituras_brutas").fetchone()[0], 2106)

        os.environ["RETENCAO_AUTOMATICA"] = "true"
        removidos = self.maintenance.executar_cleanup(conn, lote=1000)
        self.assertEqual(removidos["leituras_brutas"], 2105)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM leituras_brutas").fetchone()[0], 1)
        conn.close()

    def test_cleanup_exige_opt_in(self):
        conn = self.database.get_db()
        with self.assertRaises(RuntimeError):
            self.maintenance.executar_cleanup(conn)
        conn.close()

    def test_backup_consistente_nao_sobrescreve(self):
        conn = self.database.get_db()
        conn.execute(
            "INSERT INTO usuarios (nome, telefone) VALUES ('Teste', '67999999999')"
        )
        conn.commit()
        conn.close()
        destino = Path(self.tmp.name) / "backup.db"

        self.backup.criar_backup(destino)
        backup_conn = sqlite3.connect(destino)
        try:
            self.assertEqual(backup_conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup_conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 1)
        finally:
            backup_conn.close()
        with self.assertRaises(FileExistsError):
            self.backup.criar_backup(destino)


if __name__ == "__main__":
    unittest.main()
