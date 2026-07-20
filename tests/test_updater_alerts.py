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


class AlertasWorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ESTACAO_DB"] = str(Path(self.tmp.name) / "estacao_teste.db")
        os.environ["SECRET_KEY"] = "segredo-teste"
        os.environ["ALERTA_CONFIRMACOES_NIVEL_1"] = "1"

        import database

        self.database = importlib.reload(database)
        self.database.init_db()

        import workers.updater

        self.updater = importlib.reload(workers.updater)

    def tearDown(self):
        self.tmp.cleanup()
        for chave in (
            "ESTACAO_DB",
            "SECRET_KEY",
            "ALERTA_CONFIRMACOES_NIVEL_1",
        ):
            os.environ.pop(chave, None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def test_executar_persiste_antes_de_processar_alertas(self):
        dados = {
            "leitura_bruta_id": 321,
            "station_data_hora_local": "2026-07-19T12:00:00-04:00",
            "temp": 25.0,
            "sensacao": 26.0,
            "umidade": 70.0,
            "pressao": 1012.0,
            "uv": 3.0,
            "radiacao": 450.0,
            "vento": 8.0,
            "rajada": 12.0,
            "rajada_max": 15.0,
            "vento_dir": 180.0,
            "chuva_rate": 0.0,
            "chuva_evento": 1.0,
            "chuva_hoje": 2.0,
            "validade_alertas": {
                "temperatura": True,
                "sensacao": True,
                "vento": True,
                "chuva": True,
                "umidade": True,
                "uv": True,
            },
        }
        ordem = []
        persistir = mock.Mock(side_effect=lambda *args, **kwargs: ordem.append("persistir"))
        acumular = mock.Mock(
            side_effect=lambda *args, **kwargs: ordem.append("acumular")
            or {"rajada_max_corrigida": 15.0, "chuva_total_corrigida": 2.0}
        )
        alertar = mock.Mock(side_effect=lambda *args, **kwargs: ordem.append("alertar"))

        with (
            mock.patch.object(self.updater, "obter_dados", return_value=dados),
            mock.patch.object(
                self.updater,
                "preparar_dados_novo_dia",
                return_value=(dados, "2026-07-19"),
            ),
            mock.patch.object(self.updater, "salvar_historico_clima", persistir),
            mock.patch.object(
                self.updater.acumulados,
                "atualizar_acumulado_diario",
                acumular,
            ),
            mock.patch.object(self.updater, "verificar_alertas", alertar),
            mock.patch.object(
                self.updater,
                "carregar_estado",
                return_value={"rajada_max_nuvem": 15.0},
            ),
            mock.patch.object(self.updater, "salvar_estado"),
            mock.patch.object(self.updater, "log"),
        ):
            self.updater.executar()

        self.assertEqual(ordem, ["persistir", "acumular", "alertar"])
        persistir.assert_called_once_with(dados, leitura_bruta_id=321)
        acumular.assert_called_once_with(dados, "2026-07-19")
        alertar.assert_called_once_with(
            25.0,
            26.0,
            15.0,
            2.0,
            70.0,
            3.0,
            data_referencia="2026-07-19",
            ocorrido_em_local="2026-07-19T12:00:00-04:00",
            validade_alertas=dados["validade_alertas"],
        )

    def test_enviar_alerta_enfileira_para_usuario_inscrito(self):
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO usuarios (nome, telefone, receber_whatsapp)
            VALUES (?, ?, ?)
            """,
            ("Maria", "(67) 99999-9999", 1),
        )
        conn.commit()
        conn.close()

        self.updater.log = lambda mensagem: None

        resultado = self.updater.enviar_alerta("Alerta de teste")

        self.assertEqual(resultado, {"total": 1, "enfileirados": 1, "falhas": 0})

        conn = self.abrir_banco()
        row = conn.execute(
            """
            SELECT nome, telefone, status, tentativas, mensagem, erro
            FROM alertas_fila
            """
        ).fetchone()
        total_envios = conn.execute("SELECT COUNT(*) FROM alertas_envios").fetchone()[0]
        conn.close()

        self.assertEqual(row["nome"], "Maria")
        self.assertEqual(row["telefone"], "5567999999999")
        self.assertEqual(row["status"], "pendente")
        self.assertEqual(row["tentativas"], 0)
        self.assertIn("Alerta de teste", row["mensagem"])
        self.assertNotIn("/unsubscribe?token=", row["mensagem"])
        self.assertNotIn("Para cancelar os alertas", row["mensagem"])
        self.assertIsNone(row["erro"])
        self.assertEqual(total_envios, 0)

    def test_enviar_alerta_nao_aguarda_entre_usuarios(self):
        conn = self.abrir_banco()
        conn.executemany(
            """
            INSERT INTO usuarios (nome, telefone, receber_whatsapp)
            VALUES (?, ?, ?)
            """,
            [
                ("Maria", "67999999999", 1),
                ("Joao", "67888888888", 1),
            ],
        )
        conn.commit()
        conn.close()

        pausas = []
        self.updater.log = lambda mensagem: None
        self.updater.time.sleep = lambda segundos: pausas.append(segundos)

        resultado = self.updater.enviar_alerta("Alerta de teste")

        self.assertEqual(resultado, {"total": 2, "enfileirados": 2, "falhas": 0})
        self.assertEqual(pausas, [])

        conn = self.abrir_banco()
        total_fila = conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0]
        conn.close()
        self.assertEqual(total_fila, 2)

    def test_alerta_omite_registro_mas_preserva_horario_no_evento(self):
        conn = self.abrir_banco()
        conn.execute(
            """
            INSERT INTO usuarios (nome, telefone, receber_whatsapp)
            VALUES (?, ?, ?)
            """,
            ("Rodrigo", "67999999999", 1),
        )
        conn.commit()
        conn.close()

        self.updater.log = lambda mensagem: None
        estado = self.updater.estado_alertas_padrao("2026-07-20")
        ocorrido_em_local = "2026-07-20T07:17:00-04:00"

        with mock.patch.object(self.updater, "salvar_estado"):
            marcado = self.updater.marcar_alerta_enviado(
                estado,
                "nivel_vento",
                1,
                "🌬️ *ALERTA: Vento Forte!*\nRajadas de *42.2 km/h*.",
                valor=42.2,
                unidade="km/h",
                ocorrido_em_local=ocorrido_em_local,
            )

        conn = self.abrir_banco()
        mensagem = conn.execute("SELECT mensagem FROM alertas_fila").fetchone()[0]
        evento = conn.execute(
            "SELECT ocorrido_em_local FROM alertas_eventos"
        ).fetchone()
        conn.close()

        self.assertTrue(marcado)
        self.assertNotIn("Registro:", mensagem)
        self.assertIn("Rajadas de *42.2 km/h*.", mensagem)
        self.assertEqual(evento["ocorrido_em_local"], ocorrido_em_local)

    def test_evento_idempotente_nao_duplica_fila(self):
        self.updater.log = lambda mensagem: None
        conn = self.abrir_banco()
        conn.execute(
            "INSERT INTO usuarios (nome, telefone, receber_whatsapp) VALUES (?, ?, 1)",
            ("Maria", "67999999999"),
        )
        conn.commit()
        conn.close()
        evento = {
            "evento_id": "2026-07-19:vento:1:0",
            "data_referencia": "2026-07-19",
            "tipo": "vento",
            "nivel": 1,
            "valor": 45.0,
            "unidade": "km/h",
            "prioridade": 50,
        }

        primeiro = self.updater.enviar_alerta("Vento forte", evento=evento)
        segundo = self.updater.enviar_alerta("Vento forte", evento=evento)

        conn = self.abrir_banco()
        total_fila = conn.execute("SELECT COUNT(*) FROM alertas_fila").fetchone()[0]
        total_eventos = conn.execute("SELECT COUNT(*) FROM alertas_eventos").fetchone()[0]
        conn.close()
        self.assertEqual(primeiro["enfileirados"], 1)
        self.assertTrue(segundo["duplicado"])
        self.assertEqual(total_fila, 1)
        self.assertEqual(total_eventos, 1)

    def test_nivel_1_exige_duas_leituras_consecutivas(self):
        os.environ["ALERTA_CONFIRMACOES_NIVEL_1"] = "2"
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.data_local = lambda: "2026-07-19"
        mensagens = []
        self.updater.enviar_alerta = lambda mensagem, evento=None: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(36.0, 36.0, 0, 0, 50, 0)
        self.assertEqual(mensagens, [])
        self.updater.verificar_alertas(36.0, 36.0, 0, 0, 50, 0)
        self.assertEqual(len(mensagens), 1)

    def test_campo_invalido_nao_gera_alerta_critico(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.data_local = lambda: "2026-07-19"
        mensagens = []
        self.updater.enviar_alerta = lambda mensagem, evento=None: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(
            25, 25, 0, 0, 0, 0,
            validade_alertas={
                "temperatura": True,
                "sensacao": True,
                "vento": True,
                "chuva": True,
                "umidade": False,
                "uv": True,
            },
        )

        self.assertEqual(mensagens, [])

    def test_indice_uv_nao_gera_alerta(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.data_local = lambda: "2026-07-19"
        mensagens = []
        self.updater.enviar_alerta = lambda mensagem, evento=None: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(25, 25, 0, 0, 50, 30)

        self.assertEqual(mensagens, [])

    def test_calor_so_rearma_abaixo_do_limite_de_histerese(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        estado = self.updater.estado_alertas_padrao("2026-07-19")
        estado["nivel_calor"] = 1

        self.updater.atualizar_rearme_condicoes_instantaneas(estado, 34.0, 50)
        self.assertEqual(estado["nivel_calor"], 1)
        self.updater.atualizar_rearme_condicoes_instantaneas(estado, 32.9, 50)
        self.assertEqual(estado["nivel_calor"], 0)

    def test_alerta_frio_rearmado_informa_temperatura_maxima(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.data_local = lambda: "2026-06-25"
        self.updater.salvar_resumo_diario_banco = lambda data: None

        mensagens = []
        self.updater.enviar_alerta = lambda mensagem, evento=None: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(12.0, 12.0, 0, 0, 50, 0)
        self.updater.verificar_alertas(25.0, 25.0, 0, 0, 50, 0)
        self.updater.verificar_alertas(12.0, 12.0, 0, 0, 50, 0)

        self.assertEqual(len(mensagens), 2)
        self.assertIn("Temperatura Baixa!*", mensagens[0])
        self.assertNotIn("caiu novamente", mensagens[0])
        self.assertIn("Temperatura Baixa novamente!*", mensagens[1])
        self.assertIn("subiu até *25°C*", mensagens[1])
        self.assertIn("caiu novamente para *12°C*", mensagens[1])
        self.assertNotIn(".0°C", mensagens[1])

    def test_alerta_formata_temperatura_sem_casas_decimais(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.data_local = lambda: "2026-06-25"
        self.updater.salvar_resumo_diario_banco = lambda data: None

        mensagens = []
        self.updater.enviar_alerta = lambda mensagem, evento=None: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(12.5, 12.4, 0, 0, 50, 0)

        self.assertEqual(len(mensagens), 1)
        self.assertIn("Registrados *13°C*", mensagens[0])
        self.assertIn("Sensação térmica de *12°C*", mensagens[0])
        self.assertNotIn("12.5°C", mensagens[0])
        self.assertNotIn("12.4°C", mensagens[0])

    def test_alerta_frio_nao_repete_na_virada_do_dia_sem_rearme(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.salvar_resumo_diario_banco = lambda data: None

        datas = iter(["2026-06-25", "2026-06-26"])
        self.updater.data_local = lambda: next(datas)

        mensagens = []
        self.updater.enviar_alerta = lambda mensagem, evento=None: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(12.0, 12.0, 0, 0, 50, 0)
        self.updater.verificar_alertas(11.8, 11.8, 0, 0, 50, 0)

        estado = self.updater.carregar_estado()

        self.assertEqual(len(mensagens), 1)
        self.assertEqual(estado["data"], "2026-06-26")
        self.assertEqual(estado["nivel_frio"], 1)
        self.assertFalse(estado["frio_rearmado"])
        self.assertEqual(estado["temp_max_apos_alerta_frio"], 12.0)

    def test_virada_ignora_contadores_diarios_que_a_estacao_ainda_nao_zerou(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.salvar_resumo_diario_banco = lambda data: None
        self.database.salvar_estado_alertas(
            {
                "data": "2026-07-18",
                "nivel_calor": 1,
                "nivel_vento": 1,
                "nivel_chuva": 1,
                "nivel_umidade": 1,
                "rajada_max_nuvem": 40.4,
                "chuva_ultima_nuvem": 60.0,
            }
        )

        dados, data_leitura = self.updater.preparar_dados_novo_dia(
            {
                "station_data_hora_local": "2026-07-19T00:00:10-04:00",
                "rajada": 8.0,
                "rajada_max": 40.4,
                "chuva_hoje": 60.0,
            }
        )
        estado = self.updater.carregar_estado()

        self.assertEqual(data_leitura, "2026-07-19")
        self.assertEqual(dados["rajada_max"], 8.0)
        self.assertEqual(dados["chuva_hoje"], 0.0)
        self.assertTrue(estado["aguardando_reset_vento"])
        self.assertTrue(estado["aguardando_reset_chuva"])
        self.assertEqual(estado["nivel_vento"], 0)
        self.assertEqual(estado["nivel_chuva"], 0)
        self.assertEqual(estado["nivel_calor"], 1)
        self.assertEqual(estado["nivel_umidade"], 1)

    def test_contadores_voltam_a_ser_aceitos_depois_do_reset_da_estacao(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.database.salvar_estado_alertas(
            {
                "data": "2026-07-19",
                "aguardando_reset_vento": True,
                "vento_max_dia_anterior": 40.4,
                "aguardando_reset_chuva": True,
                "chuva_base_virada": 60.0,
            }
        )

        dados, _ = self.updater.preparar_dados_novo_dia(
            {
                "station_data_hora_local": "2026-07-19T00:00:25-04:00",
                "rajada": 4.0,
                "rajada_max": 4.0,
                "chuva_hoje": 0.0,
            }
        )
        estado = self.updater.carregar_estado()

        self.assertEqual(dados["rajada_max"], 4.0)
        self.assertEqual(dados["chuva_hoje"], 0.0)
        self.assertFalse(estado["aguardando_reset_vento"])
        self.assertFalse(estado["aguardando_reset_chuva"])

    def test_nova_rajada_superior_a_maxima_de_ontem_e_aceita_antes_do_reset(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.database.salvar_estado_alertas(
            {
                "data": "2026-07-19",
                "aguardando_reset_vento": True,
                "vento_max_dia_anterior": 48.0,
            }
        )

        dados, _ = self.updater.preparar_dados_novo_dia(
            {
                "station_data_hora_local": "2026-07-19T00:00:25-04:00",
                "rajada": 12.0,
                "rajada_max": 49.0,
                "chuva_hoje": 0.0,
            }
        )

        self.assertEqual(dados["rajada_max"], 49.0)

    def test_calor_e_umidade_nao_repetem_apenas_pela_mudanca_de_data(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.salvar_resumo_diario_banco = lambda data: None
        self.database.salvar_estado_alertas(
            {
                "data": "2026-07-18",
                "nivel_calor": 1,
                "nivel_umidade": 1,
            }
        )
        mensagens = []
        self.updater.enviar_alerta = lambda mensagem, evento=None: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(
            36.0,
            36.0,
            0.0,
            0.0,
            25.0,
            0.0,
            data_referencia="2026-07-19",
        )

        self.assertEqual(mensagens, [])

    def test_carregar_estado_migra_arquivo_para_banco(self):
        arquivo_estado = Path(self.tmp.name) / "alert_state.json"
        arquivo_estado.write_text(
            '{"data": "2026-06-25", "nivel_frio": 2, "rajada_max_nuvem": 72.5}',
            encoding="utf-8",
        )
        self.updater.STATE_FILE = str(arquivo_estado)
        self.updater.log = lambda mensagem: None

        estado = self.updater.carregar_estado()
        estado_banco = self.database.obter_estado_alertas()

        self.assertEqual(estado["data"], "2026-06-25")
        self.assertEqual(estado["nivel_frio"], 2)
        self.assertEqual(estado["rajada_max_nuvem"], 72.5)
        self.assertEqual(estado_banco["nivel_frio"], 2)
        self.assertEqual(estado_banco["rajada_max_nuvem"], 72.5)

    def test_api_le_rajada_maxima_do_estado_no_banco(self):
        import routes.api as api_module

        api_module = importlib.reload(api_module)
        self.database.salvar_estado_alertas(
            {"data": "2026-06-25", "rajada_max_nuvem": "81.4"}
        )

        self.assertEqual(api_module.rajada_maxima_estado_alertas(), 81.4)


if __name__ == "__main__":
    unittest.main()
