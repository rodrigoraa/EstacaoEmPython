import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4


ESTACAO = Path(__file__).resolve().parents[1] / "estacao"
sys.path.insert(0, str(ESTACAO))

import database  # noqa: E402
from config import nowcasting_config  # noqa: E402
from services.nowcasting_repository import (  # noqa: E402
    _local_station,
    obter_ultimo_snapshot,
    salvar_snapshot,
)
from services.nowcasting_service import (  # noqa: E402
    analisar_nowcasting,
    snapshot_operacionalmente_atual,
)


class NowcastingSnapshotRefreshTest(unittest.TestCase):
    def setUp(self):
        self.uri = f"file:nowcasting-refresh-{uuid4().hex}?mode=memory&cache=shared"
        self.conn = self.conectar()
        self.addCleanup(self.conn.close)
        database.garantir_tabela_nowcasting(self.conn)
        self.conn.execute("""
            CREATE TABLE historico_clima (
                id INTEGER PRIMARY KEY,
                station_data_hora_utc TEXT, station_data_hora_local TEXT,
                data_hora_utc TEXT, data_hora_local TEXT, data_hora TEXT,
                temp REAL, umidade REAL, pressao REAL,
                vento_vel REAL, vento_rajada REAL, vento_dir REAL,
                chuva_rate REAL, chuva_hoje REAL
            )
        """)
        self.conn.commit()
        self.db_patch = mock.patch.object(database, "get_db", side_effect=self.conectar)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.now = datetime(2026, 10, 4, 13, 20, tzinfo=timezone.utc)
        self.config = nowcasting_config()
        self.config.update({"poll_seconds": 300, "radar_max_age_minutes": 15})

    def conectar(self):
        conn = sqlite3.connect(self.uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def estado(self, now=None, radar_stale=False, local=None):
        radar = {
            "disponivel": True,
            "stale": radar_stale,
            "frame": {"id": 1, "data_frame_utc": self.now.isoformat()},
        }
        return analisar_nowcasting(
            radar, {"stations": []}, local, self.config, now=now or self.now
        )

    def linha(self, fingerprint):
        return self.conn.execute(
            "SELECT * FROM nowcasting_snapshots WHERE input_fingerprint=?",
            (fingerprint,),
        ).fetchone()

    def test_recalculo_apos_11_minutos_renova_sem_criar_linha(self):
        primeiro = self.estado()
        snapshot_id = salvar_snapshot(primeiro, "mesmas-entradas")
        posterior = self.now + timedelta(minutes=11)
        self.assertFalse(snapshot_operacionalmente_atual(
            obter_ultimo_snapshot(), self.config, now=posterior
        ))

        recalculado = self.estado(now=posterior)
        self.assertIsNone(salvar_snapshot(recalculado, "mesmas-entradas"))

        persistido = obter_ultimo_snapshot()
        self.assertEqual(persistido["snapshot_id"], snapshot_id)
        self.assertEqual(persistido["gerado_em_utc"], recalculado["gerado_em_utc"])
        self.assertEqual(persistido["radar"]["data_frame"], primeiro["radar"]["data_frame"])
        self.assertTrue(snapshot_operacionalmente_atual(
            persistido, self.config, now=posterior
        ))
        linha = self.linha("mesmas-entradas")
        self.assertEqual(linha["calculado_em_utc"], recalculado["gerado_em_utc"])
        self.assertEqual(linha["calculado_em_local"], recalculado["gerado_em"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nowcasting_snapshots").fetchone()[0],
            1,
        )

    def test_recalculo_persiste_idade_local_e_radar_stale_sem_rejuvenescer_imagem(self):
        local = {"age_minutes": 1, "stale": False, "rain_rate": 4, "rain_today": 37.6}
        salvar_snapshot(self.estado(local=local), "entrada")
        posterior = self.now + timedelta(minutes=16)
        local_antiga = dict(local, age_minutes=17, stale=True)
        recalculado = self.estado(now=posterior, radar_stale=True, local=local_antiga)

        self.assertIsNone(salvar_snapshot(recalculado, "entrada"))

        persistido = obter_ultimo_snapshot()
        self.assertEqual(persistido["escola"], local_antiga)
        self.assertEqual(json.loads(self.linha("entrada")["dados_escola_json"]), local_antiga)
        self.assertTrue(persistido["radar"]["stale"])
        self.assertFalse(persistido["radar"]["operacional"])
        self.assertFalse(snapshot_operacionalmente_atual(
            persistido, self.config, now=posterior
        ))

    def test_radar_antigo_continua_invalido_mesmo_com_stale_falso_e_horario_novo(self):
        salvar_snapshot(self.estado(), "entrada")
        posterior = self.now + timedelta(minutes=16)

        salvar_snapshot(self.estado(now=posterior), "entrada")

        persistido = obter_ultimo_snapshot()
        self.assertFalse(persistido["radar"]["stale"])
        self.assertFalse(snapshot_operacionalmente_atual(
            persistido, self.config, now=posterior
        ))

    def test_fingerprint_anterior_recalculado_volta_a_ser_ultimo(self):
        primeiro_id = salvar_snapshot(self.estado(), "entrada-a")
        segundo_id = salvar_snapshot(
            self.estado(now=self.now + timedelta(minutes=1)), "entrada-b"
        )
        self.assertEqual(obter_ultimo_snapshot()["snapshot_id"], segundo_id)

        atualizado = self.estado(now=self.now + timedelta(minutes=2))
        self.assertIsNone(salvar_snapshot(atualizado, "entrada-a"))

        self.assertEqual(obter_ultimo_snapshot()["snapshot_id"], primeiro_id)
        self.assertEqual(obter_ultimo_snapshot()["gerado_em_utc"], atualizado["gerado_em_utc"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nowcasting_snapshots").fetchone()[0],
            2,
        )

    def test_calculo_antigo_nao_sobrescreve_recalculo_mais_recente(self):
        novo = self.estado(now=self.now + timedelta(minutes=2))
        salvar_snapshot(novo, "entrada")
        antigo = self.estado()
        antigo["evidencias"] = ["Calculo fora de ordem"]

        self.assertIsNone(salvar_snapshot(antigo, "entrada"))

        persistido = obter_ultimo_snapshot()
        persistido.pop("snapshot_id")
        self.assertEqual(persistido, novo)
        self.assertEqual(self.linha("entrada")["calculado_em_utc"], novo["gerado_em_utc"])

    def test_nova_entrada_antiga_nao_passa_a_ser_ultimo_snapshot(self):
        atual_id = salvar_snapshot(
            self.estado(now=self.now + timedelta(minutes=2)), "entrada-atual"
        )
        antigo_id = salvar_snapshot(self.estado(), "entrada-antiga")

        self.assertGreater(antigo_id, atual_id)
        self.assertEqual(obter_ultimo_snapshot()["snapshot_id"], atual_id)

    def test_comparacao_de_horarios_considera_offset(self):
        atual = self.estado()
        atual["gerado_em_utc"] = "2026-10-04T09:20:00-04:00"
        salvar_snapshot(atual, "entrada")
        self.assertEqual(self.linha("entrada")["calculado_em_utc"], self.now.isoformat())
        anterior = self.estado(now=self.now - timedelta(minutes=1))

        salvar_snapshot(anterior, "entrada")
        self.assertEqual(obter_ultimo_snapshot()["gerado_em_utc"], atual["gerado_em_utc"])

        outro_id = salvar_snapshot(anterior, "outra-entrada")
        self.assertNotEqual(obter_ultimo_snapshot()["snapshot_id"], outro_id)

    def test_recalculo_atualiza_campos_resumidos_e_payload_juntos(self):
        salvar_snapshot(self.estado(), "entrada")
        novo = self.estado(now=self.now + timedelta(minutes=1))
        novo.update({
            "status": "SISTEMA_SE_APROXIMANDO",
            "nivel_evidencia": "ELEVADA",
            "indice_evidencia": 55,
            "estacoes_relevantes": [{"code": "S706"}],
            "evidencias": ["Nova evidencia"],
            "versao_algoritmo": "nova-versao",
        })
        novo["radar"].update({
            "track_id": 17, "distancia_borda_km": 20, "velocidade_kmh": 35,
            "direcao": "S", "aproximando": True, "trajetoria_compativel": True,
            "eta_minutos": 30,
        })

        salvar_snapshot(novo, "entrada")

        linha = self.linha("entrada")
        self.assertEqual(json.loads(linha["estado_json"]), novo)
        for campo in ("status", "nivel_evidencia", "indice_evidencia", "versao_algoritmo"):
            self.assertEqual(linha[campo], novo[campo])
        self.assertEqual(linha["radar_track_id"], 17)
        self.assertEqual(linha["distancia_borda_km"], 20)
        self.assertEqual(linha["velocidade_kmh"], 35)
        self.assertEqual(linha["direcao_movimento"], "S")
        self.assertEqual(linha["aproximando"], 1)
        self.assertEqual(linha["trajetoria_compativel"], 1)
        self.assertEqual(linha["eta_minutos"], 30)
        self.assertEqual(json.loads(linha["estacoes_relevantes_json"]), novo["estacoes_relevantes"])
        self.assertEqual(json.loads(linha["evidencias_json"]), novo["evidencias"])

    def test_estacao_somente_com_utc_tem_horarios_locais_e_idade_validos(self):
        medicao = self.now - timedelta(minutes=1)
        self.conn.execute(
            "INSERT INTO historico_clima (station_data_hora_utc, chuva_rate) VALUES (?, 2)",
            (medicao.isoformat(),),
        )
        self.conn.commit()

        with mock.patch("services.nowcasting_repository.agora_utc", return_value=self.now):
            local = _local_station({"local_max_age_minutes": 15})

        self.assertEqual(local["measured_at"], "2026-10-04T09:19:00-04:00")
        self.assertEqual(local["measured_at_utc"], medicao.isoformat())
        self.assertEqual(local["age_minutes"], 1)
        self.assertFalse(local["stale"])

    def test_estacao_com_horario_local_legado_e_interpretada_em_campo_grande(self):
        self.conn.execute(
            "INSERT INTO historico_clima (data_hora, chuva_rate) VALUES (?, 2)",
            ("2026-10-04 09:19:00",),
        )
        self.conn.commit()

        with mock.patch("services.nowcasting_repository.agora_utc", return_value=self.now):
            local = _local_station({"local_max_age_minutes": 15})

        self.assertEqual(local["measured_at"], "2026-10-04T09:19:00-04:00")
        self.assertEqual(local["measured_at_utc"], "2026-10-04T13:19:00+00:00")
        self.assertEqual(local["age_minutes"], 1)
        self.assertFalse(local["stale"])

    def test_estacao_futura_acima_de_um_minuto_nao_e_considerada_atual(self):
        self.conn.execute(
            "INSERT INTO historico_clima (station_data_hora_utc, chuva_rate) VALUES (?, 2)",
            ((self.now + timedelta(minutes=2)).isoformat(),),
        )
        self.conn.commit()

        with mock.patch("services.nowcasting_repository.agora_utc", return_value=self.now):
            local = _local_station({"local_max_age_minutes": 15})

        self.assertEqual(local["age_minutes"], -2)
        self.assertTrue(local["stale"])
        self.assertFalse(self.estado(local=local)["evento_local_observado"])

    def test_estacao_vence_no_limite_exato_em_segundos(self):
        medicao = self.now - timedelta(minutes=5, seconds=1)
        self.conn.execute(
            "INSERT INTO historico_clima (station_data_hora_utc, chuva_rate) VALUES (?, 2)",
            (medicao.isoformat(),),
        )
        self.conn.commit()
        with mock.patch("services.nowcasting_repository.agora_utc", return_value=self.now):
            local = _local_station({"local_max_age_minutes": 5})
        self.assertEqual(local["age_minutes"], 5)
        self.assertTrue(local["stale"])
        self.assertFalse(self.estado(local=local)["evento_local_observado"])


if __name__ == "__main__":
    unittest.main()
