"""
tug_03_bronnen.py -- Databronnen en geo-hulpfuncties (TUG-ontheffingen workflow)
Versie: 4.4.0  |  2026-05-04

Bevat: alle constanten, coördinaat-utilities, BAG-functies, reverse geocoding,
signaleringsfuncties (begraafplaatsen, maneges, N2000, NNN, luchthavens),
KDV, DUO-scholen en extract-helpers.

Geen interne project-imports; dit module heeft geen afhankelijkheden van andere
tug_03_*-modules.
"""

import copy
import hashlib
import io as _io
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from pyproj import Transformer
from shapely.geometry import Point, Polygon, MultiPolygon, shape
from shapely.ops import transform as shapely_transform

try:
    import geopandas as gpd
    _HAS_GEOPANDAS = True
except ImportError:
    gpd = None
    _HAS_GEOPANDAS = False

# ──────────────────────────────────────────────
# Globale configuratie
# ──────────────────────────────────────────────

VERSION = "4.4.0"
MODEL_LABEL = ""

OUTPUT_DIR = Path(__file__).parent / "output"
GEO_DIR    = Path(__file__).parent / "geo"

BAG_WFS = "https://service.pdok.nl/lv/bag/wfs/v2_0"
PDOK_LOCATION_API     = "https://api.pdok.nl/kadaster/location-api/v1/search"
LOCATIESERVER_REVERSE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
LOCATIESERVER_FREE    = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

MARGE_M = 75
MANEGE_SIGNAAL_MARGE = 375
BEGRAAFPLAATS_ZOEK_MARGE = 1500

N2000_WFS          = "https://service.pdok.nl/rvo/natura2000/wfs/v1_0"
N2000_WFS_LAYER    = "natura2000:natura2000"
N2000_SIGNAAL_MARGE = 500   # m buiten toetsingsstraal voor aanvullende N2000-signalering

NNN_GML_URL        = ("https://service.pdok.nl/provincies/natuurnetwerk-nederland"
                      "/atom/downloads/inspire-pv-ps.nlps-nnn.gml")
NNN_GPKG           = GEO_DIR / "nnn_gebieden.gpkg"
NNN_META           = GEO_DIR / "nnn.meta.json"
NNN_TTL_DAGEN      = 180    # herdownload pas na 180 dagen
NNN_SIGNAAL_MARGE  = 500    # m buiten toetsingsstraal voor NNN-signalering

LUCHTHAVEN_WFS       = "https://services.geodataoverijssel.nl/geoserver/B64_nutsvoorzieningen/wfs"
LUCHTHAVEN_WFS_LAYER = "B64_nutsvoorzieningen:B6_Luchthaven_puntlocaties"
LUCHTHAVEN_GRENS_M   = 1000  # wettelijke minimumafstand puntlocatie → luchthaven
LUCHTHAVEN_SIGNAAL_M = 2000  # signaleringmarge voor luchthaven

MANEGE_ZOEKTERMEN = [
    "manege", "rijschool", "hippisch", "paardencentrum", "rijvereniging",
    "ponyclub", "ruiterclub", "ruitersportcentrum", "paardensportcentrum",
    "paardensportvereniging", "paardenhouderij", "hippique",
]

GELUIDGEVOELIGE_DOELEN = {"woonfunctie", "onderwijsfunctie", "gezondheidszorgfunctie", "logiesfunctie"}

LRK_URL        = "https://www.landelijkregisterkinderopvang.nl/opendata/export_opendata_lrk.csv"
LRK_CACHE_DAYS = 7
KDV_BBOX_EXTRA = 150

DUO_PROVINCIE  = "Overijssel"
DUO_TTL_DAGEN  = 90
DUO_DATASETS   = {
    "PO":  {"resource_id": "dcc9c9a5-6d01-410b-967f-810557588ba4", "naam_veld": "VESTIGINGSNAAM"},
    "SO":  {"resource_id": "8f0f1639-712d-4adb-bb59-cabd43730dc8", "naam_veld": "VESTIGINGSNAAM"},
    "VO":  {"resource_id": "5187f8d5-ff9c-4284-8e06-4311f0354956", "naam_veld": "VESTIGINGSNAAM"},
    "MBO": {"resource_id": "1a946297-a7ca-48d5-9ae8-19ad73bf8176", "naam_veld": "INSTELLINGSNAAM"},
    "HO":  {"resource_id": "bf1da9c6-c688-4873-91b1-b12c9ac2c132", "naam_veld": "INSTELLINGSNAAM"},
}
_DUO_PO_GROEP     = ["PO"]
_DUO_OVERIG_GROEP = ["SO", "VO", "MBO", "HO"]

SCHOLEN_GEOJSON_PO     = GEO_DIR / "duo_scholen_po.geojson"
SCHOLEN_GEOJSON_OVERIG = GEO_DIR / "duo_scholen_overig.geojson"
SCHOLEN_META           = GEO_DIR / "duo_scholen.meta.json"

BAG_PAGE_SIZE = 1000

_TILE_URL  = "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"
_TILE_SIZE = 256

# Positie van de puntlocatie in het landscape canvas (fractioneel)
_LOC_CX_FRAC    = 0.40   # 40% van links
_LOC_CY_FRAC    = 0.50   # 50% van boven (geen Y-verschuiving)
_ZOOM_FILL_FRAC = 0.9    # cirkel vult 90% van kaarthoogte (ongewijzigd)

_PDF_MARGIN_MM = 15
_PDF_DPI       = 150
_PDF_PAGE_W_MM, _PDF_PAGE_H_MM = 210, 297

_MAANDEN_NL = {
    1: "januari", 2: "februari", 3: "maart", 4: "april",
    5: "mei", 6: "juni", 7: "juli", 8: "augustus",
    9: "september", 10: "oktober", 11: "november", 12: "december",
}


def _datum_leesbaar(dt):
    return f"{dt.day} {_MAANDEN_NL[dt.month]} {dt.year}, {dt.strftime('%H:%M')}"


# ──────────────────────────────────────────────
# Hulpfuncties — coördinaten
# ──────────────────────────────────────────────

def make_transformer(src, dst):
    return Transformer.from_crs(src, dst, always_xy=True)


def wgs84_to_rd(lon, lat):
    t = make_transformer("EPSG:4326", "EPSG:28992")
    return t.transform(lon, lat)


def circle_in_rd(x, y, straal):
    return Point(x, y).buffer(straal)


def circle_bbox_wgs84(circle_rd):
    t = make_transformer("EPSG:28992", "EPSG:4326")
    minx, miny, maxx, maxy = circle_rd.bounds
    lon_min, lat_min = t.transform(minx, miny)
    lon_max, lat_max = t.transform(maxx, maxy)
    return lon_min, lat_min, lon_max, lat_max


def shapely_from_geojson_geom(geom_dict):
    return shape(geom_dict)


def transform_geom_to_rd(geom_wgs84):
    t = make_transformer("EPSG:4326", "EPSG:28992")
    return shapely_transform(lambda x, y: t.transform(x, y), geom_wgs84)


def point_wgs84_to_rd(lon, lat):
    t = make_transformer("EPSG:4326", "EPSG:28992")
    return Point(*t.transform(lon, lat))


# ──────────────────────────────────────────────
# BAG — verblijfsobjecten ophalen
# ──────────────────────────────────────────────

