import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))

from services.nowcasting_service import snapshot_operacionalmente_atual
from services.radar_repository import obter_id_ultimo_frame_radar
with mock.patch("dotenv.load_dotenv", return_value=False):
    from workers import nowcasting_updater as worker


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds


class NowcastingSchedulerTest(unittest.TestCase):
    def wait(self, clock, frame_id, read_frame, poll_seconds=300):
        with (
            mock.patch.object(worker.time, "monotonic", side_effect=clock.monotonic),
            mock.patch.object(worker.time, "sleep", side_effect=clock.sleep),
            mock.patch.object(worker, "obter_id_ultimo_frame_radar", side_effect=read_frame),
        ):
            worker.aguardar_proximo_ciclo({"poll_seconds": poll_seconds}, frame_id)

    def test_imagem_chegando_apos_analise_acorda_antes_de_vencer(self):
        # Reproduz os horários enviados: análise 13:20:12; nova imagem pronta
        # 13:20:38, que já tem 12min54s e venceria antes do próximo poll normal.
        clock = Clock()
        start = datetime(2026, 10, 4, 13, 20, 12, tzinfo=timezone.utc)
        image_time = datetime(2026, 10, 4, 13, 7, 44, tzinfo=timezone.utc)
        self.wait(clock, 3683, lambda: 3684 if clock.elapsed >= 26 else 3683)
        self.assertLessEqual(clock.elapsed, 31)
        analysis_time = start + timedelta(seconds=clock.elapsed)
        state = {
            "gerado_em_utc": analysis_time.isoformat(),
            "radar": {"data_frame": image_time.isoformat(), "stale": False,
                      "operacional": True},
            "alerta_preventivo": {"nivel": "NORMAL"},
        }
        self.assertTrue(snapshot_operacionalmente_atual(
            state, {"radar_max_age_minutes": 15}, now=analysis_time))
        self.assertFalse(snapshot_operacionalmente_atual(
            state, {"radar_max_age_minutes": 15}, now=start + timedelta(seconds=300)))

    def test_sem_imagem_nova_mantem_intervalo_configurado(self):
        for frame_id in (None, 3683):
            with self.subTest(frame_id=frame_id):
                clock = Clock()
                self.wait(clock, frame_id, lambda: frame_id, poll_seconds=63)
                self.assertEqual(clock.elapsed, 63)

    def test_primeiro_frame_acorda_worker_sem_radar_anterior(self):
        clock = Clock()
        self.wait(clock, None, lambda: 1 if clock.elapsed >= 6 else None)
        self.assertEqual(clock.elapsed, 10)

    def test_erro_de_leitura_preserva_backoff_sem_repeticao(self):
        clock = Clock()
        calls = []

        def unavailable():
            calls.append(clock.elapsed)
            raise sqlite3.OperationalError("database is locked")

        with self.assertLogs(worker.logger, level="WARNING"):
            self.wait(clock, 3683, unavailable)
        self.assertEqual(clock.elapsed, 300)
        self.assertEqual(len(calls), 1)

    def test_main_observa_frame_analisado_mesmo_quando_snapshot_deduplicado(self):
        config = {"enabled": True, "poll_seconds": 300}
        result = {"snapshot": {"radar": {"frame_id": 3684}}, "new": False}
        with (
            mock.patch.object(worker, "nowcasting_config", return_value=config),
            mock.patch.object(worker, "configurar_logging"),
            mock.patch.object(worker, "executar_ciclo", return_value=result) as cycle,
            mock.patch.object(worker, "imprimir_resumo"),
            mock.patch.object(worker, "aguardar_proximo_ciclo", side_effect=KeyboardInterrupt) as wait,
        ):
            with self.assertRaises(KeyboardInterrupt):
                worker.main([])
        cycle.assert_called_once_with(config)
        wait.assert_called_once_with(config, 3684)

    def test_ciclo_com_erro_aguarda_poll_antes_de_tentar_novamente(self):
        config = {"enabled": True, "poll_seconds": 300}
        with (
            mock.patch.object(worker, "nowcasting_config", return_value=config),
            mock.patch.object(worker, "configurar_logging"),
            mock.patch.object(worker, "executar_ciclo", side_effect=[ValueError("falha"), KeyboardInterrupt]),
            mock.patch.object(worker.time, "sleep") as sleep,
            mock.patch.object(worker, "aguardar_proximo_ciclo") as wait,
            self.assertLogs(worker.logger, level="ERROR"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                worker.main([])
        sleep.assert_called_once_with(300)
        wait.assert_not_called()

    def test_modo_once_termina_sem_observar_radar(self):
        config = {"enabled": True, "poll_seconds": 300}
        with (
            mock.patch.object(worker, "nowcasting_config", return_value=config),
            mock.patch.object(worker, "configurar_logging"),
            mock.patch.object(worker, "executar_ciclo", return_value={}),
            mock.patch.object(worker, "imprimir_resumo"),
            mock.patch.object(worker, "aguardar_proximo_ciclo") as wait,
        ):
            self.assertEqual(worker.main(["--once"]), 0)
        wait.assert_not_called()


class LatestRadarFrameTest(unittest.TestCase):
    def test_somente_imagem_publicada_valida_mais_recente_dispara_analise(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE radar_frames (
            id INTEGER, status_processamento TEXT, timestamp_status TEXT,
            data_frame_utc TEXT, data_frame TEXT)""")
        conn.executemany("INSERT INTO radar_frames VALUES (?, ?, ?, ?, ?)", [
            (1, "processado", "utc_assumed", "2026-10-04T13:07:44+00:00", None),
            (2, "analisado", "utc_assumed", "2026-10-04T13:17:44+00:00", None),
            (3, "processado", "suspect", "2026-10-04T14:17:44+00:00", None),
            (4, "erro", "utc_assumed", "2026-10-04T13:27:44+00:00", None),
            (5, "processado", "utc_assumed", "2026-10-04T12:57:44+00:00", None),
            (6, "processado", None, None, "2026-10-04 12:47:44"),
        ])
        with mock.patch("services.radar_repository.database.get_db_readonly", return_value=conn):
            self.assertEqual(obter_id_ultimo_frame_radar(), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
