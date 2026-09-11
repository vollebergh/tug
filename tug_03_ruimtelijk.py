"""
tug_03_ruimtelijk.py -- Stap 4 TUG-ontheffingen workflow (orchestrator)
Versie: 4.4.0  |  2026-05-04

Invoer : tug_state.json  (classificatie.norm_toepassing + aanvraag.coord_lat/lon)
Uitvoer: tug_state.json  (sectie ruimtelijk gevuld; twee PNG-kaarten opgeslagen)

Zelfstandig gebruik (testmodus):
    python tug_03_ruimtelijk.py tug_state.json

Module-afhankelijkheden:
    tug_03_bronnen  ← databronnen, geo-utilities, constanten
    tug_03_kaart    ← PIL-kaartrendering (importeert uit bronnen)
"""

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

from shapely.geometry import MultiPoint
from shapely.ops import transform as shapely_transform, unary_union

from tug_logging import LogAccumulator, setup_logging
from tug_config import KAART_ACHTERGRONDEN, TOETSING_TOESLAG_M
from tug_geo import puntlocaties, make_transformer
from tug_03_bronnen import (
    VERSION, MODEL_LABEL, OUTPUT_DIR, GEO_DIR,
    MARGE_M, MANEGE_SIGNAAL_MARGE, KDV_BBOX_EXTRA, LUCHTHAVEN_SIGNAAL_M,
    LUCHTHAVEN_GRENS_M,
    _datum_leesbaar, _ZOOM_FILL_FRAC, _LOC_CX_FRAC, _LOC_CY_FRAC,
    _PDF_MARGIN_MM, _PDF_DPI, _PDF_PAGE_W_MM, _PDF_PAGE_H_MM,
    wgs84_to_rd, circle_in_rd,
    haal_verblijfsobjecten, filter_binnen_straal, filter_binnen_marge,
    haal_panden, koppel_gevelcontouren,
    dedupliceer_vbo, filter_geluidgevoelig, vul_woonplaats_via_reverse_geocode,
    gevel_check, signaleer_begraafplaatsen, haal_maneges_pdok,
    signaleer_natura2000, signaleer_nnn, signaleer_luchthavens,
    haal_kdv_locaties, haal_scholen,
    extract_adres, extract_lon_lat, _parse_doelen, _wpl_title,
    _geom_rings_wgs84,
    transform_geom_to_rd, shapely_from_geojson_geom,
)
from tug_03_kaart import _render_kaart, _bereken_zoom


# ──────────────────────────────────────────────
# Hulpfuncties
# ──────────────────────────────────────────────

def _naam_slug(naam: str) -> str:
    """Zet een aanvraaknaam om naar een veilige bestandsnaamcomponent."""
    slug = re.sub(r"[^\w\-]", "_", naam.strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:40]


# ──────────────────────────────────────────────
# Gedeelde classificatie-context
# ──────────────────────────────────────────────

_KDV_LABEL = "kinderdagverblijf met bedverblijf (KDV)"


def _bouw_classificatie_context(
    alle_vbo: list, geluidgevoelig_vbo: list,
    begraafplaatsen_in_straal: list,
    marge_vbo: list | None = None, marge_vbo_overig: list | None = None,
    kdv_in_straal: list | None = None, kdv_in_marge: list | None = None,
    scholen_in_straal: list | None = None, scholen_in_marge: list | None = None,
    **_kw,
) -> dict:
    """Bouwt de gedeelde lookup-context die zowel `_bouw_adresrijen` als
    `_bouw_html_markers` nodig hebben. Voorkomt dat de twee functies dezelfde
    afgeleide dicts/sets parallel opbouwen (DRY).
    """
    kdv_extra_straal = {
        str(f.get("properties", {}).get("identificatie", "")).strip(): _KDV_LABEL
        for f in (kdv_in_straal or [])
        if str(f.get("properties", {}).get("identificatie", "")).strip()
    }
    kdv_extra_marge = {
        str(f.get("properties", {}).get("identificatie", "")).strip(): _KDV_LABEL
        for f in (kdv_in_marge or [])
        if str(f.get("properties", {}).get("identificatie", "")).strip()
    }
    school_extra_straal = {
        str(f.get("properties", {}).get("vbo_id", "")).strip():
            f"school – {f.get('properties', {}).get('onderwijstype', '')} (DUO)"
        for f in (scholen_in_straal or [])
        if str(f.get("properties", {}).get("vbo_id", "")).strip()
    }
    school_extra_marge = {
        str(f.get("properties", {}).get("vbo_id", "")).strip():
            f"school – {f.get('properties', {}).get('onderwijstype', '')} (DUO)"
        for f in (scholen_in_marge or [])
        if str(f.get("properties", {}).get("vbo_id", "")).strip()
    }

    geluidgevoelig_ids = {id(f) for f in geluidgevoelig_vbo}

    # BAG VBOs die fysiek binnen een begraafplaatspolygoon vallen overslaan
    # (de begraafplaats-rij dekt dat adres al).
    begr_geom_rd = [it["_geom_rd"] for it in begraafplaatsen_in_straal if "_geom_rd" in it]
    begr_exclude_ids = set()
    if begr_geom_rd:
        for feat in list(alle_vbo) + list(marge_vbo or []) + list(marge_vbo_overig or []):
            geom = feat.get("geometry")
            if not geom:
                continue
            try:
                pt_rd = transform_geom_to_rd(shapely_from_geojson_geom(geom))
                if any(g.contains(pt_rd) for g in begr_geom_rd):
                    begr_exclude_ids.add(id(feat))
            except Exception:
                pass

    return {
        "kdv_label":           _KDV_LABEL,
        "kdv_extra_straal":    kdv_extra_straal,
        "kdv_extra_marge":     kdv_extra_marge,
        "school_extra_straal": school_extra_straal,
        "school_extra_marge":  school_extra_marge,
        "geluidgevoelig_ids":  geluidgevoelig_ids,
        "begr_exclude_ids":    begr_exclude_ids,
    }