def haal_verblijfsobjecten(circle_rd, log):
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(circle_rd)
    bbox_str = f"{lat_min},{lon_min},{lat_max},{lon_max},EPSG:4326"
    log(f"  Ophalen verblijfsobjecten via BAG WFS v2.0 (bbox {lat_min:.5f},{lon_min:.5f},{lat_max:.5f},{lon_max:.5f}) ...")

    features = []
    start_index = 0

    while True:
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "TYPENAME": "bag:verblijfsobject", "outputFormat": "application/json",
            "srsName": "EPSG:4326", "SRSNAME": "urn:ogc:def:crs:EPSG::4326",
            "BBOX": bbox_str, "count": BAG_PAGE_SIZE, "startIndex": start_index,
            "propertyName": ("bag:identificatie,bag:gebruiksdoel,bag:openbare_ruimte,"
                             "bag:huisnummer,bag:huisletter,bag:toevoeging,"
                             "bag:postcode,bag:woonplaats,bag:pandidentificatie"),
        }
        resp = requests.get(BAG_WFS, params=params, timeout=30)
        if resp.status_code != 200:
            log(f"  WAARSCHUWING: BAG WFS gaf statuscode {resp.status_code}")
            break
        data = resp.json()
        batch = data.get("features", [])
        if features == [] and batch:
            log(f"  BAG verblijfsobject property-namen: {list(batch[0].get('properties', {}).keys())}")
        features.extend(batch)
        if len(batch) < BAG_PAGE_SIZE:
            break
        start_index += BAG_PAGE_SIZE

    log(f"  {len(features)} verblijfsobjecten opgehaald binnen bbox.")
    return features


