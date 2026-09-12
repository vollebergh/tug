"""
tug_03_ruimtelijk.py -- Stap 4 TUG-ontheffingen workflow (orchestrator)
Versie: zie git (workflowversie = korte commit-hash, zie tug_config.VERSION)

Invoer : tug_state.json  (classificatie.norm_toepassing + aanvraag.coord_lat/lon)
Uitvoer: tug_state.json  (sectie ruimtelijk gevuld; vier PNG-kaarten opgeslagen)

Zelfstandig gebruik (testmodus):
    python tug_03_ruimtelijk.py tug_state.json

De stap verloopt in vier fasen, elk met een eigen functie:
    _verzamel_bevindingen()      wat ligt er in de omgeving?      → Bevindingen
    _bouw_classificatie_context() wat betekent dat per object?    → VboOordeel
    _bouw_adresrijen() / _bouw_html_markers() / _render_kaarten()  presentatie
    _schrijf_state()             resultaat vastleggen

Het oordeel wordt in de tweede fase één keer geveld; alle drie de
presentatievormen lezen datzelfde oordeel, zodat de PDF-tabel, de HTML-kaart en
de PNG-kaart elkaar niet kunnen tegenspreken.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from shapely.geometry import MultiPoint
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform, unary_union

from tug_logging import LogAccumulator, setup_logging
from tug_config import (
    KAART_ACHTERGRONDEN, TOETSING_TOESLAG_M, VERSION, MODEL_LABEL, OUTPUT_DIR,
    MARGE_M, MANEGE_SIGNAAL_MARGE, KDV_BBOX_EXTRA,
    LUCHTHAVEN_SIGNAAL_M, LUCHTHAVEN_GRENS_M,
    _ZOOM_FILL_FRAC, _LOC_CX_FRAC, _LOC_CY_FRAC,
    _PDF_MARGIN_MM, _PDF_DPI, _PDF_PAGE_W_MM, _PDF_PAGE_H_MM,
)
from tug_geo import (
    puntlocaties, make_transformer, wgs84_to_rd, circle_in_rd, naam_slug,
    feature_sleutel, extract_adres, extract_lon_lat, _datum_leesbaar,
    _geom_rings_wgs84, transform_geom_to_rd, shapely_from_geojson_geom,
)
from tug_bronnen_bag import (
    haal_verblijfsobjecten, filter_binnen_straal, filter_binnen_marge,
    haal_panden, koppel_gevelcontouren, dedupliceer_vbo, filter_geluidgevoelig,
    gevel_check, _parse_doelen,
)
from tug_bronnen_geocode import vul_woonplaats_via_reverse_geocode, _wpl_title
from tug_bronnen_brt import (
    signaleer_begraafplaatsen, haal_maneges_pdok, signaleer_luchthavens,
)
from tug_bronnen_natuur import signaleer_natura2000, signaleer_nnn
from tug_bronnen_onderwijs import haal_kdv_locaties, haal_scholen
from tug_types import Bevindingen, ContextDict, FeatureList, LogFn, Signalering, VboOordeel
from tug_03_kaart import _render_kaart, _bereken_zoom

_logger = logging.getLogger("tug.03_ruimtelijk")


# ──────────────────────────────────────────────
# Gedeelde classificatie-context
# ──────────────────────────────────────────────

_KDV_LABEL = "kinderdagverblijf met bedverblijf (KDV)"

# Bijzonderheden-tekst per band
_EXTRA_STRAAL = "Binnen toetsingsafstand"
_EXTRA_MARGE  = "Marge"


def _school_label(props: dict) -> str:
    return f"school – {props.get('onderwijstype', '')} (DUO)"


def _sleutelmap(features: FeatureList, veld: str, label) -> dict[str, str]:
    """{BAG-sleutel: label} voor de features die aan een verblijfsobject hangen."""
    resultaat = {}
    for f in features or []:
        props = f.get("properties", {})
        sleutel = str(props.get(veld, "")).strip()
        if sleutel:
            resultaat[sleutel] = label(props) if callable(label) else label
    return resultaat


def _begraafplaats_uitsluitingen(bev: Bevindingen) -> set[str]:
    """Sleutels van verblijfsobjecten die fysiek binnen een begraafplaats liggen.

    Die adressen worden al gedekt door de begraafplaatsrij zelf; ze nog eens los
    opvoeren zou hetzelfde object twee keer tellen.
    """
    begr_geom_rd = [it["_geom_rd"] for it in bev.begraafplaatsen_in_straal if "_geom_rd" in it]
    if not begr_geom_rd:
        return set()

    uitgesloten = set()
    for feat in [*bev.alle_vbo, *bev.marge_vbo, *bev.marge_vbo_overig]:
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            pt_rd = transform_geom_to_rd(shapely_from_geojson_geom(geom))
        except (ValueError, TypeError, AttributeError) as fout:
            _logger.warning(
                f"Begraafplaatscheck overgeslagen voor {feature_sleutel(feat)}: {fout}"
            )
            continue
        if any(g.contains(pt_rd) for g in begr_geom_rd):
            uitgesloten.add(feature_sleutel(feat))
    return uitgesloten


def _beoordeel_band(
    features: FeatureList,
    geluidgevoelig_sleutels: set[str],
    uitgesloten: set[str],
    kdv_extra: dict[str, str],
    school_extra: dict[str, str],
    band: str,
    samengevoegd_kdv: set[str],
    samengevoegd_school: set[str],
) -> list[VboOordeel]:
    """Vel het oordeel over één band verblijfsobjecten.

    `band` is "straal" (binnen de toetsingsafstand), "marge" (geluidgevoelig in de
    margeband) of "marge_overig" (het overige in de margeband). De uitkomst is
    per feature één VboOordeel; wie dat oordeel daarna toont — tabel, HTML-kaart
    of PNG — leest alleen nog af.

    De sets `samengevoegd_*` worden gevuld met de sleutels waarvan het KDV- of
    schoollabel al in een adresrij is opgenomen, zodat de KDV- en schoollijsten
    die objecten niet nog een tweede keer opvoeren.
    """
    oordelen = []
    for feat in features or []:
        sleutel = feature_sleutel(feat)
        if sleutel in uitgesloten:
            continue

        props  = feat.get("properties", {})
        doelen = list(props.get("gebruiksdoelen") or _parse_doelen(props.get("gebruiksdoel")))
        if sleutel in kdv_extra:
            doelen.append(kdv_extra[sleutel])
            samengevoegd_kdv.add(sleutel)
        if sleutel in school_extra:
            doelen.append(school_extra[sleutel])
            samengevoegd_school.add(sleutel)
        heeft_kdv_of_school = sleutel in kdv_extra or sleutel in school_extra

        if band == "straal":
            wettelijk = sleutel in geluidgevoelig_sleutels or heeft_kdv_of_school
            categorie = "wettelijk" if wettelijk else "overig"
            extra     = _EXTRA_STRAAL if wettelijk else ""
        elif band == "marge":
            categorie = "marge"
            extra     = _EXTRA_MARGE
        else:  # marge_overig — alleen relevant als er een KDV of school aan hangt
            categorie = "marge" if heeft_kdv_of_school else "overig"
            extra     = _EXTRA_MARGE

        oordelen.append(VboOordeel(
            feature=feat,
            sleutel=sleutel,
            categorie=categorie,
            gebruiksdoel=", ".join(doelen) if doelen else "—",
            extra=extra,
        ))
    return oordelen


def _bouw_classificatie_context(bev: Bevindingen) -> ContextDict:
    """Vel het oordeel over elk verblijfsobject, één keer voor de hele stap.

    Dit is de enige plek waar wordt bepaald of een object wettelijk relevant is,
    in de margeband valt of alleen ter informatie wordt genoemd. De adreslijsten
    van het PDF-rapport, de markers van de HTML-kaart en de PNG-kaart lezen
    hetzelfde oordeel; ze kunnen daardoor niet uiteenlopen.
    """
    kdv_extra_straal    = _sleutelmap(bev.kdv_in_straal, "identificatie", _KDV_LABEL)
    kdv_extra_marge     = _sleutelmap(bev.kdv_in_marge, "identificatie", _KDV_LABEL)
    school_extra_straal = _sleutelmap(bev.scholen_in_straal, "vbo_id", _school_label)
    school_extra_marge  = _sleutelmap(bev.scholen_in_marge, "vbo_id", _school_label)

    geluidgevoelig_sleutels = {feature_sleutel(f) for f in bev.geluidgevoelig_vbo}
    uitgesloten             = _begraafplaats_uitsluitingen(bev)

    samengevoegd_kdv_straal    = set()
    samengevoegd_school_straal = set()
    samengevoegd_kdv_marge     = set()
    samengevoegd_school_marge  = set()

    oordelen = {
        "straal": _beoordeel_band(
            bev.alle_vbo, geluidgevoelig_sleutels, uitgesloten,
            kdv_extra_straal, school_extra_straal, "straal",
            samengevoegd_kdv_straal, samengevoegd_school_straal,
        ),
        "marge": _beoordeel_band(
            bev.marge_vbo, geluidgevoelig_sleutels, uitgesloten,
            kdv_extra_marge, school_extra_marge, "marge",
            samengevoegd_kdv_marge, samengevoegd_school_marge,
        ),
        "marge_overig": _beoordeel_band(
            bev.marge_vbo_overig, geluidgevoelig_sleutels, uitgesloten,
            kdv_extra_marge, school_extra_marge, "marge_overig",
            samengevoegd_kdv_marge, samengevoegd_school_marge,
        ),
    }

    return {
        "kdv_label":           _KDV_LABEL,
        "oordelen":            oordelen,
        "uitgesloten":         uitgesloten,
        "samengevoegd_kdv":    {"straal": samengevoegd_kdv_straal,
                                "marge":  samengevoegd_kdv_marge},
        "samengevoegd_school": {"straal": samengevoegd_school_straal,
                                "marge":  samengevoegd_school_marge},
    }


def _context_van(bev: Bevindingen, context: ContextDict | None) -> ContextDict:
    return context if context is not None else _bouw_classificatie_context(bev)


def _luchthaven_gebruiksdoel(item: Signalering) -> str:
    naam = item["naam"]
    return (f"luchthaven – {naam} ({item['omschrijving']})"
            if item.get("omschrijving") else f"luchthaven – {naam}")


def _luchthaven_extra() -> str:
    return f"Aanvraaglocatie + {LUCHTHAVEN_SIGNAAL_M:,} meter".replace(",", ".")


# ──────────────────────────────────────────────
# Adresrijen samenvoegen (voor state + PDF)
# ──────────────────────────────────────────────

def _bouw_adresrijen(
    bev: Bevindingen, context: ContextDict | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Bouw de vier adreslijsten als rij-dicts {adres, pc_wpl, gebruiksdoel, extra}.
    Retourneert (wettelijk, marge, aandacht, overig).
    Functies van hetzelfde VBO (BAG + KDV/DUO) zijn in gebruiksdoel samengevoegd.
    """
    context = _context_van(bev, context)
    oordelen = context["oordelen"]
    merged_kdv_straal    = context["samengevoegd_kdv"]["straal"]
    merged_kdv_marge     = context["samengevoegd_kdv"]["marge"]
    merged_school_straal = context["samengevoegd_school"]["straal"]
    merged_school_marge  = context["samengevoegd_school"]["marge"]

    wettelijk: list[dict] = []
    aandacht:  list[dict] = []
    overig:    list[dict] = []
    marge:     list[dict] = []
    lijsten = {"wettelijk": wettelijk, "marge": marge, "overig": overig}

    def _rij_uit_oordeel(oordeel: VboOordeel) -> None:
        adres, pc, wpl = extract_adres(oordeel.feature.get("properties", {}))
        lijsten[oordeel.categorie].append({
            "adres":        adres,
            "pc_wpl":       f"{pc}  {wpl}".strip(),
            "gebruiksdoel": oordeel.gebruiksdoel,
            "extra":        oordeel.extra,
        })

    for oordeel in oordelen["straal"]:
        _rij_uit_oordeel(oordeel)

    for item in bev.begraafplaatsen_in_straal:
        wettelijk.append({"adres": item["adres"] or "—", "pc_wpl": item.get("pc_wpl", ""),
                          "gebruiksdoel": f"begraafplaats – {item['naam']}",
                          "extra": _EXTRA_STRAAL})
    for item in bev.maneges_in_straal:
        aandacht.append({"adres": item["adres"] or "—", "pc_wpl": item.get("pc_wpl", ""),
                         "gebruiksdoel": f"manege – {item['naam']}",
                         "extra": f"Toetsingsafstand + {MANEGE_SIGNAAL_MARGE} meter"})
    for item in [*bev.luchthavens_in_straal, *bev.luchthavens_in_signaal]:
        aandacht.append({
            "adres":        item["adres"] or "—",
            "pc_wpl":       "",
            "gebruiksdoel": _luchthaven_gebruiksdoel(item),
            "extra":        _luchthaven_extra(),
        })
    for item in bev.begraafplaatsen_buiten_straal:
        overig.append({"adres": item["adres"] or "—", "pc_wpl": item.get("pc_wpl", ""),
                       "gebruiksdoel": f"begraafplaats – {item['naam']}",
                       "extra": f"{item['afstand_m']} m"})
    for item in bev.maneges_buiten_straal:
        overig.append({"adres": item["adres"] or "—", "pc_wpl": item.get("pc_wpl", ""),
                       "gebruiksdoel": f"manege – {item['naam']}",
                       "extra": f"{item['afstand_m']} m"})

    for oordeel in oordelen["marge_overig"]:
        _rij_uit_oordeel(oordeel)
    for oordeel in oordelen["marge"]:
        _rij_uit_oordeel(oordeel)

    for feat in bev.kdv_in_straal:
        if feature_sleutel(feat) in merged_kdv_straal:
            continue
        adres, pc, wpl = extract_adres(feat.get("properties", {}))
        wettelijk.append({"adres": adres, "pc_wpl": f"{pc}  {wpl}".strip(),
                          "gebruiksdoel": _KDV_LABEL, "extra": _EXTRA_STRAAL})

    for feat in bev.kdv_in_marge:
        if feature_sleutel(feat) in merged_kdv_marge:
            continue
        adres, pc, wpl = extract_adres(feat.get("properties", {}))
        marge.append({"adres": adres, "pc_wpl": f"{pc}  {wpl}".strip(),
                      "gebruiksdoel": _KDV_LABEL, "extra": _EXTRA_MARGE})

    for feat in bev.scholen_in_straal:
        props = feat.get("properties", {})
        if feature_sleutel(feat) in merged_school_straal:
            continue
        wpl = _wpl_title(props.get("woonplaats", ""))
        wettelijk.append({"adres": props.get("adres", ""),
                          "pc_wpl": f"{props.get('postcode', '')}  {wpl}".strip(),
                          "gebruiksdoel": _school_label(props), "extra": _EXTRA_STRAAL})

    for feat in bev.scholen_in_marge:
        props = feat.get("properties", {})
        if feature_sleutel(feat) in merged_school_marge:
            continue
        wpl = _wpl_title(props.get("woonplaats", ""))
        marge.append({"adres": props.get("adres", ""),
                      "pc_wpl": f"{props.get('postcode', '')}  {wpl}".strip(),
                      "gebruiksdoel": _school_label(props), "extra": _EXTRA_MARGE})

    for lst in (wettelijk, aandacht, overig, marge):
        lst.sort(key=lambda r: r["adres"].lower())

    return wettelijk, marge, aandacht, overig


