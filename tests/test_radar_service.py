import json
import sys
import unittest
import traceback
from pathlib import Path
from unittest.mock import Mock, patch
from dataclasses import replace

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
        self.assertNotIn("api_key=segredo", "".join(traceback.format_exception(contexto.exception)))

    def test_erro_http_sanitizado(self):
        response = Mock(status_code=200)
        response.raise_for_status.side_effect = requests.HTTPError("url?api_key=segredo")
        session = Mock()
        session.get.return_value = response
        with self.assertRaises(RadarServiceError) as contexto:
            RedemetRadarClient("segredo", session=session).obter_frames()
        self.assertNotIn("segredo", str(contexto.exception))
        self.assertNotIn("api_key=segredo", "".join(traceback.format_exception(contexto.exception)))

    def test_json_invalido(self):
        response = Mock(status_code=200, headers={})
        response.raise_for_status.return_value = None
        response.iter_content.return_value = iter((b"JSON invalido",))
        session = Mock()
        session.get.return_value = response
        with self.assertRaisesRegex(RadarServiceError, "JSON invalido"):
            RedemetRadarClient("x", session=session).obter_frames()

    def test_chave_ausente(self):
        with self.assertRaisesRegex(RadarServiceError, "REDEMET_API_KEY"):
            RedemetRadarClient("")

    def test_download_vazio_rejeitado(self):
        response = Mock(status_code=200, headers={})
        response.iter_content.return_value = iter(())
        response.raise_for_status.return_value = None
        session = Mock()
        session.get.return_value = response
        frame = normalizar_resposta(self.payload).frames[0]
        with self.assertRaisesRegex(RadarServiceError, "vazia"):
            RedemetRadarClient("x", session=session).baixar_imagem(frame)

    def test_destinos_nao_oficiais_rejeitados_sem_chamada_http(self):
        frame = normalizar_resposta(self.payload).frames[0]
        for url in (
            "http://estatico-redemet.decea.mil.br/radar/frame.png",
            "https://127.0.0.1/radar/frame.png",
            "https://169.254.169.254/latest/meta-data/",
            "https://[::1]/radar/frame.png",
            "https://localhost/radar/frame.png",
            "https://evil.invalid/radar/frame.png",
            "https://estatico-redemet.decea.mil.br.evil.invalid/radar/frame.png",
            "https://estatico-redemet.decea.mil.br@127.0.0.1/radar/frame.png",
            "https://user:secret@estatico-redemet.decea.mil.br/radar/frame.png",
            "https://estatico-redemet.decea.mil.br:8443/radar/frame.png",
            "https://estatico-redemet.decea.mil.br/radar/../admin.png",
            "https://estatico-redemet.decea.mil.br/radar/%2e%2e/admin.png",
            "https://estatico-redemet.decea.mil.br/radar/frame.png?url=http://localhost",
            "https://estatico-redemet.decea.mil.br/radar/frame.png#fragment",
            "https://estatico-redemet.decea.mil.br/\nradar/frame.png",
        ):
            with self.subTest(url=url):
                session = Mock()
                with self.assertRaises(RadarServiceError):
                    RedemetRadarClient("x", session=session).baixar_imagem(
                        replace(frame, path_remoto=url)
                    )
                session.get.assert_not_called()
                payload = json.loads(json.dumps(self.payload))
                payload["data"]["radar"][0][0]["path"] = url
                with self.assertRaises(RadarServiceError):
                    normalizar_resposta(payload)

    def test_consulta_nao_segue_redirect_com_chave(self):
        response = Mock(status_code=302)
        session = Mock()
        session.get.return_value = response
        with self.assertRaises(RadarServiceError):
            RedemetRadarClient("segredo", session=session).obter_frames()
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        response.json.assert_not_called()
        response.close.assert_called_once()

    def test_consulta_valida_fecha_resposta_e_mantem_origem_fixa(self):
        response = Mock(status_code=200, headers={})
        response.iter_content.return_value = iter((json.dumps(self.payload).encode("utf-8"),))
        session = Mock()
        session.get.return_value = response
        resultado = RedemetRadarClient("segredo", session=session).obter_frames()
        self.assertEqual(resultado.unicos, 3)
        self.assertEqual(
            session.get.call_args.args[0],
            "https://api-redemet.decea.mil.br/produtos/radar/maxcappi",
        )
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertTrue(session.get.call_args.kwargs["stream"])
        response.close.assert_called_once()

    def test_metadados_excessivos_rejeitados_antes_de_ler(self):
        response = Mock(status_code=200, headers={"Content-Length": "101"})
        session = Mock()
        session.get.return_value = response
        with patch("services.radar_service.MAX_METADATA_BYTES", 100):
            with self.assertRaisesRegex(RadarServiceError, "limite"):
                RedemetRadarClient("x", session=session).obter_frames()
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_download_nao_segue_redirect(self):
        response = Mock(status_code=302, headers={"Location": "http://localhost/"})
        session = Mock()
        session.get.return_value = response
        with self.assertRaises(RadarServiceError):
            RedemetRadarClient("x", session=session).baixar_imagem(
                normalizar_resposta(self.payload).frames[0]
            )
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        self.assertTrue(session.get.call_args.kwargs["stream"])
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_content_length_excessivo_bloqueia_antes_de_ler(self):
        response = Mock(status_code=200, headers={"Content-Length": "26"})
        session = Mock()
        session.get.return_value = response
        with patch("services.radar_service.MAX_IMAGE_BYTES", 25):
            with self.assertRaisesRegex(RadarServiceError, "limite"):
                RedemetRadarClient("x", session=session).baixar_imagem(
                    normalizar_resposta(self.payload).frames[0]
                )
        response.iter_content.assert_not_called()
        response.close.assert_called_once()

    def test_content_length_invalido_rejeitado_sem_ler(self):
        for declarado in ("-1", "indefinido", "NaN"):
            with self.subTest(declarado=declarado):
                response = Mock(status_code=200, headers={"Content-Length": declarado})
                session = Mock()
                session.get.return_value = response
                with self.assertRaises(RadarServiceError):
                    RedemetRadarClient("x", session=session).baixar_imagem(
                        normalizar_resposta(self.payload).frames[0]
                    )
                response.iter_content.assert_not_called()
                response.close.assert_called_once()

    def test_limite_de_stream_independe_de_content_length(self):
        for headers in ({}, {"Content-Length": "1"}):
            with self.subTest(headers=headers):
                response = Mock(status_code=200, headers=headers)
                consumidos = []

                def blocos():
                    for i in range(3):
                        consumidos.append(i)
                        yield b"123456789012345"

                response.iter_content.return_value = blocos()
                session = Mock()
                session.get.return_value = response
                with patch("services.radar_service.MAX_IMAGE_BYTES", 25):
                    with self.assertRaisesRegex(RadarServiceError, "limite"):
                        RedemetRadarClient("x", session=session).baixar_imagem(
                            normalizar_resposta(self.payload).frames[0]
                        )
                self.assertEqual(consumidos, [0, 1])
                response.close.assert_called_once()

    def test_stream_valido_concatena_blocos_e_fecha_conexao(self):
        response = Mock(status_code=200, headers={"Content-Length": "6"})
        response.iter_content.return_value = iter((b"abc", b"", b"def"))
        session = Mock()
        session.get.return_value = response
        self.assertEqual(
            RedemetRadarClient("x", session=session).baixar_imagem(
                normalizar_resposta(self.payload).frames[0]
            ), b"abcdef"
        )
        response.close.assert_called_once()

    def test_falha_durante_stream_fecha_conexao_sem_revelar_url(self):
        response = Mock(status_code=200, headers={})
        response.iter_content.side_effect = requests.ConnectionError("secret-url")
        session = Mock()
        session.get.return_value = response
        with self.assertRaises(RadarServiceError) as erro:
            RedemetRadarClient("x", session=session).baixar_imagem(
                normalizar_resposta(self.payload).frames[0]
            )
        self.assertNotIn("secret-url", str(erro.exception))
        response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
