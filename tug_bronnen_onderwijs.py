"""
tug_bronnen_onderwijs.py -- Onderwijs- en kinderopvanglocaties

Bevat:
- Kinderdagverblijven (KDV) via koppeling Landelijk Register Kinderopvang (LRK CSV)
  aan BAG-verblijfsobjecten.
- Scholen via DUO Open Onderwijsdata (5 datasets: PO/SO/VO/MBO/HO), geocodeerd via
  PDOK Locatieserver en lokaal gecachet als GeoJSON.

Beide bronnen werken met een lokale kopie. Lukt het verversen niet, dan wordt de
oude kopie nog tot de noodterugvalgrens gebruikt — gemeld als verouderd — en
daarboven geldt de bron als niet geraadpleegd. Een half ververste kopie wordt
nooit weggeschreven: het GeoJSON-bestand wordt pas vervangen als alle datasets
van de groep volledig zijn opgehaald en gegeocodeerd.
"""

import copy
import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
from shapely.geometry.base import BaseGeometry

from tug_bronnen_bag import dedupliceer_vbo, haal_verblijfsobjecten
from tug_bronnen_geocode import vul_woonplaats_via_reverse_geocode
from tug_bronstatus import Bronregister
from tug_config import (
    _DUO_OVERIG_GROEP,
    _DUO_PO_GROEP,
    DUO_DATASETS,
    DUO_NOODTERUGVAL_MAX_DAGEN,
    DUO_PROVINCIE,
    DUO_TTL_DAGEN,
    GEO_DIR,
    KDV_BBOX_EXTRA,
    LOCATIESERVER_FREE,
    LRK_CACHE_DAYS,
    LRK_HEADERS,
    LRK_NOODTERUGVAL_MAX_DAGEN,
    LRK_URL,
    MARGE_M,
    MAX_BYTES_API,
    MAX_BYTES_DUO,
    MAX_BYTES_LRK,
    SCHOLEN_GEOJSON_OVERIG,
    SCHOLEN_GEOJSON_PO,
    SCHOLEN_META,
)
from tug_geo import shapely_from_geojson_geom, transform_geom_to_rd
from tug_http import BronFout, download_naar_bestand, haal, haal_json
from tug_logging import Voortgang
from tug_opslag import schrijf_prive
from tug_types import LogFn

_logger = logging.getLogger("tug.bronnen_onderwijs")


def _ouderdom_bestand(pad: Path) -> int:
    return (datetime.now() - datetime.fromtimestamp(pad.stat().st_mtime)).days


# ──────────────────────────────────────────────
# KDV — Landelijk Register Kinderopvang
# ──────────────────────────────────────────────