# ──────────────────────────────────────────────
# Adresrijen samenvoegen (voor state + PDF)
# ──────────────────────────────────────────────

def _bouw_adresrijen(
    alle_vbo, geluidgevoelig_vbo,
    begraafplaatsen_in_straal, begraafplaatsen_buiten_straal,
    maneges_in_straal, maneges_buiten_straal,
    marge_vbo=None, marge_vbo_overig=None,
    kdv_in_straal=None, kdv_in_marge=None,
    scholen_in_straal=None, scholen_in_marge=None,
    luchthavens_in_straal=None, luchthavens_in_signaal=None,
    context=None,
    **_kw,
):
    """Bouw de vier adreslijsten als rij-dicts {adres, pc_wpl, gebruiksdoel, extra}.
    Retourneert (wettelijk, marge, aandacht, overig).
    Functies van hetzelfde VBO (BAG + KDV/DUO) worden samengevoegd in gebruiksdoel.
    """
    if context is None:
        context = _bouw_classificatie_context(
            alle_vbo=alle_vbo, geluidgevoelig_vbo=geluidgevoelig_vbo,
            begraafplaatsen_in_straal=begraafplaatsen_in_straal,
            marge_vbo=marge_vbo, marge_vbo_overig=marge_vbo_overig,
            kdv_in_straal=kdv_in_straal, kdv_in_marge=kdv_in_marge,
            scholen_in_straal=scholen_in_straal, scholen_in_marge=scholen_in_marge,
        )

    _kdv_label          = context["kdv_label"]
    kdv_extra_straal    = context["kdv_extra_straal"]
    kdv_extra_marge     = context["kdv_extra_marge"]
    school_extra_straal = context["school_extra_straal"]
    school_extra_marge  = context["school_extra_marge"]
    geluidgevoelig_ids  = context["geluidgevoelig_ids"]
    begr_exclude_ids    = context["begr_exclude_ids"]

    merged_kdv_straal    = set()
    merged_school_straal = set()
    merged_kdv_marge     = set()
    merged_school_marge  = set()

    wettelijk = []
    aandacht  = []
    overig    = []
    marge     = []

    for feat in alle_vbo:
        if id(feat) in begr_exclude_ids:
            continue
        props  = feat.get("properties", {})
        adres, pc, wpl = extract_adres(props)
        ident  = str(props.get("identificatie", "")).strip()
        doelen = list(props.get("gebruiksdoelen") or _parse_doelen(props.get("gebruiksdoel")))
        if ident in kdv_extra_straal:
            doelen.append(kdv_extra_straal[ident])
            merged_kdv_straal.add(ident)
        if ident in school_extra_straal:
            doelen.append(school_extra_straal[ident])
            merged_school_straal.add(ident)
        doel_str      = ", ".join(doelen) if doelen else "—"
        pc_wpl        = f"{pc}  {wpl}".strip()
        has_kdv_school = ident in kdv_extra_straal or ident in school_extra_straal

        if id(feat) in geluidgevoelig_ids or has_kdv_school:
            wettelijk.append({"adres": adres, "pc_wpl": pc_wpl, "gebruiksdoel": doel_str,
                               "extra": "Binnen toetsingsafstand"})
        else:
            overig.append({"adres": adres, "pc_wpl": pc_wpl, "gebruiksdoel": doel_str, "extra": ""})

    for item in begraafplaatsen_in_straal:
        wettelijk.append({"adres": item["adres"] or "—", "pc_wpl": item.get("pc_wpl", ""),
                           "gebruiksdoel": f"begraafplaats – {item['naam']}",
                           "extra": "Binnen toetsingsafstand"})
    for item in maneges_in_straal:
        aandacht.append({"adres": item["adres"] or "—", "pc_wpl": item.get("pc_wpl", ""),
                          "gebruiksdoel": f"manege – {item['naam']}",
                          "extra": f"Toetsingsafstand + {MANEGE_SIGNAAL_MARGE} meter"})
    for item in (luchthavens_in_straal or []):
        gebruiksdoel = (
            f"luchthaven – {item['naam']} ({item['omschrijving']})"
            if item.get("omschrijving") else f"luchthaven – {item['naam']}"
        )
        aandacht.append({
            "adres":        item["adres"] or "—",
            "pc_wpl":       "",
            "gebruiksdoel": gebruiksdoel,
            "extra":        f"Aanvraaglocatie + {LUCHTHAVEN_SIGNAAL_M:,} meter".replace(",", "."),
        })
    for item in (luchthavens_in_signaal or []):
        gebruiksdoel = (
            f"luchthaven – {item['naam']} ({item['omschrijving']})"
            if item.get("omschrijving") else f"luchthaven – {item['naam']}"
        )
        aandacht.append({
            "adres":        item["adres"] or "—",
            "pc_wpl":       "",
            "gebruiksdoel": gebruiksdoel,
            "extra":        f"Aanvraaglocatie + {LUCHTHAVEN_SIGNAAL_M:,} meter".replace(",", "."),
        })
    for item in begraafplaatsen_buiten_straal:
        overig.append({"adres": item["adres"] or "—", "pc_wpl": item.get("pc_wpl", ""),
                        "gebruiksdoel": f"begraafplaats – {item['naam']}", "extra": f"{item['afstand_m']} m"})
    for item in maneges_buiten_straal:
        overig.append({"adres": item["adres"] or "—", "pc_wpl": item.get("pc_wpl", ""),
                        "gebruiksdoel": f"manege – {item['naam']}", "extra": f"{item['afstand_m']} m"})

    for feat in (marge_vbo_overig or []):
        if id(feat) in begr_exclude_ids:
            continue
        props  = feat.get("properties", {})
        adres, pc, wpl = extract_adres(props)
        ident  = str(props.get("identificatie", "")).strip()
        doelen = list(props.get("gebruiksdoelen") or _parse_doelen(props.get("gebruiksdoel")))
        if ident in kdv_extra_marge:
            doelen.append(kdv_extra_marge[ident])
            merged_kdv_marge.add(ident)
        if ident in school_extra_marge:
            doelen.append(school_extra_marge[ident])
            merged_school_marge.add(ident)
        has_kdv_school = ident in kdv_extra_marge or ident in school_extra_marge
        rij = {"adres": adres, "pc_wpl": f"{pc}  {wpl}".strip(),
               "gebruiksdoel": ", ".join(doelen) if doelen else "—", "extra": "Marge"}
        if has_kdv_school:
            marge.append(rij)
        else:
            overig.append(rij)

    for feat in (marge_vbo or []):
        if id(feat) in begr_exclude_ids:
            continue
        props  = feat.get("properties", {})
        adres, pc, wpl = extract_adres(props)
        ident  = str(props.get("identificatie", "")).strip()
        doelen = list(props.get("gebruiksdoelen") or _parse_doelen(props.get("gebruiksdoel")))
        if ident in kdv_extra_marge:
            doelen.append(kdv_extra_marge[ident])
            merged_kdv_marge.add(ident)
        if ident in school_extra_marge:
            doelen.append(school_extra_marge[ident])
            merged_school_marge.add(ident)
        marge.append({"adres": adres, "pc_wpl": f"{pc}  {wpl}".strip(),
                       "gebruiksdoel": ", ".join(doelen) if doelen else "—", "extra": "Marge"})

    for feat in (kdv_in_straal or []):
        ident = str(feat.get("properties", {}).get("identificatie", "")).strip()
        if ident in merged_kdv_straal:
            continue
        props = feat.get("properties", {})
        adres, pc, wpl = extract_adres(props)
        wettelijk.append({"adres": adres, "pc_wpl": f"{pc}  {wpl}".strip(),
                           "gebruiksdoel": _kdv_label, "extra": "Binnen toetsingsafstand"})

    for feat in (kdv_in_marge or []):
        ident = str(feat.get("properties", {}).get("identificatie", "")).strip()
        if ident in merged_kdv_marge:
            continue
        props = feat.get("properties", {})
        adres, pc, wpl = extract_adres(props)
        marge.append({"adres": adres, "pc_wpl": f"{pc}  {wpl}".strip(),
                       "gebruiksdoel": _kdv_label, "extra": "Marge"})

    for feat in (scholen_in_straal or []):
        props  = feat.get("properties", {})
        vbo_id = str(props.get("vbo_id", "")).strip()
        if vbo_id in merged_school_straal:
            continue
        wpl = _wpl_title(props.get("woonplaats", ""))
        wettelijk.append({"adres": props.get("adres", ""),
                           "pc_wpl": f"{props.get('postcode', '')}  {wpl}".strip(),
                           "gebruiksdoel": f"school – {props.get('onderwijstype', '')} (DUO)",
                           "extra": "Binnen toetsingsafstand"})

    for feat in (scholen_in_marge or []):
        props  = feat.get("properties", {})
        vbo_id = str(props.get("vbo_id", "")).strip()
        if vbo_id in merged_school_marge:
            continue
        wpl = _wpl_title(props.get("woonplaats", ""))
        marge.append({"adres": props.get("adres", ""),
                       "pc_wpl": f"{props.get('postcode', '')}  {wpl}".strip(),
                       "gebruiksdoel": f"school – {props.get('onderwijstype', '')} (DUO)",
                       "extra": "Marge"})

    for lst in (wettelijk, aandacht, overig, marge):
        lst.sort(key=lambda r: r["adres"].lower())

    return wettelijk, marge, aandacht, overig


