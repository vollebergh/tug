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

def _deelgebieden(geom: BaseGeometry) -> list:
    """Losse delen van een (multi)polygoon: bij ver uiteen liggende puntlocaties (B03)
    wordt per deel een eigen bbox opgevraagd i.p.v. één grote omhullende bbox."""
    return list(geom.geoms) if hasattr(geom, "geoms") else [geom]


def haal_verblijfsobjecten(circle_rd: BaseGeometry, log: LogFn) -> FeatureList:
    delen = _deelgebieden(circle_rd)
    if len(delen) > 1:
        # Dubbele features (overlap van bboxen) worden later door dedupliceer_vbo samengevoegd
        return [f for deel in delen for f in haal_verblijfsobjecten(deel, log)]
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
# BAG — gevelcontouren voor kaartweergave
# ──────────────────────────────────────────────

def haal_panden(circle_rd: BaseGeometry, log: LogFn) -> FeatureList:
    """Haal alle BAG-panden op binnen de bbox van circle_rd (WGS84-geometrie)."""
    delen = _deelgebieden(circle_rd)
    if len(delen) > 1:
        return [f for deel in delen for f in haal_panden(deel, log)]
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(circle_rd)
    bbox_str = f"{lat_min},{lon_min},{lat_max},{lon_max},EPSG:4326"
    panden = []
    start_index = 0
    while True:
        params = {
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "TYPENAME": "bag:pand", "outputFormat": "application/json",
            "srsName": "EPSG:4326", "SRSNAME": "urn:ogc:def:crs:EPSG::4326",
            "BBOX": bbox_str, "count": BAG_PAGE_SIZE, "startIndex": start_index,
            "propertyName": "bag:identificatie,bag:geom",
        }
        resp = requests.get(BAG_WFS, params=params, timeout=60)
        if resp.status_code != 200:
            log(f"  WAARSCHUWING: BAG WFS (pand) gaf statuscode {resp.status_code}; "
                f"kaart valt terug op puntweergave.")
            break
        batch = resp.json().get("features", [])
        panden.extend(batch)
        if len(batch) < BAG_PAGE_SIZE:
            break
        start_index += BAG_PAGE_SIZE
    log(f"  {len(panden)} panden opgehaald voor gevelcontouren.")
    return panden


def koppel_gevelcontouren(features: FeatureList, panden: FeatureList) -> int:
    """Zet per feature `_contour` (ringen [lon, lat]) en `_pand_id` van het pand waarin
    het adrespunt ligt. Koppeling op pandidentificatie, anders ruimtelijk (punt in pand).
    Features zonder pand behouden hun puntweergave. Retourneert het aantal gekoppelde.
    """
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    geoms, ids = [], []
    for p in panden:
        g = p.get("geometry")
        if not g:
            continue
        try:
            geoms.append(shape(g))
        except Exception:
            continue
        ids.append(str(p.get("properties", {}).get("identificatie", "")))
    if not geoms:
        return 0
    per_id = {pid: i for i, pid in enumerate(ids)}
    boom = STRtree(geoms)

    gekoppeld = 0
    for feat in features:
        props = feat.get("properties", {})
        idx = None
        pid = props.get("pandidentificatie") or props.get("maaktDeelUitVan")
        for p in ([pid] if isinstance(pid, str) else (pid or [])):
            if str(p) in per_id:
                idx = per_id[str(p)]
                break
        if idx is None:
            geom = feat.get("geometry") or {}
            if geom.get("type") != "Point":
                continue
            pt = Point(geom["coordinates"][0], geom["coordinates"][1])
            kandidaten = [i for i in boom.query(pt.buffer(1e-5)) if geoms[i].distance(pt) < 1e-5]
            if not kandidaten:
                continue
            idx = kandidaten[0]
        g = geoms[idx]
        polys = [g] if g.geom_type == "Polygon" else list(getattr(g, "geoms", []))
        feat["_contour"] = [[list(c)[:2] for c in poly.exterior.coords] for poly in polys]
        feat["_pand_id"] = ids[idx]
        gekoppeld += 1
    return gekoppeld


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
    """Haal pandgeometrie op voor het gebouw dat het VBO-punt bevat.

    Strategie — ruimtelijk in plaats van ID-gebaseerd:
    1. Bereken VBO-punt in RD.
    2. Vraag alle panden op binnen ±PAND_BBOX_ZOEK_MARGE (in RD, native CRS).
    3. Retourneer het pand waarvan de polygoon het VBO-punt bevat (containment).
       Dit is robuust tegen verouderde pandidentificaties in de BAG.
    4. Als geen pand het punt bevat: probeer strikte ID-match (pandidentificatie
       kan ook kloppen als het punt net op de grens ligt).
    5. Als ook dat niet lukt: retourneer None — nooit een willekeurig buurpand.
    """
    geom = vbo_feat.get("geometry", {})
    if not geom:
        return None
    if geom.get("type") == "Point":
        coords = geom["coordinates"]
    else:
        c = shape(geom).centroid
        coords = [c.x, c.y]
    x, y = coords[0], coords[1]
    if x <= 1000:          # WGS84 lon/lat → RD
        t = make_transformer("EPSG:4326", "EPSG:28992")
        x, y = t.transform(x, y)

    from shapely.geometry import Point as _Point
    vbo_pt_rd = _Point(x, y)

    bbox_str = (
        f"{x - PAND_BBOX_ZOEK_MARGE},{y - PAND_BBOX_ZOEK_MARGE},"
        f"{x + PAND_BBOX_ZOEK_MARGE},{y + PAND_BBOX_ZOEK_MARGE}"
    )
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "TYPENAME": "bag:pand", "outputFormat": "application/json",
        "BBOX": bbox_str, "count": 100,
    }
    resp = requests.get(BAG_WFS, params=params, timeout=30)
    if resp.status_code != 200:
        log(f"  WAARSCHUWING: pand bbox-query voor {pand_id} gaf status {resp.status_code}")
        return None
    features = resp.json().get("features", [])
    if not features:
        return None

    # Stap 3: zoek het pand dat het VBO-punt ruimtelijk bevat
    for feat in features:
        geom_dict = feat.get("geometry")
        if not geom_dict:
            continue
        try:
            pand_shp = shape(geom_dict)
            if pand_shp.contains(vbo_pt_rd) or pand_shp.distance(vbo_pt_rd) < 1.0:
                return feat
        except Exception:
            continue

    # Stap 4: geen containment — probeer strikte ID-match als vangnet
    for feat in features:
        raw_id = str(feat.get("properties", {}).get("identificatie", ""))
        if raw_id == pand_id or raw_id.endswith(pand_id) or pand_id.endswith(raw_id):
            return feat

    log(f"  WAARSCHUWING: geen pand gevonden dat VBO-punt bevat voor {pand_id}; overgeslagen.")
    return None


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
        props  = feat.get("properties", {})
        panden = (props.get("maaktDeelUitVan")
                  or props.get("pandidentificatie")
                  or [])
        if isinstance(panden, str):
            panden = [panden]
        feat["_gevel_snijdt"] = any(str(p) in snijdende_pand_ids for p in panden)
        resultaat.append(feat)
    return resultaat
