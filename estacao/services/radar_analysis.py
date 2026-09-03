"""Geometria, segmentacao experimental e tracking conservador do radar."""

from __future__ import annotations

import math
import colorsys
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


EARTH_RADIUS_KM = 6371.0088

# Paleta observada em 42 PNGs MaxCAPPI reais de Jaraguari em 03/09/2026.
# Os grupos representam somente níveis relativos de refletividade: não há aqui
# conversão para dBZ, taxa de chuva ou severidade meteorológica.
REFLECTIVITY_CLASS_NAMES = {
    1: "REFLETIVIDADE_BAIXA",
    2: "REFLETIVIDADE_MEDIA",
    3: "REFLETIVIDADE_ALTA",
    4: "REFLETIVIDADE_MUITO_ALTA",
}
REFLECTIVITY_PALETTE = {
    1: (
        (148, 148, 148), (141, 141, 141),
        (85, 253, 253), (76, 227, 245), (67, 199, 234),
        (57, 170, 223), (48, 142, 213), (38, 114, 202),
        (29, 85, 191), (19, 57, 181), (10, 29, 170), (0, 0, 159),
    ),
    2: (
        (84, 253, 0), (79, 245, 0), (73, 235, 0), (67, 225, 0),
        (61, 215, 0), (55, 205, 0), (49, 195, 0), (43, 185, 0),
        (37, 175, 0), (31, 165, 0), (25, 155, 0), (19, 145, 0),
        (13, 135, 0), (7, 125, 0), (0, 115, 0),
    ),
    3: (
        (255, 254, 0), (255, 242, 0), (255, 230, 0), (255, 218, 0),
        (255, 206, 0), (255, 194, 0), (255, 182, 0), (255, 169, 0),
        (255, 157, 0), (255, 145, 0), (255, 133, 0), (255, 121, 0),
        (255, 109, 0), (255, 97, 0), (255, 84, 0),
    ),
    4: (
        (254, 0, 0), (246, 0, 0), (228, 0, 0),
        (201, 0, 0), (193, 0, 0), (184, 0, 0), (175, 0, 0),
    ),
}
CLASS_DIAGNOSTIC_COLORS = {
    1: (29, 85, 191, 255),
    2: (43, 185, 0, 255),
    3: (255, 169, 0, 255),
    4: (254, 0, 0, 255),
}


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
    valid_radius_km: float | None = None


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
    pixels_refletividade_baixa: int = 0
    pixels_refletividade_media: int = 0
    pixels_refletividade_alta: int = 0
    pixels_refletividade_muito_alta: int = 0
    classe_predominante: str | None = None
    classe_maxima: str | None = None


@dataclass(frozen=True)
class TrackPoint:
    data_frame: datetime
    centro_lat: float
    centro_lon: float
    distancia_centro_escola_km: float
    distancia_borda_escola_km: float
    pixels_eco: int

    def __post_init__(self):
        momento = self.data_frame
        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=timezone.utc)
        object.__setattr__(self, "data_frame", momento.astimezone(timezone.utc))


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


def classificar_pixels_refletividade(rgb: np.ndarray) -> np.ndarray:
    """Classifica somente cores confirmadas na paleta real do MaxCAPPI."""
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        raise ValueError("Array RGB invalido")
    packed = (
        rgb[:, :, 0].astype(np.uint32) << 16
        | rgb[:, :, 1].astype(np.uint32) << 8
        | rgb[:, :, 2].astype(np.uint32)
    )
    classes = np.zeros(rgb.shape[:2], dtype=np.uint8)
    for codigo, cores in REFLECTIVITY_PALETTE.items():
        valores = np.asarray(
            [(r << 16) | (g << 8) | b for r, g, b in cores], dtype=np.uint32
        )
        classes[np.isin(packed, valores)] = codigo
    return classes


