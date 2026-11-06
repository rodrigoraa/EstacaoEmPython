import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
        os.environ["ALERTA_VENTO_JANELA_MINUTOS"] = "3"

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
            "ALERTA_VENTO_JANELA_MINUTOS",
        ):
            os.environ.pop(chave, None)

    def abrir_banco(self):
        conn = sqlite3.connect(os.environ["ESTACAO_DB"])
        conn.row_factory = sqlite3.Row
        return conn

    def preparar_alertas_chuva(self):
        for chave, valor in {
            "ALERTA_CHUVA_NIVEL_1": "30",
            "ALERTA_CHUVA_NIVEL_2": "50",
            "ALERTA_CHUVA_NIVEL_3": None,
        }.items():
            anterior = os.environ.get(chave)
            if anterior is None:
                self.addCleanup(os.environ.pop, chave, None)
            else:
                self.addCleanup(os.environ.__setitem__, chave, anterior)
            if valor is None:
                os.environ.pop(chave, None)
            else:
                os.environ[chave] = valor
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        conn = self.abrir_banco()
        conn.execute(
            "INSERT INTO usuarios (nome, telefone, receber_whatsapp) VALUES (?, ?, 1)",
            ("Maria", "67999999999"),
        )
        conn.commit()
        conn.close()

    def verificar_chuva(self, acumulado, data="2026-07-19", valida=True):
        self.updater.verificar_alertas(
            25, 25, 0, acumulado, 50, 0,
            data_referencia=data,
            ocorrido_em_local=f"{data}T12:00:00-04:00",
            validade_alertas={"chuva": valida},
        )

    def consultar_alertas_chuva(self):
        conn = self.abrir_banco()
        eventos = conn.execute(
            "SELECT * FROM alertas_eventos WHERE tipo = 'chuva' ORDER BY id"
        ).fetchall()
        fila = conn.execute(
            """
            SELECT fila.*
            FROM alertas_fila AS fila
            JOIN alertas_eventos AS evento USING (evento_id)
            WHERE evento.tipo = 'chuva'
            ORDER BY fila.id
            """
        ).fetchall()
        conn.close()
        return eventos, fila

    def test_chuva_abaixo_de_100_preserva_limites_de_30_e_50(self):
        self.preparar_alertas_chuva()

        for acumulado, niveis in (
            (29.9, []),
            (30.0, [1]),
            (49.9, [1]),
            (50.0, [1, 2]),
            (99.9, [1, 2]),
        ):
            with self.subTest(acumulado=acumulado):
                self.verificar_chuva(acumulado)
                eventos, fila = self.consultar_alertas_chuva()
                self.assertEqual([evento["nivel"] for evento in eventos], niveis)
                self.assertEqual(len(fila), len(niveis))

    def test_chuva_100_enfileira_nivel_3_imediato_apos_50(self):
        self.preparar_alertas_chuva()
        os.environ["ALERTA_CONFIRMACOES_NIVEL_1"] = "3"

        self.verificar_chuva(50.0)
        self.verificar_chuva(100.0)

        eventos, fila = self.consultar_alertas_chuva()
        self.assertEqual([evento["nivel"] for evento in eventos], [2, 3])
        self.assertEqual([alerta["prioridade"] for alerta in fila], [80, 100])
        self.assertEqual(eventos[-1]["evento_id"], "2026-07-19:chuva:3:0")
        self.assertEqual(eventos[-1]["valor"], 100.0)
        self.assertEqual(eventos[-1]["unidade"], "mm")
        self.assertEqual(
            eventos[-1]["ocorrido_em_local"], "2026-07-19T12:00:00-04:00"
        )
        self.assertIn("ALERTA EXTREMO", fila[-1]["mensagem"])
        self.assertIn("Chuva Muito Forte", fila[-1]["mensagem"])
        self.assertIn("100.0 mm", fila[-1]["mensagem"])
        self.assertEqual(self.database.obter_estado_alertas()["nivel_chuva"], 3)

    def test_chuva_salto_acima_de_100_enfileira_apenas_nivel_3(self):
        self.preparar_alertas_chuva()

        self.verificar_chuva(123.4)

        eventos, fila = self.consultar_alertas_chuva()
        self.assertEqual([evento["nivel"] for evento in eventos], [3])
        self.assertEqual(len(fila), 1)
        self.assertEqual(fila[0]["prioridade"], 100)
        self.assertIn("123.4 mm", fila[0]["mensagem"])

    def test_chuva_nivel_3_nao_duplica_apos_polls_e_reinicio(self):
        self.preparar_alertas_chuva()

        self.verificar_chuva(100.0)
        self.verificar_chuva(102.5)
        self.updater = importlib.reload(self.updater)
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.verificar_chuva(120.0)

        eventos, fila = self.consultar_alertas_chuva()
        self.assertEqual([evento["nivel"] for evento in eventos], [3])
        self.assertEqual(len(fila), 1)
        self.assertEqual(self.updater.carregar_estado()["nivel_chuva"], 3)
        estado_arquivo = json.loads(Path(self.updater.STATE_FILE).read_text())
        self.assertEqual(estado_arquivo["nivel_chuva"], 3)

    def test_chuva_nivel_3_nao_rebaixa_apos_queda_do_acumulado(self):
        self.preparar_alertas_chuva()

        for acumulado in (100.0, 49.0, 30.0, 99.9, 100.0):
            self.verificar_chuva(acumulado)

        eventos, fila = self.consultar_alertas_chuva()
        self.assertEqual([evento["nivel"] for evento in eventos], [3])
        self.assertEqual(len(fila), 1)
        self.assertEqual(self.database.obter_estado_alertas()["nivel_chuva"], 3)

    def test_chuva_nivel_3_reinicia_no_dia_seguinte(self):
        self.preparar_alertas_chuva()

        self.verificar_chuva(100.0)
        self.verificar_chuva(0.0, data="2026-07-20")
        self.assertEqual(self.updater.carregar_estado()["nivel_chuva"], 0)
        self.verificar_chuva(100.0, data="2026-07-20")

        eventos, fila = self.consultar_alertas_chuva()
        self.assertEqual(
            [(evento["data_referencia"], evento["nivel"]) for evento in eventos],
            [("2026-07-19", 3), ("2026-07-20", 3)],
        )
        self.assertEqual(len(fila), 2)
        self.assertEqual(self.updater.carregar_estado()["data"], "2026-07-20")
        self.assertEqual(self.updater.carregar_estado()["nivel_chuva"], 3)

    def test_chuva_nivel_3_respeita_limite_configurado(self):
        self.preparar_alertas_chuva()
        os.environ["ALERTA_CHUVA_NIVEL_3"] = "120"

        self.verificar_chuva(119.9)
        eventos, _ = self.consultar_alertas_chuva()
        self.assertEqual([evento["nivel"] for evento in eventos], [2])
        self.verificar_chuva(120.0)

        eventos, fila = self.consultar_alertas_chuva()
        self.assertEqual([evento["nivel"] for evento in eventos], [2, 3])
        self.assertEqual(fila[-1]["prioridade"], 100)
        self.assertIn("120.0 mm", fila[-1]["mensagem"])

    def test_chuva_invalida_nao_enfileira_nivel_3(self):
        self.preparar_alertas_chuva()

        self.verificar_chuva(180.0, valida=False)

        eventos, fila = self.consultar_alertas_chuva()
        self.assertEqual(eventos, [])
        self.assertEqual(fila, [])
        self.assertEqual(self.database.obter_estado_alertas()["nivel_chuva"], 0)

        self.verificar_chuva(180.0)

        eventos, fila = self.consultar_alertas_chuva()
        self.assertEqual([evento["nivel"] for evento in eventos], [3])
        self.assertEqual(len(fila), 1)

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

    def test_alerta_vento_aguarda_janela_e_usa_maior_rajada(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        mensagens = []
        eventos = []
        inicio = datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
        horarios = iter(
            [
                inicio,
                inicio + timedelta(minutes=1),
                inicio + timedelta(minutes=3),
            ]
        )
        self.updater.agora_local = lambda: next(horarios)
        self.updater.enviar_alerta = lambda mensagem, evento=None: (
            mensagens.append(mensagem),
            eventos.append(evento),
        ) and {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(
            25, 25, 40.4, 0, 50, 0,
            data_referencia="2026-07-19",
            ocorrido_em_local="2026-07-19T12:00:00-04:00",
        )
        self.updater.verificar_alertas(
            25, 25, 46.2, 0, 50, 0,
            data_referencia="2026-07-19",
            ocorrido_em_local="2026-07-19T12:01:00-04:00",
        )
        self.assertEqual(mensagens, [])

        self.updater.verificar_alertas(
            25, 25, 46.2, 0, 50, 0,
            data_referencia="2026-07-19",
            ocorrido_em_local="2026-07-19T12:03:00-04:00",
        )

        self.assertEqual(
            mensagens,
            ["🌬️ *ALERTA: Vento Forte!*\nRajadas de *46.2 km/h*."],
        )
        self.assertEqual(eventos[0]["valor"], 46.2)
        self.assertEqual(
            eventos[0]["ocorrido_em_local"], "2026-07-19T12:01:00-04:00"
        )
        estado = self.updater.carregar_estado()
        self.assertEqual(estado["nivel_vento"], 1)
        self.assertIsNone(estado["vento_observacao_inicio"])

    def test_alerta_vento_forte_e_imediato_durante_observacao(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        mensagens = []
        inicio = datetime(2026, 7, 19, 12, 0, tzinfo=timezone(timedelta(hours=-4)))
        horarios = iter([inicio, inicio + timedelta(seconds=30)])
        self.updater.agora_local = lambda: next(horarios)
        self.updater.enviar_alerta = lambda mensagem, evento=None: mensagens.append(
            mensagem
        ) or {"total": 1, "enfileirados": 1, "falhas": 0}

        self.updater.verificar_alertas(
            25, 25, 40.4, 0, 50, 0, data_referencia="2026-07-19"
        )
        self.updater.verificar_alertas(
            25, 25, 72.0, 0, 50, 0, data_referencia="2026-07-19"
        )

        self.assertEqual(
            mensagens,
            ["🌪️ *ALERTA FORTE: Vento Muito Forte!*\nRajadas de *72.0 km/h*."],
        )
        estado = self.updater.carregar_estado()
        self.assertEqual(estado["nivel_vento"], 2)
        self.assertIsNone(estado["vento_observacao_inicio"])

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

    def test_alerta_frio_rearmado_usa_mesma_mensagem_do_alerta_normal(self):
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
        self.assertEqual(mensagens[0], mensagens[1])
        self.assertIn("Temperatura Abaixou!*", mensagens[0])
        self.assertIn("Registrados *12°C*", mensagens[0])
        self.assertNotIn("novamente", mensagens[1])
        self.assertNotIn("subiu até", mensagens[1])

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

    def test_virada_usa_ultima_maxima_bruta_quando_corrigida_e_maior(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.salvar_resumo_diario_banco = lambda data: None
        estado = self.updater.estado_alertas_padrao("2026-10-02")
        estado.update({"nivel_vento": 1, "rajada_max_nuvem": 66.3})
        self.database.salvar_estado_alertas(estado)
        # A nuvem resetou o máximo durante o dia; o banco preserva o pico real.
        self.updater.preparar_dados_novo_dia({
            "station_data_hora_local": "2026-10-02T23:59:00-04:00",
            "rajada": 8.0, "rajada_max": 40.4, "chuva_hoje": 0,
        })
        dados, _ = self.updater.preparar_dados_novo_dia({
            "station_data_hora_local": "2026-10-03T00:00:00-04:00",
            "rajada": 8.0, "rajada_max": 40.4, "chuva_hoje": 0,
        })
        self.assertEqual(dados["rajada_max"], 8.0)
        self.assertTrue(self.updater.carregar_estado()["aguardando_reset_vento"])

    def test_nova_maxima_apos_reset_intermediario_nao_e_descartada(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.salvar_resumo_diario_banco = lambda data: None
        estado = self.updater.estado_alertas_padrao("2026-10-02")
        estado.update({"nivel_vento": 1, "rajada_max_nuvem": 66.3})
        self.database.salvar_estado_alertas(estado)
        self.updater.preparar_dados_novo_dia({
            "station_data_hora_local": "2026-10-02T23:59:00-04:00",
            "rajada": 8.0, "rajada_max": 40.4, "chuva_hoje": 0,
        })
        dados, _ = self.updater.preparar_dados_novo_dia({
            "station_data_hora_local": "2026-10-03T00:00:00-04:00",
            "rajada": 45.0, "rajada_max": 45.0, "chuva_hoje": 0,
        })
        self.assertEqual(dados["rajada_max"], 45.0)

    def test_maxima_ausente_na_nuvem_nao_apaga_referencia_anterior(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.salvar_resumo_diario_banco = lambda data: None
        estado = self.updater.estado_alertas_padrao("2026-10-02")
        estado.update({"rajada_max_nuvem": 66.3, "vento_ultima_max_nuvem": 40.4})
        self.database.salvar_estado_alertas(estado)
        self.updater.preparar_dados_novo_dia({
            "station_data_hora_local": "2026-10-02T23:59:00-04:00",
            "rajada": 8.0, "rajada_max": 8.0, "chuva_hoje": 0,
            "validade_alertas": {"vento": True, "vento_max_diario": False},
        })
        dados, _ = self.updater.preparar_dados_novo_dia({
            "station_data_hora_local": "2026-10-03T00:00:00-04:00",
            "rajada": 8.0, "rajada_max": 8.0, "chuva_hoje": 0,
            "validade_alertas": {"vento": True, "vento_max_diario": False},
        })
        dados, _ = self.updater.preparar_dados_novo_dia({
            "station_data_hora_local": "2026-10-03T00:01:00-04:00",
            "rajada": 8.0, "rajada_max": 40.4, "chuva_hoje": 0,
            "validade_alertas": {"vento": True, "vento_max_diario": True},
        })
        self.assertEqual(dados["rajada_max"], 8.0)

    def test_conversao_distingue_maxima_ausente_do_fallback_de_rajada(self):
        from services import weather_service
        resposta = mock.Mock()
        resposta.json.return_value = {"data": [{"lastData": {"windgustmph": 5.0}}]}
        with mock.patch.object(weather_service.requests, "get", return_value=resposta):
            dados = weather_service.obter_dados(persistir_bruto=False)
        self.assertEqual(dados["rajada_max"], 8.0)
        self.assertIs(dados["validade_alertas"].get("vento_max_diario"), False)

    def test_referencia_valida_sobrevive_a_dia_inteiro_sem_maxima_na_nuvem(self):
        self.updater.STATE_FILE = str(Path(self.tmp.name) / "alert_state.json")
        self.updater.log = lambda mensagem: None
        self.updater.salvar_resumo_diario_banco = lambda data: None
        estado = self.updater.estado_alertas_padrao("2026-10-02")
        estado.update({"rajada_max_nuvem": 40.4, "vento_ultima_max_nuvem": 40.4})
        self.database.salvar_estado_alertas(estado)
        self.updater.preparar_dados_novo_dia({
            "station_data_hora_local": "2026-10-03T00:00:00-04:00",
            "rajada": 8.0, "rajada_max": 8.0, "chuva_hoje": 0,
            "validade_alertas": {"vento": True, "vento_max_diario": False},
        })
        # Simula o máximo corrigido que executar() preserva após um dia calmo.
        estado = self.updater.carregar_estado()
        estado["rajada_max_nuvem"] = 8.0
        self.database.salvar_estado_alertas(estado)
        dados, _ = self.updater.preparar_dados_novo_dia({
            "station_data_hora_local": "2026-10-04T00:00:00-04:00",
            "rajada": 8.0, "rajada_max": 40.4, "chuva_hoje": 0,
            "validade_alertas": {"vento": True, "vento_max_diario": True},
        })
        self.assertEqual(dados["rajada_max"], 8.0)

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
