"""
tug_bronnen_bag.py -- BAG WFS-bronnen (TUG-ontheffingen workflow)

Bevat alle functies voor het ophalen en filteren van BAG-verblijfsobjecten
en pandgeometrieën, inclusief gevel-check en geluidgevoeligheidsfilter.
"""

import copy

import requests
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from tug_config import (
    BAG_WFS, BAG_PAGE_SIZE, GELUIDGEVOELIGE_DOELEN,
    MARGE_M, PAND_BBOX_ZOEK_MARGE,
)
from tug_geo import (
    circle_bbox_wgs84, make_transformer,
    shapely_from_geojson_geom, transform_geom_to_rd,
)
from tug_types import Feature, FeatureList, LogFn


# ──────────────────────────────────────────────
# BAG — verblijfsobjecten ophalen
# ──────────────────────────────────────────────

def haal_verblijfsobjecten(circle_rd: BaseGeometry, log: LogFn) -> FeatureList:
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(circle_rd)
    bbox_str = f"{lat_min},{lon_min},{lat_max},{lon_max},EPSG:4326"
    log(f"  Ophalen verblijfsobjecten via BAG WFS v2.0 "
        f"(bbox {lat_min:.5f},{lon_min:.5f},{lat_max:.5f},{lon_max:.5f}) ...")

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
            log(f"  BAG verblijfsobject property-namen: "
                f"{list(batch[0].get('properties', {}).keys())}")
        features.extend(batch)
        if len(batch) < BAG_PAGE_SIZE:
            break
        start_index += BAG_PAGE_SIZE

    log(f"  {len(features)} verblijfsobjecten opgehaald binnen bbox.")
    return features


def filter_binnen_straal(
    features: FeatureList, circle_rd: BaseGeometry, log: LogFn
) -> FeatureList:
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


def filter_binnen_marge(
    features: FeatureList,
    circle_rd: BaseGeometry,
    circle_marge_rd: BaseGeometry,
    log: LogFn,
) -> FeatureList:
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


# ──────────────────────────────────────────────
# Deduplicatie en gebruiksdoelfilter
# ──────────────────────────────────────────────

def _parse_doelen(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        result = []
        for item in raw:
            result.extend(d.strip() for d in str(item).split(",") if d.strip())
        return result
    return [d.strip() for d in str(raw).split(",") if d.strip()]


def dedupliceer_vbo(features: FeatureList, log: LogFn) -> FeatureList:
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
        log(f"  Deduplicatie: {len(features)} → {len(resultaat)} unieke VBO's "
            f"({n_dupes} dubbele gebruiksdoel-regels samengevoegd).")
    else:
        log(f"  Deduplicatie: geen dubbele VBO's ({len(resultaat)} unieke VBO's).")
    return resultaat


def filter_geluidgevoelig(features: FeatureList, log: LogFn) -> FeatureList:
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
        pid = (feat.get("properties", {}).get("pandidentificatie")
               or feat.get("properties", {}).get("maaktDeelUitVan"))
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
    bbox_str = (
        f"{x - PAND_BBOX_ZOEK_MARGE},{y - PAND_BBOX_ZOEK_MARGE},"
        f"{x + PAND_BBOX_ZOEK_MARGE},{y + PAND_BBOX_ZOEK_MARGE}"
    )
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


def gevel_check(
    geluidgevoelig_features: FeatureList, circle_rd: BaseGeometry, log: LogFn
) -> FeatureList:
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
            log(f"  Gevel-check: {i}/{totaal_panden} "
                f"({round(i / totaal_panden * 100)}%) panden gecontroleerd ...")

    log(f"  {len(snijdende_pand_ids)} panden waarvan geometrie de straalcirkel snijdt.")

    resultaat = []
    for feat in geluidgevoelig_features:
        panden = feat.get("properties", {}).get("maaktDeelUitVan", [])
        if isinstance(panden, str):
            panden = [panden]
        feat["_gevel_snijdt"] = any(str(p) in snijdende_pand_ids for p in panden)
        resultaat.append(feat)
    return resultaat