# ──────────────────────────────────────────────
# HTML-kaart — markerdata en polygonen bouwen
# ──────────────────────────────────────────────

def _bouw_html_markers(
    alle_vbo, geluidgevoelig_vbo,
    begraafplaatsen_in_straal, begraafplaatsen_buiten_straal,
    maneges_in_straal, maneges_buiten_straal,
    marge_vbo=None, marge_vbo_overig=None,
    kdv_in_straal=None, kdv_in_marge=None,
    scholen_in_straal=None, scholen_in_marge=None,
    n2000_in_straal=None, n2000_in_signaal=None,
    nnn_in_straal=None, nnn_in_signaal=None,
    luchthavens_in_straal=None, luchthavens_in_signaal=None,
    context=None,
    **_kw,
):
    """Bouw markers- en polygonen-lijsten voor de interactieve HTML-kaartexport."""
    if context is None:
        context = _bouw_classificatie_context(
            alle_vbo=alle_vbo, geluidgevoelig_vbo=geluidgevoelig_vbo,
            begraafplaatsen_in_straal=begraafplaatsen_in_straal,
            marge_vbo=marge_vbo, marge_vbo_overig=marge_vbo_overig,
            kdv_in_straal=kdv_in_straal, kdv_in_marge=kdv_in_marge,
            scholen_in_straal=scholen_in_straal, scholen_in_marge=scholen_in_marge,
        )

    markers   = []
    polygonen = []

    _kdv_label          = context["kdv_label"]
    kdv_extra_straal    = context["kdv_extra_straal"]
    kdv_extra_marge     = context["kdv_extra_marge"]
    school_extra_straal = context["school_extra_straal"]
    school_extra_marge  = context["school_extra_marge"]
    geluidgevoelig_ids  = context["geluidgevoelig_ids"]
    begr_exclude_ids    = context["begr_exclude_ids"]

    merged_kdv_straal    = set()
    merged_school_straal = set()
    merged_kdv_marge     = set()
    merged_school_marge  = set()

    def _vbo_marker(feat, categorie, gebruiksdoel_override=None, extra=""):
        props = feat.get("properties", {})
        adres, pc, wpl = extract_adres(props)
        doelen = props.get("gebruiksdoelen") or _parse_doelen(props.get("gebruiksdoel"))
        try:
            lat_f, lon_f = extract_lon_lat(feat)
        except Exception:
            return None
        return {
            "lat": lat_f, "lon": lon_f,
            "categorie": categorie,
            "adres": adres,
            "pc_wpl": f"{pc}  {wpl}".strip(),
            "gebruiksdoel": gebruiksdoel_override or (", ".join(doelen) if doelen else "—"),
            "extra": extra,
            "contour": feat.get("_contour"), "pand_id": feat.get("_pand_id"),
        }

    for feat in alle_vbo:
        if id(feat) in begr_exclude_ids:
            continue
        props  = feat.get("properties", {})
        ident  = str(props.get("identificatie", "")).strip()
        doelen = list(props.get("gebruiksdoelen") or _parse_doelen(props.get("gebruiksdoel")))
        if ident in kdv_extra_straal:
            doelen.append(kdv_extra_straal[ident])
            merged_kdv_straal.add(ident)
        if ident in school_extra_straal:
            doelen.append(school_extra_straal[ident])
            merged_school_straal.add(ident)
        doel_override  = ", ".join(doelen) if doelen else "—"
        has_kdv_school = ident in kdv_extra_straal or ident in school_extra_straal
        if id(feat) in geluidgevoelig_ids or has_kdv_school:
            m = _vbo_marker(feat, "wettelijk",
                            gebruiksdoel_override=doel_override,
                            extra="Binnen toetsingsafstand")
        else:
            m = _vbo_marker(feat, "overig", gebruiksdoel_override=doel_override)
        if m:
            markers.append(m)

    for feat in (marge_vbo or []):
        if id(feat) in begr_exclude_ids:
            continue
        props  = feat.get("properties", {})
        ident  = str(props.get("identificatie", "")).strip()
        doelen = list(props.get("gebruiksdoelen") or _parse_doelen(props.get("gebruiksdoel")))
        if ident in kdv_extra_marge:
            doelen.append(kdv_extra_marge[ident])
            merged_kdv_marge.add(ident)
        if ident in school_extra_marge:
            doelen.append(school_extra_marge[ident])
            merged_school_marge.add(ident)
        m = _vbo_marker(feat, "marge",
                        gebruiksdoel_override=", ".join(doelen) if doelen else "—",
                        extra="Marge")
        if m:
            markers.append(m)

    for feat in (marge_vbo_overig or []):
        if id(feat) in begr_exclude_ids:
            continue
        props  = feat.get("properties", {})
        ident  = str(props.get("identificatie", "")).strip()
        doelen = list(props.get("gebruiksdoelen") or _parse_doelen(props.get("gebruiksdoel")))
        if ident in kdv_extra_marge:
            doelen.append(kdv_extra_marge[ident])
            merged_kdv_marge.add(ident)
        if ident in school_extra_marge:
            doelen.append(school_extra_marge[ident])
            merged_school_marge.add(ident)
        has_kdv_school = ident in kdv_extra_marge or ident in school_extra_marge
        m = _vbo_marker(feat, "marge" if has_kdv_school else "overig",
                        gebruiksdoel_override=", ".join(doelen) if doelen else "—",
                        extra="Marge")
        if m:
            markers.append(m)

    for feat in (kdv_in_straal or []):
        ident = str(feat.get("properties", {}).get("identificatie", "")).strip()
        if ident in merged_kdv_straal:
            continue
        m = _vbo_marker(feat, "wettelijk",
                        gebruiksdoel_override=_kdv_label,
                        extra="Binnen toetsingsafstand")
        if m:
            markers.append(m)

    for feat in (kdv_in_marge or []):
        ident = str(feat.get("properties", {}).get("identificatie", "")).strip()
        if ident in merged_kdv_marge:
            continue
        m = _vbo_marker(feat, "marge",
                        gebruiksdoel_override=_kdv_label,
                        extra="Marge")
        if m:
            markers.append(m)

    for feat in (scholen_in_straal or []):
        props  = feat.get("properties", {})
        vbo_id = str(props.get("vbo_id", "")).strip()
        if vbo_id in merged_school_straal:
            continue
        geom  = feat.get("geometry", {})
        try:
            lon_f, lat_f = geom["coordinates"][0], geom["coordinates"][1]
        except Exception:
            continue
        wpl = _wpl_title(props.get("woonplaats", ""))
        markers.append({
            "lat": lat_f, "lon": lon_f, "categorie": "wettelijk",
            "adres": props.get("adres", ""),
            "pc_wpl": f"{props.get('postcode','')}  {wpl}".strip(),
            "gebruiksdoel": f"school – {props.get('onderwijstype','')} (DUO)",
            "extra": "Binnen toetsingsafstand",
            "contour": feat.get("_contour"), "pand_id": feat.get("_pand_id"),
        })

    for feat in (scholen_in_marge or []):
        props  = feat.get("properties", {})
        vbo_id = str(props.get("vbo_id", "")).strip()
        if vbo_id in merged_school_marge:
            continue
        geom  = feat.get("geometry", {})
        try:
            lon_f, lat_f = geom["coordinates"][0], geom["coordinates"][1]
        except Exception:
            continue
        wpl = _wpl_title(props.get("woonplaats", ""))
        markers.append({
            "lat": lat_f, "lon": lon_f, "categorie": "marge",
            "adres": props.get("adres", ""),
            "pc_wpl": f"{props.get('postcode','')}  {wpl}".strip(),
            "gebruiksdoel": f"school – {props.get('onderwijstype','')} (DUO)",
            "extra": "Marge",
            "contour": feat.get("_contour"), "pand_id": feat.get("_pand_id"),
        })

    for item in maneges_in_straal:
        markers.append({
            "lat": item["lat"], "lon": item["lon"], "categorie": "aandacht",
            "adres": item.get("adres") or item["naam"],
            "pc_wpl": item.get("pc_wpl", ""),
            "gebruiksdoel": f"manege – {item['naam']}",
            "extra": f"Toetsingsafstand + {MANEGE_SIGNAAL_MARGE} meter",
        })

    for item in (luchthavens_in_straal or []):
        markers.append({
            "lat": item["lat"], "lon": item["lon"], "categorie": "luchthaven",
            "adres": item["adres"] or item["naam"],
            "pc_wpl": "",
            "gebruiksdoel": f"luchthaven – {item['naam']}",
            "extra": f"Aanvraaglocatie + {LUCHTHAVEN_SIGNAAL_M} meter (< {LUCHTHAVEN_GRENS_M} m — NIET TOEGESTAAN)",
        })

    for item in (luchthavens_in_signaal or []):
        markers.append({
            "lat": item["lat"], "lon": item["lon"], "categorie": "luchthaven",
            "adres": item["adres"] or item["naam"],
            "pc_wpl": "",
            "gebruiksdoel": f"luchthaven – {item['naam']}",
            "extra": f"Aanvraaglocatie + {LUCHTHAVEN_SIGNAAL_M} meter",
        })

    for item in maneges_buiten_straal:
        markers.append({
            "lat": item["lat"], "lon": item["lon"], "categorie": "overig",
            "adres": item.get("adres") or item["naam"],
            "pc_wpl": item.get("pc_wpl", ""),
            "gebruiksdoel": f"manege – {item['naam']}",
            "extra": f"{item['afstand_m']} m",
        })

    for item in begraafplaatsen_in_straal:
        polygonen.append({
            "naam": item["naam"], "type": "begraafplaats", "in_straal": True,
            "rings": item.get("poly_rings", []),
            "lat": item["lat"], "lon": item["lon"],
        })

    for item in begraafplaatsen_buiten_straal:
        polygonen.append({
            "naam": item["naam"], "type": "begraafplaats", "in_straal": False,
            "rings": item.get("poly_rings", []),
            "lat": item["lat"], "lon": item["lon"],
        })

    # NNN eerst toevoegen (Leaflet rendert later-toegevoegde lagen bovenop)
    for item in (nnn_in_straal or []):
        polygonen.append({
            "naam": item["naam"], "type": "nnn", "in_straal": True,
            "rings": item.get("poly_rings", []),
        })
    for item in (nnn_in_signaal or []):
        polygonen.append({
            "naam": item["naam"], "type": "nnn", "in_straal": False,
            "rings": item.get("poly_rings", []),
        })

    # N2000 daarna (rendert bovenop NNN zodat N2000 dominant blijft)
    for item in (n2000_in_straal or []):
        polygonen.append({
            "naam": item["naam"], "type": "n2000", "in_straal": True,
            "rings": item.get("poly_rings", []),
        })
    for item in (n2000_in_signaal or []):
        polygonen.append({
            "naam": item["naam"], "type": "n2000", "in_straal": False,
            "rings": item.get("poly_rings", []),
        })

    return markers, polygonen


