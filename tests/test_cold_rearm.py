import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ESTACAO_DIR = Path(__file__).resolve().parents[1] / "estacao"
sys.path.insert(0, str(ESTACAO_DIR))


class RearmeFrioTest(unittest.TestCase):
    def setUp(self):
        temporario = tempfile.TemporaryDirectory()
        self.addCleanup(temporario.cleanup)
        ambiente = mock.patch.dict(
            os.environ,
            {
                "ESTACAO_DB": str(Path(temporario.name) / "frio.db"),
                "SECRET_KEY": "segredo-teste",
                "ALERTA_CONFIRMACOES_NIVEL_1": "2",
                "ALERTA_FRIO_NIVEL_1": "12.4",
                "ALERTA_FRIO_NIVEL_2": "5",
                "ALERTA_FRIO_NIVEL_3": "2",
                "ALERTA_FRIO_REARME": "15",
            },
        )
        ambiente.start()
        self.addCleanup(ambiente.stop)

        import database
        from workers import updater

        self.database = importlib.reload(database)
        self.database.init_db()
        self.updater = importlib.reload(updater)
        arquivo_estado = mock.patch.object(
            self.updater,
            "STATE_FILE",
            str(Path(temporario.name) / "alert_state.json"),
        )
        arquivo_estado.start()
        self.addCleanup(arquivo_estado.stop)
        conn = self.database.get_db()
        conn.execute(
            "INSERT INTO usuarios (nome, telefone, receber_whatsapp) VALUES (?, ?, 1)",
            ("Maria", "67999999999"),
        )
        conn.commit()
        conn.close()

    def leitura(self, temperatura, sensacao, sensacao_valida=True):
        self.updater.verificar_alertas(
            temperatura,
            sensacao,
            0,
            0,
            50,
            0,
            data_referencia="2026-07-19",
            ocorrido_em_local="2026-07-19T12:00:00-04:00",
            validade_alertas={
                "temperatura": True,
                "sensacao": sensacao_valida,
            },
        )

    def eventos_enfileirados(self):
        conn = self.database.get_db()
        eventos = conn.execute(
            """
            SELECT evento.evento_id
            FROM alertas_eventos evento
            JOIN alertas_fila fila USING (evento_id)
            WHERE evento.tipo = 'frio'
            ORDER BY evento.id
            """
        ).fetchall()
        conn.close()
        return [evento["evento_id"] for evento in eventos]

    def test_sensacao_fria_persistente_nao_rearma_nem_duplica(self):
        for _ in range(5):
            self.leitura(16, 12)

        self.assertEqual(
            self.eventos_enfileirados(), ["2026-07-19:frio:1:0"]
        )
        estado = self.database.obter_estado_alertas()
        self.assertEqual(estado["nivel_frio"], 1)
        self.assertEqual(estado["ciclo_frio"], 0)

    def test_temperatura_fria_preserva_ciclo_apos_sensacao_normalizar(self):
        self.leitura(16, 12)
        self.leitura(16, 12)
        self.leitura(12, 16)
        self.leitura(12, 16)

        self.assertEqual(
            self.eventos_enfileirados(), ["2026-07-19:frio:1:0"]
        )
        self.assertEqual(self.database.obter_estado_alertas()["ciclo_frio"], 0)

    def test_recuperacao_dos_dois_campos_permite_novo_ciclo(self):
        self.leitura(16, 12)
        self.leitura(16, 12)
        self.leitura(16, 16)

        estado = self.database.obter_estado_alertas()
        self.assertEqual((estado["nivel_frio"], estado["ciclo_frio"]), (0, 1))

        for _ in range(4):
            self.leitura(16, 12)

        self.assertEqual(
            self.eventos_enfileirados(),
            ["2026-07-19:frio:1:0", "2026-07-19:frio:1:1"],
        )
        self.assertEqual(self.database.obter_estado_alertas()["ciclo_frio"], 1)

    def test_sensacao_invalida_nao_impede_recuperacao(self):
        self.leitura(12, 12)
        self.leitura(12, 12)
        self.leitura(16, 12, sensacao_valida=False)
        self.leitura(16, 12, sensacao_valida=False)

        estado = self.database.obter_estado_alertas()
        self.assertEqual((estado["nivel_frio"], estado["ciclo_frio"]), (0, 1))
        self.assertEqual(
            self.eventos_enfileirados(), ["2026-07-19:frio:1:0"]
        )


if __name__ == "__main__":
    unittest.main()
