import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class HealthCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "estacao_teste.db")
        os.environ["SECRET_KEY"] = "teste"
        os.environ.pop("ADMIN_ALERT_PHONE", None)

        import database
        import workers.health_check as health_check

        self.database = importlib.reload(database)
        self.health_check = importlib.reload(health_check)
        self.database.init_db()

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("ESTACAO_DB", None)
        os.environ.pop("SECRET_KEY", None)
        os.environ.pop("ADMIN_ALERT_PHONE", None)
        os.environ.pop("HEALTH_ALERT_COOLDOWN_MINUTOS", None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def inserir_coleta(self, minutos_atras=0):
        data = (
            self.health_check.agora_local() - timedelta(minutes=minutos_atras)
        ).replace(microsecond=0).isoformat()
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO historico_clima (data_hora_local, data_hora, temp)
            VALUES (?, ?, ?)
            """,
            (data, data, 23.0),
        )
        conn.commit()
        conn.close()

    def test_coleta_recente_fica_ok(self):
        self.inserir_coleta(minutos_atras=1)
        conn = self.abrir_banco()

        problemas = self.health_check.avaliar_saude(conn)
        conn.close()

        self.assertEqual(problemas, [])

    def test_coleta_atrasada_vira_problema(self):
        self.inserir_coleta(minutos_atras=20)
        conn = self.abrir_banco()

        problemas = self.health_check.avaliar_saude(conn)
        conn.close()

        self.assertIn("coleta_atrasada", {problema["codigo"] for problema in problemas})

    def test_fila_antiga_e_falhas_viram_problema(self):
        self.inserir_coleta(minutos_atras=1)
        antigo = "2024-01-01T00:00:00+00:00"
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO alertas_fila (criado_em, telefone, mensagem, status)
            VALUES (?, ?, ?, ?)
            """,
            (antigo, "5567999999999", "Pendente", "pendente"),
        )
        conn.execute(
            """
            INSERT INTO alertas_fila (telefone, mensagem, status)
            VALUES (?, ?, ?)
            """,
            ("5567888888888", "Falhou", "falhou"),
        )
        conn.commit()

        problemas = self.health_check.avaliar_saude(conn)
        conn.close()
        codigos = {problema["codigo"] for problema in problemas}

        self.assertIn("fila_pendente_antiga", codigos)
        self.assertIn("falhas_whatsapp", codigos)

    def test_alerta_admin_respeita_cooldown(self):
        os.environ["ADMIN_ALERT_PHONE"] = "67999999999"
        os.environ["HEALTH_ALERT_COOLDOWN_MINUTOS"] = "60"
        self.health_check = importlib.reload(self.health_check)
        self.inserir_coleta(minutos_atras=20)
        envios = []
        self.health_check.enviar_mensagem_admin = (
            lambda telefone, mensagem: envios.append((telefone, mensagem))
        )

        primeiro = self.health_check.executar_health_check()
        segundo = self.health_check.executar_health_check()

        self.assertFalse(primeiro["ok"])
        self.assertTrue(primeiro["notificado"])
        self.assertFalse(segundo["notificado"])
        self.assertEqual(len(envios), 1)
        self.assertEqual(envios[0][0], "67999999999")


if __name__ == "__main__":
    unittest.main()