def filter_binnen_straal(features, circle_rd, log):
    binnen = []
    totaal = len(features)
    stap = max(1, totaal // 5)
    for i, feat in enumerate(features, 1):
        geom = feat.get("geometry")
        if not geom:
            continue
        geom_rd = transform_geom_to_rd(shapely_from_geojson_geom(geom))
        if circle_rd.contains(geom_rd) or circle_rd.intersects(geom_rd):
            binnen.append(feat)
        if totaal > 50 and i % stap == 0:
            log(f"  Puntcheck: {i}/{totaal} ({round(i / totaal * 100)}%) verwerkt ...")
    log(f"  {len(binnen)} verblijfsobjecten vallen binnen de straalcirkel (puntcheck).")
    return binnen


def filter_binnen_marge(features, circle_rd, circle_marge_rd, log):
    marge = []
    totaal = len(features)
    stap = max(1, totaal // 5)
    for i, feat in enumerate(features, 1):
        geom = feat.get("geometry")
        if not geom:
            continue
        geom_rd = transform_geom_to_rd(shapely_from_geojson_geom(geom))
        in_marge  = circle_marge_rd.contains(geom_rd) or circle_marge_rd.intersects(geom_rd)
        in_straal = circle_rd.contains(geom_rd) or circle_rd.intersects(geom_rd)
        if in_marge and not in_straal:
            marge.append(feat)
        if totaal > 50 and i % stap == 0:
            log(f"  Margeband puntcheck: {i}/{totaal} ({round(i / totaal * 100)}%) verwerkt ...")
    log(f"  {len(marge)} verblijfsobjecten vallen in de margeband (+{MARGE_M} m).")
    return marge


def _parse_doelen(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        result = []
        for item in raw:
            result.extend(d.strip() for d in str(item).split(",") if d.strip())
        return result
    return [d.strip() for d in str(raw).split(",") if d.strip()]


def dedupliceer_vbo(features, log):
    gegroepeerd = {}
    for feat in features:
        props = feat.get("properties", {})
        ident = str(props.get("identificatie") or props.get("id") or id(feat))
        doelen_nieuw = _parse_doelen(props.get("gebruiksdoel") or props.get("gebruiksdoelen"))
        if ident not in gegroepeerd:
            feat_copy = copy.deepcopy(feat)
            feat_copy["properties"]["gebruiksdoelen"] = list(doelen_nieuw)
            gegroepeerd[ident] = feat_copy
        else:
            bestaande = gegroepeerd[ident]["properties"]["gebruiksdoelen"]
            for d in doelen_nieuw:
                if d and d not in bestaande:
                    bestaande.append(d)
    resultaat = list(gegroepeerd.values())
    n_dupes = len(features) - len(resultaat)
    if n_dupes > 0:
        log(f"  Deduplicatie: {len(features)} → {len(resultaat)} unieke VBO's ({n_dupes} dubbele gebruiksdoel-regels samengevoegd).")
    else:
        log(f"  Deduplicatie: geen dubbele VBO's ({len(resultaat)} unieke VBO's).")
    return resultaat


def filter_geluidgevoelig(features, log):
    resultaat = [
        f for f in features
        if set(f.get("properties", {}).get("gebruiksdoelen") or
               _parse_doelen(f.get("properties", {}).get("gebruiksdoel"))) & GELUIDGEVOELIGE_DOELEN
    ]
    log(f"  {len(resultaat)} verblijfsobjecten hebben een geluidgevoelig gebruiksdoel.")
    return resultaat


# ──────────────────────────────────────────────
# BAG — pandgeometrie ophalen en gevel-check
# ──────────────────────────────────────────────

def haal_pand_ids(features):
    pand_ids = set()
    for feat in features:
        pid = feat.get("properties", {}).get("pandidentificatie") or feat.get("properties", {}).get("maaktDeelUitVan")
        if not pid:
            continue
        if isinstance(pid, list):
            for p in pid:
                if p:
                    pand_ids.add(str(p))
        else:
            pand_ids.add(str(pid))
    return pand_ids


def haal_pand_geometrie_via_bbox(pand_id, vbo_feat, log):
    geom = vbo_feat.get("geometry", {})
    if not geom:
        return None
    coords = None
    if geom.get("type") == "Point":
        coords = geom["coordinates"]
    else:
        c = shape(geom).centroid
        coords = [c.x, c.y]
    x, y = coords[0], coords[1]
    if x <= 1000:
        t = make_transformer("EPSG:4326", "EPSG:28992")
        x, y = t.transform(x, y)
    marge = 100
    bbox_str = f"{x - marge},{y - marge},{x + marge},{y + marge}"
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "TYPENAME": "bag:pand", "outputFormat": "application/json",
        "BBOX": bbox_str, "count": 50,
    }
    resp = requests.get(BAG_WFS, params=params, timeout=30)
    if resp.status_code != 200:
        log(f"  WAARSCHUWING: pand bbox-query voor {pand_id} gaf status {resp.status_code}")
        return None
    features = resp.json().get("features", [])
    if not features:
        return None
    for feat in features:
        raw_id = str(feat.get("properties", {}).get("identificatie", ""))
        if raw_id == pand_id or raw_id.endswith(pand_id) or pand_id.endswith(raw_id):
            return feat
    return features[0]


def gevel_check(geluidgevoelig_features, circle_rd, log):
    log("  Gevel-check: pandgeometrie ophalen voor gefilterde verblijfsobjecten ...")
    pand_ids = haal_pand_ids(geluidgevoelig_features)
    log(f"  {len(pand_ids)} unieke panden te controleren.")

    pand_id_to_vbo = {}
    for feat in geluidgevoelig_features:
        props = feat.get("properties", {})
        pid = props.get("pandidentificatie") or props.get("maaktDeelUitVan")
        if not pid:
            continue
        pids = [pid] if not isinstance(pid, list) else pid
        for p in pids:
            if str(p) not in pand_id_to_vbo:
                pand_id_to_vbo[str(p)] = feat

    snijdende_pand_ids = set()
    pand_ids_lijst = list(pand_ids)
    totaal_panden = len(pand_ids_lijst)
    stap_pand = max(1, totaal_panden // 10)
    for i, pid in enumerate(pand_ids_lijst, 1):
        vbo_ref = pand_id_to_vbo.get(pid)
        pand_feat = haal_pand_geometrie_via_bbox(pid, vbo_ref, log) if vbo_ref else None
        if not pand_feat:
            continue
        geom_dict = pand_feat.get("geometry")
        if not geom_dict:
            log(f"  WAARSCHUWING: pand {pid} heeft geen geometrie in response.")
            continue
        if circle_rd.intersects(shape(geom_dict)):
            snijdende_pand_ids.add(pid)
        if totaal_panden > 10 and i % stap_pand == 0:
            log(f"  Gevel-check: {i}/{totaal_panden} ({round(i / totaal_panden * 100)}%) panden gecontroleerd ...")

    log(f"  {len(snijdende_pand_ids)} panden waarvan geometrie de straalcirkel snijdt.")

    resultaat = []
    for feat in geluidgevoelig_features:
        panden = feat.get("properties", {}).get("maaktDeelUitVan", [])
        if isinstance(panden, str):
            panden = [panden]
        feat["_gevel_snijdt"] = any(str(p) in snijdende_pand_ids for p in panden)
        resultaat.append(feat)
    return resultaat


# ──────────────────────────────────────────────
# Hulpfuncties — reverse geocoding
# ──────────────────────────────────────────────

def reverse_geocode(lat, lon, log):
    try:
        resp = requests.get(
            LOCATIESERVER_REVERSE,
            params={"lat": lat, "lon": lon, "type": "adres", "rows": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            docs = resp.json().get("response", {}).get("docs", [])
            if docs:
                doc = docs[0]
                weergave = doc.get("weergavenaam")
                if weergave:
                    return weergave
                straat = doc.get("straatnaam", "")
                huisnr = doc.get("huisnummer", "")
                pc = doc.get("postcode", "")
                wpl = doc.get("woonplaatsnaam", "")
                return f"{straat} {huisnr}, {pc} {wpl}".strip(", ")
    except Exception as e:
        log(f"  WAARSCHUWING: reverse geocode mislukt ({lat:.5f},{lon:.5f}): {e}")
    return ""


def reverse_geocode_adres_wpl(lat, lon, log):
    """Retourneert (adres_str, pc_wpl_str) via PDOK Locatieserver reverse geocode."""
    try:
        resp = requests.get(
            LOCATIESERVER_REVERSE,
            params={"lat": lat, "lon": lon, "type": "adres", "rows": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            docs = resp.json().get("response", {}).get("docs", [])
            if docs:
                doc    = docs[0]
                straat = doc.get("straatnaam", "")
                huisnr = doc.get("huisnummer", "")
                pc     = doc.get("postcode", "")
                wpl    = doc.get("woonplaatsnaam", "")
                adres  = f"{straat} {huisnr}".strip()
                pc_wpl = f"{pc}  {wpl}".strip()
                if not adres:
                    weergave = doc.get("weergavenaam", "")
                    if "," in weergave:
                        parts  = weergave.rsplit(",", 1)
                        adres  = parts[0].strip()
                        pc_wpl = pc_wpl or parts[1].strip()
                    else:
                        adres = weergave
                return adres or "—", pc_wpl
    except Exception as e:
        log(f"  WAARSCHUWING: reverse geocode mislukt ({lat:.5f},{lon:.5f}): {e}")
    return "—", ""


def _wpl_title(s):
    """Corrigeert UPPERCASE plaatsnamen (DUO) naar Titel-casing."""
    return " ".join(w.capitalize() for w in s.split()) if s else s


def _heeft_straatnaam(props):
    return bool(
        props.get("openbare_ruimte") or props.get("openbareruimtenaam")
        or props.get("openbareRuimteNaam") or props.get("straatnaam")
        or props.get("straatNaam") or props.get("naamOpenbareRuimte")
        or props.get("korteNaam")
    )


def vul_woonplaats_via_reverse_geocode(features, log):
    ontbrekend = [
        f for f in features
        if not (f.get("properties", {}).get("woonplaatsnaam")
                or f.get("properties", {}).get("woonplaatsNaam")
                or f.get("properties", {}).get("woonplaats"))
        or not _heeft_straatnaam(f.get("properties", {}))
    ]
    if not ontbrekend:
        log("  Adres reverse geocode: geen VBO's zonder woonplaats/straatnaam, overgeslagen.")
        return

    cache = {}
    gevuld_wpl = 0
    gevuld_straat = 0
    _rd_transformer = None

    for feat in ontbrekend:
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            lat, lon = extract_lon_lat(feat)
        except Exception:
            continue

        if lon > 1000:
            if _rd_transformer is None:
                _rd_transformer = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)
            wgs_lon, wgs_lat = _rd_transformer.transform(lon, lat)
            lat, lon = wgs_lat, wgs_lon

        cache_key = (round(lat, 7), round(lon, 7))
        if cache_key not in cache:
            try:
                resp = requests.get(
                    LOCATIESERVER_REVERSE,
                    params={"lat": lat, "lon": lon, "type": "adres", "rows": 1},
                    timeout=10,
                )
                gevonden = {"wpl": "", "straat": ""}
                if resp.status_code == 200:
                    docs = resp.json().get("response", {}).get("docs", [])
                    if docs:
                        doc = docs[0]
                        weergave = doc.get("weergavenaam", "")
                        gevonden["straat"] = doc.get("straatnaam", "")
                        if not gevonden["straat"] and weergave:
                            m_s = re.match(r'^(.*?)\s+\d', weergave)
                            if m_s:
                                gevonden["straat"] = m_s.group(1).strip()
                        gevonden["wpl"] = doc.get("woonplaatsnaam", "")
                        if not gevonden["wpl"] and weergave:
                            m_w = re.search(r"\d{4}[A-Z]{2}\s+(.+)$", weergave)
                            if m_w:
                                gevonden["wpl"] = m_w.group(1).strip()
                cache[cache_key] = gevonden
            except Exception:
                cache[cache_key] = {"wpl": "", "straat": ""}

        gevonden = cache[cache_key]
        props = feat["properties"]
        if gevonden["wpl"] and not (props.get("woonplaatsnaam") or props.get("woonplaatsNaam") or props.get("woonplaats")):
            props["woonplaatsnaam"] = gevonden["wpl"]
            gevuld_wpl += 1
        if gevonden["straat"] and not _heeft_straatnaam(props):
            props["openbareruimtenaam"] = gevonden["straat"]
            gevuld_straat += 1

    log(f"  Adres reverse geocode: {len(ontbrekend)} VBO's verwerkt via {len(cache)} unieke locaties "
        f"({gevuld_wpl} woonplaats, {gevuld_straat} straatnaam ingevuld).")


def _pdok_location_haal_polygoon(href, log):
    try:
        resp = requests.get(href, timeout=15)
        if resp.status_code != 200:
            log(f"  WAARSCHUWING: BRT polygoon-opvraging gaf status {resp.status_code} ({href})")
            return None
        return resp.json()
    except Exception as e:
        log(f"  WAARSCHUWING: BRT polygoon-opvraging mislukt: {e}")
        return None


def _geom_rings_wgs84(geom_wgs):
    if geom_wgs.geom_type == "Polygon":
        return [list(geom_wgs.exterior.coords)]
    elif geom_wgs.geom_type == "MultiPolygon":
        return [list(p.exterior.coords) for p in geom_wgs.geoms]
    return []


# ──────────────────────────────────────────────
# Begraafplaatsen — PDOK Location API (BRT)
# ──────────────────────────────────────────────

def signaleer_begraafplaatsen(circle_rd, straal, log):
    centrum = circle_rd.centroid
    circle_marge_rd = circle_rd.buffer(MARGE_M)
    circle_zoek_rd  = circle_rd.buffer(BEGRAAFPLAATS_ZOEK_MARGE)
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(circle_zoek_rd)
    bbox_str = f"{lon_min},{lat_min},{lon_max},{lat_max}"

    log(f"  Begraafplaatsen ophalen via PDOK Location API / BRT "
        f"(zoek-bbox = straal+{BEGRAAFPLAATS_ZOEK_MARGE} m = {straal + BEGRAAFPLAATS_ZOEK_MARGE:.0f} m; "
        f"intersectie-check op straal+{MARGE_M} m) ...")

    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")
    gevonden_ids = set()
    definitief = []
    buiten_straal = []

    for zoekterm in ("begraafplaats", "erebegraafplaats"):
        params = {"q": zoekterm, "functioneel_gebied[version]": "1", "bbox": bbox_str, "limit": 50}
        try:
            resp = requests.get(PDOK_LOCATION_API, params=params, timeout=15)
            if resp.status_code != 200:
                log(f"  WAARSCHUWING: PDOK Location API gaf status {resp.status_code} voor '{zoekterm}' — overgeslagen.")
                continue
            kandidaten = resp.json().get("features", [])
        except Exception as e:
            log(f"  WAARSCHUWING: PDOK Location API mislukt voor '{zoekterm}': {e}")
            continue

        log(f"  Zoekterm '{zoekterm}': {len(kandidaten)} kandidaten in bbox.")

        for feat in kandidaten:
            feat_id = feat.get("id", "")
            if feat_id in gevonden_ids:
                continue
            gevonden_ids.add(feat_id)

            props = feat.get("properties", {})
            naam  = props.get("display_name", "Onbekende begraafplaats")
            hrefs = props.get("href", [])
            if not hrefs:
                continue
            href = hrefs[0] if isinstance(hrefs, list) else hrefs

            poly_feat = _pdok_location_haal_polygoon(href, log)
            if not poly_feat:
                continue
            geom_dict = poly_feat.get("geometry")
            if not geom_dict:
                continue

            geom_wgs = shapely_from_geojson_geom(geom_dict)
            geom_rd  = transform_geom_to_rd(geom_wgs)
            rings    = _geom_rings_wgs84(geom_wgs)
            centroid_rd = geom_rd.centroid
            afstand  = centrum.distance(centroid_rd)
            lon_c, lat_c = t_to_wgs.transform(centroid_rd.x, centroid_rd.y)

            item = {"naam": naam, "afstand_m": round(afstand), "adres": "", "pc_wpl": "", "lat": lat_c, "lon": lon_c, "poly_rings": rings, "_geom_rd": geom_rd}

            if circle_rd.intersects(geom_rd):
                item["adres"], item["pc_wpl"] = reverse_geocode_adres_wpl(lat_c, lon_c, log)
                log(f"  Treffer: '{naam}' — polygoon snijdt toetsingsafstand {straal:.0f} m (centroid op {afstand:.0f} m).")
                definitief.append(item)
            else:
                log(f"  In bbox, buiten straal: '{naam}' (centroid op {afstand:.0f} m).")
                buiten_straal.append(item)

    log(f"\n  {len(definitief)} begraafplaats(en) snijden toetsingsafstand | {len(buiten_straal)} in bbox maar buiten straal.")
    return {"definitief": definitief, "signalen": [], "buiten_straal": buiten_straal}


# ──────────────────────────────────────────────
# Maneges — PDOK Location API (BRT gebouw-collectie)
# ──────────────────────────────────────────────

def haal_maneges_pdok(circle_rd, straal, log):
    centrum = circle_rd.centroid
    signaal_cirkel = circle_rd.buffer(MANEGE_SIGNAAL_MARGE)
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(signaal_cirkel)
    bbox_str = f"{lon_min},{lat_min},{lon_max},{lat_max}"
    aandacht_straal_tot = straal + MANEGE_SIGNAAL_MARGE

    log(f"  Maneges ophalen via PDOK Location API / BRT "
        f"(aandachtsgebied = straal + {MANEGE_SIGNAAL_MARGE} m = {aandacht_straal_tot:.0f} m) ...")

    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")
    gevonden_ids = set()
    in_straal = []
    buiten_straal = []

    for zoekterm in MANEGE_ZOEKTERMEN:
        params = {"q": zoekterm, "gebouw[version]": "1", "bbox": bbox_str, "limit": 50}
        try:
            resp = requests.get(PDOK_LOCATION_API, params=params, timeout=15)
            if resp.status_code != 200:
                log(f"  WAARSCHUWING: PDOK Location API gaf status {resp.status_code} voor '{zoekterm}' — overgeslagen.")
                continue
            kandidaten = resp.json().get("features", [])
        except Exception as e:
            log(f"  WAARSCHUWING: PDOK Location API mislukt voor '{zoekterm}': {e}")
            continue

        log(f"  Zoekterm '{zoekterm}': {len(kandidaten)} kandidaten in bbox.")

        for feat in kandidaten:
            feat_id = feat.get("id", "")
            if feat_id in gevonden_ids:
                continue
            gevonden_ids.add(feat_id)

            props = feat.get("properties", {})
            naam  = props.get("display_name", "Onbekende manege")
            hrefs = props.get("href", [])
            if not hrefs:
                continue
            href = hrefs[0] if isinstance(hrefs, list) else hrefs

            poly_feat = _pdok_location_haal_polygoon(href, log)
            if not poly_feat:
                continue
            geom_dict = poly_feat.get("geometry")
            if not geom_dict:
                continue

            geom_rd     = transform_geom_to_rd(shapely_from_geojson_geom(geom_dict))
            centroid_rd = geom_rd.centroid
            afstand     = centrum.distance(centroid_rd)
            lon_c, lat_c = t_to_wgs.transform(centroid_rd.x, centroid_rd.y)

            item = {"naam": naam, "afstand_m": round(afstand), "adres": "", "pc_wpl": "", "lat": lat_c, "lon": lon_c}

            if signaal_cirkel.intersects(geom_rd):
                item["adres"], item["pc_wpl"] = reverse_geocode_adres_wpl(lat_c, lon_c, log)
                log(f"  Treffer ('{zoekterm}'): '{naam}' — polygoon snijdt aandachtsgebied (centroid op {afstand:.0f} m).")
                in_straal.append(item)
            else:
                log(f"  In bbox, buiten aandachtsgebied ('{zoekterm}'): '{naam}' (centroid op {afstand:.0f} m).")
                buiten_straal.append(item)

    log(f"  {len(in_straal)} manege(s) binnen aandachtsgebied | {len(buiten_straal)} buiten aandachtsgebied.")
    return {"in_straal": in_straal, "buiten_straal": buiten_straal}


# ──────────────────────────────────────────────
# Natura 2000 — PDOK WFS (RVO), on-the-fly
# ──────────────────────────────────────────────

def signaleer_natura2000(circle_rd, straal, log):
    """Query Natura 2000-gebieden via PDOK WFS (geen lokale cache, on-the-fly BBOX-query).

    Check: puntlocatie (stijg-/landingsplaats), NIET de toetsingsstraalcirkel.
    Retourneert {'in_straal': [...], 'in_signaal': [...]}
      in_straal  — puntlocatie ligt BINNEN het N2000-gebied
      in_signaal — puntlocatie ligt BUITEN maar op < N2000_SIGNAAL_MARGE m van het N2000-gebied
    """
    punt_rd    = circle_rd.centroid                        # de eigenlijke puntlocatie
    signaal_rd = punt_rd.buffer(N2000_SIGNAAL_MARGE)      # zoekbbox = punt + signaalstraal
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(signaal_rd)
    # PDOK WFS 2.0 + EPSG:4326: axis-volgorde is lat-first (conform de CRS-definitie)
    bbox_str = f"{lat_min},{lon_min},{lat_max},{lon_max},EPSG:4326"

    log(f"  Natura 2000-gebieden ophalen via PDOK WFS "
        f"(puntlocatie ± {N2000_SIGNAAL_MARGE} m, bbox {lat_min:.4f},{lon_min:.4f},"
        f"{lat_max:.4f},{lon_max:.4f}) ...")

    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "TYPENAME":     N2000_WFS_LAYER,
        "outputFormat": "application/json",
        "srsName":      "EPSG:4326",
        "SRSNAME":      "urn:ogc:def:crs:EPSG::4326",
        "BBOX":         bbox_str,
    }
    try:
        resp = requests.get(N2000_WFS, params=params, timeout=30)
        if resp.status_code != 200:
            log(f"  WAARSCHUWING: N2000 WFS gaf status {resp.status_code} — overgeslagen.")
            return {"in_straal": [], "in_signaal": []}
        kandidaten = resp.json().get("features", [])
    except Exception as e:
        log(f"  WAARSCHUWING: N2000 WFS mislukt: {e}")
        return {"in_straal": [], "in_signaal": []}

    log(f"  N2000: {len(kandidaten)} kandidaat/kandidaten in bbox.")

    in_straal  = []
    in_signaal = []

    for feat in kandidaten:
        geom_dict = feat.get("geometry")
        if not geom_dict:
            continue
        try:
            geom_wgs = shapely_from_geojson_geom(geom_dict)
            geom_rd  = transform_geom_to_rd(geom_wgs)
        except Exception:
            continue
        props     = feat.get("properties", {})
        naam      = (props.get("naam") or props.get("NAAM") or props.get("NAME")
                     or props.get("naamN2K") or props.get("gebiedsnaam")
                     or "Onbekend N2000-gebied")
        afstand_p = punt_rd.distance(geom_rd)             # afstand punt → polygoonrand
        rings     = _geom_rings_wgs84(geom_wgs)
        item      = {"naam": naam, "afstand_m": round(afstand_p), "poly_rings": rings}

        if geom_rd.contains(punt_rd):
            log(f"  N2000 TREFFER: puntlocatie ligt BINNEN '{naam}'.")
            in_straal.append(item)
        elif afstand_p <= N2000_SIGNAAL_MARGE:
            log(f"  N2000 nabij: puntlocatie op {afstand_p:.0f} m van '{naam}' "
                f"(< {N2000_SIGNAAL_MARGE} m).")
            in_signaal.append(item)

    # Dedupliceer op naam: VR/HR/VR+HR zijn allen N2000 en hebben dezelfde gebiedsnaam.
    # Bij meerdere geometrieën voor dezelfde naam geldt: in_straal heeft voorrang.
    seen = set()
    in_straal_dedup = []
    for item in in_straal:
        if item["naam"] not in seen:
            seen.add(item["naam"])
            in_straal_dedup.append(item)
    in_straal = in_straal_dedup
    in_signaal = [item for item in in_signaal if item["naam"] not in seen]

    log(f"  N2000: puntlocatie binnen {len(in_straal)} gebied(en) | "
        f"{len(in_signaal)} nabij (< {N2000_SIGNAAL_MARGE} m).")
    return {"in_straal": in_straal, "in_signaal": in_signaal}


# ──────────────────────────────────────────────
# NNN — ATOM-download + lokale GeoPackage-cache
# ──────────────────────────────────────────────

def _nnn_gpkg_actueel():
    """True als de GeoPackage bestaat én jonger is dan NNN_TTL_DAGEN."""
    if not NNN_GPKG.exists():
        return False
    if NNN_META.exists():
        try:
            meta = json.loads(NNN_META.read_text(encoding="utf-8"))
            ts   = datetime.fromisoformat(meta.get("timestamp", ""))
            return (datetime.now() - ts).days < NNN_TTL_DAGEN
        except Exception:
            pass
    leeftijd = (datetime.now() - datetime.fromtimestamp(NNN_GPKG.stat().st_mtime)).days
    return leeftijd < NNN_TTL_DAGEN


def _nnn_download_gml(gml_pad, log):
    """Download NNN GML (~185 MB) met voortgangsindicator. Retourneert True bij succes."""
    log(f"  NNN: GML downloaden van PDOK (~185 MB) ...")
    try:
        resp  = requests.get(NNN_GML_URL, stream=True, timeout=300)
        resp.raise_for_status()
        totaal     = int(resp.headers.get("content-length", 0))
        ontvangen  = 0
        totaal_mb  = totaal / (1024 * 1024) if totaal else 0
        with open(gml_pad, "wb") as fout:
            for chunk in resp.iter_content(chunk_size=131072):
                if chunk:
                    fout.write(chunk)
                    ontvangen += len(chunk)
                    mb = ontvangen / (1024 * 1024)
                    if totaal:
                        pct = ontvangen / totaal * 100
                        print(f"\r  NNN downloaden: {mb:.1f} MB / {totaal_mb:.1f} MB "
                              f"({pct:.0f}%)   ", end="", flush=True)
                    else:
                        print(f"\r  NNN downloaden: {mb:.1f} MB ontvangen   ",
                              end="", flush=True)
        print()
        log(f"  NNN: GML opgeslagen ({ontvangen / (1024 * 1024):.1f} MB).")
        return True
    except Exception as e:
        print()
        log(f"  FOUT: NNN GML downloaden mislukt: {e}")
        return False


def _nnn_detecteer_naam_kolom(gdf, log):
    """Detecteer de gebiedsnaamkolom in de ingelezen NNN GeoDataFrame."""
    kandidaten = [
        "naam", "name", "NAAM", "NAME",
        "siteName", "SITENAAM", "localId", "localid",
        "siteDesignation", "inspireid_localid",
    ]
    for k in kandidaten:
        if k in gdf.columns:
            log(f"  NNN: naamkolom gevonden: '{k}'")
            return k
    # Fallback: eerste tekst-kolom
    for col in gdf.columns:
        if col.lower() != "geometry" and str(gdf[col].dtype) == "object":
            log(f"  NNN: naamkolom niet direct herkend, gebruik '{col}' als fallback.")
            return col
    log("  NNN WAARSCHUWING: geen naamkolom gevonden — gebieden worden genummerd.")
    return None


def _nnn_maak_gpkg(log):
    """Download NNN GML, converteer naar GeoPackage in EPSG:28992. True bij succes."""
    if not _HAS_GEOPANDAS:
        log("  FOUT: geopandas niet geïnstalleerd — NNN-verwerking overgeslagen. "
            "Installeer via: pip install geopandas")
        return False

    GEO_DIR.mkdir(parents=True, exist_ok=True)
    gml_pad = GEO_DIR / "_nnn_tmp.gml"

    if not _nnn_download_gml(gml_pad, log):
        return False

    # Laagnamen uitlezen (INSPIRE GML kan meerdere lagen bevatten)
    try:
        import fiona
        lagen = fiona.listlayers(str(gml_pad))
        log(f"  NNN GML: {len(lagen)} laag/lagen gevonden: {lagen}")
        laag = lagen[0] if lagen else None
    except Exception as e:
        log(f"  NNN: lagen uitlezen mislukt ({e}) — eerste laag wordt gebruikt.")
        laag = None

    log("  NNN: GML inlezen met geopandas (INSPIRE GML, kan 1–3 minuten duren) ...")
    try:
        kwargs = {"layer": laag} if laag else {}
        gdf = gpd.read_file(str(gml_pad), **kwargs)
        log(f"  NNN: {len(gdf)} gebieden ingelezen | CRS = {gdf.crs}.")
        log(f"  NNN: beschikbare kolommen: {list(gdf.columns)}")
    except Exception as e:
        log(f"  FOUT: NNN GML lezen mislukt: {e}")
        gml_pad.unlink(missing_ok=True)
        return False

    naam_col = _nnn_detecteer_naam_kolom(gdf, log)

    log("  NNN: coördinaten omzetten naar RD New (EPSG:28992) ...")
    try:
        gdf_rd = gdf.to_crs("EPSG:28992")
    except Exception as e:
        log(f"  FOUT: CRS-conversie EPSG:3035 → EPSG:28992 mislukt: {e}")
        gml_pad.unlink(missing_ok=True)
        return False

    if naam_col:
        gdf_out = gdf_rd[[naam_col, "geometry"]].copy()
        gdf_out = gdf_out.rename(columns={naam_col: "naam"})
    else:
        gdf_out = gdf_rd[["geometry"]].copy()
        gdf_out["naam"] = [f"NNN-gebied {i + 1}" for i in range(len(gdf_out))]
    gdf_out["naam"] = gdf_out["naam"].fillna("NNN-gebied (naamloos)").astype(str)

    log(f"  NNN: GeoPackage opslaan ({NNN_GPKG.name}) ...")
    try:
        gdf_out.to_file(str(NNN_GPKG), driver="GPKG", layer="nnn")
        kb = NNN_GPKG.stat().st_size // 1024
        log(f"  NNN: GeoPackage opgeslagen ({kb} KB, {len(gdf_out)} gebieden).")
    except Exception as e:
        log(f"  FOUT: NNN GeoPackage opslaan mislukt: {e}")
        gml_pad.unlink(missing_ok=True)
        return False

    # Tijdelijk GML verwijderen (185 MB terugwinnen)
    try:
        gml_pad.unlink()
        log("  NNN: tijdelijk GML-bestand verwijderd.")
    except Exception:
        pass

    NNN_META.write_text(
        json.dumps(
            {"timestamp": datetime.now().isoformat(), "n_gebieden": len(gdf_out)},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return True


def signaleer_nnn(circle_rd, straal, log):
    """Laad NNN GeoPackage (download + verwerk indien nodig) en check intersectie.

    Retourneert {'in_straal': [...], 'in_signaal': [...]}
      in_straal  — polygoon overlapt met toetsingsstraal
      in_signaal — polygoon overlapt met toetsingsstraal + NNN_SIGNAAL_MARGE,
                   maar raakt toetsingsstraal niet aan
    """
    if not _HAS_GEOPANDAS:
        log("  NNN: geopandas niet beschikbaar — overgeslagen. "
            "Installeer via: pip install geopandas")
        return {"in_straal": [], "in_signaal": []}

    log("  NNN: cache-status controleren ...")
    if not _nnn_gpkg_actueel():
        log(f"  NNN: GeoPackage ontbreekt of ouder dan {NNN_TTL_DAGEN} dagen — bijwerken ...")
        if not _nnn_maak_gpkg(log):
            log("  NNN: aanmaken mislukt — NNN-signalering overgeslagen.")
            return {"in_straal": [], "in_signaal": []}
    else:
        meta_str = ""
        if NNN_META.exists():
            try:
                meta = json.loads(NNN_META.read_text(encoding="utf-8"))
                ts   = datetime.fromisoformat(meta["timestamp"])
                meta_str = f", {meta.get('n_gebieden','?')} gebieden, aangemaakt {ts.strftime('%Y-%m-%d')}"
            except Exception:
                pass
        log(f"  NNN: GeoPackage actueel ({NNN_GPKG.name}{meta_str}).")

    punt_rd    = circle_rd.centroid                   # de eigenlijke puntlocatie
    signaal_rd = punt_rd.buffer(NNN_SIGNAAL_MARGE)   # zoekbbox = punt + signaalstraal
    minx, miny, maxx, maxy = signaal_rd.bounds
    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")

    log(f"  NNN: gebieden ophalen uit GeoPackage "
        f"(bbox = punt+{NNN_SIGNAAL_MARGE} m) ...")
    try:
        gdf = gpd.read_file(str(NNN_GPKG), layer="nnn", bbox=(minx, miny, maxx, maxy))
        log(f"  NNN: {len(gdf)} kandidaat/kandidaten in bbox.")
    except Exception as e:
        log(f"  FOUT: NNN GeoPackage lezen mislukt: {e}")
        return {"in_straal": [], "in_signaal": []}

    in_straal  = []
    in_signaal = []

    for _, rij in gdf.iterrows():
        geom_rd = rij.geometry
        if geom_rd is None or geom_rd.is_empty:
            continue
        naam      = str(rij.get("naam", "NNN-gebied"))
        afstand_p = punt_rd.distance(geom_rd)         # afstand punt → polygoonrand
        geom_wgs  = shapely_transform(lambda x, y: t_to_wgs.transform(x, y), geom_rd)
        rings     = _geom_rings_wgs84(geom_wgs)
        item      = {"naam": naam, "afstand_m": round(afstand_p), "poly_rings": rings}

        if geom_rd.contains(punt_rd):
            log(f"  NNN TREFFER: puntlocatie ligt BINNEN '{naam}'.")
            in_straal.append(item)
        elif afstand_p <= NNN_SIGNAAL_MARGE:
            log(f"  NNN nabij: puntlocatie op {afstand_p:.0f} m van '{naam}' "
                f"(< {NNN_SIGNAAL_MARGE} m).")
            in_signaal.append(item)

    log(f"  NNN: {len(in_straal)} puntlocatie binnen gebied | "
        f"{len(in_signaal)} nabij (< {NNN_SIGNAAL_MARGE} m).")
    return {"in_straal": in_straal, "in_signaal": in_signaal}


# ──────────────────────────────────────────────
# Luchthavens — GeoPortaal Overijssel WFS
# ──────────────────────────────────────────────

def signaleer_luchthavens(circle_rd, log):
    """Haalt luchthavenpuntlocaties op via GeoPortaal Overijssel WFS (on-the-fly, geen cache).

    Retourneert {'in_straal': [...], 'in_signaal': [...]}
      in_straal  — aanvraaglocatie binnen LUCHTHAVEN_GRENS_M (1.000 m) van luchthaven
                   → puntlocatie NIET toegestaan
      in_signaal — aanvraaglocatie op 1.000–2.000 m van luchthaven
                   → signalering
    """
    punt_rd  = circle_rd.centroid
    t_to_rd  = make_transformer("EPSG:4326", "EPSG:28992")

    log(f"  Luchthavens: WFS opvragen ({LUCHTHAVEN_WFS_LAYER}) ...")
    params = {
        "SERVICE":      "WFS",
        "VERSION":      "2.0.0",
        "REQUEST":      "GetFeature",
        "TYPENAMES":    LUCHTHAVEN_WFS_LAYER,
        "OUTPUTFORMAT": "application/json",
        "SRSNAME":      "EPSG:4326",
    }
    try:
        resp = requests.get(LUCHTHAVEN_WFS, params=params, timeout=30)
        resp.raise_for_status()
        fc = resp.json()
    except Exception as e:
        log(f"  FOUT: Luchthavens WFS ophalen mislukt: {e}")
        return {"in_straal": [], "in_signaal": []}

    features = fc.get("features", [])
    log(f"  Luchthavens: {len(features)} locaties ontvangen.")

    in_straal  = []
    in_signaal = []

    for feat in features:
        props = feat.get("properties", {}) or {}
        geom  = feat.get("geometry", {}) or {}
        if geom.get("type") != "Point":
            continue
        coords  = geom.get("coordinates", [])
        if len(coords) < 2:
            continue
        lon_lh, lat_lh = coords[0], coords[1]
        lh_x, lh_y    = t_to_rd.transform(lon_lh, lat_lh)
        lh_pt_rd       = Point(lh_x, lh_y)
        afstand        = round(punt_rd.distance(lh_pt_rd))

        naam         = props.get("NAAM") or "Onbekend"
        omschrijving = props.get("OMSCHRIJVING") or ""
        gebruik      = props.get("GEBRUIK") or ""
        beperking    = props.get("BEPERKING") or ""
        exploitant   = props.get("EXPLOITANT") or ""
        adres_lh     = props.get("ADRES") or ""

        item = {
            "naam":         naam,
            "omschrijving": omschrijving,
            "gebruik":      gebruik,
            "beperking":    beperking,
            "exploitant":   exploitant,
            "adres":        adres_lh,
            "afstand_m":    afstand,
            "lat":          lat_lh,
            "lon":          lon_lh,
        }

        if afstand <= LUCHTHAVEN_GRENS_M:
            log(f"  LUCHTHAVEN CONFLICT: '{naam}' op {afstand} m van aanvraaglocatie "
                f"(< {LUCHTHAVEN_GRENS_M} m — NIET TOEGESTAAN).")
            in_straal.append(item)
        elif afstand <= LUCHTHAVEN_SIGNAAL_M:
            log(f"  Luchthaven signalering: '{naam}' op {afstand} m van aanvraaglocatie "
                f"(< {LUCHTHAVEN_SIGNAAL_M} m).")
            in_signaal.append(item)
        else:
            log(f"  Luchthaven buiten signaalgebied: '{naam}' op {afstand} m.")

    log(f"  Luchthavens: {len(in_straal)} binnen {LUCHTHAVEN_GRENS_M} m | "
        f"{len(in_signaal)} binnen {LUCHTHAVEN_SIGNAAL_M} m.")
    return {"in_straal": in_straal, "in_signaal": in_signaal}


# ──────────────────────────────────────────────
# KDV — Landelijk Register Kinderopvang
# ──────────────────────────────────────────────

def _laad_lrk_csv(log):
    lrk_pad = GEO_DIR / "lrk_kinderopvang.csv"
    downloaden = True
    if lrk_pad.exists():
        leeftijd = (datetime.now() - datetime.fromtimestamp(lrk_pad.stat().st_mtime)).days
        if leeftijd < LRK_CACHE_DAYS:
            log(f"  LRK CSV: lokaal bestand gebruikt ({lrk_pad.name}, {leeftijd} dag(en) oud).")
            downloaden = False

    if downloaden:
        log(f"  LRK CSV: downloaden van {LRK_URL} ...")
        try:
            resp = requests.get(LRK_URL, stream=True, timeout=120)
            resp.raise_for_status()
            totaal = int(resp.headers.get("content-length", 0))
            ontvangen = 0
            with open(lrk_pad, "wb") as fout:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fout.write(chunk)
                        ontvangen += len(chunk)
                        if totaal:
                            print(f"\r  LRK downloaden: {ontvangen // 1024} KB / {totaal // 1024} KB ({ontvangen / totaal * 100:.0f}%)   ", end="", flush=True)
                        else:
                            print(f"\r  LRK downloaden: {ontvangen // 1024} KB ontvangen   ", end="", flush=True)
            print()
            log(f"  LRK CSV opgeslagen: {lrk_pad.name} ({ontvangen // 1024} KB).")
        except Exception as e:
            print()
            log(f"  FOUT: LRK CSV downloaden mislukt: {e}")
            return None

    for sep in (";", ","):
        for enc in ("utf-8", "latin-1"):
            try:
                df = pd.read_csv(lrk_pad, sep=sep, encoding=enc, dtype=str, low_memory=False)
                if len(df.columns) > 3:
                    log(f"  LRK CSV geladen: {len(df)} rijen, {len(df.columns)} kolommen (sep='{sep}', enc='{enc}').")
                    return df
            except Exception:
                continue

    log("  FOUT: LRK CSV kon niet worden geparsed.")
    return None


def haal_kdv_locaties(circle_rd, straal, log):
    circle_kdv_rd = circle_rd.buffer(KDV_BBOX_EXTRA)
    df = _laad_lrk_csv(log)
    if df is None:
        log("  WAARSCHUWING: LRK CSV niet beschikbaar — KDV-detectie overgeslagen.")
        return {"in_straal": [], "in_marge": []}

    kolom_map = {k.lower().replace(" ", "_").replace("-", "_"): k for k in df.columns}
    type_col = kolom_map.get("type_oko") or kolom_map.get("typeoko")
    bag_col  = kolom_map.get("bag_id")  or kolom_map.get("bagid")

    if not type_col or not bag_col:
        log(f"  WAARSCHUWING: LRK-kolommen 'type_oko' / 'bag_id' niet gevonden. Beschikbare kolommen: {list(df.columns[:15])}")
        return {"in_straal": [], "in_marge": []}

    kdv_df = df[df[type_col].str.strip().str.upper() == "KDV"]
    log(f"  LRK: {len(kdv_df)} KDV-locaties (van {len(df)} rijen totaal).")
    kdv_bag_ids = {str(v).strip() for v in kdv_df[bag_col].dropna() if str(v).strip()}
    log(f"  LRK: {len(kdv_bag_ids)} unieke BAG-IDs voor KDV-locaties.")

    if not kdv_bag_ids:
        log("  WAARSCHUWING: Geen BAG-IDs gevonden voor KDV — matching overgeslagen.")
        return {"in_straal": [], "in_marge": []}

    log(f"  BAG VBO's ophalen in bbox straal+{KDV_BBOX_EXTRA} m voor KDV-matching ...")
    vbo_features = haal_verblijfsobjecten(circle_kdv_rd, log)
    vbo_features = dedupliceer_vbo(vbo_features, log)
    totaal = len(vbo_features)
    log(f"  BAG/LRK matching: {totaal} VBO's controleren op {len(kdv_bag_ids)} KDV bag_ids ...")

    in_straal = []
    in_marge  = []

    for i, feat in enumerate(vbo_features, 1):
        props   = feat.get("properties", {})
        ident   = str(props.get("identificatie",   "")).strip()
        p_ident = str(props.get("pandidentificatie", "")).strip()
        if ident not in kdv_bag_ids and p_ident not in kdv_bag_ids:
            continue
        geom = feat.get("geometry")
        if not geom:
            continue
        geom_rd = transform_geom_to_rd(shapely_from_geojson_geom(geom))
        feat = copy.deepcopy(feat)
        feat["properties"]["_kdv"] = True
        if circle_rd.contains(geom_rd) or circle_rd.intersects(geom_rd):
            in_straal.append(feat)
        else:
            in_marge.append(feat)
        if totaal > 200 and i % 200 == 0:
            print(f"\r  KDV matching: {i}/{totaal} VBO's gecontroleerd ...  ", end="", flush=True)

    if totaal > 200:
        print()

    vul_woonplaats_via_reverse_geocode(in_straal, log)
    vul_woonplaats_via_reverse_geocode(in_marge,  log)
    log(f"  KDV: {len(in_straal)} locatie(s) binnen straal | {len(in_marge)} in margeband (+{KDV_BBOX_EXTRA} m).")
    return {"in_straal": in_straal, "in_marge": in_marge}


# ──────────────────────────────────────────────
# DUO Open Onderwijsdata — scholen preprocessing
# ──────────────────────────────────────────────

def _lees_scholen_meta():
    if SCHOLEN_META.exists():
        try:
            return json.loads(SCHOLEN_META.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _schrijf_scholen_meta(meta):
    SCHOLEN_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _duo_download_bytes(type_key, log):
    resource_id = DUO_DATASETS[type_key]["resource_id"]
    url = f"https://onderwijsdata.duo.nl/datastore/dump/{resource_id}?format=json"
    log(f"    DUO {type_key}: downloaden van {resource_id} ...")
    try:
        resp = requests.get(url, timeout=180)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        log(f"    FOUT: DUO {type_key} downloaden mislukt: {e}")
        return None


def _geocodeer_adres(straat, huisnr, postcode):
    query = f"{straat} {huisnr}, {postcode}".strip(", ").strip()
    try:
        resp = requests.get(
            LOCATIESERVER_FREE,
            params={"q": query, "fq": "type:adres", "fl": "id,adresseerbaarobject_id,centroide_ll", "rows": 1},
            timeout=15,
        )
        resp.raise_for_status()
        docs = resp.json().get("response", {}).get("docs", [])
        if not docs:
            return None, None, None, "geen_resultaat"
        doc = docs[0]
        vbo_id    = doc.get("adresseerbaarobject_id", "")
        centroide = doc.get("centroide_ll", "")
        m = re.match(r"POINT\(\s*([0-9.]+)\s+([0-9.]+)\s*\)", centroide)
        if not m:
            return None, None, vbo_id or None, "fout"
        return float(m.group(1)), float(m.group(2)), vbo_id, "ok"
    except Exception:
        return None, None, None, "fout"


def _geocodeer_groep(groep_sleutel, groep_keys, geojson_pad, log, meta, cached_bytes=None):
    features = []
    cb = cached_bytes or {}

    for type_key in groep_keys:
        ds        = DUO_DATASETS[type_key]
        naam_veld = ds["naam_veld"]
        raw = cb.get(type_key) or _duo_download_bytes(type_key, log)
        if raw is None:
            log(f"    DUO {type_key}: overgeslagen (download mislukt).")
            continue

        sha = hashlib.sha256(raw).hexdigest()
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            log(f"    DUO {type_key}: JSON-parse mislukt: {e}")
            continue

        velden  = [f["id"] for f in data.get("fields", [])]
        records = data.get("records", [])
        log(f"    DUO {type_key}: {len(records)} records geladen.")
        if velden:
            log(f"    DUO {type_key}: velden (eerste 8): {velden[:8]}")

        def veld(rec, naam):
            if isinstance(rec, dict):
                return str(rec.get(naam, "")).strip()
            try:
                idx = velden.index(naam)
                return str(rec[idx]).strip() if idx < len(rec) else ""
            except (ValueError, IndexError, TypeError):
                return ""

        rec_prov = [r for r in records if veld(r, "PROVINCIE").upper() == DUO_PROVINCIE.upper()]
        log(f"    DUO {type_key}: {len(rec_prov)} vestigingen in {DUO_PROVINCIE}.")

        fouten = 0
        type_features_start = len(features)

        for i, rec in enumerate(rec_prov, 1):
            pct = i / len(rec_prov) * 100
            print(f"\r    DUO {type_key}: {i}/{len(rec_prov)} ({pct:.0f}%) — {fouten} fout(en)   ", end="", flush=True)
            naam      = veld(rec, naam_veld)
            straat    = veld(rec, "STRAATNAAM")
            huisnr    = veld(rec, "HUISNUMMER-TOEVOEGING")
            postcode  = veld(rec, "POSTCODE")
            woonplaats = veld(rec, "PLAATSNAAM")
            adres     = f"{straat} {huisnr}".strip()
            if not straat or not postcode:
                fouten += 1
                continue
            lon, lat, vbo_id, status = _geocodeer_adres(straat, huisnr, postcode)
            if status != "ok":
                fouten += 1
            if lon is None:
                continue
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "naam": naam, "onderwijstype": type_key, "adres": adres,
                    "postcode": postcode, "woonplaats": woonplaats,
                    "vbo_id": vbo_id or "", "geocode_status": status,
                },
            })

        if rec_prov:
            print()

        n_type = len(features) - type_features_start
        log(f"    DUO {type_key}: {n_type} features geocodeerd; {fouten} fout(en).")
        meta[type_key] = {
            "hash": sha, "timestamp": datetime.now().isoformat(),
            "totaal": len(rec_prov), "geocodeer_fouten": fouten,
        }

    geojson = {"type": "FeatureCollection", "features": features}
    geojson_pad.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
    log(f"    GeoJSON opgeslagen: {geojson_pad.name} ({len(features)} features).")
    return features


def _verwerk_scholen_groep(groep_sleutel, groep_keys, geojson_pad, log):
    meta = _lees_scholen_meta()
    cached_bytes = {}
    moet_vernieuwen = not geojson_pad.exists()

    if not moet_vernieuwen:
        for type_key in groep_keys:
            type_meta = meta.get(type_key, {})
            ts_str = type_meta.get("timestamp", "")
            if not ts_str:
                moet_vernieuwen = True
                break
            try:
                ts = datetime.fromisoformat(ts_str)
                ouderdom = (datetime.now() - ts).days
                if ouderdom < DUO_TTL_DAGEN:
                    continue
                log(f"    DUO {type_key}: TTL verlopen ({ouderdom} dagen) → hash-check ...")
                raw = _duo_download_bytes(type_key, log)
                if raw is None:
                    log(f"    DUO {type_key}: download mislukt, bestaande GeoJSON behouden.")
                    continue
                cached_bytes[type_key] = raw
                sha = hashlib.sha256(raw).hexdigest()
                if sha != type_meta.get("hash", ""):
                    log(f"    DUO {type_key}: hash gewijzigd → {groep_sleutel} hergeocodeert.")
                    moet_vernieuwen = True
                    break
                else:
                    log(f"    DUO {type_key}: hash ongewijzigd. GeoJSON hergebruikt.")
                    meta[type_key]["timestamp"] = datetime.now().isoformat()
                    _schrijf_scholen_meta(meta)
            except Exception:
                moet_vernieuwen = True
                break

    if moet_vernieuwen:
        log(f"  DUO {groep_sleutel}: GeoJSON aanmaken / vernieuwen ...")
        GEO_DIR.mkdir(parents=True, exist_ok=True)
        _geocodeer_groep(groep_sleutel, groep_keys, geojson_pad, log, meta, cached_bytes)
        _schrijf_scholen_meta(meta)
    else:
        log(f"  DUO {groep_sleutel}: GeoJSON actueel, geen vernieuwing nodig.")

    if not geojson_pad.exists():
        log(f"  WAARSCHUWING: {geojson_pad.name} bestaat niet — schooldetectie voor {groep_sleutel} overgeslagen.")
        return []

    try:
        data = json.loads(geojson_pad.read_text(encoding="utf-8"))
        return data.get("features", [])
    except Exception as e:
        log(f"  FOUT: {geojson_pad.name} kon niet worden gelezen: {e}")
        return []


def _laad_scholen(log):
    log("  DUO PO-groep controleren ...")
    po_features = _verwerk_scholen_groep("PO", _DUO_PO_GROEP, SCHOLEN_GEOJSON_PO, log)
    log("  DUO overig-groep controleren ...")
    overig_features = _verwerk_scholen_groep("overig", _DUO_OVERIG_GROEP, SCHOLEN_GEOJSON_OVERIG, log)
    totaal = len(po_features) + len(overig_features)
    log(f"  DUO scholen geladen: {len(po_features)} PO + {len(overig_features)} overig = {totaal} totaal.")
    return po_features + overig_features


def haal_scholen(circle_rd, straal, log):
    alle_scholen = _laad_scholen(log)
    circle_marge_rd = circle_rd.buffer(MARGE_M)
    in_straal = []
    in_marge  = []

    for feat in alle_scholen:
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            pt_rd = transform_geom_to_rd(shapely_from_geojson_geom(geom))
        except Exception:
            continue
        if circle_rd.contains(pt_rd) or circle_rd.intersects(pt_rd):
            in_straal.append(feat)
        elif circle_marge_rd.contains(pt_rd) or circle_marge_rd.intersects(pt_rd):
            in_marge.append(feat)

    log(f"  DUO scholen: {len(in_straal)} binnen straal | {len(in_marge)} in marge (+{MARGE_M} m).")
    return {"in_straal": in_straal, "in_marge": in_marge}


# ──────────────────────────────────────────────
# Adres samenvoegen uit BAG properties
# ──────────────────────────────────────────────

def extract_adres(props):
    straat = (props.get("openbare_ruimte") or props.get("openbareruimtenaam")
              or props.get("openbareRuimteNaam") or props.get("straatnaam")
              or props.get("straatNaam") or props.get("naamOpenbareRuimte")
              or props.get("korteNaam") or "")
    huisnr = props.get("huisnummer") or ""
    toev   = props.get("huisletter") or ""
    toev2  = props.get("huisnummertoevoeging") or props.get("toevoeging") or ""
    pc     = props.get("postcode") or ""
    wpl    = (props.get("woonplaatsnaam") or props.get("woonplaatsNaam") or props.get("woonplaats") or "")
    adres  = f"{straat} {huisnr}{toev}{(' ' + toev2) if toev2 else ''}".strip()
    return adres, pc, wpl


def extract_lon_lat(feat):
    geom = feat.get("geometry", {})
    if geom.get("type") == "Point":
        lon, lat = geom["coordinates"][0], geom["coordinates"][1]
        return lat, lon
    geom_obj = shapely_from_geojson_geom(geom)
    c = geom_obj.centroid
    return c.y, c.x
