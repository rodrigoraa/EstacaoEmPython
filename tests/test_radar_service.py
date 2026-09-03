import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "estacao"))

from services.radar_service import (  # noqa: E402
    RadarServiceError,
    RedemetRadarClient,
    avaliar_timestamp_frame,
    normalizar_resposta,
)


class RadarServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(
            (ROOT / "tests" / "fixtures" / "redemet_radar.json").read_text(encoding="utf-8")
        )

    def test_achata_radar_aninhado(self):
        resultado = normalizar_resposta(self.payload)
        self.assertEqual(resultado.recebidos, 4)

    def test_remove_path_duplicado(self):
        resultado = normalizar_resposta(self.payload)
        self.assertEqual(resultado.unicos, 3)
        self.assertEqual(len(resultado.frames), 3)

    def test_ordena_e_preserva_intervalos_irregulares(self):
        frames = normalizar_resposta(self.payload).frames
        intervalos = [
            int((b.data_frame - a.data_frame).total_seconds() / 60)
            for a, b in zip(frames, frames[1:])
        ]
        self.assertEqual(intervalos, [20, 10])

    def test_timestamp_redemet_e_utc_aware_com_dia_local_anterior(self):
        payload = json.loads(json.dumps(self.payload))
        frame = payload["data"]["radar"][0][0]
        frame["data"] = "2026-09-03 00:30:00"
        payload["data"]["radar"] = [[frame]]
        resultado = normalizar_resposta(payload).frames[0]
        self.assertEqual(resultado.data_frame_utc.utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual(resultado.data_frame_local.isoformat(), "2026-09-02T20:30:00-04:00")
        self.assertEqual(resultado.data_frame_raw, "2026-09-03 00:30:00")
        self.assertEqual(resultado.timestamp_status, "utc_assumed")

    def test_sanity_check_tolera_passado_e_futuro_curto(self):
        frame = normalizar_resposta(self.payload).frames[-1]
        self.assertEqual(
            avaliar_timestamp_frame(frame, frame.data_frame + timedelta(minutes=5)).timestamp_status,
            "utc_assumed",
        )
        self.assertEqual(
            avaliar_timestamp_frame(frame, frame.data_frame - timedelta(minutes=20)).timestamp_status,
            "utc_assumed",
        )

    def test_sanity_check_marca_futuro_distante_como_suspect(self):
        frame = normalizar_resposta(self.payload).frames[-1]
        coletado = frame.data_frame - timedelta(hours=4)
        avaliado = avaliar_timestamp_frame(frame, coletado, max_future_minutes=30)
        self.assertEqual(avaliado.timestamp_status, "suspect")
        self.assertEqual(avaliado.data_frame_raw, frame.data_frame_raw)

    def test_status_de_erro_rejeitado(self):
        with self.assertRaises(RadarServiceError):
            normalizar_resposta({"status": False, "data": {"radar": []}})

    def test_frame_sem_campo_obrigatorio_rejeitado(self):
        payload = json.loads(json.dumps(self.payload))
        del payload["data"]["radar"][0][0]["path"]
        with self.assertRaisesRegex(RadarServiceError, "path"):
            normalizar_resposta(payload)

    def test_timeout_sanitizado(self):
        session = Mock()
        session.get.side_effect = requests.Timeout("api_key=segredo")
        client = RedemetRadarClient("segredo", session=session)
        with self.assertRaisesRegex(RadarServiceError, "Timeout") as contexto:
            client.obter_frames()
        self.assertNotIn("segredo", str(contexto.exception))

    def test_erro_http_sanitizado(self):
        response = Mock()
        response.raise_for_status.side_effect = requests.HTTPError("url?api_key=segredo")
        session = Mock()
        session.get.return_value = response
        with self.assertRaises(RadarServiceError) as contexto:
            RedemetRadarClient("segredo", session=session).obter_frames()
        self.assertNotIn("segredo", str(contexto.exception))

    def test_json_invalido(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("invalido")
        session = Mock()
        session.get.return_value = response
        with self.assertRaisesRegex(RadarServiceError, "JSON invalido"):
            RedemetRadarClient("x", session=session).obter_frames()

    def test_chave_ausente(self):
        with self.assertRaisesRegex(RadarServiceError, "REDEMET_API_KEY"):
            RedemetRadarClient("")

    def test_download_vazio_rejeitado(self):
        response = Mock(content=b"")
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        frame = normalizar_resposta(self.payload).frames[0]
        with self.assertRaisesRegex(RadarServiceError, "vazia"):
            RedemetRadarClient("x", session=session).baixar_imagem(frame)


if __name__ == "__main__":
    unittest.main()
