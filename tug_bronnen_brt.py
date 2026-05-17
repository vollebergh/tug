"""
tug_bronnen_brt.py -- PDOK Location API + Overijssel WFS-bronnen

Bevat signaleringsfuncties voor begraafplaatsen, maneges en luchthavens.
Begraafplaatsen en maneges worden opgespoord via de PDOK Locatieserver (BRT-dataset);
luchthavens via de WFS van GeoPortaal Overijssel.
"""

import requests
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from tug_config import (
    PDOK_LOCATION_API,
    MARGE_M, MANEGE_SIGNAAL_MARGE, BEGRAAFPLAATS_ZOEK_MARGE, MANEGE_ZOEKTERMEN,
    LUCHTHAVEN_WFS, LUCHTHAVEN_WFS_LAYER, LUCHTHAVEN_GRENS_M, LUCHTHAVEN_SIGNAAL_M,
)
from tug_geo import (
    circle_bbox_wgs84, make_transformer,
    shapely_from_geojson_geom, transform_geom_to_rd, _geom_rings_wgs84,
)
from tug_bronnen_geocode import reverse_geocode_adres_wpl, _pdok_location_haal_polygoon
from tug_types import LogFn, SignaalResultaat


# ──────────────────────────────────────────────
# Begraafplaatsen — PDOK Location API (BRT)
# ──────────────────────────────────────────────

def signaleer_begraafplaatsen(
    circle_rd: BaseGeometry, straal: float, log: LogFn
) -> SignaalResultaat:
    centrum = circle_rd.centroid
    circle_zoek_rd  = circle_rd.buffer(BEGRAAFPLAATS_ZOEK_MARGE)
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(circle_zoek_rd)
    bbox_str = f"{lon_min},{lat_min},{lon_max},{lat_max}"

    log(f"  Begraafplaatsen ophalen via PDOK Location API / BRT "
        f"(zoek-bbox = straal+{BEGRAAFPLAATS_ZOEK_MARGE} m = "
        f"{straal + BEGRAAFPLAATS_ZOEK_MARGE:.0f} m; "
        f"intersectie-check op toetsingsafstand {straal:.0f} m) ...")

    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")
    gevonden_ids = set()
    definitief = []
    buiten_straal = []

    for zoekterm in ("begraafplaats", "erebegraafplaats"):
        params = {"q": zoekterm, "gebouw[version]": "1", "bbox": bbox_str, "limit": 50}
        try:
            resp = requests.get(PDOK_LOCATION_API, params=params, timeout=15)
            if resp.status_code != 200:
                log(f"  WAARSCHUWING: PDOK Location API gaf status {resp.status_code} "
                    f"voor '{zoekterm}' — overgeslagen.")
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

            geom_rd     = transform_geom_to_rd(shapely_from_geojson_geom(geom_dict))
            centroid_rd = geom_rd.centroid
            afstand     = centrum.distance(centroid_rd)
            lon_c, lat_c = t_to_wgs.transform(centroid_rd.x, centroid_rd.y)
            rings        = _geom_rings_wgs84(shapely_from_geojson_geom(geom_dict))

            item = {
                "naam":       naam,
                "afstand_m":  round(afstand),
                "adres":      "", "pc_wpl": "",
                "lat":        lat_c, "lon": lon_c,
                "poly_rings": rings,
                "_geom_rd":   geom_rd,
            }

            if circle_rd.intersects(geom_rd):
                item["adres"], item["pc_wpl"] = reverse_geocode_adres_wpl(lat_c, lon_c, log)
                log(f"  Treffer: '{naam}' — polygoon snijdt toetsingsafstand "
                    f"{straal:.0f} m (centroid op {afstand:.0f} m).")
                definitief.append(item)
            else:
                log(f"  In bbox, buiten toetsingsafstand: '{naam}' "
                    f"(centroid op {afstand:.0f} m).")
                buiten_straal.append(item)

    log(f"\n  {len(definitief)} begraafplaats(en) snijden toetsingsafstand | "
        f"{len(buiten_straal)} in bbox maar buiten straal.")
    return {"definitief": definitief, "buiten_straal": buiten_straal}


# ──────────────────────────────────────────────
# Maneges — PDOK Location API (BRT)
# ──────────────────────────────────────────────

def haal_maneges_pdok(
    circle_rd: BaseGeometry, straal: float, log: LogFn
) -> SignaalResultaat:
    centrum = circle_rd.centroid
    signaal_cirkel = circle_rd.buffer(MANEGE_SIGNAAL_MARGE)
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(signaal_cirkel)
    bbox_str = f"{lon_min},{lat_min},{lon_max},{lat_max}"
    aandacht_straal_tot = straal + MANEGE_SIGNAAL_MARGE

    log(f"  Maneges ophalen via PDOK Location API / BRT "
        f"(aandachtsgebied = straal + {MANEGE_SIGNAAL_MARGE} m = "
        f"{aandacht_straal_tot:.0f} m) ...")

    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")
    gevonden_ids = set()
    in_straal = []
    buiten_straal = []

    for zoekterm in MANEGE_ZOEKTERMEN:
        params = {"q": zoekterm, "gebouw[version]": "1", "bbox": bbox_str, "limit": 50}
        try:
            resp = requests.get(PDOK_LOCATION_API, params=params, timeout=15)
            if resp.status_code != 200:
                log(f"  WAARSCHUWING: PDOK Location API gaf status {resp.status_code} "
                    f"voor '{zoekterm}' — overgeslagen.")
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

            item = {
                "naam":      naam,
                "afstand_m": round(afstand),
                "adres":     "", "pc_wpl": "",
                "lat":       lat_c, "lon": lon_c,
            }

            if signaal_cirkel.intersects(geom_rd):
                item["adres"], item["pc_wpl"] = reverse_geocode_adres_wpl(lat_c, lon_c, log)
                log(f"  Treffer ('{zoekterm}'): '{naam}' — polygoon snijdt aandachtsgebied "
                    f"(centroid op {afstand:.0f} m).")
                in_straal.append(item)
            else:
                log(f"  In bbox, buiten aandachtsgebied ('{zoekterm}'): '{naam}' "
                    f"(centroid op {afstand:.0f} m).")
                buiten_straal.append(item)

    log(f"  {len(in_straal)} manege(s) binnen aandachtsgebied | "
        f"{len(buiten_straal)} buiten aandachtsgebied.")
    return {"in_straal": in_straal, "buiten_straal": buiten_straal}


# ──────────────────────────────────────────────
# Luchthavens — GeoPortaal Overijssel WFS
# ──────────────────────────────────────────────

def signaleer_luchthavens(circle_rd: BaseGeometry, log: LogFn) -> SignaalResultaat:
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
        lh_x, lh_y     = t_to_rd.transform(lon_lh, lat_lh)
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
