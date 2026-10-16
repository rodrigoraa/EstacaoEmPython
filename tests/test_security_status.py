import importlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))

from workers.security_status import coletar_status


class SecurityStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "security.db"
        self.env = patch.dict(os.environ, {
            "APP_ENV": "development", "ESTACAO_DB": str(self.path),
            "SECRET_KEY": "private-secret-must-not-appear-in-json",
            "ADMIN_PASSWORD": "private-password-must-not-appear-in-json",
            "RATELIMIT_STORAGE_URI": "redis://private-user:private-password@127.0.0.1:6379/1",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_missing_db_is_not_created_and_no_secret_is_printed(self):
        report = coletar_status()
        self.assertFalse(self.path.exists())
        text = json.dumps(report)
        self.assertNotIn("private-secret", text)
        self.assertNotIn("private-password", text)
        self.assertNotIn("redis://", text)
        self.assertTrue(report["somente_leitura"])
        self.assertGreater(report["pendencias"], 0)

    def test_existing_db_is_not_migrated_or_changed(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("CREATE TABLE usuarios(id INTEGER PRIMARY KEY, telefone TEXT)")
            conn.execute("INSERT INTO usuarios VALUES(1, '123')")
        before = self.path.read_bytes()
        report = coletar_status()
        self.assertEqual(self.path.read_bytes(), before)
        statuses = {check["item"]: check["status"] for check in report["checks"]}
        self.assertEqual(statuses["migracao_cadastros"], "pendente")
        self.assertEqual(statuses["migracao_sessoes"], "pendente")

    def test_migrated_db_is_detected_without_creating_session_or_sending(self):
        import database
        importlib.reload(database).init_db()
        report = coletar_status()
        statuses = {check["item"]: check["status"] for check in report["checks"]}
        self.assertEqual(statuses["migracao_cadastros"], "ok")
        self.assertEqual(statuses["migracao_sessoes"], "ok")
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admin_sessoes").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