def criar_mascara_area_valida(
    bounds: GeoBounds,
    width: int,
    height: int,
    radar_lat: float,
    radar_lon: float,
    radius_km: float | None = None,
    alpha: np.ndarray | None = None,
) -> np.ndarray:
    """Retorna a cobertura geográfica circular, opcionalmente limitada pelo alfa."""
    if radius_km is None:
        radius_km = min(
            haversine_km(radar_lat, radar_lon, bounds.lat_min, radar_lon),
            haversine_km(radar_lat, radar_lon, bounds.lat_max, radar_lon),
            haversine_km(radar_lat, radar_lon, radar_lat, bounds.lon_min),
            haversine_km(radar_lat, radar_lon, radar_lat, bounds.lon_max),
        )
    xs = np.arange(width, dtype=np.float64)
    ys = np.arange(height, dtype=np.float64)
    lons = bounds.lon_min + xs / max(1, width - 1) * (bounds.lon_max - bounds.lon_min)
    lats = bounds.lat_max - ys / max(1, height - 1) * (bounds.lat_max - bounds.lat_min)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    cobertura = _distancias_vetorizadas(
        lat_grid, lon_grid, radar_lat, radar_lon
    ) <= float(radius_km)
    if alpha is not None:
        cobertura &= alpha > 0
    return cobertura


def criar_mascaras_eco(
    rgb: np.ndarray,
    config: DetectionConfig,
    mascara_valida: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    classes = classificar_pixels_refletividade(rgb)
    eco = classes > 0
    if mascara_valida is not None:
        eco &= mascara_valida.astype(bool)
    original = eco.astype(np.uint8) * 255
    processada = original.copy()
    kernel = np.ones((3, 3), np.uint8)
    if config.close_iterations:
        processada = cv2.morphologyEx(
            processada,
            cv2.MORPH_CLOSE,
            kernel,
            iterations=config.close_iterations,
        )
    if config.dilate_iterations:
        processada = cv2.dilate(
            processada, kernel, iterations=config.dilate_iterations
        )
    return original, processada


def criar_mascara_eco(rgb: np.ndarray, config: DetectionConfig) -> np.ndarray:
    """Compatibilidade: retorna a mascara usada para conectar componentes."""
    return criar_mascaras_eco(rgb, config)[1]


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
    rgba = np.asarray(imagem.convert("RGBA"))
    rgb = rgba[:, :, :3]
    height, width = rgb.shape[:2]
    mascara_valida = criar_mascara_area_valida(
        bounds,
        width,
        height,
        radar_lat,
        radar_lon,
        radius_km=config.valid_radius_km,
        alpha=rgba[:, :, 3],
    )
    classes_refletividade = classificar_pixels_refletividade(rgb)
    mascara_original, mascara_processada = criar_mascaras_eco(
        rgb, config, mascara_valida
    )
    total, labels, stats, centroids = cv2.connectedComponentsWithStats(
        mascara_processada, connectivity=8
    )
    clusters: list[RadarCluster] = []
    for label in range(1, total):
        componente_processado = labels == label
        componente_original = componente_processado & (mascara_original > 0)
        pixels = int(np.count_nonzero(componente_original))
        if pixels < config.min_cluster_pixels:
            continue
        x = int(stats[label, cv2.CC_STAT_LEFT])
        y = int(stats[label, cv2.CC_STAT_TOP])
        w = int(stats[label, cv2.CC_STAT_WIDTH])
        h = int(stats[label, cv2.CC_STAT_HEIGHT])
        orig_y, orig_x = np.nonzero(componente_original)
        cx, cy = float(np.mean(orig_x)), float(np.mean(orig_y))
        centro_lat, centro_lon = pixel_para_latlon(cx, cy, bounds, width, height)

        # A morfologia apenas descobre quais fragmentos pertencem ao mesmo
        # sistema. Centro, intensidade e borda meteorológica usam somente os
        # pixels realmente presentes no PNG original.
        componente = componente_original.astype(np.uint8)
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

        classes_cluster = classes_refletividade[componente_original]
        contagens = {
            codigo: int(np.count_nonzero(classes_cluster == codigo))
            for codigo in REFLECTIVITY_CLASS_NAMES
        }
        presentes = [codigo for codigo, total_classe in contagens.items() if total_classe]
        predominante_codigo = max(contagens, key=lambda codigo: contagens[codigo])
        maxima_codigo = max(presentes)
        classe_predominante = REFLECTIVITY_CLASS_NAMES[predominante_codigo]
        classe_maxima = REFLECTIVITY_CLASS_NAMES[maxima_codigo]
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
                intensidade_codigo=classe_predominante,
                pixels_refletividade_baixa=contagens[1],
                pixels_refletividade_media=contagens[2],
                pixels_refletividade_alta=contagens[3],
                pixels_refletividade_muito_alta=contagens[4],
                classe_predominante=classe_predominante,
                classe_maxima=classe_maxima,
            )
        )
    return sorted(clusters, key=lambda item: item.distancia_borda_escola_km)


