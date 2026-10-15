import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "estacao"))

with mock.patch("dotenv.load_dotenv", return_value=False):
    from workers import radar_updater as worker


class Clock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


class RadarSchedulerTest(unittest.TestCase):
    def executar_ate_segundo_ciclo(self, trabalho, *, falha=False):
        clock = Clock()
        inicios = []
        config = {"enabled": True, "poll_seconds": 300}

        def executar_ciclo(*, config):
            inicios.append(clock.elapsed)
            if len(inicios) == 2:
                raise KeyboardInterrupt
            clock.elapsed += trabalho
            if falha:
                raise RuntimeError("REDEMET indisponivel")
            return {}

        with (
            mock.patch.object(worker, "radar_config", return_value=config),
            mock.patch.object(worker, "configurar_logging"),
            mock.patch.object(worker, "imprimir_resumo"),
            mock.patch.object(worker, "executar_ciclo", side_effect=executar_ciclo),
            mock.patch.object(worker.time, "monotonic", side_effect=clock.monotonic),
            mock.patch.object(worker.time, "sleep", side_effect=clock.sleep),
            mock.patch.object(worker.logger, "error") as erro,
        ):
            with self.assertRaises(KeyboardInterrupt):
                worker.main([])
        self.assertEqual(erro.call_count, int(falha))
        return inicios, clock.sleeps

    def test_processamento_de_90_segundos_nao_se_soma_ao_poll_de_300(self):
        inicios, pausas = self.executar_ate_segundo_ciclo(90)
        self.assertEqual(inicios, [0, 300])
        self.assertEqual(pausas, [210])

    def test_ciclo_quase_no_prazo_aguarda_apenas_tempo_restante(self):
        inicios, pausas = self.executar_ate_segundo_ciclo(290)
        self.assertEqual(inicios, [0, 300])
        self.assertEqual(pausas, [10])

    def test_ciclo_no_prazo_ou_acima_dele_tem_pausa_minima(self):
        for trabalho in (300, 400):
            with self.subTest(trabalho=trabalho):
                inicios, pausas = self.executar_ate_segundo_ciclo(trabalho)
                self.assertEqual(inicios, [0, trabalho + 30])
                self.assertEqual(pausas, [30])

    def test_falha_respeita_poll_e_backoff_minimo_sem_loop_imediato(self):
        for trabalho, pausa in ((0, 300), (290, 30), (400, 30)):
            with self.subTest(trabalho=trabalho):
                inicios, pausas = self.executar_ate_segundo_ciclo(trabalho, falha=True)
                self.assertEqual(inicios, [0, trabalho + pausa])
                self.assertEqual(pausas, [pausa])

    def test_modo_once_termina_sem_esperar_poll(self):
        config = {"enabled": True, "poll_seconds": 300}
        with (
            mock.patch.object(worker, "radar_config", return_value=config),
            mock.patch.object(worker, "configurar_logging"),
            mock.patch.object(worker, "executar_ciclo", return_value={}) as ciclo,
            mock.patch.object(worker, "imprimir_resumo"),
            mock.patch.object(worker.time, "monotonic") as monotonic,
            mock.patch.object(worker.time, "sleep") as sleep,
        ):
            self.assertEqual(worker.main(["--once"]), 0)
        ciclo.assert_called_once_with(config=config)
        monotonic.assert_not_called()
        sleep.assert_not_called()

    def test_radar_desabilitado_termina_sem_repetir_ciclo(self):
        config = {"enabled": False, "poll_seconds": 300}
        with (
            mock.patch.object(worker, "radar_config", return_value=config),
            mock.patch.object(worker, "configurar_logging"),
            mock.patch.object(worker, "executar_ciclo", return_value={}) as ciclo,
            mock.patch.object(worker, "imprimir_resumo"),
            mock.patch.object(worker.time, "monotonic") as monotonic,
            mock.patch.object(worker.time, "sleep") as sleep,
        ):
            self.assertEqual(worker.main([]), 0)
        ciclo.assert_called_once_with(config=config)
        monotonic.assert_not_called()
        sleep.assert_not_called()


class RadarCycleMetricsTest(unittest.TestCase):
    def test_log_separa_atraso_remoto_do_tempo_de_processamento_sem_expor_urls(self):
        clock = Clock()
        inicio = datetime(2026, 10, 14, 13, 20, 33, tzinfo=timezone.utc)
        frames = [
            SimpleNamespace(
                data_frame_utc=datetime(2026, 10, 14, 13, minuto, 44, tzinfo=timezone.utc),
                data_utc_texto=f"2026-10-14T13:{minuto:02d}:44+00:00",
                path_remoto=f"https://radar.test/imagem{minuto}?api_key=nao-expor-secret",
            )
            for minuto in (2, 7)
        ]
        config = {"enabled": True, "api_key": "nao-expor-secret", "stale_minutes": 15}
        client = mock.Mock()

        def obter_frames():
            clock.elapsed += 5
            return SimpleNamespace(frames=frames, recebidos=2, unicos=2)

        def processar_frame(*args):
            clock.elapsed += 45
            return 1, 0, True

        client.obter_frames.side_effect = obter_frames
        estado = {"frame": {"data_frame_utc": frames[-1].data_utc_texto,
                            "idade_minutos": 14.4, "clusters_significativos": 0}}
        with (
            mock.patch.object(worker.database, "init_db"),
            mock.patch.object(worker, "frame_existente", return_value=None),
            mock.patch.object(worker, "processar_frame", side_effect=processar_frame) as processar,
            mock.patch.object(worker, "obter_estado_radar", return_value=estado),
            mock.patch.object(worker, "agora_utc", side_effect=lambda: inicio + timedelta(seconds=clock.elapsed)),
            mock.patch.object(worker.time, "monotonic", side_effect=clock.monotonic),
            self.assertLogs(worker.logger, level="INFO") as logs,
        ):
            resultado = worker.executar_ciclo(client=client, config=config)

        texto = "\n".join(logs.output)
        self.assertIn("latest_remote_utc=2026-10-14T13:07:44+00:00", texto)
        self.assertIn("idade_na_coleta_min=12.9", texto)
        self.assertIn("duracao_s=95.0 processamento_s=90.0", texto)
        self.assertIn("latest_published_utc=2026-10-14T13:07:44+00:00 idade_final_min=14.4", texto)
        self.assertNotIn("nao-expor-secret", texto)
        self.assertNotIn("https://", texto)
        self.assertEqual([chamada.args[1] for chamada in processar.call_args_list], frames)
        self.assertEqual(resultado["novos"], 2)


if __name__ == "__main__":
    unittest.main()
