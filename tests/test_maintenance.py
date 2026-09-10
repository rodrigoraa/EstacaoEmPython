import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
            self.assertEqual(backup_conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(backup_conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 1)
        finally:
            backup_conn.close()
        with self.assertRaises(FileExistsError):
            self.backup.criar_backup(destino)

    def test_backup_usa_lotes_incrementais_sem_pausa_artificial(self):
        destino = Path(self.tmp.name) / "backup.db"
        conn_origem = mock.MagicMock()
        conn_destino = mock.MagicMock()
        conn_destino.execute.return_value.fetchone.return_value = ("ok",)

        with mock.patch.object(
            self.backup.sqlite3,
            "connect",
            side_effect=(conn_origem, conn_destino),
        ):
            self.backup.criar_backup(destino)

        conn_origem.backup.assert_called_once_with(
            conn_destino,
            pages=1024,
            progress=mock.ANY,
            sleep=0.1,
        )
        self.assertTrue(callable(conn_origem.backup.call_args.kwargs["progress"]))
        conn_destino.execute.assert_any_call("PRAGMA quick_check")
        conn_destino.close.assert_called_once_with()
        conn_origem.close.assert_called_once_with()

    def test_callback_de_progresso_limita_logs_sem_dormir(self):
        progresso = self.backup.progresso_backup()
        with mock.patch.object(self.backup.time, "sleep") as sleep:
            with self.assertLogs(self.backup.logger, level="INFO") as logs:
                for remaining in range(10000, -1, -1):
                    progresso(sqlite3.SQLITE_OK, remaining, 10000)
            sleep.assert_not_called()
        self.assertEqual(len(logs.output), 4)
        for percentual, linha in zip((25, 50, 75, 100), logs.output):
            self.assertIn(f"{percentual}%", linha)

    def test_quick_check_invalido_remove_backup_parcial(self):
        destino = Path(self.tmp.name) / "backup.db"
        conn_origem = mock.MagicMock()
        conn_destino = mock.MagicMock()
        conn_destino.execute.return_value.fetchone.return_value = ("corrupt",)

        with mock.patch.object(
            self.backup.sqlite3,
            "connect",
            side_effect=(conn_origem, conn_destino),
        ):
            with self.assertRaisesRegex(RuntimeError, "quick_check retornou: corrupt"):
                self.backup.criar_backup(destino)

        self.assertFalse(destino.exists())
        conn_destino.close.assert_called_once_with()
        conn_origem.close.assert_called_once_with()

    def test_falha_no_backup_fecha_conexoes_e_remove_arquivo_parcial(self):
        for erro in (RuntimeError("falha"), KeyboardInterrupt()):
            with self.subTest(tipo=type(erro).__name__):
                destino = Path(self.tmp.name) / f"backup-{type(erro).__name__}.db"
                conn_origem = mock.MagicMock()
                conn_destino = mock.MagicMock()
                conn_origem.backup.side_effect = erro

                with mock.patch.object(
                    self.backup.sqlite3,
                    "connect",
                    side_effect=(conn_origem, conn_destino),
                ):
                    with self.assertRaises(type(erro)):
                        self.backup.criar_backup(destino)

                self.assertFalse(destino.exists())
                conn_destino.close.assert_called_once_with()
                conn_origem.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
