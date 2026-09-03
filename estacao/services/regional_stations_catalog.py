"""Catalogo unico das estacoes regionais monitoradas."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RegionalStation:
    code: str
    display_name: str
    configured_lat: float
    configured_lon: float


REGIONAL_STATIONS = {
    station.code: station
    for station in (
        RegionalStation("A721", "Dourados", -22.19388888, -54.91138888),
        RegionalStation("S706", "Caarapó", -22.65694444, -54.81944443),
        RegionalStation("A749", "Juti", -22.85722222, -54.60555555),
        RegionalStation("S735", "Naviraí", -22.899909, -53.869219),
        RegionalStation("A709", "Ivinhema", -22.30055555, -53.82277777),
        RegionalStation("S708", "Culturama", -22.30861111, -54.32583332),
    )
}
REGIONAL_STATION_CODES = tuple(REGIONAL_STATIONS)
