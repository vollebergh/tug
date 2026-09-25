"""
tug_geo.py -- Generieke geo-utilities (TUG-ontheffingen workflow)

Bevat coördinaattransformaties (WGS84 ↔ RD New), cirkel- en geometrie-helpers
en extract-helpers voor BAG-features. Geen project-specifieke domeinlogica.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from tug_config import MAX_PUNT_AFSTAND_M, NAAM_SLUG_MAX, NL_BBOX, _MAANDEN_NL
from tug_types import Feature


# ──────────────────────────────────────────────
# Namen en identiteit
# ──────────────────────────────────────────────

def naam_slug(naam: str | None) -> str:
    """Bestandsnaamveilige variant van de dossieromschrijving.

    Wordt gebruikt voor de tijdelijke aanvraag-JSON van de GUI én voor de namen
    van de PNG-, PDF- en HTML-export, zodat invoer en uitvoer aan elkaar te
    koppelen zijn. Eén implementatie, zodat die namen niet uiteen kunnen lopen.
    """
    slug = re.sub(r"[^\w\-]+", "_", (naam or "").strip(), flags=re.UNICODE)
    return slug.strip("_")[:NAAM_SLUG_MAX]


def json_voor_script(waarde: Any) -> str:
    """JSON die veilig in een `<script>`-blok van een HTML-document kan staan.

    `<`, `>` en `&` worden als unicode-escape geschreven. Daarmee kan geen naam uit
    een register de scripttag sluiten (`</script>`) of de HTML-parser in een
    commentaar- of dubbel-escapetoestand brengen (`<!--<script>`); de JSON zelf
    verandert er niet door. U+2028/U+2029 gaan mee voor oudere JavaScript-engines.
    """
    tekst = json.dumps(waarde, ensure_ascii=False)
    for teken, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                          ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        tekst = tekst.replace(teken, escape)
    return tekst


def feature_sleutel(feat: Feature) -> str:
    """Stabiele identiteit van een feature, bruikbaar als sleutel in een set of dict.

    Features worden tussen bronstap, classificatie en drie presentatievormen
    doorgegeven. `id()` volstaat daarvoor niet: dat is alleen geldig zolang elke
    lijst exact dezelfde objecten bevat en die objecten in leven blijven. Eén
    kopie, deepcopy of JSON-ronde onderweg en alle lookups missen — stil, want
    een missende sleutel levert geen fout op maar een ander oordeel.

    De BAG-identificatie is de sleutel waar die bestaat; DUO-scholen dragen het
    BAG-id van hun vestiging als `vbo_id`. Ontbreekt beide, dan wordt een sleutel
    afgeleid uit naam en geometrie: ook dat blijft gelijk na kopiëren.
    """
    props = feat.get("properties") or {}
    for veld in ("identificatie", "vbo_id", "id"):
        waarde = str(props.get(veld) or "").strip()
        if waarde:
            return waarde
    kern = json.dumps(
        [props.get("naam", ""), props.get("adres", ""), feat.get("geometry")],
        sort_keys=True, ensure_ascii=False,
    )
    return "afgeleid:" + hashlib.sha256(kern.encode("utf-8")).hexdigest()[:16]


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
    if geom_wgs.geom_type == "MultiPolygon":
        return [list(p.exterior.coords) for p in geom_wgs.geoms]
    return []


# ──────────────────────────────────────────────
# Puntlocaties (B03: één of meer per aanvraag)
# ──────────────────────────────────────────────

def in_nederland(lat: float, lon: float) -> bool:
    """Ligt dit punt binnen de omhullende van Nederland?

    Vangt de meest voorkomende invoerfout: lat en lon verwisseld. De GUI toetst
    dit bij handmatige invoer, de validatiestap bij een aanvraag-JSON — beide
    via deze ene functie, zodat het venster niet groen kan geven waar het rapport
    een gebrek meldt.
    """
    lat_min, lat_max, lon_min, lon_max = NL_BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def puntlocaties(aanvraag: dict[str, Any]) -> list[tuple[float, float]]:
    """Retourneer de puntlocaties van een aanvraag als lijst (lat, lon).

    `coord_lat` en `coord_lon` zijn elk een getal (één locatie) of een lijst van
    even lange lengte (meerdere locaties; de n-de lat hoort bij de n-de lon).
    Liggen meerdere locaties verder dan MAX_PUNT_AFSTAND_M uit elkaar, dan is dat
    een melding en geen fout (B14): zie max_onderlinge_afstand().
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
        except (TypeError, ValueError) as fout:
            raise ValueError(
                f"puntlocatie {i}: geen geldige coördinaten ({la!r}, {lo!r})"
            ) from fout
        if not in_nederland(la, lo):
            raise ValueError(f"puntlocatie {i}: ({la}, {lo}) ligt niet in Nederland "
                             f"(lat/lon verwisseld?)")
        punten.append((la, lo))
    return punten


def max_onderlinge_afstand(punten: list[tuple[float, float]]) -> float:
    """Grootste onderlinge afstand in meters (RD) tussen puntlocaties (lat, lon).

    0 bij minder dan twee punten. Boven MAX_PUNT_AFSTAND_M volgt een melding in
    schil en rapport; de toetsing gaat door (B14).
    """
    rd = [wgs84_to_rd(lo, la) for la, lo in punten]
    return max((((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
                for i, a in enumerate(rd) for b in rd[i + 1:]), default=0.0)


def puntafstand_melding(afstand: float) -> str:
    """Eén zin over de onderlinge afstand, gelijk in schil, rapport en HTML-kaart."""
    tekst = f"Grootste onderlinge afstand tussen de puntlocaties: {afstand:.0f} m"
    if afstand > MAX_PUNT_AFSTAND_M:
        tekst += (f" — meer dan {MAX_PUNT_AFSTAND_M} m. De toetsing is uitgevoerd op de "
                  f"vereniging van de cirkels rond alle puntlocaties; beoordeel of dit één "
                  f"samenhangend terrein is")
    return tekst + "."


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
