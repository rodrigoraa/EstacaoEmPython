import importlib
import inspect
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

        with mock.patch.object(self.backup.time, "sleep"):
            self.backup.criar_backup(destino)
        backup_conn = sqlite3.connect(destino)
        try:
            self.assertEqual(backup_conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(backup_conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0], 1)
        finally:
            backup_conn.close()
        with self.assertRaises(FileExistsError):
            self.backup.criar_backup(destino)

    def test_backup_usa_lotes_pequenos_e_callback_de_progresso(self):
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
            pages=50,
            progress=self.backup.progresso_backup,
            sleep=0.25,
        )
        conn_destino.execute.assert_called_once_with("PRAGMA quick_check")
        conn_destino.close.assert_called_once_with()
        conn_origem.close.assert_called_once_with()

    def test_callback_de_progresso_controla_o_ritmo_sem_pausa_final(self):
        parametros = inspect.signature(self.backup.progresso_backup).parameters
        self.assertEqual(list(parametros), ["status", "remaining", "total"])

        with mock.patch.object(self.backup.time, "sleep") as sleep:
            self.backup.progresso_backup(0, 10, 20)
            sleep.assert_called_once_with(0.25)

            sleep.reset_mock()
            self.backup.progresso_backup(0, 0, 20)
            sleep.assert_not_called()

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