# ──────────────────────────────────────────────
# HTML-kaart — markerdata en polygonen bouwen
# ──────────────────────────────────────────────

def _bouw_html_markers(
    bev: Bevindingen, context: ContextDict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Bouw markers- en polygonen-lijsten voor de interactieve HTML-kaartexport."""
    context = _context_van(bev, context)
    oordelen = context["oordelen"]
    merged_kdv_straal    = context["samengevoegd_kdv"]["straal"]
    merged_kdv_marge     = context["samengevoegd_kdv"]["marge"]
    merged_school_straal = context["samengevoegd_school"]["straal"]
    merged_school_marge  = context["samengevoegd_school"]["marge"]

    markers:   list[dict] = []
    polygonen: list[dict] = []

    def _vbo_marker(feat, categorie, gebruiksdoel_override=None, extra=""):
        props = feat.get("properties", {})
        adres, pc, wpl = extract_adres(props)
        doelen = props.get("gebruiksdoelen") or _parse_doelen(props.get("gebruiksdoel"))
        try:
            lat_f, lon_f = extract_lon_lat(feat)
        except (KeyError, IndexError, TypeError, ValueError) as fout:
            _logger.warning(f"Marker overgeslagen voor {adres or feature_sleutel(feat)}: {fout}")
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

    def _marker_uit_oordeel(oordeel: VboOordeel) -> None:
        m = _vbo_marker(oordeel.feature, oordeel.categorie,
                        gebruiksdoel_override=oordeel.gebruiksdoel, extra=oordeel.extra)
        if m:
            markers.append(m)

    for band in ("straal", "marge", "marge_overig"):
        for oordeel in oordelen[band]:
            _marker_uit_oordeel(oordeel)

    for feat in bev.kdv_in_straal:
        if feature_sleutel(feat) in merged_kdv_straal:
            continue
        m = _vbo_marker(feat, "wettelijk",
                        gebruiksdoel_override=_KDV_LABEL, extra=_EXTRA_STRAAL)
        if m:
            markers.append(m)

    for feat in bev.kdv_in_marge:
        if feature_sleutel(feat) in merged_kdv_marge:
            continue
        m = _vbo_marker(feat, "marge", gebruiksdoel_override=_KDV_LABEL, extra=_EXTRA_MARGE)
        if m:
            markers.append(m)

    for feat, categorie, extra, samengevoegd in (
        *((f, "wettelijk", _EXTRA_STRAAL, merged_school_straal) for f in bev.scholen_in_straal),
        *((f, "marge", _EXTRA_MARGE, merged_school_marge) for f in bev.scholen_in_marge),
    ):
        props = feat.get("properties", {})
        if feature_sleutel(feat) in samengevoegd:
            continue
        geom = feat.get("geometry", {})
        try:
            lon_f, lat_f = geom["coordinates"][0], geom["coordinates"][1]
        except (KeyError, IndexError, TypeError) as fout:
            _logger.warning(f"Schoolmarker zonder bruikbare geometrie overgeslagen: {fout}")
            continue
        wpl = _wpl_title(props.get("woonplaats", ""))
        markers.append({
            "lat": lat_f, "lon": lon_f, "categorie": categorie,
            "adres": props.get("adres", ""),
            "pc_wpl": f"{props.get('postcode','')}  {wpl}".strip(),
            "gebruiksdoel": _school_label(props),
            "extra": extra,
            "contour": feat.get("_contour"), "pand_id": feat.get("_pand_id"),
        })

    for item in bev.maneges_in_straal:
        markers.append({
            "lat": item["lat"], "lon": item["lon"], "categorie": "aandacht",
            "adres": item.get("adres") or item["naam"],
            "pc_wpl": item.get("pc_wpl", ""),
            "gebruiksdoel": f"manege – {item['naam']}",
            "extra": f"Toetsingsafstand + {MANEGE_SIGNAAL_MARGE} meter",
        })

    for item in bev.luchthavens_in_straal:
        markers.append({
            "lat": item["lat"], "lon": item["lon"], "categorie": "luchthaven",
            "adres": item["adres"] or item["naam"],
            "pc_wpl": "",
            "gebruiksdoel": f"luchthaven – {item['naam']}",
            "extra": (f"Aanvraaglocatie + {LUCHTHAVEN_SIGNAAL_M} meter "
                      f"(< {LUCHTHAVEN_GRENS_M} m — NIET TOEGESTAAN)"),
        })

    for item in bev.luchthavens_in_signaal:
        markers.append({
            "lat": item["lat"], "lon": item["lon"], "categorie": "luchthaven",
            "adres": item["adres"] or item["naam"],
            "pc_wpl": "",
            "gebruiksdoel": f"luchthaven – {item['naam']}",
            "extra": f"Aanvraaglocatie + {LUCHTHAVEN_SIGNAAL_M} meter",
        })

    for item in bev.maneges_buiten_straal:
        markers.append({
            "lat": item["lat"], "lon": item["lon"], "categorie": "overig",
            "adres": item.get("adres") or item["naam"],
            "pc_wpl": item.get("pc_wpl", ""),
            "gebruiksdoel": f"manege – {item['naam']}",
            "extra": f"{item['afstand_m']} m",
        })

    for item in bev.begraafplaatsen_in_straal:
        polygonen.append({
            "naam": item["naam"], "type": "begraafplaats", "in_straal": True,
            "rings": item.get("poly_rings", []),
            "lat": item["lat"], "lon": item["lon"],
        })

    for item in bev.begraafplaatsen_buiten_straal:
        polygonen.append({
            "naam": item["naam"], "type": "begraafplaats", "in_straal": False,
            "rings": item.get("poly_rings", []),
            "lat": item["lat"], "lon": item["lon"],
        })

    # NNN eerst toevoegen (Leaflet rendert later-toegevoegde lagen bovenop),
    # N2000 daarna zodat die dominant blijft.
    for gebieden, soort, in_straal in (
        (bev.nnn_in_straal, "nnn", True),
        (bev.nnn_in_signaal, "nnn", False),
        (bev.n2000_in_straal, "n2000", True),
        (bev.n2000_in_signaal, "n2000", False),
    ):
        for item in gebieden:
            polygonen.append({
                "naam": item["naam"], "type": soort, "in_straal": in_straal,
                "rings": item.get("poly_rings", []),
            })

    return markers, polygonen


# ──────────────────────────────────────────────
# Fase 1 — bronnen bevragen
# ──────────────────────────────────────────────

def _nnn_via_n2000(
    n2000_in_straal: list[Signalering], nnn_in_straal: list[Signalering],
) -> list[Signalering]:
    """Vul een ontbrekende NNN-treffer aan op grond van een N2000-treffer.

    Natura 2000-gebieden liggen per definitie binnen het Natuurnetwerk. Meldt de
    N2000-bron wel een treffer en de NNN-cache niet, dan is die cache verouderd
    en niet de werkelijkheid: de NNN-treffer wordt dan aangenomen.
    """
    if not n2000_in_straal or nnn_in_straal:
        return nnn_in_straal
    return [
        {"naam": f"(via N2000: {i['naam']})", "afstand_m": 0.0, "poly_rings": [],
         "punten_binnen": i.get("punten_binnen", [])}
        for i in n2000_in_straal
    ]


def _promoveer_gevels(
    marge_vbo: FeatureList, geluidgevoelig: FeatureList, alle_vbo: FeatureList,
    circle_rd: BaseGeometry, log: LogFn,
) -> FeatureList:
    """Promoveer margeband-VBO's waarvan de gevel de toetsingsafstand snijdt.

    Hun BAG-punt ligt buiten de toetsingsafstand, maar de gevel van het pand kan
    er nog wel in snijden; dan is het object wettelijk relevant. Retourneert de
    margeband zonder de gepromoveerde objecten; `geluidgevoelig` en `alle_vbo`
    worden ter plekke aangevuld, zodat adresrijen, kaart en HTML-markers de
    promoties meenemen.
    """
    if not marge_vbo:
        log("  Geen geluidgevoelige VBOs in margeband — gevel-check overgeslagen.")
        return marge_vbo

    gecontroleerd  = gevel_check(marge_vbo, circle_rd, log)
    promoties      = [f for f in gecontroleerd if f.get("_gevel_snijdt")]
    resterend      = [f for f in gecontroleerd if not f.get("_gevel_snijdt")]
    if not promoties:
        log("  Geen margeband-VBO heeft een gevel die de toetsingsafstand snijdt.")
        return resterend

    geluidgevoelig.extend(promoties)
    alle_vbo.extend(promoties)
    log(f"  {len(promoties)} margeband-VBO(s) gepromoveerd naar wettelijk relevant "
        f"(gevel binnen toetsingsafstand).")
    return resterend


def _verzamel_bevindingen(
    circle_rd: BaseGeometry, circle_marge_rd: BaseGeometry, punten_rd: MultiPoint,
    straal: float, log: LogFn,
) -> Bevindingen:
    """Bevraag alle bronnen en breng de uitkomsten samen in één Bevindingen-record."""
    log("\nStap 3: Verblijfsobjecten ophalen via BAG WFS v2.0 ...")
    alle_vbo_bbox = haal_verblijfsobjecten(circle_marge_rd, log)

    log("\nStap 4: Filteren op punten binnen straalcirkel ...")
    alle_vbo = filter_binnen_straal(alle_vbo_bbox, circle_rd, log)
    log(f"  Totaal features binnen straal: {len(alle_vbo)}")

    log("\nStap 4b: VBO's dedupliceren ...")
    alle_vbo = dedupliceer_vbo(alle_vbo, log)
    vul_woonplaats_via_reverse_geocode(alle_vbo, log)

    log(f"\nStap 4d: Marge-adressen filteren (band +{MARGE_M} m) ...")
    marge_vbo_raw = dedupliceer_vbo(
        filter_binnen_marge(alle_vbo_bbox, circle_rd, circle_marge_rd, log), log
    )
    marge_vbo = filter_geluidgevoelig(marge_vbo_raw, log)
    vul_woonplaats_via_reverse_geocode(marge_vbo, log)
    geluidgevoelige_marge = {feature_sleutel(f) for f in marge_vbo}
    marge_vbo_overig = [
        f for f in marge_vbo_raw if feature_sleutel(f) not in geluidgevoelige_marge
    ]
    log(f"  {len(marge_vbo_overig)} niet-geluidgevoelige VBO's in margeband (kaartweergave).")
    vul_woonplaats_via_reverse_geocode(marge_vbo_overig, log)

    log("\nStap 5: Filteren op geluidgevoelige gebruiksdoelen ...")
    geluidgevoelig = filter_geluidgevoelig(alle_vbo, log)

    log("\nStap 6: Gevel-check op margeband (gevel snijdt toetsingsafstand → wettelijk) ...")
    marge_vbo = _promoveer_gevels(marge_vbo, geluidgevoelig, alle_vbo, circle_rd, log)

    log("\nStap 7: Begraafplaatsen — PDOK Location API (BRT) ...")
    begraafplaatsen = signaleer_begraafplaatsen(circle_rd, straal, log, punten_rd=punten_rd)

    log(f"\nStap 8: KDV — LRK CSV + BAG-matching (bbox straal+{KDV_BBOX_EXTRA} m) ...")
    kdv = haal_kdv_locaties(circle_rd, log)

    log("\nStap 9: Scholen — DUO Open Onderwijsdata ...")
    scholen = haal_scholen(circle_rd, log)

    log("\nStap 10: Maneges — PDOK Location API (BRT) ...")
    maneges = haal_maneges_pdok(circle_rd, straal, log, punten_rd=punten_rd)

    log("\nStap 10b: Luchthavens — GeoPortaal Overijssel WFS (on-the-fly) ...")
    luchthavens = signaleer_luchthavens(circle_rd, log, punten_rd=punten_rd)

    log("\nStap 11: Natura 2000 — PDOK WFS (on-the-fly) ...")
    n2000 = signaleer_natura2000(circle_rd, log, punten_rd=punten_rd)

    log("\nStap 12: Natuurnetwerk Nederland — GeoPackage-cache (ATOM-bron) ...")
    nnn = signaleer_nnn(circle_rd, log, punten_rd=punten_rd)

    nnn_in_straal = _nnn_via_n2000(n2000["in_straal"], nnn["in_straal"])
    if nnn_in_straal is not nnn["in_straal"]:
        log("  NNN: puntlocatie valt binnen N2000-gebied — N2000 is subset van NNN, "
            "NNN-treffer aangenomen.")

    bev = Bevindingen(
        alle_vbo=alle_vbo,
        geluidgevoelig_vbo=geluidgevoelig,
        marge_vbo=marge_vbo,
        marge_vbo_overig=marge_vbo_overig,
        begraafplaatsen_in_straal=begraafplaatsen["definitief"],
        begraafplaatsen_buiten_straal=begraafplaatsen["buiten_straal"],
        maneges_in_straal=maneges["in_straal"],
        maneges_buiten_straal=maneges["buiten_straal"],
        kdv_in_straal=kdv["in_straal"],
        kdv_in_marge=kdv["in_marge"],
        scholen_in_straal=scholen["in_straal"],
        scholen_in_marge=scholen["in_marge"],
        n2000_in_straal=n2000["in_straal"],
        n2000_in_signaal=n2000["in_signaal"],
        nnn_in_straal=nnn_in_straal,
        nnn_in_signaal=nnn["in_signaal"],
        luchthavens_in_straal=luchthavens["in_straal"],
        luchthavens_in_signaal=luchthavens["in_signaal"],
    )

    log("\nStap 12b: Gevelcontouren — BAG-panden voor kaartweergave ...")
    panden         = haal_panden(circle_marge_rd, log)
    kaartobjecten  = bev.kaartobjecten()
    n_contour      = koppel_gevelcontouren(kaartobjecten, panden)
    log(f"  {n_contour}/{len(kaartobjecten)} kaartobjecten gekoppeld aan een gevelcontour "
        f"(overige als punt weergegeven).")

    return bev


# ──────────────────────────────────────────────
# Fase 3 — kaarten renderen
# ──────────────────────────────────────────────

# Satelliet eerst (primair, B04), daarna topografisch als aanvulling (B05)
_ACHTERGROND_VOLGORDE = ("satelliet", "topografisch")
_KAART_TITELS         = {"situatie": "Situatiekaart", "omgeving": "Omgevingskaart"}


def _kaartformaat() -> tuple[int, int]:
    """Pixelformaat van een kaart: de PDF-pagina liggend, op _PDF_DPI."""
    content_w_mm = _PDF_PAGE_W_MM - 2 * _PDF_MARGIN_MM
    content_h_mm = _PDF_PAGE_H_MM - 2 * _PDF_MARGIN_MM
    return (int(content_h_mm / 25.4 * _PDF_DPI), int(content_w_mm / 25.4 * _PDF_DPI))


def _render_kaarten(
    bev: Bevindingen, context: ContextDict, lon: float, lat: float,
    zooms: dict[str, int], straal: float,
    punten: list[tuple[float, float]], toetsing_rings: list, signaal_rings: list,
    naam_infix: str, timestamp: str, log: LogFn,
) -> list[dict[str, str]]:
    """Render de vier kaarten (twee uitsneden × twee achtergronden) en sla ze op."""
    map_w_px, map_h_px = _kaartformaat()
    kaarten_png = []
    for achtergrond in _ACHTERGROND_VOLGORDE:
        for soort, zoom in zooms.items():
            img = _render_kaart(
                lon, lat, zoom, map_w_px, map_h_px, straal,
                bev=bev, oordelen=context["oordelen"], toon_legenda=True, log=log,
                achtergrond=achtergrond, punten=punten,
                toetsing_rings=toetsing_rings, signaal_rings=signaal_rings,
            )
            pad = OUTPUT_DIR / f"tug_kaart_{soort}_{achtergrond}{naam_infix}_{timestamp}.png"
            img.rotate(90, expand=True).save(pad, format="PNG")
            titel = f"{_KAART_TITELS[soort]} — {KAART_ACHTERGRONDEN[achtergrond]['titel']}"
            kaarten_png.append({"titel": titel, "pad": str(pad)})
            log(f"  {titel} opgeslagen: {pad.name}")
    return kaarten_png


# ──────────────────────────────────────────────
# Hoofdfunctie — leest en schrijft tug_state.json
# ──────────────────────────────────────────────

def _straal_uit_state(state: dict, aanvraag: dict) -> tuple[float, float]:
    """Lden-afstand uit de classificatiestap plus de vaste toeslag."""
    classificatie = state.get("classificatie", {})
    straal = classificatie.get("norm_toepassing") or aanvraag.get("straal_override")
    if straal is None:
        _logger.error(
            "FOUT: straal niet beschikbaar "
            "(classificatie.norm_toepassing ontbreekt en geen straal_override in aanvraag)."
        )
        sys.exit(1)
    straal_lden = float(straal)
    return straal_lden, straal_lden + TOETSING_TOESLAG_M


def _log_kop(log: LogFn, state: dict, punten, straal_lden: float, straal: float,
             datum: str) -> None:
    """Openingsregels van het proceslogboek: invoer, versie en maatgevend toestel."""
    lv_resultaten = state.get("classificatie", {}).get("luchtvaartuigen", [])
    maatgevend    = [r["registratie"] for r in lv_resultaten if r.get("norm_m") == straal_lden]
    alle_lv       = [
        f"{r['registratie']} ({r['norm_m']} m)"
        if r.get("norm_m") is not None
        else f"{r['registratie']} (geen norm bepaald — buiten de toetsing)"
        for r in lv_resultaten
    ]

    log(f"{'=' * 60}")
    log("TUG-ontheffingen — Stap 4: Ruimtelijke analyse")
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
        if maatgevend:
            log(f"  Maatgevend: {', '.join(maatgevend)} → Lden-afstand {straal_lden:.0f} m")


def _zonder_geometrie(gebieden: list[Signalering], met_punten: bool = False) -> list[dict]:
    """Natuurgebieden zoals ze in de state worden vastgelegd: zonder polygonen."""
    rijen = []
    for item in gebieden:
        rij = {"naam": item["naam"], "afstand_m": item["afstand_m"]}
        if met_punten:
            rij["punten_binnen"] = item.get("punten_binnen", [])
        rijen.append(rij)
    return rijen


def run(state_pad: str | Path) -> None:
    state_pad = Path(state_pad)
    state     = json.loads(state_pad.read_text(encoding="utf-8"))
    aanvraag  = state["aanvraag"]

    setup_logging()

    try:
        punten = puntlocaties(aanvraag)
    except ValueError as e:
        _logger.error(f"FOUT: ongeldige puntlocatie(s): {e}")
        sys.exit(1)

    # Kaartcentrum = gemiddelde van de puntlocaties (bij één locatie: die locatie zelf)
    lat = sum(p[0] for p in punten) / len(punten)
    lon = sum(p[1] for p in punten) / len(punten)
    straal_lden, straal = _straal_uit_state(state, aanvraag)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    now       = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    datum     = _datum_leesbaar(now)

    log = LogAccumulator("tug.03_ruimtelijk")
    _log_kop(log, state, punten, straal_lden, straal, datum)

    log("\nStap 1: GPS-coördinaten (WGS84) omzetten naar RD ...")
    punten_rd_xy = [wgs84_to_rd(p_lon, p_lat) for p_lat, p_lon in punten]
    for i, (x, y) in enumerate(punten_rd_xy, 1):
        log(f"  Puntlocatie {i} — RD: x={x:.0f}, y={y:.0f}")
    punten_rd = MultiPoint(punten_rd_xy)

    log("\nStap 2: Cirkelbuffers aanmaken in RD ...")
    # Toetsingsgebied = vereniging van de cirkels rond alle puntlocaties (B03)
    circle_rd       = unary_union([circle_in_rd(x, y, straal) for x, y in punten_rd_xy])
    circle_marge_rd = unary_union([circle_in_rd(x, y, straal + MARGE_M) for x, y in punten_rd_xy])
    log(f"  Straalcirkel: r={straal:.0f} m  |  Margeband: r={straal + MARGE_M:.0f} m "
        f"(+{MARGE_M} m)"
        + (f"  |  {len(punten)} cirkels samengevoegd tot één toetsingsgebied"
           if len(punten) > 1 else ""))

    # ── Fase 1: wat ligt er in de omgeving? ──
    bev = _verzamel_bevindingen(circle_rd, circle_marge_rd, punten_rd, straal, log)

    # ── Fase 2: wat betekent dat per object? ──
    log("\nStap 13: Adresrijen samenvoegen ...")
    context = _bouw_classificatie_context(bev)
    wettelijk, marge_rijen, aandacht, overig = _bouw_adresrijen(bev, context=context)
    html_markers, html_polygonen = _bouw_html_markers(bev, context=context)

    # ── Fase 3: presentatie ──
    log("\nStap 14: Kaarten renderen en opslaan ...")
    signaal_straal     = straal + MANEGE_SIGNAAL_MARGE
    map_w_px, map_h_px = _kaartformaat()
    log(f"  Kaartresolutie: {map_w_px}×{map_h_px} px (landscape, {_PDF_DPI} dpi)")

    # Toetsings- en aandachtsgebied als omtrek (WGS84-ringen) voor kaart en HTML
    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")

    def _ringen(geom_rd):
        return [[list(c) for c in ring]
                for ring in _geom_rings_wgs84(shapely_transform(t_to_wgs.transform, geom_rd))]

    signaal_rd     = unary_union([circle_in_rd(x, y, signaal_straal) for x, y in punten_rd_xy])
    toetsing_rings = _ringen(circle_rd)
    signaal_rings  = _ringen(signaal_rd)

    # Zoom zo kiezen dat alle cirkels passen: straal + grootste afstand kaartcentrum → puntlocatie
    c_x, c_y  = wgs84_to_rd(lon, lat)
    spreiding = max(((x - c_x) ** 2 + (y - c_y) ** 2) ** 0.5 for x, y in punten_rd_xy)
    zooms = {
        "situatie": _bereken_zoom(lat, straal + spreiding, _ZOOM_FILL_FRAC, map_h_px),
        "omgeving": _bereken_zoom(lat, signaal_straal + spreiding, _ZOOM_FILL_FRAC, map_h_px),
    }
    log(
        f"  Situatiekaart: zoom {zooms['situatie']} (straal {straal:.0f} m) | "
        f"Omgevingskaart: zoom {zooms['omgeving']} (aandachtsgebied {signaal_straal:.0f} m) | "
        f"locatie {_LOC_CX_FRAC*100:.0f}%/{_LOC_CY_FRAC*100:.0f}% van canvas"
    )

    slug        = naam_slug(aanvraag.get("naam", ""))
    naam_infix  = f"_{slug}" if slug else ""
    kaarten_png = _render_kaarten(
        bev, context, lon, lat, zooms, straal,
        punten, toetsing_rings, signaal_rings, naam_infix, timestamp, log,
    )

    log(f"\n{'=' * 60}")
    log("Stap 4 voltooid.")
    log(
        f"  Wettelijk relevant: {len(wettelijk)} | Marge: {len(marge_rijen)} | "
        f"Aandacht: {len(aandacht)} | Overig: {len(overig)}"
    )
    log(f"{'=' * 60}")

    # ── Fase 4: resultaat vastleggen ──
    state["ruimtelijk"] = {
        "naam":               aanvraag.get("naam", ""),
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
        "n2000_in_straal":    _zonder_geometrie(bev.n2000_in_straal, met_punten=True),
        "n2000_in_signaal":   _zonder_geometrie(bev.n2000_in_signaal),
        "nnn_in_straal":      _zonder_geometrie(bev.nnn_in_straal, met_punten=True),
        "nnn_in_signaal":     _zonder_geometrie(bev.nnn_in_signaal),
        "luchthavens_in_straal":  [
            {"naam": i["naam"], "afstand_m": i["afstand_m"], "omschrijving": i["omschrijving"]}
            for i in bev.luchthavens_in_straal
        ],
        "luchthavens_in_signaal": [
            {"naam": i["naam"], "afstand_m": i["afstand_m"], "omschrijving": i["omschrijving"]}
            for i in bev.luchthavens_in_signaal
        ],
        "statistieken":       bev.statistieken(),
        "log_regels":         log.lines,
    }

    state.setdefault("logboek", []).append({
        "stap":      "03_ruimtelijk",
        "tijdstip":  datetime.now().isoformat(),
        "niveau":    "info",
        "bericht":   (f"Ruimtelijke analyse voltooid: {len(wettelijk)} "
                      f"wettelijk relevante adressen."),
    })

    state_pad.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    _logger.info(f"State geschreven naar {state_pad}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 2:
        setup_logging()
        _logger.error("Gebruik: python tug_03_ruimtelijk.py tug_state.json")
        sys.exit(1)
    run(sys.argv[1])
