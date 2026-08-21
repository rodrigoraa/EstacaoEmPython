import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ESTACAO_DIR = PROJECT_ROOT / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class MelhoriasMeteorologiaPersistenciaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "teste.db")
        os.environ["SECRET_KEY"] = "segredo-teste"
        os.environ["PUBLIC_BASE_URL"] = "https://meteo.test/"

        import database
        import persistence
        import routes.api
        import routes.public

        self.database = importlib.reload(database)
        self.persistence = importlib.reload(persistence)
        self.api = importlib.reload(routes.api)
        self.public = importlib.reload(routes.public)
        self.database.init_db()

    def tearDown(self):
        self.tmp.cleanup()
        for chave in (
            "ESTACAO_DB",
            "SECRET_KEY",
            "PUBLIC_BASE_URL",
            "TEMPESTADE_CHUVA_RATE_MIN",
            "TEMPESTADE_RAJADA_MIN",
        ):
            os.environ.pop(chave, None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def test_deduplicacao_reutiliza_id_sem_apagar_historico(self):
        raw = {"dateutc": 1787300000000, "tempf": 50}
        primeiro = self.persistence.salvar_leitura_bruta(raw, {"temp": 10})
        segundo, inserida = self.persistence.salvar_leitura_bruta(
            raw, {"temp": 11}, retornar_status=True
        )

        conn = self.abrir_banco()
        total = conn.execute("SELECT COUNT(*) FROM leituras_brutas").fetchone()[0]
        payload = conn.execute(
            "SELECT dados_convertidos_json FROM leituras_brutas WHERE id = ?", (primeiro,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(segundo, primeiro)
        self.assertFalse(inserida)
        self.assertEqual(total, 1)
        self.assertIn('"temp": 10', payload)

    def test_previsao_nao_coleta_nem_persiste_leitura(self):
        conn = self.abrir_banco()
        conn.execute(
            "INSERT INTO historico_clima (temp, data_hora, data_hora_local) "
            "VALUES (0, '2026-08-21 10:00:00', '2026-08-21T10:00:00-04:00')"
        )
        conn.commit()
        antes = conn.execute("SELECT COUNT(*) FROM leituras_brutas").fetchone()[0]
        conn.close()

        app = Flask(
            __name__,
            template_folder=str(ESTACAO_DIR / "templates"),
            static_folder=str(ESTACAO_DIR / "static"),
        )
        app.config.update(TESTING=True, RATELIMIT_ENABLED=False, SECRET_KEY="teste")
        import extensions

        importlib.reload(extensions).limiter.init_app(app)
        app.register_blueprint(self.public.public_routes)
        self.public.obter_previsao = lambda **kwargs: None
        resposta = app.test_client().get("/previsao")

        conn = self.abrir_banco()
        depois = conn.execute("SELECT COUNT(*) FROM leituras_brutas").fetchone()[0]
        conn.close()
        self.assertEqual(resposta.status_code, 200)
        self.assertEqual(antes, depois)

    def test_temperatura_zero_e_negativa_entram_na_minima_mensal(self):
        conn = self.abrir_banco()
        conn.executemany(
            "INSERT INTO historico_diario (data, temp_min, temp_max) VALUES (?, ?, ?)",
            (("2026-01-01", 0, 5), ("2026-01-02", -5, 2)),
        )
        conn.commit()
        conn.close()
        app = Flask(__name__)
        app.register_blueprint(self.api.api_routes)

        resposta = app.test_client().get("/api/historico_consulta?ano=2026&mes=01")
        payload = resposta.get_json()
        self.assertEqual(payload["min_temp"], -5)
        self.assertEqual(payload["temp_min"][:2], [0, -5])

    def test_tempestade_exige_chuva_atual_e_rajada_temporalmente_proxima(self):
        atual = {
            "chuva_rate": 12,
            "vento_rajada": 20,
            "data_hora_local": "2026-08-21T10:00:00-04:00",
            "data_hora": None,
        }
        recente = {
            "chuva_rate": 0,
            "vento_rajada": 65,
            "data_hora_local": "2026-08-21T09:55:00-04:00",
            "data_hora": None,
        }
        antiga = dict(recente, data_hora_local="2026-08-21T08:00:00-04:00")

        self.assertTrue(self.api.possivel_tempestade(atual, [atual, recente]))
        self.assertFalse(self.api.possivel_tempestade(atual, [atual, antiga]))
        self.assertFalse(
            self.api.possivel_tempestade(dict(atual, chuva_rate=0), [atual, recente])
        )


class MigrationLegadaTest(unittest.TestCase):
    def test_migration_aditiva_idempotente_preserva_usuario_legado(self):
        with tempfile.TemporaryDirectory() as tmp:
            caminho = Path(tmp) / "legado.db"
            conn = sqlite3.connect(caminho)
            conn.execute(
                """
                CREATE TABLE usuarios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nome TEXT NOT NULL,
                    telefone TEXT NOT NULL UNIQUE,
                    endereco TEXT,
                    ativo INTEGER DEFAULT 1,
                    receber_whatsapp INTEGER DEFAULT 0,
                    criado_em TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO usuarios (nome, telefone, receber_whatsapp) VALUES ('Legado', '67999999999', 1)"
            )
            conn.commit()
            conn.close()
            os.environ["ESTACAO_DB"] = str(caminho)
            try:
                import database

                database = importlib.reload(database)
                database.init_db()
                database.init_db()
                conn = sqlite3.connect(caminho)
                usuario = conn.execute(
                    "SELECT nome, receber_whatsapp, status_cadastro FROM usuarios"
                ).fetchone()
                versao = conn.execute("SELECT versao FROM schema_version WHERE id = 1").fetchone()[0]
                total = conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
                conn.close()
                self.assertEqual(usuario, ("Legado", 1, "ativo"))
                self.assertEqual(versao, database.SCHEMA_VERSION)
                self.assertEqual(total, 1)
            finally:
                os.environ.pop("ESTACAO_DB", None)


if __name__ == "__main__":
    unittest.main()

