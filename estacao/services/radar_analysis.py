"""Geometria, segmentacao experimental e tracking conservador do radar."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class GeoBounds:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float


@dataclass(frozen=True)
class DetectionConfig:
    min_cluster_pixels: int = 100
    close_iterations: int = 2
    dilate_iterations: int = 1
    clutter_radius_km: float = 50.0


@dataclass(frozen=True)
class RadarCluster:
    numero: int
    pixels_eco: int
    centro_x: float
    centro_y: float
    centro_lat: float
    centro_lon: float
    bbox_x: int
    bbox_y: int
    bbox_width: int
    bbox_height: int
    distancia_centro_escola_km: float
    distancia_borda_escola_km: float
    distancia_radar_km: float
    direcao_relativa_escola: str
    suspeito_clutter: bool
    intensidade_codigo: str


@dataclass(frozen=True)
class TrackPoint:
    data_frame: datetime
    centro_lat: float
    centro_lon: float
    distancia_centro_escola_km: float
    distancia_borda_escola_km: float
    pixels_eco: int


@dataclass(frozen=True)
class TrackAnalysis:
    primeiro_frame_em: datetime
    ultimo_frame_em: datetime
    quantidade_frames: int
    duracao_minutos: float
    deslocamento_total_km: float
    velocidade_media_kmh: float | None
    bearing_movimento: float | None
    direcao_movimento: str | None
    distancia_centro_escola_km: float
    distancia_borda_escola_km: float
    aproximando: bool | None
    taxa_aproximacao_kmh: float | None
    trajetoria_compativel: bool
    menor_aproximacao_km: float | None
    eta_minutos: float | None
    status: str


def latlon_para_pixel(
    lat: float, lon: float, bounds: GeoBounds, width: int, height: int
) -> tuple[float, float]:
    if width < 2 or height < 2:
        raise ValueError("A imagem precisa ter ao menos 2x2 pixels")
    if bounds.lon_max <= bounds.lon_min or bounds.lat_max <= bounds.lat_min:
        raise ValueError("Limites geograficos invalidos")
    x = (lon - bounds.lon_min) / (bounds.lon_max - bounds.lon_min) * (width - 1)
    y = (bounds.lat_max - lat) / (bounds.lat_max - bounds.lat_min) * (height - 1)
    return x, y


def pixel_para_latlon(
    x: float, y: float, bounds: GeoBounds, width: int, height: int
) -> tuple[float, float]:
    if width < 2 or height < 2:
        raise ValueError("A imagem precisa ter ao menos 2x2 pixels")
    lon = bounds.lon_min + x / (width - 1) * (bounds.lon_max - bounds.lon_min)
    lat = bounds.lat_max - y / (height - 1) * (bounds.lat_max - bounds.lat_min)
    return lat, lon


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = p2 - p1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing_graus(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)
    y = math.sin(dlon) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def direcao_cardinal(bearing: float | None) -> str | None:
    if bearing is None:
        return None
    nomes = ("N", "NE", "L", "SE", "S", "SO", "O", "NO")
    return nomes[int((bearing + 22.5) // 45) % 8]


def _distancias_vetorizadas(
    lats: np.ndarray, lons: np.ndarray, target_lat: float, target_lon: float
) -> np.ndarray:
    p1 = np.radians(lats)
    p2 = math.radians(target_lat)
    dlat = p2 - p1
    dlon = math.radians(target_lon) - np.radians(lons)
    a = np.sin(dlat / 2) ** 2 + np.cos(p1) * math.cos(p2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.minimum(1.0, np.sqrt(a)))


def criar_mascara_eco(rgb: np.ndarray, config: DetectionConfig) -> np.ndarray:
    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    azul_ciano = (b >= 140) & (r <= 110) & ((b - r) >= 50)
    verde = (g >= 120) & (r <= 110) & (b <= 140) & ((g - r) >= 50)
    mascara = ((azul_ciano | verde).astype(np.uint8)) * 255
    kernel = np.ones((3, 3), np.uint8)
    if config.close_iterations:
        mascara = cv2.morphologyEx(
            mascara,
            cv2.MORPH_CLOSE,
            kernel,
            iterations=config.close_iterations,
        )
    if config.dilate_iterations:
        mascara = cv2.dilate(mascara, kernel, iterations=config.dilate_iterations)
    return mascara


def detectar_clusters(
    imagem: Image.Image,
    bounds: GeoBounds,
    target_lat: float,
    target_lon: float,
    radar_lat: float,
    radar_lon: float,
    config: DetectionConfig | None = None,
) -> list[RadarCluster]:
    """Detecta areas coloridas; o resultado e eco experimental, nao chuva confirmada."""
    config = config or DetectionConfig()
    rgb = np.asarray(imagem.convert("RGB"))
    height, width = rgb.shape[:2]
    mascara = criar_mascara_eco(rgb, config)
    total, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mascara, connectivity=8
    )
    clusters: list[RadarCluster] = []
    for label in range(1, total):
        pixels = int(stats[label, cv2.CC_STAT_AREA])
        if pixels < config.min_cluster_pixels:
            continue
        x = int(stats[label, cv2.CC_STAT_LEFT])
        y = int(stats[label, cv2.CC_STAT_TOP])
        w = int(stats[label, cv2.CC_STAT_WIDTH])
        h = int(stats[label, cv2.CC_STAT_HEIGHT])
        cx, cy = (float(v) for v in centroids[label])
        centro_lat, centro_lon = pixel_para_latlon(cx, cy, bounds, width, height)

        componente = (labels == label).astype(np.uint8)
        interior = cv2.erode(componente, np.ones((3, 3), np.uint8), iterations=1)
        borda_y, borda_x = np.nonzero(componente - interior)
        lons = bounds.lon_min + borda_x / (width - 1) * (bounds.lon_max - bounds.lon_min)
        lats = bounds.lat_max - borda_y / (height - 1) * (bounds.lat_max - bounds.lat_min)
        distancia_borda = float(
            np.min(_distancias_vetorizadas(lats, lons, target_lat, target_lon))
        )
        distancia_centro = haversine_km(
            centro_lat, centro_lon, target_lat, target_lon
        )
        distancia_radar = haversine_km(
            centro_lat, centro_lon, radar_lat, radar_lon
        )

        pixels_rgb = rgb[labels == label]
        verdes = np.count_nonzero(
            (pixels_rgb[:, 1] >= 120)
            & (pixels_rgb[:, 0] <= 110)
            & (pixels_rgb[:, 2] <= 140)
        )
        azuis = max(0, len(pixels_rgb) - verdes)
        intensidade = "MISTO" if verdes and azuis else ("VERDE" if verdes else "AZUL_CIANO")
        clusters.append(
            RadarCluster(
                numero=len(clusters) + 1,
                pixels_eco=pixels,
                centro_x=cx,
                centro_y=cy,
                centro_lat=centro_lat,
                centro_lon=centro_lon,
                bbox_x=x,
                bbox_y=y,
                bbox_width=w,
                bbox_height=h,
                distancia_centro_escola_km=distancia_centro,
                distancia_borda_escola_km=distancia_borda,
                distancia_radar_km=distancia_radar,
                direcao_relativa_escola=direcao_cardinal(
                    bearing_graus(target_lat, target_lon, centro_lat, centro_lon)
                ) or "-",
                suspeito_clutter=distancia_radar <= config.clutter_radius_km,
                intensidade_codigo=intensidade,
            )
        )
    return sorted(clusters, key=lambda item: item.distancia_borda_escola_km)


def pode_associar(
    anterior: TrackPoint, atual: TrackPoint, max_speed_kmh: float
) -> tuple[bool, float]:
    horas = (atual.data_frame - anterior.data_frame).total_seconds() / 3600
    if horas <= 0:
        return False, math.inf
    distancia = haversine_km(
        anterior.centro_lat, anterior.centro_lon, atual.centro_lat, atual.centro_lon
    )
    velocidade = distancia / horas
    razao_tamanho = max(anterior.pixels_eco, atual.pixels_eco) / max(
        1, min(anterior.pixels_eco, atual.pixels_eco)
    )
    return velocidade <= max_speed_kmh and razao_tamanho <= 4.0, distancia


def _xy_local(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    norte = (lat - ref_lat) * 111.195
    leste = (lon - ref_lon) * 111.195 * math.cos(math.radians(ref_lat))
    return leste, norte


def analisar_track(
    pontos: Sequence[TrackPoint],
    target_lat: float,
    target_lon: float,
    min_frames: int = 3,
    min_duration_minutes: float = 10.0,
    max_speed_kmh: float = 150.0,
    intercept_radius_km: float = 25.0,
) -> TrackAnalysis:
    if not pontos:
        raise ValueError("Track sem pontos")
    pontos = sorted(pontos, key=lambda p: p.data_frame)
    primeiro, ultimo = pontos[0], pontos[-1]
    duracao_h = (ultimo.data_frame - primeiro.data_frame).total_seconds() / 3600
    passos = [
        haversine_km(a.centro_lat, a.centro_lon, b.centro_lat, b.centro_lon)
        for a, b in zip(pontos, pontos[1:])
    ]
    caminho = sum(passos)
    deslocamento = haversine_km(
        primeiro.centro_lat,
        primeiro.centro_lon,
        ultimo.centro_lat,
        ultimo.centro_lon,
    )
    velocidade = caminho / duracao_h if duracao_h > 0 else None
    bearing = (
        bearing_graus(
            primeiro.centro_lat,
            primeiro.centro_lon,
            ultimo.centro_lat,
            ultimo.centro_lon,
        )
        if deslocamento >= 0.5
        else None
    )
    taxa = (
        (primeiro.distancia_centro_escola_km - ultimo.distancia_centro_escola_km)
        / duracao_h
        if duracao_h > 0
        else None
    )
    aproximando = None if taxa is None or abs(taxa) < 1.0 else taxa > 0
    coerencia = deslocamento / caminho if caminho > 0 else 0.0
    velocidades_passos = []
    for a, b, distancia_passo in zip(pontos, pontos[1:], passos):
        horas_passo = (b.data_frame - a.data_frame).total_seconds() / 3600
        velocidades_passos.append(
            distancia_passo / horas_passo if horas_passo > 0 else math.inf
        )
    movimento_valido = bool(
        len(pontos) >= min_frames
        and duracao_h * 60 >= min_duration_minutes
        and velocidade is not None
        and 2.0 <= velocidade <= max_speed_kmh
        and all(v <= max_speed_kmh for v in velocidades_passos)
        and coerencia >= 0.6
    )

    trajetoria = False
    menor_aproximacao = None
    eta = None
    if movimento_valido and aproximando:
        x0, y0 = _xy_local(
            primeiro.centro_lat, primeiro.centro_lon, target_lat, target_lon
        )
        x1, y1 = _xy_local(
            ultimo.centro_lat, ultimo.centro_lon, target_lat, target_lon
        )
        vx, vy = (x1 - x0) / duracao_h, (y1 - y0) / duracao_h
        velocidade_quadrada = vx * vx + vy * vy
        t_closest = -(x1 * vx + y1 * vy) / velocidade_quadrada
        if t_closest >= 0:
            closest_x, closest_y = x1 + vx * t_closest, y1 + vy * t_closest
            menor_aproximacao = math.hypot(closest_x, closest_y)
            trajetoria = menor_aproximacao <= intercept_radius_km
            if trajetoria:
                # Primeiro instante futuro em que o centro entra no raio-alvo.
                b = 2 * (x1 * vx + y1 * vy)
                c = x1 * x1 + y1 * y1 - intercept_radius_km**2
                discriminante = max(0.0, b * b - 4 * velocidade_quadrada * c)
                entrada_h = (-b - math.sqrt(discriminante)) / (2 * velocidade_quadrada)
                eta = max(0.0, entrada_h * 60)

    if len(pontos) < min_frames:
        status = "DADOS_INSUFICIENTES"
    elif not movimento_valido:
        status = "ECO_ESTACIONARIO" if deslocamento < 2 else "DADOS_INSUFICIENTES"
    elif trajetoria:
        status = "TRAJETORIA_COMPATIVEL"
    elif aproximando:
        status = "APROXIMANDO"
    elif aproximando is False:
        status = "AFASTANDO"
    else:
        status = "SISTEMA_EM_MOVIMENTO"

    return TrackAnalysis(
        primeiro_frame_em=primeiro.data_frame,
        ultimo_frame_em=ultimo.data_frame,
        quantidade_frames=len(pontos),
        duracao_minutos=max(0.0, duracao_h * 60),
        deslocamento_total_km=caminho,
        velocidade_media_kmh=velocidade if movimento_valido else None,
        bearing_movimento=bearing if movimento_valido else None,
        direcao_movimento=direcao_cardinal(bearing) if movimento_valido else None,
        distancia_centro_escola_km=ultimo.distancia_centro_escola_km,
        distancia_borda_escola_km=ultimo.distancia_borda_escola_km,
        aproximando=aproximando if movimento_valido else None,
        taxa_aproximacao_kmh=taxa if movimento_valido else None,
        trajetoria_compativel=trajetoria,
        menor_aproximacao_km=menor_aproximacao,
        eta_minutos=eta,
        status=status,
    )


def abrir_imagem_png(conteudo: bytes) -> Image.Image:
    try:
        original = Image.open(BytesIO(conteudo))
        formato = original.format
        original.verify()
        imagem = Image.open(BytesIO(conteudo)).convert("RGB")
    except Exception as erro:
        raise ValueError("Arquivo recebido nao e uma imagem PNG valida") from erro
    if (formato or "").upper() != "PNG":
        raise ValueError("Imagem do radar nao esta no formato PNG")
    return imagem


def gerar_imagem_analisada(
    imagem: Image.Image,
    bounds: GeoBounds,
    target_lat: float,
    target_lon: float,
    clusters: Sequence[RadarCluster],
) -> Image.Image:
    saida = imagem.convert("RGB").copy()
    draw = ImageDraw.Draw(saida)
    width, height = saida.size
    escola_x, escola_y = latlon_para_pixel(target_lat, target_lon, bounds, width, height)
    fonte = ImageFont.load_default()

    km_lat_pixel = (bounds.lat_max - bounds.lat_min) * 111.195 / (height - 1)
    km_lon_pixel = (
        (bounds.lon_max - bounds.lon_min)
        * 111.195
        * math.cos(math.radians(target_lat))
        / (width - 1)
    )
    for raio in (25, 50, 100, 150, 200, 250, 300):
        raio_y_px = raio / km_lat_pixel
        raio_x_px = raio / km_lon_pixel
        caixa = (
            escola_x - raio_x_px,
            escola_y - raio_y_px,
            escola_x + raio_x_px,
            escola_y + raio_y_px,
        )
        if caixa[0] >= 0 and caixa[1] >= 0 and caixa[2] < width and caixa[3] < height:
            draw.ellipse(caixa, outline=(255, 255, 255), width=1)
            draw.text((escola_x + 3, escola_y - raio_y_px + 2), f"{raio} km", fill="white", font=fonte)

    r = max(5, min(width, height) // 80)
    draw.ellipse((escola_x - r, escola_y - r, escola_x + r, escola_y + r), fill=(255, 40, 40), outline="white", width=2)
    draw.text((escola_x + r + 3, escola_y - r), "EE Sao Jose", fill="white", font=fonte, stroke_width=2, stroke_fill="black")

    for cluster in clusters:
        cor = (255, 193, 7) if cluster.suspeito_clutter else (0, 255, 170)
        caixa = (
            cluster.bbox_x,
            cluster.bbox_y,
            cluster.bbox_x + cluster.bbox_width,
            cluster.bbox_y + cluster.bbox_height,
        )
        draw.rectangle(caixa, outline=cor, width=max(2, width // 375))
        rotulo = f"E{cluster.numero} borda {cluster.distancia_borda_escola_km:.1f} km"
        if cluster.suspeito_clutter:
            rotulo += " - clutter?"
        draw.text(
            (cluster.bbox_x, max(0, cluster.bbox_y - 13)),
            rotulo,
            fill=cor,
            font=fonte,
            stroke_width=2,
            stroke_fill="black",
        )
    return saida