def _laad_lrk_csv(log: LogFn, bronnen: Bronregister) -> pd.DataFrame | None:
    lrk_pad = GEO_DIR / "lrk_kinderopvang.csv"
    ouderdom = _ouderdom_bestand(lrk_pad) if lrk_pad.exists() else None

    if ouderdom is not None and ouderdom < LRK_CACHE_DAYS:
        log(f"  LRK CSV: lokaal bestand gebruikt ({lrk_pad.name}, {ouderdom} dag(en) oud).")
        bronnen.cache("lrk", ouderdom)
    else:
        log(f"  LRK CSV: downloaden van {LRK_URL} ...")
        GEO_DIR.mkdir(parents=True, exist_ok=True)
        try:
            ontvangen = download_naar_bestand(
                LRK_URL, lrk_pad, headers=LRK_HEADERS, max_bytes=MAX_BYTES_LRK,
                timeout=(30, 120), label="LRK downloaden",
            )
        except BronFout as fout:
            if ouderdom is not None and ouderdom <= LRK_NOODTERUGVAL_MAX_DAGEN:
                log(f"  FOUT: LRK CSV downloaden mislukt ({fout}) — verouderde lokale kopie "
                    f"gebruikt ({ouderdom} dag(en) oud).")
                bronnen.verouderd("lrk", ouderdom, f"verversen mislukt: {fout}")
            else:
                log(f"  FOUT: LRK CSV downloaden mislukt ({fout}) — geen bruikbare lokale "
                    f"kopie; kinderopvang niet getoetst.")
                bronnen.mislukt("lrk", str(fout))
                return None
        else:
            log(f"  LRK CSV opgeslagen: {lrk_pad.name} ({ontvangen // 1024} KB).")
            bronnen.geraadpleegd("lrk")

    for sep in (";", ","):
        for enc in ("utf-8", "latin-1"):
            try:
                df = pd.read_csv(lrk_pad, sep=sep, encoding=enc, dtype=str, low_memory=False)
                if len(df.columns) > 3:
                    log(f"  LRK CSV geladen: {len(df)} rijen, {len(df.columns)} kolommen "
                        f"(sep='{sep}', enc='{enc}').")
                    return df
            except (ValueError, UnicodeDecodeError, pd.errors.ParserError) as fout:
                _logger.debug(f"LRK CSV niet leesbaar met sep='{sep}', enc='{enc}': {fout}")
                continue

    log("  FOUT: LRK CSV kon niet worden geparsed.")
    bronnen.mislukt("lrk", "CSV niet te lezen")
    return None


def haal_kdv_locaties(
    circle_rd: BaseGeometry, log: LogFn, *, bronnen: Bronregister,
) -> dict[str, list]:
    circle_kdv_rd = circle_rd.buffer(KDV_BBOX_EXTRA)
    df = _laad_lrk_csv(log, bronnen)
    if df is None:
        return {"in_straal": [], "in_marge": []}

    kolom_map = {k.lower().replace(" ", "_").replace("-", "_"): k for k in df.columns}
    type_col = kolom_map.get("type_oko") or kolom_map.get("typeoko")
    bag_col  = kolom_map.get("bag_id")  or kolom_map.get("bagid")

    if not type_col or not bag_col:
        log(f"  FOUT: LRK-kolommen 'type_oko' / 'bag_id' niet gevonden — kinderopvang niet "
            f"getoetst. Beschikbare kolommen: {list(df.columns[:15])}")
        bronnen.mislukt("lrk", "kolommen 'type_oko' / 'bag_id' niet gevonden")
        return {"in_straal": [], "in_marge": []}

    kdv_df = df[df[type_col].str.strip().str.upper() == "KDV"]
    log(f"  LRK: {len(kdv_df)} KDV-locaties (van {len(df)} rijen totaal).")
    kdv_bag_ids = {str(v).strip() for v in kdv_df[bag_col].dropna() if str(v).strip()}
    log(f"  LRK: {len(kdv_bag_ids)} unieke BAG-IDs voor KDV-locaties.")

    if not kdv_bag_ids:
        # Het landelijke register bevat duizenden KDV's; nul betekent een gewijzigd
        # bestandsformaat, geen lege provincie.
        log("  FOUT: geen BAG-IDs voor KDV in het LRK-bestand — kinderopvang niet getoetst.")
        bronnen.mislukt("lrk", "geen KDV-locaties met BAG-id in het bestand")
        return {"in_straal": [], "in_marge": []}

    log(f"  BAG VBO's ophalen in bbox straal+{KDV_BBOX_EXTRA} m voor KDV-matching ...")
    vbo_features = haal_verblijfsobjecten(circle_kdv_rd, log, bronnen=bronnen)
    vbo_features = dedupliceer_vbo(vbo_features, log)
    totaal = len(vbo_features)
    log(f"  BAG/LRK matching: {totaal} VBO's controleren op "
        f"{len(kdv_bag_ids)} KDV bag_ids ...")

    in_straal = []
    in_marge  = []

    with Voortgang() as voortgang:
        for i, feat in enumerate(vbo_features, 1):
            if totaal > 200 and i % 200 == 0:
                voortgang(f"  KDV matching: {i}/{totaal} VBO's gecontroleerd ...")
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

    vul_woonplaats_via_reverse_geocode(in_straal, log, bronnen=bronnen)
    vul_woonplaats_via_reverse_geocode(in_marge, log, bronnen=bronnen)
    log(f"  KDV: {len(in_straal)} locatie(s) binnen straal | "
        f"{len(in_marge)} in margeband (+{KDV_BBOX_EXTRA} m).")
    return {"in_straal": in_straal, "in_marge": in_marge}


