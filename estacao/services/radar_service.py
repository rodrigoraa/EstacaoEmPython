"""Cliente pequeno e defensivo para o produto MaxCAPPI da REDEMET."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from typing import Any, Iterable

import requests

from services.outbound_security import InvalidOutboundURL, validar_url_imagem_redemet


REDEMET_RADAR_URL = "https://api-redemet.decea.mil.br/produtos/radar/maxcappi"
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024


class RadarServiceError(RuntimeError):
    """Falha externa sanitizada, sem URL completa nem credenciais."""


def _ler_corpo_limitado(resposta, limite: int, nome: str) -> bytes:
    declarado = resposta.headers.get("Content-Length")
    if declarado is not None:
        try:
            tamanho = int(declarado)
        except (TypeError, ValueError) as erro:
            raise RadarServiceError("Tamanho declarado da resposta invalido") from erro
        if tamanho < 0 or tamanho > limite:
            raise RadarServiceError(f"{nome} excede o limite permitido")
    conteudo = bytearray()
    for bloco in resposta.iter_content(chunk_size=64 * 1024):
        if not bloco:
            continue
        if len(conteudo) + len(bloco) > limite:
            raise RadarServiceError(f"{nome} excede o limite permitido")
        conteudo.extend(bloco)
    if not conteudo:
        raise RadarServiceError(f"{nome} vazia")
    return bytes(conteudo)


@dataclass(frozen=True)
class RadarFrame:
    radar_codigo: str
    produto: str
    data_frame: datetime
    path_remoto: str
    lat_center: float
    lon_center: float
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    raio_km: float | None = None
    tamanho: int | None = None
    data_frame_raw: str | None = None
    timestamp_status: str = "utc_assumed"

    def __post_init__(self):
        momento = self.data_frame
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "data_frame", momento.astimezone(timezone.utc))
        if self.data_frame_raw is None:
            object.__setattr__(
                self,
                "data_frame_raw",
                momento.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            )

    @property
    def data_frame_utc(self) -> datetime:
        return self.data_frame

    @property
    def data_frame_local(self) -> datetime:
        from time_utils import LOCAL_TZ

        return self.data_frame.astimezone(LOCAL_TZ)

    @property
    def data_texto(self) -> str:
        return self.data_frame_utc.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def data_utc_texto(self) -> str:
        return self.data_frame_utc.replace(microsecond=0).isoformat()

    @property
    def data_local_texto(self) -> str:
        return self.data_frame_local.replace(microsecond=0).isoformat()


def avaliar_timestamp_frame(
    frame: RadarFrame,
    coletado_em_utc: datetime,
    max_future_minutes: float = 30.0,
) -> RadarFrame:
    """Marca como suspeito somente um futuro além da tolerância operacional.

    A REDEMET não documenta inequivocamente o fuso do campo ``data``. Enquanto
    isso não mudar, a interpretação UTC continua sendo uma hipótese explícita.
    """
    coletado = coletado_em_utc
    if coletado.tzinfo is None:
        coletado = coletado.replace(tzinfo=timezone.utc)
    coletado = coletado.astimezone(timezone.utc)
    diferenca_minutos = (frame.data_frame_utc - coletado).total_seconds() / 60
    status = (
        "suspect"
        if diferenca_minutos > max(0.0, float(max_future_minutes))
        else "utc_assumed"
    )
    return replace(frame, timestamp_status=status)


@dataclass(frozen=True)
class RadarFetchResult:
    frames: tuple[RadarFrame, ...]
    recebidos: int
    unicos: int


def _iterar_frames(valor: Any) -> Iterable[dict[str, Any]]:
    """Achata a estrutura real ``radar: [[{frame}], ...]`` recursivamente."""
    if isinstance(valor, dict):
        yield valor
    elif isinstance(valor, (list, tuple)):
        for item in valor:
            yield from _iterar_frames(item)


def _float_obrigatorio(frame: dict[str, Any], campo: str) -> float:
    try:
        return float(frame[campo])
    except (KeyError, TypeError, ValueError) as erro:
        raise RadarServiceError(f"Frame REDEMET sem campo numerico valido: {campo}") from erro


def normalizar_frame(frame: dict[str, Any], produto_padrao: str) -> RadarFrame:
    obrigatorios = ("localidade", "path", "data")
    faltantes = [campo for campo in obrigatorios if not str(frame.get(campo, "")).strip()]
    if faltantes:
        raise RadarServiceError(
            "Frame REDEMET sem campos obrigatorios: " + ", ".join(faltantes)
        )

    path = str(frame["path"]).strip()
    try:
        validar_url_imagem_redemet(path)
    except InvalidOutboundURL as erro:
        raise RadarServiceError("Frame REDEMET possui URL de imagem nao permitida") from erro
    data_frame_raw = str(frame["data"])
    try:
        data_frame = datetime.strptime(
            data_frame_raw.strip(), "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    except ValueError as erro:
        raise RadarServiceError("Frame REDEMET possui timestamp invalido") from erro

    lat_min = _float_obrigatorio(frame, "lat_min")
    lat_max = _float_obrigatorio(frame, "lat_max")
    lon_min = _float_obrigatorio(frame, "lon_min")
    lon_max = _float_obrigatorio(frame, "lon_max")
    if not lat_min < lat_max or not lon_min < lon_max:
        raise RadarServiceError("Frame REDEMET possui limites geograficos invalidos")

    try:
        tamanho = int(frame["tamanho"]) if frame.get("tamanho") is not None else None
        raio = float(frame["raio"]) if frame.get("raio") is not None else None
    except (TypeError, ValueError) as erro:
        raise RadarServiceError("Frame REDEMET possui metadados invalidos") from erro

    return RadarFrame(
        radar_codigo=str(frame["localidade"]).strip().lower(),
        produto=str(frame.get("tipo") or produto_padrao).strip().lower(),
        data_frame=data_frame,
        path_remoto=path,
        lat_center=_float_obrigatorio(frame, "lat_center"),
        lon_center=_float_obrigatorio(frame, "lon_center"),
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
        raio_km=raio,
        tamanho=tamanho,
        data_frame_raw=data_frame_raw,
        timestamp_status="utc_assumed",
    )


def normalizar_resposta(payload: Any, produto_padrao: str = "maxcappi") -> RadarFetchResult:
    if not isinstance(payload, dict) or payload.get("status") is not True:
        raise RadarServiceError("REDEMET retornou status de erro")
    data = payload.get("data")
    if not isinstance(data, dict) or "radar" not in data:
        raise RadarServiceError("Resposta REDEMET sem data.radar")

    produto = str(data.get("tipo") or produto_padrao)
    brutos = list(_iterar_frames(data["radar"]))
    normalizados = [normalizar_frame(frame, produto) for frame in brutos]

    # Path identifica o arquivo publicado. Se a API repetir o ultimo item,
    # conservamos apenas uma copia; timestamp continua validado e ordenado.
    por_path: dict[str, RadarFrame] = {}
    for frame in normalizados:
        anterior = por_path.get(frame.path_remoto)
        if anterior is None or frame.data_frame > anterior.data_frame:
            por_path[frame.path_remoto] = frame
    frames = tuple(sorted(por_path.values(), key=lambda item: item.data_frame))
    return RadarFetchResult(frames=frames, recebidos=len(brutos), unicos=len(frames))


class RedemetRadarClient:
    def __init__(
        self,
        api_key: str,
        area: str = "jr",
        anima: int = 15,
        timeout: int = 30,
        produto: str = "maxcappi",
        session=None,
    ):
        if not api_key:
            raise RadarServiceError("REDEMET_API_KEY nao configurada")
        self._api_key = api_key
        self.area = area
        self.anima = anima
        self.timeout = timeout
        self.produto = str(produto or "maxcappi").strip().lower()
        if self.produto != "maxcappi":
            raise RadarServiceError("Produto de radar nao suportado")
        self.session = session or requests.Session()

    def obter_frames(self) -> RadarFetchResult:
        resposta = None
        try:
            resposta = self.session.get(
                REDEMET_RADAR_URL,
                params={"api_key": self._api_key, "area": self.area, "anima": self.anima},
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
            resposta.raise_for_status()
            if not 200 <= resposta.status_code < 300:
                raise RadarServiceError("REDEMET retornou resposta HTTP nao permitida")
            try:
                conteudo = _ler_corpo_limitado(resposta, MAX_METADATA_BYTES, "Resposta REDEMET")
                payload = json.loads(conteudo)
            except (ValueError, TypeError, RecursionError) as erro:
                raise RadarServiceError("REDEMET retornou JSON invalido") from erro
        except requests.Timeout:
            raise RadarServiceError("Timeout ao consultar a REDEMET") from None
        except requests.RequestException:
            raise RadarServiceError("Falha HTTP ao consultar a REDEMET") from None
        finally:
            if resposta is not None:
                resposta.close()
        return normalizar_resposta(payload, produto_padrao=self.produto)

    def baixar_imagem(self, frame: RadarFrame) -> bytes:
        # Revalida também frames reconstruídos do banco ou criados diretamente.
        try:
            validar_url_imagem_redemet(frame.path_remoto)
        except InvalidOutboundURL as erro:
            raise RadarServiceError("URL da imagem do radar nao permitida") from erro
        resposta = None
        try:
            resposta = self.session.get(
                frame.path_remoto,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
                headers={"Accept": "image/png", "Accept-Encoding": "identity"},
            )
            resposta.raise_for_status()
            if not 200 <= resposta.status_code < 300:
                raise RadarServiceError("Download do radar retornou resposta HTTP nao permitida")
            return _ler_corpo_limitado(resposta, MAX_IMAGE_BYTES, "Imagem do radar")
        except requests.Timeout:
            raise RadarServiceError("Timeout ao baixar imagem do radar") from None
        except requests.RequestException:
            raise RadarServiceError("Falha HTTP ao baixar imagem do radar") from None
        finally:
            if resposta is not None:
                resposta.close()
