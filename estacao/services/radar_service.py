"""Cliente pequeno e defensivo para o produto MaxCAPPI da REDEMET."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urlsplit

import requests


REDEMET_RADAR_URL = "https://api-redemet.decea.mil.br/produtos/radar/maxcappi"
MAX_IMAGE_BYTES = 25 * 1024 * 1024


class RadarServiceError(RuntimeError):
    """Falha externa sanitizada, sem URL completa nem credenciais."""


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

    @property
    def data_texto(self) -> str:
        return self.data_frame.strftime("%Y-%m-%d %H:%M:%S")


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
    partes = urlsplit(path)
    if partes.scheme not in {"http", "https"} or not partes.netloc:
        raise RadarServiceError("Frame REDEMET possui URL de imagem invalida")
    try:
        data_frame = datetime.strptime(str(frame["data"]).strip(), "%Y-%m-%d %H:%M:%S")
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
        try:
            resposta = self.session.get(
                REDEMET_RADAR_URL,
                params={"api_key": self._api_key, "area": self.area, "anima": self.anima},
                timeout=self.timeout,
            )
            resposta.raise_for_status()
        except requests.Timeout as erro:
            raise RadarServiceError("Timeout ao consultar a REDEMET") from erro
        except requests.RequestException as erro:
            raise RadarServiceError("Falha HTTP ao consultar a REDEMET") from erro
        try:
            payload = resposta.json()
        except (ValueError, TypeError) as erro:
            raise RadarServiceError("REDEMET retornou JSON invalido") from erro
        return normalizar_resposta(payload, produto_padrao=self.produto)

    def baixar_imagem(self, frame: RadarFrame) -> bytes:
        try:
            resposta = self.session.get(frame.path_remoto, timeout=self.timeout)
            resposta.raise_for_status()
        except requests.Timeout as erro:
            raise RadarServiceError("Timeout ao baixar imagem do radar") from erro
        except requests.RequestException as erro:
            raise RadarServiceError("Falha HTTP ao baixar imagem do radar") from erro
        conteudo = resposta.content
        if not conteudo:
            raise RadarServiceError("Imagem do radar vazia")
        if len(conteudo) > MAX_IMAGE_BYTES:
            raise RadarServiceError("Imagem do radar excede o limite permitido")
        return conteudo
