import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))
from workers import backup_db


class BackupSQLiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origem = self.root / "origem.db"
        self.destino = self.root / "destino.db"
        with sqlite3.connect(self.origem) as conn:
            conn.execute("CREATE TABLE leituras (id INTEGER PRIMARY KEY, valor BLOB)")
            conn.executemany("INSERT INTO leituras VALUES (?, ?)",
                             ((i, bytes(4096)) for i in range(1100)))
        conn.close()

    def backup(self):
        return backup_db.criar_backup(self.destino, origem=self.origem)

    def test_copia_consistente_preserva_dados_e_origem(self):
        antes = self.origem.read_bytes()
        self.assertEqual(self.backup(), self.destino)
        with sqlite3.connect(self.destino) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(conn.execute("SELECT COUNT(*), SUM(id) FROM leituras").fetchone(),
                             (1100, sum(range(1100))))
        conn.close()
        self.assertEqual(self.origem.read_bytes(), antes)
        self.assertEqual(set(self.root.iterdir()), {self.origem, self.destino})
        if os.name == "posix":
            self.assertEqual(self.destino.stat().st_mode & 0o777, 0o600)

    def test_escrita_concorrente_em_wal_e_delete(self):
        for modo in ("WAL", "DELETE"):
            with self.subTest(modo=modo):
                writer = sqlite3.connect(self.origem, timeout=1)
                try:
                    writer.execute(f"PRAGMA journal_mode={modo}")
                    inserido = False
                    def progresso(status, remaining, total):
                        nonlocal inserido
                        if remaining > 0 and not inserido:
                            writer.execute("INSERT INTO leituras (valor) VALUES ('nova')")
                            writer.commit()
                            inserido = True
                    with mock.patch.object(backup_db, "progresso_backup", return_value=progresso):
                        self.backup()
                    self.assertTrue(inserido, "Banco precisa ocupar mais que um lote")
                    with sqlite3.connect(self.destino) as copia:
                        self.assertEqual(copia.execute("PRAGMA quick_check").fetchone(), ("ok",))
                        self.assertEqual(copia.execute("SELECT COUNT(*) FROM leituras").fetchone(),
                                         writer.execute("SELECT COUNT(*) FROM leituras").fetchone())
                    copia.close()
                    self.destino.unlink()
                finally:
                    writer.close()

    def test_falha_real_no_callback_remove_parciais_e_auxiliares(self):
        anterior = self.root / "anterior.db-journal"
        anterior.write_bytes(b"nao tocar")
        def falhar(*args):
            raise RuntimeError("falha durante copia")
        with mock.patch.object(backup_db, "progresso_backup", return_value=falhar):
            with self.assertRaisesRegex(RuntimeError, "falha durante copia"):
                self.backup()
        self.assertEqual(set(self.root.iterdir()), {self.origem, anterior})
        self.assertEqual(anterior.read_bytes(), b"nao tocar")
        self.backup()  # Conexões anteriores não deixaram o banco bloqueado.

    def test_destino_existente_e_origem_nunca_sao_removidos(self):
        self.destino.write_bytes(b"backup anterior")
        with self.assertRaises(FileExistsError):
            self.backup()
        self.assertEqual(self.destino.read_bytes(), b"backup anterior")
        with self.assertRaises(ValueError):
            backup_db.criar_backup(self.origem, origem=self.origem)
        self.assertTrue(self.origem.is_file())

    def test_destino_criado_por_concorrente_e_preservado(self):
        def publicar(*args):
            self.destino.write_bytes(b"concorrente")
            raise FileExistsError("concorrente")
        with mock.patch.object(backup_db.os, "link", side_effect=publicar):
            with self.assertRaises(FileExistsError):
                self.backup()
        self.assertEqual(self.destino.read_bytes(), b"concorrente")
        self.assertFalse(list(self.root.glob(".backup-sqlite-*")))

    def test_auxiliares_antigos_do_destino_sao_preservados_e_impedem_publicacao(self):
        for sufixo in ("-journal", "-wal", "-shm"):
            with self.subTest(sufixo=sufixo):
                auxiliar = Path(f"{self.destino}{sufixo}")
                auxiliar.write_bytes(b"residuo antigo")
                with self.assertRaises(FileExistsError):
                    self.backup()
                self.assertEqual(auxiliar.read_bytes(), b"residuo antigo")
                self.assertFalse(self.destino.exists())
                auxiliar.unlink()

    def test_origem_ausente_nao_cria_banco(self):
        ausente = self.root / "ausente.db"
        with self.assertRaises(FileNotFoundError):
            backup_db.criar_backup(self.destino, origem=ausente)
        self.assertFalse(ausente.exists())
        self.assertFalse(self.destino.exists())

    def test_falha_ao_abrir_destino_fecha_origem(self):
        origem = mock.MagicMock()
        with mock.patch.object(backup_db.sqlite3, "connect", side_effect=[origem, OSError("disco")]):
            with self.assertRaises(OSError):
                self.backup()
        origem.close.assert_called_once()
        self.assertFalse(list(self.root.glob(".backup-sqlite-*")))

    def test_prazo_interrompe_retries_busy(self):
        with mock.patch.object(backup_db.time, "monotonic", side_effect=[0, 11]):
            progresso = backup_db.progresso_backup(max_segundos=10)
            with self.assertRaises(TimeoutError):
                progresso(sqlite3.SQLITE_BUSY, 0, 0)

    def test_cli_retorna_erro_com_motivo(self):
        resultado = subprocess.run(
            [sys.executable, str(ROOT / "estacao/workers/backup_db.py"),
             "--origem", str(self.root / "ausente.db"), str(self.destino)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertNotEqual(resultado.returncode, 0)
        self.assertIn("não encontrado", resultado.stderr)
        self.assertFalse(self.destino.exists())


if __name__ == "__main__":
    unittest.main()