def diagnosticar_paleta(
    imagem: Image.Image,
    bounds: GeoBounds,
    radar_lat: float,
    radar_lon: float,
    radius_km: float | None = None,
) -> tuple[dict, Image.Image, Image.Image]:
    """Produz métricas e máscaras locais sem inferência meteorológica."""
    rgba = np.asarray(imagem.convert("RGBA"))
    rgb = rgba[:, :, :3]
    height, width = rgb.shape[:2]
    cobertura = criar_mascara_area_valida(
        bounds, width, height, radar_lat, radar_lon, radius_km=radius_km
    )
    visivel = rgba[:, :, 3] > 0
    classes = classificar_pixels_refletividade(rgb)
    eco = (classes > 0) & cobertura & visivel
    cores, totais = np.unique(rgb[visivel], axis=0, return_counts=True)
    principais = []
    for cor, total in sorted(
        zip(cores.tolist(), totais.tolist()), key=lambda item: item[1], reverse=True
    ):
        r, g, b = cor
        hue, saturation, value = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        codigo = int(classificar_pixels_refletividade(
            np.asarray([[[r, g, b]]], dtype=np.uint8)
        )[0, 0])
        principais.append(
            {
                "rgb": [r, g, b],
                "hsv": [round(hue * 360, 1), round(saturation * 100, 1), round(value * 100, 1)],
                "pixels": total,
                "classe": REFLECTIVITY_CLASS_NAMES.get(codigo, "DESCARTADO"),
            }
        )
    total_pixels = width * height
    contagens_classes = {
        REFLECTIVITY_CLASS_NAMES[codigo]: int(np.count_nonzero((classes == codigo) & eco))
        for codigo in REFLECTIVITY_CLASS_NAMES
    }
    mascara_original = Image.fromarray(eco.astype(np.uint8) * 255, mode="L")
    classes_rgba = np.zeros((height, width, 4), dtype=np.uint8)
    for codigo, cor in CLASS_DIAGNOSTIC_COLORS.items():
        classes_rgba[(classes == codigo) & eco] = cor
    mascara_classes = Image.fromarray(classes_rgba, mode="RGBA")
    resumo = {
        "dimensoes": [width, height],
        "modo": imagem.mode,
        "pixels_totais": total_pixels,
        "pixels_visiveis": int(np.count_nonzero(visivel)),
        "pixels_area_geografica_valida": int(np.count_nonzero(cobertura)),
        "pixels_eco": int(np.count_nonzero(eco)),
        "pixels_descartados_visiveis": int(np.count_nonzero(visivel & ~eco)),
        "percentual_imagem_eco": float(
            round(np.count_nonzero(eco) * 100 / total_pixels, 4)
        ),
        "classes": contagens_classes,
        "principais_cores": principais,
    }
    return resumo, mascara_original, mascara_classes


