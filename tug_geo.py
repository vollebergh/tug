"""
tug_geo.py -- Generieke geo-utilities (TUG-ontheffingen workflow)

Bevat coördinaattransformaties (WGS84 ↔ RD New), cirkel- en geometrie-helpers
en extract-helpers voor BAG-features. Geen project-specifieke domeinlogica.
"""

from datetime import datetime
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from tug_config import MAX_PUNT_AFSTAND_M, _MAANDEN_NL
from tug_types import Feature


# ──────────────────────────────────────────────
# Datumweergave
# ──────────────────────────────────────────────

def _datum_leesbaar(dt: datetime) -> str:
    return f"{dt.day} {_MAANDEN_NL[dt.month]} {dt.year}, {dt.strftime('%H:%M')}"


# ──────────────────────────────────────────────
# Coördinaattransformaties
# ──────────────────────────────────────────────

def make_transformer(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(src, dst, always_xy=True)


def wgs84_to_rd(lon: float, lat: float) -> tuple[float, float]:
    t = make_transformer("EPSG:4326", "EPSG:28992")
    return t.transform(lon, lat)


def circle_in_rd(x: float, y: float, straal: float) -> Polygon:
    return Point(x, y).buffer(straal)


def circle_bbox_wgs84(circle_rd: BaseGeometry) -> tuple[float, float, float, float]:
    t = make_transformer("EPSG:28992", "EPSG:4326")
    minx, miny, maxx, maxy = circle_rd.bounds
    lon_min, lat_min = t.transform(minx, miny)
    lon_max, lat_max = t.transform(maxx, maxy)
    return lon_min, lat_min, lon_max, lat_max


def shapely_from_geojson_geom(geom_dict: dict[str, Any]) -> BaseGeometry:
    return shape(geom_dict)


def transform_geom_to_rd(geom_wgs84: BaseGeometry) -> BaseGeometry:
    t = make_transformer("EPSG:4326", "EPSG:28992")
    return shapely_transform(lambda x, y: t.transform(x, y), geom_wgs84)


def point_wgs84_to_rd(lon: float, lat: float) -> Point:
    t = make_transformer("EPSG:4326", "EPSG:28992")
    return Point(*t.transform(lon, lat))


def _geom_rings_wgs84(geom_wgs: BaseGeometry) -> list[list[tuple[float, float]]]:
    if geom_wgs.geom_type == "Polygon":
        return [list(geom_wgs.exterior.coords)]
    elif geom_wgs.geom_type == "MultiPolygon":
        return [list(p.exterior.coords) for p in geom_wgs.geoms]
    return []


# ──────────────────────────────────────────────
# Puntlocaties (B03: één of meer per aanvraag)
# ──────────────────────────────────────────────

def puntlocaties(aanvraag: dict[str, Any]) -> list[tuple[float, float]]:
    """Retourneer de puntlocaties van een aanvraag als lijst (lat, lon).

    `coord_lat` en `coord_lon` zijn elk een getal (één locatie) of een lijst van
    even lange lengte (meerdere locaties; de n-de lat hoort bij de n-de lon).
    Meerdere locaties liggen onderling hooguit MAX_PUNT_AFSTAND_M uit elkaar.
    Gooit ValueError bij een ongeldige invoer.
    """
    lat, lon = aanvraag.get("coord_lat"), aanvraag.get("coord_lon")
    lats = lat if isinstance(lat, list) else [lat]
    lons = lon if isinstance(lon, list) else [lon]
    if len(lats) != len(lons):
        raise ValueError(f"coord_lat ({len(lats)}) en coord_lon ({len(lons)}) "
                         f"bevatten niet evenveel waarden")
    if not lats:
        raise ValueError("coord_lat/coord_lon mogen geen lege lijst zijn")
    punten = []
    for i, (la, lo) in enumerate(zip(lats, lons), 1):
        try:
            la, lo = float(la), float(lo)
        except (TypeError, ValueError):
            raise ValueError(f"puntlocatie {i}: geen geldige coördinaten ({la!r}, {lo!r})")
        if not (50.0 <= la <= 54.0 and 3.0 <= lo <= 8.0):
            raise ValueError(f"puntlocatie {i}: ({la}, {lo}) ligt niet in Nederland "
                             f"(lat/lon verwisseld?)")
        punten.append((la, lo))

    # Meerdere puntlocaties mogen onderling niet verder dan MAX_PUNT_AFSTAND_M uit elkaar liggen
    rd = [wgs84_to_rd(lo, la) for la, lo in punten]
    for i in range(len(rd)):
        for j in range(i + 1, len(rd)):
            afstand = ((rd[i][0] - rd[j][0]) ** 2 + (rd[i][1] - rd[j][1]) ** 2) ** 0.5
            if afstand > MAX_PUNT_AFSTAND_M:
                raise ValueError(
                    f"puntlocatie {i + 1} en {j + 1} liggen {afstand:.0f} m uit elkaar; "
                    f"maximaal {MAX_PUNT_AFSTAND_M} m toegestaan"
                )
    return punten


# ──────────────────────────────────────────────
# BAG-extract helpers
# ──────────────────────────────────────────────

def extract_adres(props: dict[str, Any]) -> tuple[str, str, str]:
    straat = (props.get("openbare_ruimte") or props.get("openbareruimtenaam")
              or props.get("openbareRuimteNaam") or props.get("straatnaam")
              or props.get("straatNaam") or props.get("naamOpenbareRuimte")
              or props.get("korteNaam") or "")
    huisnr = props.get("huisnummer") or ""
    toev   = props.get("huisletter") or ""
    toev2  = props.get("huisnummertoevoeging") or props.get("toevoeging") or ""
    pc     = props.get("postcode") or ""
    wpl    = (props.get("woonplaatsnaam") or props.get("woonplaatsNaam")
              or props.get("woonplaats") or "")
    adres  = f"{straat} {huisnr}{toev}{(' ' + toev2) if toev2 else ''}".strip()
    return adres, pc, wpl


def extract_lon_lat(feat: Feature) -> tuple[float, float]:
    geom = feat.get("geometry", {})
    if geom.get("type") == "Point":
        lon, lat = geom["coordinates"][0], geom["coordinates"][1]
        return lat, lon
    geom_obj = shapely_from_geojson_geom(geom)
    c = geom_obj.centroid
    return c.y, c.x