# ──────────────────────────────────────────────
# DUO Open Onderwijsdata — scholen preprocessing
# ──────────────────────────────────────────────

def _lees_scholen_meta():
    if SCHOLEN_META.exists():
        try:
            return json.loads(SCHOLEN_META.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            _logger.warning("scholen-meta onleesbaar (%s) — wordt opnieuw aangemaakt.", e)
    return {}


def _schrijf_scholen_meta(meta):
    SCHOLEN_META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _duo_download_bytes(type_key: str, log: LogFn) -> bytes:
    """Download één DUO-dataset. Gooit BronFout."""
    resource_id = DUO_DATASETS[type_key]["resource_id"]
    url = f"https://onderwijsdata.duo.nl/datastore/dump/{resource_id}?format=json"
    log(f"    DUO {type_key}: downloaden van {resource_id} ...")
    return haal(url, timeout=(30, 180), max_bytes=MAX_BYTES_DUO)


def _geocodeer_adres(straat, huisnr, postcode):
    """Geocodeer één schooladres. Een adres zonder treffer is een gewone uitkomst;
    een onbereikbare Locatieserver (ook na één nieuwe poging) is een BronFout."""
    query = f"{straat} {huisnr}, {postcode}".strip(", ").strip()
    params = {"q": query, "fq": "type:adres",
              "fl": "id,adresseerbaarobject_id,centroide_ll", "rows": 1}
    try:
        data = haal_json(LOCATIESERVER_FREE, params=params, timeout=15, max_bytes=MAX_BYTES_API)
    except BronFout:
        data = haal_json(LOCATIESERVER_FREE, params=params, timeout=15, max_bytes=MAX_BYTES_API)
    docs = data.get("response", {}).get("docs", [])
    if not docs:
        return None, None, None, "geen_resultaat"
    doc = docs[0]
    vbo_id    = doc.get("adresseerbaarobject_id", "")
    centroide = doc.get("centroide_ll", "")
    m = re.match(r"POINT\(\s*([0-9.]+)\s+([0-9.]+)\s*\)", centroide)
    if not m:
        return None, None, vbo_id or None, "fout"
    return float(m.group(1)), float(m.group(2)), vbo_id, "ok"


def _maak_veldlezer(velden: list[str]):
    """Geeft een functie die één veld uit een DUO-record leest.

    De DUO-API levert records als dict of als rij; in het tweede geval geeft de
    veldenlijst van diezelfde dataset de kolomvolgorde. Die lijst wordt hier
    expliciet meegegeven in plaats van uit de omsluitende lus geleend.
    """
    def veld(rec, naam):
        if isinstance(rec, dict):
            return str(rec.get(naam, "")).strip()
        try:
            idx = velden.index(naam)
            return str(rec[idx]).strip() if idx < len(rec) else ""
        except (ValueError, IndexError, TypeError):
            return ""
    return veld


def _geocodeer_dataset(type_key: str, raw: bytes, log: LogFn) -> tuple[list[dict], dict]:
    """Zet één DUO-dataset om naar features. Gooit BronFout bij een onbruikbare dataset."""
    naam_veld = DUO_DATASETS[type_key]["naam_veld"]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as fout:
        raise BronFout(f"DUO {type_key}: dataset is geen geldige JSON") from fout
    if not isinstance(data, dict):
        raise BronFout(f"DUO {type_key}: onverwachte structuur")

    velden  = [f["id"] for f in data.get("fields", []) if isinstance(f, dict) and "id" in f]
    records = data.get("records", [])
    log(f"    DUO {type_key}: {len(records)} records geladen.")
    if velden:
        log(f"    DUO {type_key}: velden (eerste 8): {velden[:8]}")

    veld = _maak_veldlezer(velden)
    rec_prov = [r for r in records if veld(r, "PROVINCIE").upper() == DUO_PROVINCIE.upper()]
    log(f"    DUO {type_key}: {len(rec_prov)} vestigingen in {DUO_PROVINCIE}.")
    if not rec_prov:
        raise BronFout(f"DUO {type_key}: geen vestigingen in {DUO_PROVINCIE} — "
                       f"bestandsformaat gewijzigd?")

    features = []
    fouten = 0
    with Voortgang() as voortgang:
        for i, rec in enumerate(rec_prov, 1):
            voortgang(f"    DUO {type_key}: {i}/{len(rec_prov)} "
                      f"({i / len(rec_prov) * 100:.0f}%) — {fouten} fout(en)")
            naam       = veld(rec, naam_veld)
            straat     = veld(rec, "STRAATNAAM")
            huisnr     = veld(rec, "HUISNUMMER-TOEVOEGING")
            postcode   = veld(rec, "POSTCODE")
            woonplaats = veld(rec, "PLAATSNAAM")
            adres      = f"{straat} {huisnr}".strip()
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

    log(f"    DUO {type_key}: {len(features)} features geocodeerd; {fouten} fout(en).")
    meta = {"hash": hashlib.sha256(raw).hexdigest(), "timestamp": datetime.now().isoformat(),
            "totaal": len(rec_prov), "geocodeer_fouten": fouten}
    return features, meta


def _groep_ouderdom(meta: dict, groep_keys: list[str], geojson_pad: Path) -> int | None:
    """Ouderdom van de oudste dataset in de groep; zonder meta de bestandsdatum."""
    if not geojson_pad.exists():
        return None
    leeftijden = []
    for type_key in groep_keys:
        try:
            ts = datetime.fromisoformat(meta.get(type_key, {}).get("timestamp", ""))
        except (ValueError, TypeError):
            return _ouderdom_bestand(geojson_pad)
        leeftijden.append((datetime.now() - ts).days)
    return max(leeftijden)


def _ververs_scholen_groep(groep_sleutel, groep_keys, geojson_pad, log, meta) -> None:
    """Haal de groep opnieuw op; geocodeer alleen als een dataset is gewijzigd.

    Gooit BronFout zodra één dataset niet volledig binnenkomt; het bestaande
    GeoJSON-bestand blijft dan onaangeroerd.
    """
    ruw = {type_key: _duo_download_bytes(type_key, log) for type_key in groep_keys}
    ongewijzigd = geojson_pad.exists() and all(
        hashlib.sha256(ruw[k]).hexdigest() == meta.get(k, {}).get("hash") for k in groep_keys
    )
    if ongewijzigd:
        log(f"  DUO {groep_sleutel}: datasets ongewijzigd — GeoJSON hergebruikt.")
        for type_key in groep_keys:
            meta[type_key]["timestamp"] = datetime.now().isoformat()
        _schrijf_scholen_meta(meta)
        return

    log(f"  DUO {groep_sleutel}: GeoJSON aanmaken / vernieuwen ...")
    features: list[dict] = []
    nieuwe_meta = {}
    for type_key in groep_keys:
        type_features, nieuwe_meta[type_key] = _geocodeer_dataset(type_key, ruw[type_key], log)
        features.extend(type_features)

    GEO_DIR.mkdir(parents=True, exist_ok=True)
    tijdelijk = geojson_pad.with_name(geojson_pad.name + ".part")
    schrijf_prive(tijdelijk, json.dumps({"type": "FeatureCollection", "features": features},
                                        ensure_ascii=False))
    tijdelijk.replace(geojson_pad)
    meta.update(nieuwe_meta)
    _schrijf_scholen_meta(meta)
    log(f"    GeoJSON opgeslagen: {geojson_pad.name} ({len(features)} features).")


def _verwerk_scholen_groep(groep_sleutel, groep_keys, geojson_pad, log, bronnen: Bronregister):
    meta = _lees_scholen_meta()
    ouderdom = _groep_ouderdom(meta, groep_keys, geojson_pad)

    if ouderdom is not None and ouderdom < DUO_TTL_DAGEN:
        log(f"  DUO {groep_sleutel}: GeoJSON actueel ({ouderdom} dag(en) oud).")
        bronnen.cache("duo", ouderdom)
    else:
        try:
            _ververs_scholen_groep(groep_sleutel, groep_keys, geojson_pad, log, meta)
        except BronFout as fout:
            if ouderdom is not None and ouderdom <= DUO_NOODTERUGVAL_MAX_DAGEN:
                log(f"  FOUT: DUO {groep_sleutel} verversen mislukt ({fout}) — verouderde "
                    f"GeoJSON gebruikt ({ouderdom} dag(en) oud).")
                bronnen.verouderd("duo", ouderdom, f"groep {groep_sleutel}: {fout}")
            else:
                log(f"  FOUT: DUO {groep_sleutel} verversen mislukt ({fout}) — geen bruikbare "
                    f"lokale kopie; scholen ({groep_sleutel}) niet getoetst.")
                bronnen.mislukt("duo", f"groep {groep_sleutel}: {fout}")
                return []
        else:
            bronnen.geraadpleegd("duo")

    try:
        data = json.loads(geojson_pad.read_text(encoding="utf-8"))
        return data.get("features", [])
    except (OSError, ValueError) as fout:
        log(f"  FOUT: {geojson_pad.name} kon niet worden gelezen: {fout}")
        bronnen.mislukt("duo", f"{geojson_pad.name} niet leesbaar")
        return []


def _laad_scholen(log, bronnen: Bronregister):
    log("  DUO PO-groep controleren ...")
    po_features = _verwerk_scholen_groep("PO", _DUO_PO_GROEP, SCHOLEN_GEOJSON_PO, log, bronnen)
    log("  DUO overig-groep controleren ...")
    overig_features = _verwerk_scholen_groep(
        "overig", _DUO_OVERIG_GROEP, SCHOLEN_GEOJSON_OVERIG, log, bronnen,
    )
    totaal = len(po_features) + len(overig_features)
    log(f"  DUO scholen geladen: {len(po_features)} PO + "
        f"{len(overig_features)} overig = {totaal} totaal.")
    return po_features + overig_features


def haal_scholen(circle_rd: BaseGeometry, log: LogFn, *, bronnen: Bronregister) -> dict[str, list]:
    alle_scholen = _laad_scholen(log, bronnen)
    circle_marge_rd = circle_rd.buffer(MARGE_M)
    in_straal = []
    in_marge  = []

    for feat in alle_scholen:
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            pt_rd = transform_geom_to_rd(shapely_from_geojson_geom(geom))
        except (ValueError, TypeError, KeyError) as fout:
            _logger.warning(f"School met onleesbare geometrie overgeslagen: {fout}")
            continue
        if circle_rd.contains(pt_rd) or circle_rd.intersects(pt_rd):
            in_straal.append(feat)
        elif circle_marge_rd.contains(pt_rd) or circle_marge_rd.intersects(pt_rd):
            in_marge.append(feat)

    log(f"  DUO scholen: {len(in_straal)} binnen straal | "
        f"{len(in_marge)} in marge (+{MARGE_M} m).")
    return {"in_straal": in_straal, "in_marge": in_marge}
