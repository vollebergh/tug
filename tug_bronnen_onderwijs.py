"""
tug_bronnen_onderwijs.py -- Onderwijs- en kinderopvanglocaties

Bevat:
- Kinderdagverblijven (KDV) via koppeling Landelijk Register Kinderopvang (LRK CSV)
  aan BAG-verblijfsobjecten.
- Scholen via DUO Open Onderwijsdata (5 datasets: PO/SO/VO/MBO/HO), geocodeerd via
  PDOK Locatieserver en lokaal gecachet als GeoJSON.
"""

import copy
import hashlib
import json
import logging
import re
from datetime import datetime

import pandas as pd
import requests
from shapely.geometry.base import BaseGeometry

from tug_config import (
    GEO_DIR,
    LRK_URL, LRK_CACHE_DAYS, LRK_HEADERS, KDV_BBOX_EXTRA,
    LOCATIESERVER_FREE,
    DUO_PROVINCIE, DUO_TTL_DAGEN, DUO_DATASETS,
    _DUO_PO_GROEP, _DUO_OVERIG_GROEP,
    SCHOLEN_GEOJSON_PO, SCHOLEN_GEOJSON_OVERIG, SCHOLEN_META,
    MARGE_M,
)
from tug_geo import shapely_from_geojson_geom, transform_geom_to_rd
from tug_bronnen_bag import haal_verblijfsobjecten, dedupliceer_vbo
from tug_bronnen_geocode import vul_woonplaats_via_reverse_geocode
from tug_types import LogFn


_logger = logging.getLogger("tug.bronnen_onderwijs")


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
        # Download naar een tijdelijk bestand, zodat een afgebroken download
        # de cache niet beschadigt
        tmp_pad = lrk_pad.with_suffix(".csv.part")
        try:
            resp = requests.get(LRK_URL, headers=LRK_HEADERS, stream=True, timeout=120)
            resp.raise_for_status()
            totaal = int(resp.headers.get("content-length", 0))
            ontvangen = 0
            with tmp_pad.open("wb") as fout:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fout.write(chunk)
                        ontvangen += len(chunk)
                        if totaal:
                            print(f"\r  LRK downloaden: {ontvangen // 1024} KB / "
                                  f"{totaal // 1024} KB "
                                  f"({ontvangen / totaal * 100:.0f}%)   ",
                                  end="", flush=True)
                        else:
                            print(f"\r  LRK downloaden: {ontvangen // 1024} KB ontvangen   ",
                                  end="", flush=True)
            print()
            tmp_pad.replace(lrk_pad)
            log(f"  LRK CSV opgeslagen: {lrk_pad.name} ({ontvangen // 1024} KB).")
        except Exception as e:
            print()
            tmp_pad.unlink(missing_ok=True)
            log(f"  FOUT: LRK CSV downloaden mislukt: {e}")
            if not lrk_pad.exists():
                return None
            leeftijd = (datetime.now() - datetime.fromtimestamp(lrk_pad.stat().st_mtime)).days
            log(f"  WAARSCHUWING: verouderde lokale LRK CSV als noodoplossing gebruikt "
                f"({lrk_pad.name}, {leeftijd} dag(en) oud).")

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
    return None


def haal_kdv_locaties(circle_rd: BaseGeometry, log: LogFn) -> dict[str, list]:
    circle_kdv_rd = circle_rd.buffer(KDV_BBOX_EXTRA)
    df = _laad_lrk_csv(log)
    if df is None:
        log("  WAARSCHUWING: LRK CSV niet beschikbaar — KDV-detectie overgeslagen.")
        return {"in_straal": [], "in_marge": []}

    kolom_map = {k.lower().replace(" ", "_").replace("-", "_"): k for k in df.columns}
    type_col = kolom_map.get("type_oko") or kolom_map.get("typeoko")
    bag_col  = kolom_map.get("bag_id")  or kolom_map.get("bagid")

    if not type_col or not bag_col:
        log(f"  WAARSCHUWING: LRK-kolommen 'type_oko' / 'bag_id' niet gevonden. "
            f"Beschikbare kolommen: {list(df.columns[:15])}")
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
    log(f"  BAG/LRK matching: {totaal} VBO's controleren op "
        f"{len(kdv_bag_ids)} KDV bag_ids ...")

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
            print(f"\r  KDV matching: {i}/{totaal} VBO's gecontroleerd ...  ",
                  end="", flush=True)

    if totaal > 200:
        print()

    vul_woonplaats_via_reverse_geocode(in_straal, log)
    vul_woonplaats_via_reverse_geocode(in_marge,  log)
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
            logging.getLogger("tug.bronnen_onderwijs").warning(
                "scholen-meta onleesbaar (%s) — wordt opnieuw aangemaakt.", e
            )
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
            params={
                "q": query, "fq": "type:adres",
                "fl": "id,adresseerbaarobject_id,centroide_ll", "rows": 1,
            },
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
    except (requests.RequestException, ValueError, KeyError, IndexError) as fout:
        _logger.debug(f"Geocodering mislukt voor {straat} {huisnr}, {postcode}: {fout}")
        return None, None, None, "fout"


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


def _geocodeer_groep(groep_keys, geojson_pad, log, meta, cached_bytes=None):
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

        veld = _maak_veldlezer(velden)

        rec_prov = [r for r in records if veld(r, "PROVINCIE").upper() == DUO_PROVINCIE.upper()]
        log(f"    DUO {type_key}: {len(rec_prov)} vestigingen in {DUO_PROVINCIE}.")

        fouten = 0
        type_features_start = len(features)

        for i, rec in enumerate(rec_prov, 1):
            pct = i / len(rec_prov) * 100
            print(f"\r    DUO {type_key}: {i}/{len(rec_prov)} ({pct:.0f}%) — "
                  f"{fouten} fout(en)   ", end="", flush=True)
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
                log(f"    DUO {type_key}: hash ongewijzigd. GeoJSON hergebruikt.")
                meta[type_key]["timestamp"] = datetime.now().isoformat()
                _schrijf_scholen_meta(meta)
            except (OSError, ValueError, KeyError) as fout:
                _logger.debug(f"DUO-meta onbruikbaar ({fout}); groep wordt hergeocodeerd")
                moet_vernieuwen = True
                break

    if moet_vernieuwen:
        log(f"  DUO {groep_sleutel}: GeoJSON aanmaken / vernieuwen ...")
        GEO_DIR.mkdir(parents=True, exist_ok=True)
        _geocodeer_groep(groep_keys, geojson_pad, log, meta, cached_bytes)
        _schrijf_scholen_meta(meta)
    else:
        log(f"  DUO {groep_sleutel}: GeoJSON actueel, geen vernieuwing nodig.")

    if not geojson_pad.exists():
        log(f"  WAARSCHUWING: {geojson_pad.name} bestaat niet — "
            f"schooldetectie voor {groep_sleutel} overgeslagen.")
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
    overig_features = _verwerk_scholen_groep(
        "overig", _DUO_OVERIG_GROEP, SCHOLEN_GEOJSON_OVERIG, log
    )
    totaal = len(po_features) + len(overig_features)
    log(f"  DUO scholen geladen: {len(po_features)} PO + "
        f"{len(overig_features)} overig = {totaal} totaal.")
    return po_features + overig_features


def haal_scholen(circle_rd: BaseGeometry, log: LogFn) -> dict[str, list]:
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