def pode_associar(
    anterior: TrackPoint,
    atual: TrackPoint,
    max_speed_kmh: float,
    max_size_ratio: float = 4.0,
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
    return velocidade <= max_speed_kmh and razao_tamanho <= max_size_ratio, distancia


def diferenca_angular_graus(a: float, b: float) -> float:
    return abs((float(a) - float(b) + 180) % 360 - 180)


def _projetar_posicao(
    penultimo: TrackPoint, ultimo: TrackPoint, momento: datetime
) -> tuple[float, float] | None:
    dt_historico = (ultimo.data_frame - penultimo.data_frame).total_seconds() / 3600
    dt_futuro = (momento - ultimo.data_frame).total_seconds() / 3600
    if dt_historico <= 0 or dt_futuro <= 0:
        return None
    x, y = _xy_local(
        penultimo.centro_lat,
        penultimo.centro_lon,
        ultimo.centro_lat,
        ultimo.centro_lon,
    )
    previsto_x = -x / dt_historico * dt_futuro
    previsto_y = -y / dt_historico * dt_futuro
    previsto_lat = ultimo.centro_lat + previsto_y / 111.195
    escala_lon = 111.195 * math.cos(math.radians(ultimo.centro_lat))
    if abs(escala_lon) < 1e-6:
        return None
    previsto_lon = ultimo.centro_lon + previsto_x / escala_lon
    return previsto_lat, previsto_lon


def custo_associacao(
    historico: Sequence[TrackPoint],
    atual: TrackPoint,
    max_speed_kmh: float,
    max_size_ratio: float = 4.0,
    max_direction_change_deg: float = 90.0,
    prediction_weight: float = 0.65,
    max_gap_minutes: float = 180.0,
) -> tuple[bool, float, dict]:
    """Gating e custo deterministico; menor custo representa maior continuidade."""
    if not historico:
        return False, math.inf, {}
    ultimo = historico[-1]
    dt_horas = (atual.data_frame - ultimo.data_frame).total_seconds() / 3600
    if dt_horas <= 0 or dt_horas * 60 > max_gap_minutes:
        return False, math.inf, {"dt_horas": dt_horas}
    valido, distancia_anterior = pode_associar(
        ultimo, atual, max_speed_kmh, max_size_ratio
    )
    if not valido:
        return False, math.inf, {"distancia_anterior_km": distancia_anterior}

    tamanho_razao = max(ultimo.pixels_eco, atual.pixels_eco) / max(
        1, min(ultimo.pixels_eco, atual.pixels_eco)
    )
    permitido_km = max(1.0, max_speed_kmh * dt_horas)
    distancia_prevista = distancia_anterior
    mudanca_direcao = None
    previsto = None
    if len(historico) >= 2:
        penultimo = historico[-2]
        previsto = _projetar_posicao(penultimo, ultimo, atual.data_frame)
        if previsto:
            distancia_prevista = haversine_km(
                previsto[0], previsto[1], atual.centro_lat, atual.centro_lon
            )
            if distancia_prevista > max(10.0, permitido_km):
                return False, math.inf, {"distancia_prevista_km": distancia_prevista}
        deslocamento_historico = haversine_km(
            penultimo.centro_lat,
            penultimo.centro_lon,
            ultimo.centro_lat,
            ultimo.centro_lon,
        )
        if deslocamento_historico >= 0.5 and distancia_anterior >= 0.5:
            direcao_historica = bearing_graus(
                penultimo.centro_lat,
                penultimo.centro_lon,
                ultimo.centro_lat,
                ultimo.centro_lon,
            )
            direcao_atual = bearing_graus(
                ultimo.centro_lat,
                ultimo.centro_lon,
                atual.centro_lat,
                atual.centro_lon,
            )
            mudanca_direcao = diferenca_angular_graus(
                direcao_historica, direcao_atual
            )
            if mudanca_direcao > max_direction_change_deg:
                return False, math.inf, {"mudanca_direcao": mudanca_direcao}

    peso = min(1.0, max(0.0, prediction_weight)) if previsto else 0.0
    custo_distancia = (
        peso * distancia_prevista + (1.0 - peso) * distancia_anterior
    ) / permitido_km
    custo_tamanho = math.log(max(1.0, tamanho_razao)) / math.log(
        max(1.0001, max_size_ratio)
    )
    custo_direcao = (
        mudanca_direcao / max_direction_change_deg
        if mudanca_direcao is not None and max_direction_change_deg > 0
        else 0.0
    )
    custo = custo_distancia * 0.7 + custo_tamanho * 0.2 + custo_direcao * 0.1
    return True, custo, {
        "dt_horas": dt_horas,
        "distancia_anterior_km": distancia_anterior,
        "distancia_prevista_km": distancia_prevista,
        "razao_tamanho": tamanho_razao,
        "mudanca_direcao": mudanca_direcao,
    }


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
        imagem = Image.open(BytesIO(conteudo)).convert("RGBA")
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