# ──────────────────────────────────────────────
# Hoofdfunctie — leest en schrijft tug_state.json
# ──────────────────────────────────────────────

def run(state_pad: str | Path) -> None:
    state_pad = Path(state_pad)
    state     = json.loads(state_pad.read_text(encoding="utf-8"))
    aanvraag  = state["aanvraag"]

    setup_logging()
    _root_logger = logging.getLogger("tug.03_ruimtelijk")

    try:
        punten = puntlocaties(aanvraag)
    except ValueError as e:
        _root_logger.error(f"FOUT: ongeldige puntlocatie(s): {e}")
        sys.exit(1)
    # Kaartcentrum = gemiddelde van de puntlocaties (bij één locatie: die locatie zelf)
    lat = sum(p[0] for p in punten) / len(punten)
    lon = sum(p[1] for p in punten) / len(punten)

    classificatie = state.get("classificatie", {})
    straal = classificatie.get("norm_toepassing") or aanvraag.get("straal_override")
    if straal is None:
        _root_logger.error(
            "FOUT: straal niet beschikbaar "
            "(classificatie.norm_toepassing ontbreekt en geen straal_override in aanvraag)."
        )
        sys.exit(1)
    straal_lden = float(straal)
    straal      = straal_lden + TOETSING_TOESLAG_M

    # Maatgevend luchtvaartuig bepalen voor logboek
    lv_resultaten  = classificatie.get("luchtvaartuigen", [])
    maatgevend_lv  = [r["registratie"] for r in lv_resultaten if r.get("norm_m") == straal_lden]
    alle_lv        = [f"{r['registratie']} ({r.get('norm_m', '?')} m)" for r in lv_resultaten]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    now       = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    datum     = _datum_leesbaar(now)

    log = LogAccumulator("tug.03_ruimtelijk")

    log(f"{'=' * 60}")
    log(f"TUG-ontheffingen — Stap 4: Ruimtelijke analyse")
    log(f"## Gegenereerd: {datum}")
    log(f"## Workflowversie: {VERSION}")
    log(f"## Model: {MODEL_LABEL if MODEL_LABEL else 'geen taalmodel gebruikt'}")
    log(f"{'=' * 60}")
    for i, (p_lat, p_lon) in enumerate(punten, 1):
        log(f"Input: puntlocatie {i}: lat={p_lat:.6f}, lon={p_lon:.6f}")
    log(f"Input: {len(punten)} puntlocatie(s), straal={straal:.0f} m "
        f"(Lden-afstand {straal_lden:.0f} m + toeslag {TOETSING_TOESLAG_M} m)")
    if alle_lv:
        log(f"  Luchtvaartuigen: {', '.join(alle_lv)}")
        if maatgevend_lv:
            log(f"  Maatgevend: {', '.join(maatgevend_lv)} → Lden-afstand {straal_lden:.0f} m")

    log("\nStap 1: GPS-coördinaten (WGS84) omzetten naar RD ...")
    punten_rd_xy = [wgs84_to_rd(p_lon, p_lat) for p_lat, p_lon in punten]
    for i, (x, y) in enumerate(punten_rd_xy, 1):
        log(f"  Puntlocatie {i} — RD: x={x:.0f}, y={y:.0f}")
    punten_rd = MultiPoint(punten_rd_xy)

    log("\nStap 2: Cirkelbuffers aanmaken in RD ...")
    # Toetsingsgebied = vereniging van de cirkels rond alle puntlocaties (B03)
    circle_rd       = unary_union([circle_in_rd(x, y, straal) for x, y in punten_rd_xy])
    circle_marge_rd = unary_union([circle_in_rd(x, y, straal + MARGE_M) for x, y in punten_rd_xy])
    log(f"  Straalcirkel: r={straal:.0f} m  |  Margeband: r={straal + MARGE_M:.0f} m (+{MARGE_M} m)"
        + (f"  |  {len(punten)} cirkels samengevoegd tot één toetsingsgebied" if len(punten) > 1 else ""))

    log("\nStap 3: Verblijfsobjecten ophalen via BAG WFS v2.0 ...")
    alle_vbo_bbox = haal_verblijfsobjecten(circle_marge_rd, log)

    log("\nStap 4: Filteren op punten binnen straalcirkel ...")
    alle_vbo = filter_binnen_straal(alle_vbo_bbox, circle_rd, log)
    log(f"  Totaal features binnen straal: {len(alle_vbo)}")

    log("\nStap 4b: VBO's dedupliceren ...")
    alle_vbo = dedupliceer_vbo(alle_vbo, log)
    vul_woonplaats_via_reverse_geocode(alle_vbo, log)

    log(f"\nStap 4d: Marge-adressen filteren (band +{MARGE_M} m) ...")
    marge_vbo_raw  = filter_binnen_marge(alle_vbo_bbox, circle_rd, circle_marge_rd, log)
    marge_vbo_raw  = dedupliceer_vbo(marge_vbo_raw, log)
    marge_vbo      = filter_geluidgevoelig(marge_vbo_raw, log)
    vul_woonplaats_via_reverse_geocode(marge_vbo, log)
    geluidgevoelig_marge_ids = {id(f) for f in marge_vbo}
    marge_vbo_overig = [f for f in marge_vbo_raw if id(f) not in geluidgevoelig_marge_ids]
    log(f"  {len(marge_vbo_overig)} niet-geluidgevoelige VBO's in margeband (kaartweergave).")
    vul_woonplaats_via_reverse_geocode(marge_vbo_overig, log)

    log("\nStap 5: Filteren op geluidgevoelige gebruiksdoelen ...")
    geluidgevoelig = filter_geluidgevoelig(alle_vbo, log)

    log("\nStap 6: Gevel-check op margeband (gevel snijdt toetsingsafstand → wettelijk) ...")
    # Pas gevel_check toe op geluidgevoelige margeband-VBOs: hun BAG-punt ligt
    # buiten de toetsingsafstand, maar hun gevel kan er nog wel in snijden.
    # Promoveer die VBOs van marge naar wettelijk relevant.
    if marge_vbo:
        marge_vbo_gevel = gevel_check(marge_vbo, circle_rd, log)
        gevel_promoties = [f for f in marge_vbo_gevel if f.get("_gevel_snijdt")]
        marge_vbo       = [f for f in marge_vbo_gevel if not f.get("_gevel_snijdt")]
        if gevel_promoties:
            geluidgevoelig.extend(gevel_promoties)
            # Voeg ook toe aan alle_vbo zodat adresrijen, kaart en HTML-markers
            # de promoties meenemen — de adresrijen-loop itereert over alle_vbo,
            # niet over geluidgevoelig, en zou ze anders niet zien.
            alle_vbo.extend(gevel_promoties)
            log(
                f"  {len(gevel_promoties)} margeband-VBO(s) gepromoveerd naar wettelijk relevant "
                f"(gevel binnen toetsingsafstand)."
            )
        else:
            log("  Geen margeband-VBO heeft een gevel die de toetsingsafstand snijdt.")
    else:
        log("  Geen geluidgevoelige VBOs in margeband — gevel-check overgeslagen.")

    log("\nStap 7: Begraafplaatsen — PDOK Location API (BRT) ...")
    bgt_result = signaleer_begraafplaatsen(circle_rd, straal, log, punten_rd=punten_rd)
    begraafplaatsen_in_straal  = bgt_result["definitief"]
    begraafplaatsen_buiten_straal = bgt_result["buiten_straal"]

    log(f"\nStap 8: KDV — LRK CSV + BAG-matching (bbox straal+{KDV_BBOX_EXTRA} m) ...")
    kdv_result    = haal_kdv_locaties(circle_rd, straal, log)
    kdv_in_straal = kdv_result["in_straal"]
    kdv_in_marge  = kdv_result["in_marge"]

    log("\nStap 9: Scholen — DUO Open Onderwijsdata ...")
    scholen_result    = haal_scholen(circle_rd, straal, log)
    scholen_in_straal = scholen_result["in_straal"]
    scholen_in_marge  = scholen_result["in_marge"]

    log("\nStap 10: Maneges — PDOK Location API (BRT) ...")
    maneges_result     = haal_maneges_pdok(circle_rd, straal, log, punten_rd=punten_rd)
    maneges_in_straal  = maneges_result["in_straal"]
    maneges_buiten_straal = maneges_result["buiten_straal"]

    log("\nStap 10b: Luchthavens — GeoPortaal Overijssel WFS (on-the-fly) ...")
    luchthavens_result      = signaleer_luchthavens(circle_rd, log, punten_rd=punten_rd)
    luchthavens_in_straal   = luchthavens_result["in_straal"]
    luchthavens_in_signaal  = luchthavens_result["in_signaal"]

    log("\nStap 11: Natura 2000 — PDOK WFS (on-the-fly) ...")
    n2000_result     = signaleer_natura2000(circle_rd, straal, log, punten_rd=punten_rd)
    n2000_in_straal  = n2000_result["in_straal"]
    n2000_in_signaal = n2000_result["in_signaal"]

    log("\nStap 12: Natuurnetwerk Nederland — GeoPackage-cache (ATOM-bron) ...")
    nnn_result     = signaleer_nnn(circle_rd, straal, log, punten_rd=punten_rd)
    nnn_in_straal  = nnn_result["in_straal"]
    nnn_in_signaal = nnn_result["in_signaal"]

    # N2000 ⊂ NNN: alle N2000-gebieden zijn ook NNN. Als de puntlocatie binnen N2000
    # valt maar niet binnen NNN (bijv. doordat de GeoPackage-cache verouderd is),
    # dan nemen we NNN-treffer aan op basis van de N2000-hit.
    if n2000_in_straal and not nnn_in_straal:
        log("  NNN: puntlocatie valt binnen N2000-gebied — N2000 is subset van NNN, NNN-treffer aangenomen.")
        nnn_in_straal = [
            {"naam": f"(via N2000: {i['naam']})", "afstand_m": 0.0, "poly_rings": [],
             "punten_binnen": i.get("punten_binnen", [])}
            for i in n2000_in_straal
        ]

    log("\nStap 12b: Gevelcontouren — BAG-panden voor kaartweergave ...")
    panden = haal_panden(circle_marge_rd, log)
    kaart_objecten = [
        *alle_vbo, *marge_vbo, *marge_vbo_overig,
        *kdv_in_straal, *kdv_in_marge, *scholen_in_straal, *scholen_in_marge,
    ]
    n_contour = koppel_gevelcontouren(kaart_objecten, panden)
    log(f"  {n_contour}/{len(kaart_objecten)} kaartobjecten gekoppeld aan een gevelcontour "
        f"(overige als punt weergegeven).")

    log("\nStap 13: Adresrijen samenvoegen ...")
    _args_kaart = dict(
        alle_vbo=alle_vbo, geluidgevoelig_vbo=geluidgevoelig,
        begraafplaatsen_in_straal=begraafplaatsen_in_straal,
        begraafplaatsen_buiten_straal=begraafplaatsen_buiten_straal,
        maneges_in_straal=maneges_in_straal, maneges_buiten_straal=maneges_buiten_straal,
        marge_vbo=marge_vbo, marge_vbo_overig=marge_vbo_overig,
        kdv_in_straal=kdv_in_straal, kdv_in_marge=kdv_in_marge,
        scholen_in_straal=scholen_in_straal, scholen_in_marge=scholen_in_marge,
        n2000_in_straal=n2000_in_straal, n2000_in_signaal=n2000_in_signaal,
        nnn_in_straal=nnn_in_straal, nnn_in_signaal=nnn_in_signaal,
        luchthavens_in_straal=luchthavens_in_straal, luchthavens_in_signaal=luchthavens_in_signaal,
    )
    # Bouw de classificatie-context één keer en deel deze met beide presentatiefuncties
    classificatie_context = _bouw_classificatie_context(**_args_kaart)
    wettelijk, marge_rijen, aandacht, overig = _bouw_adresrijen(
        context=classificatie_context, **_args_kaart
    )
    html_markers, html_polygonen = _bouw_html_markers(
        context=classificatie_context, **_args_kaart
    )

    log("\nStap 14: Kaarten renderen en opslaan ...")
    signaal_straal = straal + MANEGE_SIGNAAL_MARGE
    content_w_mm = _PDF_PAGE_W_MM - 2 * _PDF_MARGIN_MM
    content_h_mm = _PDF_PAGE_H_MM - 2 * _PDF_MARGIN_MM
    map_w_px = int(content_h_mm / 25.4 * _PDF_DPI)
    map_h_px = int(content_w_mm / 25.4 * _PDF_DPI)
    log(f"  Kaartresolutie: {map_w_px}×{map_h_px} px (landscape, {_PDF_DPI} dpi)")

    render_kwargs = dict(
        alle_vbo=alle_vbo, geluidgevoelig_vbo=geluidgevoelig,
        begraafplaatsen_in_straal=begraafplaatsen_in_straal,
        begraafplaatsen_buiten_straal=begraafplaatsen_buiten_straal,
        maneges_in_straal=maneges_in_straal, maneges_buiten_straal=maneges_buiten_straal,
        toon_legenda=True, log=log,
        marge_vbo=marge_vbo, marge_vbo_overig=marge_vbo_overig,
        kdv_in_straal=kdv_in_straal, kdv_in_marge=kdv_in_marge,
        scholen_in_straal=scholen_in_straal, scholen_in_marge=scholen_in_marge,
        n2000_in_straal=n2000_in_straal, n2000_in_signaal=n2000_in_signaal,
        nnn_in_straal=nnn_in_straal, nnn_in_signaal=nnn_in_signaal,
        luchthavens_in_straal=luchthavens_in_straal, luchthavens_in_signaal=luchthavens_in_signaal,
    )

    # Toetsings- en aandachtsgebied als omtrek (WGS84-ringen) voor kaart en HTML
    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")
    def _ringen(geom_rd):
        return [[list(c) for c in ring]
                for ring in _geom_rings_wgs84(shapely_transform(t_to_wgs.transform, geom_rd))]
    signaal_rd     = unary_union([circle_in_rd(x, y, signaal_straal) for x, y in punten_rd_xy])
    toetsing_rings = _ringen(circle_rd)
    signaal_rings  = _ringen(signaal_rd)
    render_kwargs.update(punten=punten, toetsing_rings=toetsing_rings, signaal_rings=signaal_rings)

    # Zoom zo kiezen dat alle cirkels passen: straal + grootste afstand kaartcentrum → puntlocatie
    c_x, c_y  = wgs84_to_rd(lon, lat)
    spreiding = max(((x - c_x) ** 2 + (y - c_y) ** 2) ** 0.5 for x, y in punten_rd_xy)
    zoom1 = _bereken_zoom(lat, straal + spreiding, _ZOOM_FILL_FRAC, map_h_px)
    zoom2 = _bereken_zoom(lat, signaal_straal + spreiding, _ZOOM_FILL_FRAC, map_h_px)
    log(
        f"  Situatiekaart: zoom {zoom1} (straal {straal:.0f} m) | "
        f"Omgevingskaart: zoom {zoom2} (aandachtsgebied {signaal_straal:.0f} m) | "
        f"locatie {_LOC_CX_FRAC*100:.0f}%/{_LOC_CY_FRAC*100:.0f}% van canvas"
    )

    naam_slug  = _naam_slug(state.get("aanvraag", {}).get("naam", ""))
    naam_infix = f"_{naam_slug}" if naam_slug else ""
    # Satelliet eerst (primair, B04), daarna topografisch als aanvulling (B05)
    KAART_TITELS = {"situatie": "Situatiekaart", "omgeving": "Omgevingskaart"}
    kaarten_png = []
    for achtergrond in ("satelliet", "topografisch"):
        for soort, zoom in (("situatie", zoom1), ("omgeving", zoom2)):
            img = _render_kaart(lon, lat, zoom, map_w_px, map_h_px, straal, signaal_straal,
                                achtergrond=achtergrond, **render_kwargs)
            pad = OUTPUT_DIR / f"tug_kaart_{soort}_{achtergrond}{naam_infix}_{timestamp}.png"
            img.rotate(90, expand=True).save(pad, format="PNG")
            titel = f"{KAART_TITELS[soort]} — {KAART_ACHTERGRONDEN[achtergrond]['titel']}"
            kaarten_png.append({"titel": titel, "pad": str(pad)})
            log(f"  {titel} opgeslagen: {pad.name}")

    statistieken = {
        "vbo_in_straal":             len(alle_vbo),
        "geluidgevoelig":            len(geluidgevoelig),
        "marge_geluidgevoelig":      len(marge_vbo),
        "marge_overig":              len(marge_vbo_overig),
        "begraafplaatsen_in_straal": len(begraafplaatsen_in_straal),
        "maneges_in_straal":         len(maneges_in_straal),
        "kdv_in_straal":             len(kdv_in_straal),
        "kdv_in_marge":              len(kdv_in_marge),
        "scholen_in_straal":         len(scholen_in_straal),
        "scholen_in_marge":          len(scholen_in_marge),
        "n2000_in_straal":           len(n2000_in_straal),
        "n2000_in_signaal":          len(n2000_in_signaal),
        "nnn_in_straal":             len(nnn_in_straal),
        "nnn_in_signaal":            len(nnn_in_signaal),
        "luchthavens_in_straal":     len(luchthavens_in_straal),
        "luchthavens_in_signaal":    len(luchthavens_in_signaal),
    }

    log(f"\n{'=' * 60}")
    log("Stap 4 voltooid.")
    log(
        f"  Wettelijk relevant: {len(wettelijk)} | Marge: {len(marge_rijen)} | "
        f"Aandacht: {len(aandacht)} | Overig: {len(overig)}"
    )
    log(f"{'=' * 60}")

    state["ruimtelijk"] = {
        "naam":               state.get("aanvraag", {}).get("naam", ""),
        "straal":             straal,
        "straal_lden":        straal_lden,
        "punten":             [list(pt) for pt in punten],
        "toetsing_rings":     toetsing_rings,
        "signaal_rings":      signaal_rings,
        "workflow_versie":    VERSION,
        "lat":                lat,
        "lon":                lon,
        "timestamp":          timestamp,
        "datum_leesbaar":     datum,
        "kaarten_png":        kaarten_png,
        "adressen_wettelijk": wettelijk,
        "adressen_marge":     marge_rijen,
        "adressen_aandacht":  aandacht,
        "adressen_overig":    overig,
        "html_markers":       html_markers,
        "html_polygonen":     html_polygonen,
        "n2000_in_straal":    [{"naam": i["naam"], "afstand_m": i["afstand_m"],
                                "punten_binnen": i.get("punten_binnen", [])} for i in n2000_in_straal],
        "n2000_in_signaal":   [{"naam": i["naam"], "afstand_m": i["afstand_m"]} for i in n2000_in_signaal],
        "nnn_in_straal":      [{"naam": i["naam"], "afstand_m": i["afstand_m"],
                                "punten_binnen": i.get("punten_binnen", [])} for i in nnn_in_straal],
        "nnn_in_signaal":     [{"naam": i["naam"], "afstand_m": i["afstand_m"]} for i in nnn_in_signaal],
        "luchthavens_in_straal":  [
            {"naam": i["naam"], "afstand_m": i["afstand_m"], "omschrijving": i["omschrijving"]}
            for i in luchthavens_in_straal
        ],
        "luchthavens_in_signaal": [
            {"naam": i["naam"], "afstand_m": i["afstand_m"], "omschrijving": i["omschrijving"]}
            for i in luchthavens_in_signaal
        ],
        "statistieken":       statistieken,
        "log_regels":         log.lines,
    }

    state.setdefault("logboek", []).append({
        "stap":      "03_ruimtelijk",
        "tijdstip":  datetime.now().isoformat(),
        "niveau":    "info",
        "bericht":   f"Ruimtelijke analyse voltooid: {len(wettelijk)} wettelijk relevante adressen.",
    })

    state_pad.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.getLogger("tug.03_ruimtelijk").info(f"State geschreven naar {state_pad}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 2:
        setup_logging()
        logging.getLogger("tug.03_ruimtelijk").error(
            "Gebruik: python tug_03_ruimtelijk.py tug_state.json"
        )
        sys.exit(1)
    run(sys.argv[1])
