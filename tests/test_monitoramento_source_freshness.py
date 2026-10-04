import os
import re
import sys
import time
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

from services.nowcasting_service import preparar_estado_nowcasting_admin  # noqa: E402


NOW = datetime(2026, 10, 4, 13, 20, 12, tzinfo=timezone.utc)
CONFIG = {"poll_seconds": 300, "radar_max_age_minutes": 15,
          "local_max_age_minutes": 5}


def snapshot(rain_rate=0):
    return {
        "gerado_em_utc": NOW.isoformat(), "gerado_em": "2026-10-04T09:20:12-04:00",
        "radar": {
            "disponivel": True, "operacional": False, "stale": True,
            "data_frame": "2026-10-04T08:57:45-04:00",
            "imagem_disponivel": True, "frame_id": 100,
        },
        "escola": {
            "measured_at": "2026-10-04T09:19:00-04:00", "age_minutes": 1,
            "stale": False, "rain_rate": rain_rate, "rain_today": 37.6,
            "temperature": 25, "humidity": 95, "pressure": 1000,
            "wind_speed": 5, "wind_gust": 10,
        },
        "ameaca_principal": {
            "distance_km": 20, "approaching": True,
            "trajectory_confidence": "ALTA", "trajectory_frames_used": 5,
            "projected_impact": True, "projected_impact_eta_minutes": 12,
        },
        "alerta_preventivo": {"nivel": "VERMELHO"},
        "evento_local_observado": rain_rate > 0,
    }


class SourceFreshnessTest(unittest.TestCase):
    def prepare(self, state, now=NOW):
        return preparar_estado_nowcasting_admin(state, CONFIG, now=now)

    def test_chuva_fresca_independente_do_radar_atrasado(self):
        for rate, observed in ((0, False), (6.2, True)):
            with self.subTest(rate=rate):
                state = snapshot(rate)
                before = deepcopy(state)
                result = self.prepare(state)
                self.assertEqual(state, before)
                self.assertTrue(result["analise_atual"])
                self.assertFalse(result["snapshot_desatualizado"])
                self.assertFalse(result["monitoramento_atual"])
                self.assertFalse(result["radar_atual"])
                self.assertTrue(result["estacao_atual"])
                self.assertIs(result["chuva_na_estacao"], observed)
                self.assertIs(result["estado"]["evento_local_observado"], observed)
                self.assertEqual(result["motivo_indisponibilidade"], "radar_stale")
                self.assertAlmostEqual(result["frescor_fontes"]["radar"]["idade_minutos"], 22.4)
                self.assertEqual(result["estado"]["alerta_preventivo"]["nivel"], "INDISPONIVEL")

    def test_estacao_embutida_envelhece_sem_renovar_a_medicao(self):
        state = snapshot(8)
        result = self.prepare(state, NOW + timedelta(minutes=5))
        self.assertFalse(result["estacao_atual"])
        self.assertIsNone(result["chuva_na_estacao"])
        self.assertTrue(result["estado"]["escola"]["stale"])
        self.assertEqual(result["estado"]["escola"]["measured_at"], state["escola"]["measured_at"])
        self.assertGreater(result["estado"]["escola"]["age_minutes"], 5)
        self.assertFalse(result["estado"]["evento_local_observado"])

    def test_timestamp_local_legado_e_utc_explicito(self):
        state = snapshot(1)
        state["escola"]["measured_at"] = "2026-10-04 09:19:00"
        self.assertTrue(self.prepare(state)["estacao_atual"])
        state["escola"].update(measured_at="invalido", measured_at_utc="2026-10-04T13:19:00+00:00")
        result = self.prepare(state)
        self.assertTrue(result["estacao_atual"])
        self.assertEqual(result["estado"]["escola"]["measured_at"], "2026-10-04T09:19:00-04:00")

    def test_taxa_numerica_em_texto_e_normalizada_para_exibicao(self):
        state = snapshot()
        state["escola"]["rain_rate"] = "1.5"
        result = self.prepare(state)
        self.assertIs(result["chuva_na_estacao"], True)
        self.assertEqual(result["estado"]["escola"]["rain_rate"], 1.5)

    def test_estacao_stale_sem_horario_invalido_ou_futuro_nao_confirma_chuva(self):
        variants = [None, {}, {"measured_at": "invalido", "stale": False},
                    {"measured_at": (NOW + timedelta(minutes=2)).isoformat(), "stale": False},
                    {"measured_at": NOW.isoformat(), "stale": True},
                    {"measured_at": NOW.isoformat()}]
        for local in variants:
            with self.subTest(local=local):
                state = snapshot(5)
                state["escola"] = {**local, "rain_rate": 5} if local is not None else None
                result = self.prepare(state)
                self.assertFalse(result["estacao_atual"])
                self.assertIsNone(result["chuva_na_estacao"])
                self.assertFalse(result["estado"]["evento_local_observado"])

    def test_taxa_invalida_nao_confunde_acumulado_diario_com_chuva_atual(self):
        for rate in (None, True, "invalido", float("nan"), float("inf"), -1):
            with self.subTest(rate=rate):
                state = snapshot()
                state["escola"]["rain_rate"] = rate
                result = self.prepare(state)
                self.assertTrue(result["estacao_atual"])
                self.assertIsNone(result["chuva_na_estacao"])
                self.assertIsNone(result["estado"]["escola"]["rain_rate"])

    def test_imagem_envelhece_apesar_de_flag_stale_salvo_false(self):
        state = snapshot()
        state["radar"].update(operacional=True, stale=False,
                              data_frame=(NOW - timedelta(minutes=14)).isoformat())
        self.assertTrue(self.prepare(state)["monitoramento_atual"])
        result = self.prepare(state, NOW + timedelta(minutes=2))
        self.assertTrue(result["analise_atual"])
        self.assertFalse(result["radar_atual"])
        self.assertTrue(result["estado"]["radar"]["stale"])
        self.assertFalse(result["estado"]["radar"]["operacional"])

    def test_analise_antiga_nao_torna_radar_operacional(self):
        state = snapshot()
        state["gerado_em_utc"] = (NOW - timedelta(minutes=20)).isoformat()
        state["radar"].update(operacional=True, stale=False, data_frame=NOW.isoformat())
        result = self.prepare(state)
        self.assertTrue(result["snapshot_desatualizado"])
        self.assertFalse(result["monitoramento_atual"])
        self.assertFalse(result["estado"]["radar"]["operacional"])
        self.assertEqual(result["motivo_indisponibilidade"], "snapshot_stale")


class SourceFreshnessPageTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"APP_ENV": "development"})
        self.env.start()
        self.addCleanup(self.env.stop)
        from app import create_app
        self.client = create_app({"TESTING": True, "SECRET_KEY": "teste",
                                  "RATELIMIT_ENABLED": False}).test_client()
        with self.client.session_transaction() as session:
            session.update(logado=True, ultimo_acesso=time.time(), csrf_token="teste")
        controls = {name: {"enabled": False, "source": "server", "updated_at": None}
                    for name in ("public", "test")}
        for target, value in (
            ("routes.nowcasting.nowcasting_config", CONFIG),
            ("routes.nowcasting.obter_controles", controls),
            ("routes.nowcasting.obter_status_alerta_teste_admin", {}),
            ("routes.nowcasting.obter_status_alerta_publico", {}),
            ("services.nowcasting_service.agora_utc", NOW),
        ):
            patch = mock.patch(target, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def page_and_api(self, state):
        with mock.patch("routes.nowcasting._estado_seguro", return_value=state):
            page = self.client.get("/admin/monitoramento")
            api = self.client.get("/admin/api/nowcasting/status")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(api.status_code, 200)
        text = page.get_data(as_text=True)
        overview = re.search(r'<section aria-label="Resumo visual".*?</section>', text, re.S).group()
        return text, overview, api.get_json()

    def test_radar_antigo_mostra_aviso_especifico_e_chuva_local(self):
        for rate, label in ((0, "NÃO"), (3, "SIM")):
            with self.subTest(rate=rate):
                text, overview, payload = self.page_and_api(snapshot(rate))
                self.assertIn("RADAR DESATUALIZADO", text)
                self.assertNotIn("MONITORAMENTO DESATUALIZADO", text)
                self.assertIn("limite: 15 minutos", text)
                self.assertIn(f"<small>Chuva na estação</small><strong>{label}</strong>", overview)
                self.assertNotIn("20 km", overview)
                self.assertNotIn("APROXIMANDO", overview)
                self.assertNotIn("~12 min", overview)
                self.assertIn(f"{rate:.1f} mm/h", text)
                self.assertTrue(payload["analise_atual"])
                self.assertFalse(payload["radar_atual"])
                self.assertTrue(payload["estacao_atual"])
                self.assertFalse(payload["monitoramento_atual"])

    def test_estacao_antiga_nao_aparece_como_chuva_atual(self):
        state = snapshot(999)
        state["escola"]["measured_at"] = (NOW - timedelta(minutes=20)).isoformat()
        text, overview, payload = self.page_and_api(state)
        self.assertIn("Dados locais desatualizados", text)
        self.assertIn("<small>Chuva na estação</small><strong>INDEFINIDO</strong>", overview)
        self.assertNotIn("999.0 mm/h", text)
        self.assertFalse(payload["estacao_atual"])

    def test_snapshot_antigo_mantem_aviso_e_esconde_projecoes_do_resumo(self):
        state = snapshot()
        state["gerado_em_utc"] = (NOW - timedelta(minutes=20)).isoformat()
        text, overview, payload = self.page_and_api(state)
        self.assertIn("MONITORAMENTO DESATUALIZADO", text)
        self.assertNotIn("20 km", overview)
        self.assertNotIn("APROXIMANDO", overview)
        self.assertNotIn("~12 min", overview)
        self.assertTrue(payload["snapshot_desatualizado"])
        self.assertEqual(payload["alerta_preventivo"]["nivel"], "INDISPONIVEL")


if __name__ == "__main__":
    unittest.main()
