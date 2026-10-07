import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))


class RuntimeAlertControlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "ESTACAO_DB": str(Path(self.tmp.name) / "test.db"),
            "SECRET_KEY": "test", "RATELIMIT_ENABLED": "false",
            "NOWCASTING_ALERTS_ENABLED": "false",
            "NOWCASTING_TEST_ALERTS_ENABLED": "true",
            "ALERTA_CHUVA_NIVEL_1": "30", "ALERTA_CHUVA_NIVEL_2": "50",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        import database
        import app
        self.database = importlib.reload(database)
        self.database.init_db()
        self.app = importlib.reload(app).app
        self.app.config.update(TESTING=True, RATELIMIT_ENABLED=False)
        self.client = self.app.test_client()
        from services import runtime_alert_controls
        self.controls = runtime_alert_controls

    def login(self):
        with self.client.session_transaction() as session:
            session["logado"] = True
            session["ultimo_acesso"] = time.time()
            session["csrf_token"] = "csrf-test"

    def post(self, name, enabled, csrf="csrf-test"):
        return self.client.post(f"/admin/alert-controls/{name}",
                                data={"csrf_token": csrf, "enabled": enabled})

    def test_precedencia_e_json_invalido(self):
        base = {"alerts_enabled": False, "test_alerts_enabled": True}
        self.assertEqual(self.controls.obter_controles(base)["public"],
                         {"enabled": False, "source": "server", "updated_at": None})
        self.assertTrue(self.controls.obter_controles(base)["test"]["enabled"])
        self.assertTrue(self.controls.obter_controles({**base, "alerts_enabled": True})["public"]["enabled"])
        self.controls.salvar_controle("public", True)
        self.controls.salvar_controle("test", False)
        effective = self.controls.aplicar_controles(base)
        self.assertTrue(effective["alerts_enabled"])
        self.assertFalse(effective["test_alerts_enabled"])
        self.assertEqual(self.controls.obter_controles(base)["public"]["source"], "admin")
        self.controls.salvar_controle("public", False)
        self.assertFalse(self.controls.aplicar_controles({**base, "alerts_enabled": True})["alerts_enabled"])
        conn = self.database.get_db()
        conn.execute("UPDATE estado_alertas SET valor_json=? WHERE chave=?",
                     ('{"public_alerts_enabled": "true"}', self.controls.STATE_KEY))
        conn.commit()
        conn.close()
        fallback = self.controls.obter_controles(base)
        self.assertFalse(fallback["public"]["enabled"])
        self.assertTrue(fallback["test"]["enabled"])
        self.assertEqual(fallback["public"]["source"], "fail_safe")
        with mock.patch.object(self.database, "get_db_readonly", side_effect=OSError("unavailable")):
            self.assertEqual(self.controls.obter_controles(base)["public"]["enabled"], False)

    def test_falha_de_leitura_bloqueia_publico_mas_preserva_teste_e_monitoramento(self):
        base = {"enabled": True, "radar_enabled": True, "alerts_enabled": True,
                "test_alerts_enabled": True}
        self.assertEqual(self.controls.obter_controles(base)["public"]["source"], "server")
        self.assertTrue(self.controls.aplicar_controles(base)["alerts_enabled"])
        self.controls.salvar_controle("test", False)
        self.assertFalse(self.controls.aplicar_controles(base)["test_alerts_enabled"])
        conn = self.database.get_db()
        conn.execute("UPDATE estado_alertas SET valor_json=? WHERE chave=?",
                     ('{"public_alerts_enabled": "true"}', self.controls.STATE_KEY))
        conn.commit()
        conn.close()
        effective = self.controls.aplicar_controles(base)
        self.assertFalse(effective["alerts_enabled"])
        self.assertTrue(effective["test_alerts_enabled"])
        self.assertTrue(effective["enabled"])
        self.assertTrue(effective["radar_enabled"])
        with mock.patch.object(self.database, "get_db_readonly", side_effect=OSError("unavailable")):
            controls = self.controls.obter_controles(base)
            self.assertEqual(controls["public"], {"enabled": False, "source": "fail_safe",
                                                  "updated_at": None})
            self.assertTrue(controls["test"]["enabled"])
            self.login()
            api = self.client.get("/admin/api/alert-controls").get_json()
            self.assertEqual(api["public"]["source"], "fail_safe")
            self.assertNotIn("SECRET_KEY", json.dumps(api))
            self.assertIn("Proteção de segurança".encode(),
                          self.client.get("/admin/monitoramento").data)
        self.controls.salvar_controle("public", True)
        self.assertTrue(self.controls.aplicar_controles(base)["alerts_enabled"])

    def test_post_auth_csrf_boolean_estrito_e_chave_separada(self):
        self.assertEqual(self.post("public", "true").status_code, 401)
        self.assertEqual(self.client.get("/admin/api/alert-controls").status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/admin/alert-controls/public").status_code, 405)
        self.assertEqual(self.post("public", "true", "wrong").status_code, 403)
        for value in ("1", "True", "null", "", "falsex"):
            self.assertEqual(self.post("public", value).status_code, 400)
        self.assertEqual(self.client.post("/admin/alert-controls/public", data={
            "csrf_token": "csrf-test", "enabled": "true", "SECRET_KEY": "x"}).status_code, 400)
        conn = self.database.get_db()
        conn.execute("INSERT INTO estado_alertas (chave, valor_json) VALUES ('principal', ?)",
                     (json.dumps({"chuva": 30}),))
        conn.commit()
        conn.close()
        self.assertEqual(self.post("public", "true").status_code, 303)
        conn = self.database.get_db_readonly()
        try:
            self.assertEqual(json.loads(conn.execute(
                "SELECT valor_json FROM estado_alertas WHERE chave='principal'").fetchone()[0]),
                {"chuva": 30})
        finally:
            conn.close()
        api = self.client.get("/admin/api/alert-controls").get_json()
        self.assertTrue(api["public"]["enabled"])
        self.assertNotIn("SECRET_KEY", json.dumps(api))
        page = self.client.get("/admin/monitoramento")
        self.assertIn("Origem: Painel administrativo".encode(), page.data)
        self.assertIn("Alertas públicos: ATIVADOS".encode(), page.data)
        self.assertIn("Alertas públicos por radar: ATIVADO".encode(),
                      self.client.get("/admin/radar").data)
        self.assertEqual(self.post("public", "false").status_code, 303)
        page = self.client.get("/admin/monitoramento")
        self.assertIn("Alertas públicos desativados".encode(), page.data)
        self.assertNotIn("Monitoramento atualizado".encode(), page.data)

    def test_escritas_concorrentes_preservam_os_dois_controles(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda entry: self.controls.salvar_controle(*entry),
                                    (("public", True), ("test", False))))
        self.assertEqual(results, [None, None])
        effective = self.controls.obter_controles({"alerts_enabled": False,
                                                  "test_alerts_enabled": True})
        self.assertTrue(effective["public"]["enabled"])
        self.assertFalse(effective["test"]["enabled"])

    def test_desativar_publico_preserva_fila_existente(self):
        self.login()
        conn = self.database.get_db()
        conn.execute("INSERT INTO alertas_fila (telefone, mensagem, status, evento_id) "
                     "VALUES (?, ?, 'pendente', ?)",
                     ("5567999999999", "Mensagem já enfileirada", "nowcasting:antigo:atencao"))
        conn.commit()
        conn.close()
        self.assertEqual(self.post("public", "false").status_code, 303)
        conn = self.database.get_db_readonly()
        try:
            row = conn.execute("SELECT status, mensagem FROM alertas_fila WHERE evento_id=?",
                               ("nowcasting:antigo:atencao",)).fetchone()
            self.assertEqual((row["status"], row["mensagem"]),
                             ("pendente", "Mensagem já enfileirada"))
        finally:
            conn.close()

    def test_proximo_ciclo_relê_sem_reiniciar_e_mantem_monitoramento(self):
        from config import nowcasting_config
        from workers import nowcasting_updater
        base = {**nowcasting_config(), "enabled": True}
        snapshot = {"radar": {}, "status": "NORMAL", "nivel_evidencia": "SEM_EVIDENCIA",
                    "alerta_preventivo": {"nivel": "NORMAL", "would_send": False},
                    "ameacas": [], "estacoes_relevantes": []}
        public_configs = []
        test_configs = []
        def public(config, _state):
            public_configs.append(dict(config))
            return 0
        def test(_state, config):
            test_configs.append(dict(config))
            return {}
        with (mock.patch.object(nowcasting_updater, "carregar_entradas_nowcasting",
                                return_value=({}, {"stations": []}, None, "same")),
              mock.patch.object(nowcasting_updater, "analisar_nowcasting", return_value=snapshot),
              mock.patch.object(nowcasting_updater, "salvar_snapshot", return_value=None),
              mock.patch.object(nowcasting_updater, "enfileirar_alertas_nowcasting", side_effect=public),
              mock.patch.object(nowcasting_updater, "processar_alerta_teste_admin", side_effect=test)):
            self.assertFalse(nowcasting_updater.executar_ciclo(base)["disabled"])
            self.controls.salvar_controle("public", True)
            self.controls.salvar_controle("test", False)
            self.assertFalse(nowcasting_updater.executar_ciclo(base)["disabled"])
            self.controls.salvar_controle("public", False)
            self.assertFalse(nowcasting_updater.executar_ciclo(base)["disabled"])
        self.assertEqual([c["alerts_enabled"] for c in public_configs], [False, True, False])
        self.assertEqual([c["test_alerts_enabled"] for c in test_configs], [True, False, False])
        self.assertEqual(base["alerts_enabled"], False)
        from workers.updater import configuracao_alertas
        self.assertEqual((configuracao_alertas()["chuva_1"],
                          configuracao_alertas()["chuva_2"]), (30, 50))

    def test_pagina_sem_radar_e_switch_desligado(self):
        self.login()
        page = self.client.get("/admin/monitoramento")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Monitoramento Meteorológico".encode(), page.data)
        self.assertIn(b"Alertas p", page.data)
        self.assertIn("Dourados".encode(), page.data)
        self.assertIn("Ativar alertas públicos".encode(), page.data)
        self.assertNotIn(b'href="/admin/radar"', self.client.get("/admin").data)

    def test_resumo_projecao_e_chuva_local(self):
        self.login()
        now = datetime.now(timezone.utc).isoformat()
        state = {
            "gerado_em_utc": now, "gerado_em": now,
            "radar": {"operacional": True, "stale": False, "frame_id": 1,
                      "imagem_disponivel": True, "data_frame": now},
            "alerta_preventivo": {"nivel": "VERMELHO"},
            "ameaca_principal": {"distance_km": 42, "approaching": True,
                                  "trajectory_confidence": "ALTA",
                                  "trajectory_frames_used": 5,
                                  "projected_impact": True,
                                  "projected_impact_eta_minutes": 30},
            "ameacas": [], "escola": None, "evento_local_observado": False,
            "indice_evidencia": 73,
        }
        with mock.patch("routes.nowcasting._estado_seguro", return_value=state):
            page = self.client.get("/admin/monitoramento")
            self.assertIn("Possível chuva em aproximação".encode(), page.data)
            self.assertIn(b"~30 min", page.data)
            self.assertIn(b"/admin/radar/imagem/1", page.data)
            state["ameaca_principal"]["projected_impact"] = False
            self.assertIn("passagem ao lado".encode(),
                          self.client.get("/admin/monitoramento").data)
            state["ameaca_principal"]["trajectory_confidence"] = "BAIXA"
            self.assertIn("Ainda não há dados suficientes".encode(),
                          self.client.get("/admin/monitoramento").data)
            state["evento_local_observado"] = True
            self.assertNotIn("Chuva observada em São José".encode(),
                             self.client.get("/admin/monitoramento").data)
            state["escola"] = {
                "measured_at_utc": now, "stale": False, "rain_rate": 2,
                "temperature": 25, "humidity": 90, "pressure": 1000,
                "wind_speed": 5, "wind_gust": 10,
            }
            self.assertIn("Chuva observada em São José".encode(),
                          self.client.get("/admin/monitoramento").data)

    def test_projecao_respeita_minimo_publico_configurado(self):
        from config import nowcasting_config
        self.login()
        now = datetime.now(timezone.utc).isoformat()
        state = {"gerado_em_utc": now, "gerado_em": now,
                 "radar": {"operacional": True, "stale": False, "data_frame": now},
                 "ameaca_principal": {"distance_km": 42, "approaching": True,
                                       "trajectory_confidence": "ALTA",
                                       "projected_impact": True,
                                       "projected_impact_eta_minutes": 30},
                 "ameacas": [], "escola": None, "evento_local_observado": False}
        with mock.patch("routes.nowcasting._estado_seguro", return_value=state):
            for minimum in (4, 5, 6):
                with self.subTest(minimum=minimum):
                    config = {**nowcasting_config(), "public_trajectory_min_frames": minimum}
                    with mock.patch("routes.nowcasting.nowcasting_config", return_value=config):
                        for frames in (minimum - 1, minimum):
                            state["ameaca_principal"]["trajectory_frames_used"] = frames
                            page = self.client.get("/admin/monitoramento")
                            self.assertEqual(page.status_code, 200)
                            self.assertEqual("Possível chuva em aproximação".encode() in page.data,
                                             frames >= minimum)
                            self.assertEqual(b"~30 min" in page.data, frames >= minimum)


if __name__ == "__main__":
    unittest.main()
