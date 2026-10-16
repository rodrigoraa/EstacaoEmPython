import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))
from services.subscriber_security import (
    garantir_seguranca_cadastros, reservar_envio, validar_cadastro, validar_telefone,
)


class SubscriberValidationTest(unittest.TestCase):
    def test_national_and_country_formats_have_one_destination(self):
        self.assertEqual(validar_telefone("(67) 99999-9999"), "5567999999999")
        self.assertEqual(validar_telefone("+55 (67) 99999-9999"), "5567999999999")
        self.assertEqual(validar_telefone("(55) 99999-9999"), "5555999999999")
        self.assertEqual(validar_telefone("(67) 3443-1234"), "556734431234")

    def test_rejects_fake_ddd_non_ascii_malformed_and_wrong_length(self):
        for value in ("1111111111111", "00999999999", "67123456789", "679999999999",
                      "67abc999999999", "６７９９９９９９９９９", "67\x00999999999", "1", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validar_telefone(value)

    def test_cadastro_bounds_and_controls_are_authoritative(self):
        self.assertEqual(validar_cadastro(" Ana ", "67999999999", " Rua 1 "),
                         ("Ana", "5567999999999", "Rua 1"))
        for nome, endereco in (("x" * 121, "Rua 1"), ("Ana", "x" * 241),
                               ("A\nna", "Rua 1"), ("A", "Rua 1"), ("Ana", "")):
            with self.subTest(nome=nome, endereco=endereco), self.assertRaises(ValueError):
                validar_cadastro(nome, "67999999999", endereco)


class LegacySubscriberMigrationTest(unittest.TestCase):
    def setUp(self):
        import database
        self.database = database
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.database.garantir_tabela_usuarios(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_duplicates_preserve_ids_history_and_existing_consent(self):
        self.conn.execute("""INSERT INTO usuarios (nome, telefone, endereco, receber_whatsapp, status_cadastro)
                             VALUES ('Pending', '67999999999', 'Rua 1', 0, 'pendente')""")
        self.conn.execute("""INSERT INTO usuarios (nome, telefone, endereco, receber_whatsapp, status_cadastro)
                             VALUES ('Consented', '+55 (67) 99999-9999', 'Rua 1', 1, 'ativo')""")
        self.conn.execute("CREATE TABLE history (usuario_id INTEGER, detalhe TEXT)")
        self.conn.execute("INSERT INTO history VALUES (1, 'preservar')")
        garantir_seguranca_cadastros(self.conn)
        rows = self.conn.execute("SELECT * FROM usuarios ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["duplicado_de"], 2)
        self.assertEqual((rows[0]["ativo"], rows[0]["receber_whatsapp"]), (0, 0))
        self.assertEqual((rows[1]["ativo"], rows[1]["receber_whatsapp"]), (1, 1))
        self.assertEqual(self.conn.execute("SELECT detalhe FROM history").fetchone()[0], "preservar")
        changes = self.conn.total_changes
        garantir_seguranca_cadastros(self.conn)
        self.assertEqual(self.conn.total_changes, changes)

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("""INSERT INTO usuarios (nome, telefone, endereco)
                                 VALUES ('Duplicate', '5567999999999', 'Rua 2')""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE usuarios SET ativo = 1, receber_whatsapp = 1 WHERE id = 1")

        self.conn.execute("""UPDATE usuarios SET telefone = '67988887777', ativo = 1,
                             receber_whatsapp = 1 WHERE id = 1""")
        recovered = self.conn.execute("SELECT * FROM usuarios WHERE id = 1").fetchone()
        self.assertIsNone(recovered["duplicado_de"])
        self.assertEqual(recovered["telefone_canonico"], "5567988887777")
        self.assertEqual(recovered["receber_whatsapp"], 1)

    def test_invalid_legacy_number_is_retained_for_admin_correction(self):
        self.conn.execute("""INSERT INTO usuarios (nome, telefone, endereco, receber_whatsapp)
                             VALUES ('Legacy', '123', 'Rua 1', 1)""")
        garantir_seguranca_cadastros(self.conn)
        row = self.conn.execute("SELECT * FROM usuarios").fetchone()
        self.assertEqual(row["telefone"], "123")
        self.assertEqual((row["ativo"], row["receber_whatsapp"]), (0, 0))
        self.assertIsNone(row["telefone_canonico"])
        self.assertEqual(len(row["cadastro_nonce"]), 64)


class AtomicCooldownTest(unittest.TestCase):
    def test_one_reservation_across_connections_and_bounded_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "cooldown.db")
            conn = sqlite3.connect(path)
            conn.execute("""CREATE TABLE cadastro_mensagens_controle (
                telefone TEXT, finalidade TEXT, reservado_em REAL,
                PRIMARY KEY(telefone, finalidade))""")
            conn.commit()
            conn.close()

            def reserve(_):
                db = sqlite3.connect(path, timeout=10)
                try:
                    result = reservar_envio(db, "67999999999", "confirmacao", agora=1000)
                    db.commit()
                    return result
                finally:
                    db.close()

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(reserve, range(4)))
            self.assertEqual(sum(results), 1)
            conn = sqlite3.connect(path)
            self.assertFalse(reservar_envio(conn, "5567999999999", "confirmacao", agora=1299))
            self.assertTrue(reservar_envio(conn, "67999999999", "confirmacao", agora=1300))
            conn.close()


if __name__ == "__main__":
    unittest.main()
