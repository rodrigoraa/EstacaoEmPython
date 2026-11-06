import importlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


class WindHistoricalSequenceTest(unittest.TestCase):
    def test_pico_de_05_setembro_nao_vira_novo_alerta_em_06_setembro(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {
            "ESTACAO_DB": str(Path(tmp) / "test.db"), "SECRET_KEY": "test-only",
            "APP_ENV": "development", "ALERTA_VENTO_JANELA_MINUTOS": "3",
        }):
            import database
            import persistence
            import workers.updater
            database = importlib.reload(database)
            database.init_db()
            updater = importlib.reload(workers.updater)
            updater.STATE_FILE = str(Path(tmp) / "state.json")
            logs = []
            updater.log = logs.append
            estado = updater.estado_alertas_padrao("2026-09-05")
            estado.update({"nivel_vento": 1, "rajada_max_nuvem": 43.9})
            database.salvar_estado_alertas(estado)
            with database.get_db() as conn:
                conn.execute("INSERT INTO usuarios (nome, telefone, receber_whatsapp) VALUES ('Teste', '67999999999', 1)")
                conn.execute("INSERT INTO acumulados_diarios (data, rajada_max_corrigida) VALUES ('2026-09-05', 43.9)")

            def executar(horario, rajada, maxima):
                momento = datetime.fromisoformat(horario)
                utc = momento.astimezone(timezone.utc).isoformat()
                dados = {
                    "station_timestamp_ms": int(momento.timestamp() * 1000),
                    "station_data_hora_local": horario, "station_data_hora_utc": utc,
                    "temp": 25, "sensacao": 25, "umidade": 60, "pressao": 1012,
                    "uv": 0, "radiacao": 0, "vento": 5, "rajada": rajada,
                    "rajada_max": maxima, "vento_dir": 180, "chuva_rate": 0,
                    "chuva_evento": 0, "chuva_hoje": 0,
                    "validade_alertas": {"vento": True, "vento_max_diario": True},
                }
                with (
                    mock.patch.object(updater, "obter_dados", return_value=dados),
                    mock.patch.object(updater, "agora_local", return_value=momento),
                    mock.patch.object(persistence, "iso_local", return_value=horario),
                    mock.patch.object(persistence, "iso_utc", return_value=utc),
                ):
                    updater.executar()

            # Leituras reais fornecidas pelo usuário, sem dados pessoais.
            anteriores = (21.9, 25.6, 18.3, 40.4, 31.4, 23.8, 33.2, 27.7, 27.7, 23.8)
            atuais = (21.9, 23.8, 29.5, 36.7, 25.6, 27.7, 25.6, 29.5,
                      25.6, 36.7, 29.5, 18.3, 21.9, 34.9, 21.9)
            for minuto, rajada in enumerate(anteriores, 50):
                executar(f"2026-09-05T23:{minuto:02d}:00-04:00", rajada,
                         34.9 if minuto < 53 else 40.4)
            for minuto, rajada in enumerate(atuais):
                executar(f"2026-09-06T00:{minuto:02d}:00-04:00", rajada, 40.4)
            with database.get_db() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM historico_clima").fetchone()[0], 25)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_eventos WHERE tipo='vento'").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT rajada_max_corrigida FROM acumulados_diarios WHERE data='2026-09-06'").fetchone()[0], 36.7)

            # Controle: uma nova rajada legítima continua gerando seu alerta.
            for minuto in range(15, 19):
                executar(f"2026-09-06T00:{minuto:02d}:00-04:00",
                         42.2 if minuto == 15 else 25.6, 42.2)
            with database.get_db() as conn:
                eventos = conn.execute("SELECT valor, ocorrido_em_local FROM alertas_eventos WHERE tipo='vento'").fetchall()
                self.assertEqual([tuple(r) for r in eventos], [(42.2, '2026-09-06T00:15:00-04:00')])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM historico_clima").fetchone()[0], 29)
            self.assertFalse([mensagem for mensagem in logs if mensagem.startswith("❌ Erro")])


if __name__ == "__main__":
    unittest.main()
