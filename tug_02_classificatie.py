"""
tug_02_classificatie.py -- Stap 3 TUG-ontheffingen workflow
Versie: 1.0.0  |  2026-05-01

Stap 3a: PH-code → Luchtvaartregister ILT → ICAO-code
Stap 3b: ICAO-code → NLR-tabel → Appendix Categorie + Afstandsnorm
Stap 3c: PM-categorieën (013/015/016/017) → interactieve invoer vergunningverlener (standaard 500 m)
Stap 3d: Meerdere luchtvaartuigen → norm_toepassing = maximum

Invoer : tug_state.json  (aanvraag.luchtvaartuigen)
Uitvoer: tug_state.json  (sectie classificatie gevuld)

Vereist: pip install odfpy
"""

import hashlib
import io
import json
import logging
import re
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from tug_logging import LogAccumulator, setup_logging

# ──────────────────────────────────────────────
# Configuratie
# ──────────────────────────────────────────────

VERSION = "1.0.0"

GEO_DIR           = Path(__file__).parent / "geo"
REGISTER_ODS_PAD  = GEO_DIR / "luchtvaartuigregister_ilt.ods"
REGISTER_META_PAD = GEO_DIR / "luchtvaartregister.meta.json"
REGISTER_TTL_DAGEN = 30

ILT_PAGINA_URL = (
    "https://www.ilent.nl/documenten/lijsten/luchtvaart/"
    "databestanden/luchtvaartregister-data"
)
ILT_URL_BASIS = (
    "https://www.ilent.nl/site/binaries/site-content/collections/documents/"
    "lijsten/luchtvaart/databestanden/luchtvaartregister-data/"
    "luchtvaartuigregister-ilt-datas2-{datum}.ods"
)

# ──────────────────────────────────────────────
# NLR vliegtuigcategorieën (CR-96650L Suppl. 1, okt. 2022, p. 15–16)
# norm_m = None voor PM-categorieën (013, 015, 016, 017).
# Handmatig bijwerken wanneer ILT/NLR de tabel wijzigt.
# ──────────────────────────────────────────────

NLR_TABEL = {
    # Appendix 010 — 250 m
    "A119": ("010", 250), "ALO2": ("010", 250), "ALO3": ("010", 250),
    "AS50": ("010", 250), "AS55": ("010", 250), "B06":  ("010", 250),
    "B06T": ("010", 250), "B105": ("010", 250), "B407": ("010", 250),
    "B47G": ("010", 250), "EC20": ("010", 250), "EC30": ("010", 250),
    "EN48": ("010", 250), "GAZL": ("010", 250), "H500": ("010", 250),
    "MD52": ("010", 250), "MD60": ("010", 250), "R66":  ("010", 250),
    "S330": ("010", 250),
    # Appendix 011 — 150 m
    "DRAG": ("011", 150), "EN28": ("011", 150), "G2CA": ("011", 150),
    "H269": ("011", 150), "R22":  ("011", 150), "R44":  ("011", 150),
    # Appendix 012 — 350 m
    "A139": ("012", 350), "B212": ("012", 350), "B214": ("012", 350),
    "B412": ("012", 350), "LYNX": ("012", 350), "S76":  ("012", 350),
    # Appendix 013 — PM (norm niet vastgesteld)
    "E35X": ("013", None),
    # Appendix 014 — 500 m
    "A189": ("014", 500), "AS32": ("014", 500), "AS3B": ("014", 500),
    "BSTP": ("014", 500), "EH10": ("014", 500), "FREL": ("014", 500),
    "H47":  ("014", 500), "H53":  ("014", 500), "H60":  ("014", 500),
    "H64":  ("014", 500), "MI26": ("014", 500), "MI8":  ("014", 500),
    "PUMA": ("014", 500), "S61":  ("014", 500),
    # Appendix 015 — PM
    "A109": ("015", None), "B222": ("015", None), "BK17": ("015", None),
    "EC35": ("015", None), "EC45": ("015", None), "EXPL": ("015", None),
    "MI2":  ("015", None), "S360": ("015", None),
    # Appendix 016 — PM
    "A169": ("016", None), "AS65": ("016", None), "B430": ("016", None),
    "EC55": ("016", None), "S65C": ("016", None), "UH1":  ("016", None),
    # Appendix 017 — PM
    "NH90": ("017", None),
}

NLR_TYPEN = {
    "A119": ["AGUSTA A-119 Koala"],
    "ALO2": ["SUD-EST SE-3130 Alouette 2"],
    "ALO3": ["SUD SA-316", "SUD SA-319", "SUD SE-3160 Alouette 3"],
    "AS50": ["AEROSPATIALE AS-350 Ecureuil/AStar/SuperStar/Fennec",
             "AEROSPATIALE AS-550 Ecureuil/AStar/SuperStar/Fennec"],
    "AS55": ["AEROSPATIALE AS-355 Ecureuil 2/TwinStar/Fennec",
             "AEROSPATIALE AS-555 Ecureuil 2/TwinStar/Fennec"],
    "B06":  ["BELL 206A/B/L JetRanger/LongRanger/CombatScout",
             "BELL 406 JetRanger", "BELL TH-206"],
    "B06T": ["BELL 206LT TwinRanger"],
    "B105": ["BOLKOW BO-105"],
    "B407": ["BELL 407"],
    "B47G": ["BELL 47D/47G/47H Trooper"],
    "EC20": ["EUROCOPTER EC-120 Colibri"],
    "EC30": ["EUROCOPTER EC-130"],
    "EN48": ["ENSTROM 480", "ENSTROM TH-28"],
    "GAZL": ["SUD SA-341 Gazelle", "SUD SA-342 Gazelle"],
    "H500": ["HUGHES 369/500/530F Defender"],
    "MD52": ["MD HELICOPTERS MD-520N"],
    "MD60": ["MCDONNELL MD-600N"],
    "R66":  ["ROBINSON R-66"],
    "S330": ["SCHWEIZER 269D 330"],
    "DRAG": ["DF HELICOPTERS Dragonfly"],
    "EN28": ["ENSTROM F-28 Sentinel/Falcon", "ENSTROM 280 Shark"],
    "G2CA": ["GUIMBAL G-2 Cabri"],
    "H269": ["HUGHES 269/200/280/300 SkyKnight", "HUGHES TH-300 SkyKnight"],
    "R22":  ["ROBINSON R-22 Beta/Mariner"],
    "R44":  ["ROBINSON R-44 Astro/Raven/Clipper"],
    "A139": ["BELL-AGUSTA AB-139"],
    "B212": ["BELL 212, Twin Two-Twelve"],
    "B214": ["BELL 214A/B/C Isfahan/Biglifter"],
    "B412": ["BELL 412 Sentinel/Arapaho"],
    "LYNX": ["WESTLAND Lynx", "WESTLAND SuperLynx", "WESTLAND Battlefield Lynx"],
    "S76":  ["SIKORSKY S-76/H-76/AUH-76 Spirit/Eagle"],
    "E35X": ["EUROCOPTER EC-135 P3/T3"],
    "A189": ["AGUSTA-WESTLAND AW-189"],
    "AS32": ["AEROSPATIALE AS-332/AS-532 Super Puma/Tiger/Cougar"],
    "AS3B": ["AEROSPATIALE AS-332L2 Super Puma Mk2"],
    "BSTP": ["BELL 214ST Super Transport"],
    "EH10": ["EHI EH-101 Heliliner"],
    "FREL": ["AEROSPATIALE SA-321 Super Frelon"],
    "H47":  ["BOEING VERTOL CH-47/MH-47/HT-17 Chinook"],
    "H53":  ["SIKORSKY S-65"],
    "H60":  ["SIKORSKY AH-60/S-70 Blackhawk/Jayhawk/PaveHawk/Knighthawk"],
    "H64":  ["MCDONNELL DOUGLAS AH-64 Longbow Apache"],
    "MI26": ["MIL Mi-26"],
    "MI8":  ["MIL Mi-8/9/17/19/171/172"],
    "PUMA": ["SUD SA-330 Puma"],
    "S61":  ["SIKORSKY S-61A/B/D/L/N"],
    "A109": ["AGUSTA A-109 Power"],
    "B222": ["BELL 222"],
    "BK17": ["MBB-KAWASAKI BK-117"],
    "EC35": ["EUROCOPTER EC-135 (voor P3/T3)", "EUROCOPTER EC-635 (voor P3/T3)"],
    "EC45": ["EUROCOPTER EC-145"],
    "EXPL": ["MD HELICOPTERS MD-900 Combat Explorer", "MD HELICOPTERS MD-902 Explorer"],
    "MI2":  ["PZL-SWIDNIK (MIL) Mi-2 Kania/KittyHawk/Bazant"],
    "S360": ["AEROSPATIALE SA-360/361 Dauphin"],
    "A169": ["AGUSTA-WESTLAND AW-169"],
    "AS65": ["AEROSPATIALE AS-365/AS-565 Dauphin 2/Panther",
             "AEROSPATIALE SA-365F/K/M/N Dauphin 2/Panther"],
    "B430": ["BELL 430"],
    "EC55": ["EUROCOPTER EC-155"],
    "S65C": ["AEROSPATIALE SA-365C Dauphin 2"],
    "UH1":  ["BELL 204/205/210/EH1/HH1/UH1/SH-1/TH-1 Iroquois/Huey"],
    "NH90": ["NHI NH-90 (civiele operaties)"],
}


# ──────────────────────────────────────────────
# Luchtvaartregister — meta / hash-hulpfuncties
# ──────────────────────────────────────────────

def _lees_meta():
    if REGISTER_META_PAD.exists():
        try:
            return json.loads(REGISTER_META_PAD.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logging.getLogger("tug.02_classificatie").warning(
                f"  WAARSCHUWING: meta-bestand {REGISTER_META_PAD.name} onleesbaar "
                f"({e.__class__.__name__}: {e}) — wordt genegeerd; "
                f"register wordt opnieuw gedownload."
            )
    return {}


def _schrijf_meta(meta):
    GEO_DIR.mkdir(parents=True, exist_ok=True)
    REGISTER_META_PAD.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _ods_data_hash(ods_bytes):
    """SHA-256 van content.xml in het ODS ZIP-archief (negeer metadata)."""
    with zipfile.ZipFile(io.BytesIO(ods_bytes)) as z:
        content_xml = z.read("content.xml")
    return hashlib.sha256(content_xml).hexdigest()


# ──────────────────────────────────────────────
# Luchtvaartregister — URL opzoeken en downloaden
# ──────────────────────────────────────────────

def _zoek_register_url(log):
    """
    Scrape de ILT-pagina voor de directe download-URL van het registerbestand.
    Fallback: probeer datumgebaseerde URL's (vandaag t/m 14 dagen geleden).
    """
    log(f"  Register-URL opzoeken via {ILT_PAGINA_URL} ...")
    try:
        resp = requests.get(ILT_PAGINA_URL, timeout=15)
        resp.raise_for_status()
        m = re.search(
            r'href="([^"]*luchtvaartuigregister-ilt-datas2-[^"]*\.ods)"',
            resp.text,
        )
        if m:
            href = m.group(1)
            url  = href if href.startswith("http") else "https://www.ilent.nl" + href
            log(f"  Register-URL gevonden: {url}")
            return url
        log("  WAARSCHUWING: Register-URL niet gevonden in paginabron, datumfallback proberen ...")
    except Exception as e:
        log(f"  WAARSCHUWING: ILT-pagina niet bereikbaar ({e}), datumfallback proberen ...")

    # Datumfallback: zoek bestand gepubliceerd in de afgelopen 14 dagen
    for dagen_terug in range(0, 15):
        datum = (datetime.now() - timedelta(days=dagen_terug)).strftime("%Y-%m-%d")
        url   = ILT_URL_BASIS.format(datum=datum)
        try:
            r = requests.head(url, timeout=10, allow_redirects=True)
            if r.status_code == 200:
                log(f"  Register-URL via datumfallback: {url}")
                return url
        except Exception:
            continue

    log("  FOUT: Geen geldige register-URL gevonden.")
    return None


def _download_register(log, force=False):
    """
    Laad het luchtvaartregister als pandas DataFrame.
    Cachelogica:
      - Binnen TTL en force=False → lokaal bestand hergebruiken.
      - TTL verlopen of force=True → scrape URL, download, vergelijk hash.
        - Hash ongewijzigd → update download_datum, hergebruik lokaal bestand.
        - Hash gewijzigd of geen lokaal bestand → sla nieuw bestand op.
    Retourneert (DataFrame, meta_dict).
    """
    meta = _lees_meta()

    binnen_ttl = (
        not force
        and REGISTER_ODS_PAD.exists()
        and meta.get("download_datum")
        and (datetime.now() - datetime.fromisoformat(meta["download_datum"])).days < REGISTER_TTL_DAGEN
    )

    if binnen_ttl:
        ouderdom = (datetime.now() - datetime.fromisoformat(meta["download_datum"])).days
        log(f"  Register: lokaal bestand gebruikt ({REGISTER_ODS_PAD.name}, {ouderdom} dag(en) oud).")
        df = pd.read_excel(REGISTER_ODS_PAD, engine="odf", header=0, dtype=str)
        return df, meta

    url = _zoek_register_url(log)
    if not url:
        if REGISTER_ODS_PAD.exists():
            log("  WAARSCHUWING: Geen URL gevonden — lokaal bestand als noodoplossing gebruikt.")
            df = pd.read_excel(REGISTER_ODS_PAD, engine="odf", header=0, dtype=str)
            return df, meta
        raise RuntimeError(
            "Luchtvaartregister niet beschikbaar: geen URL gevonden en geen lokaal bestand."
        )

    log(f"  Register: downloaden van {url} ...")
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        if REGISTER_ODS_PAD.exists():
            log(f"  WAARSCHUWING: Download mislukt ({e}) — lokaal bestand als noodoplossing gebruikt.")
            df = pd.read_excel(REGISTER_ODS_PAD, engine="odf", header=0, dtype=str)
            return df, meta
        raise

    nieuwe_bytes = resp.content
    nieuwe_hash  = _ods_data_hash(nieuwe_bytes)

    if REGISTER_ODS_PAD.exists() and nieuwe_hash == meta.get("data_hash", ""):
        log("  Register: hash ongewijzigd — lokaal bestand hergebruikt, TTL verlengd.")
    else:
        GEO_DIR.mkdir(parents=True, exist_ok=True)
        REGISTER_ODS_PAD.write_bytes(nieuwe_bytes)
        log(f"  Register: nieuw bestand opgeslagen ({REGISTER_ODS_PAD.name}).")
        meta["data_hash"] = nieuwe_hash

    meta["download_datum"] = datetime.now().isoformat()
    meta["bron_url"]       = url
    _schrijf_meta(meta)

    df = pd.read_excel(REGISTER_ODS_PAD, engine="odf", header=0, dtype=str)
    log(f"  Register: {len(df)} rijen geladen.")
    return df, meta


# ──────────────────────────────────────────────
# Luchtvaartregister — PH-code opzoeken
# ──────────────────────────────────────────────

def _vind_kolommen(df, log):
    """
    Zoek de registratie- en ICAO-kolom op naam (kolomkop bevat metadata-tekst).
    Retourneert (reg_kolom, icao_kolom) of (None, None) bij niet gevonden.
    """
    reg_kolom  = next((c for c in df.columns if isinstance(c, str) and "Registration" in c), None)
    icao_kolom = next((c for c in df.columns if isinstance(c, str) and "ICAO" in c), None)

    if not reg_kolom:
        log(f"  FOUT: Kolom 'Registration' niet gevonden. Beschikbare kolommen: {list(df.columns[:10])}")
    if not icao_kolom:
        log(f"  FOUT: Kolom met 'ICAO' niet gevonden. Beschikbare kolommen: {list(df.columns[:10])}")

    return reg_kolom, icao_kolom


def _zoek_icao_in_register(ph_code, df, log):
    """
    Zoek de ICAO-code voor ph_code in het register-DataFrame.
    Retourneert de ICAO-code (string, gestript) of None.
    """
    reg_kolom, icao_kolom = _vind_kolommen(df, log)
    if not reg_kolom or not icao_kolom:
        return None

    ph_norm = ph_code.strip().upper()
    mask    = df[reg_kolom].astype(str).str.strip().str.upper() == ph_norm
    rijen   = df[mask]

    if rijen.empty:
        return None

    icao = str(rijen.iloc[0][icao_kolom]).strip()
    return icao if icao and icao.upper() != "NAN" else None


# ──────────────────────────────────────────────
# Interactieve invoer vergunningverlener
# ──────────────────────────────────────────────

def _vraag_handmatige_norm(reden, standaard_m=None):
    """
    Vraag de vergunningverlener om een afstandsnorm in te voeren.
    standaard_m: standaardwaarde die wordt gebruikt bij ENTER (None = verplichte invoer).
    Retourneert een positief geheel getal.
    """
    print()
    print(f"  {'─' * 55}")
    print(f"  ⚠  Handmatige invoer vereist")
    print(f"  Reden: {reden}")
    if standaard_m is not None:
        print(f"  Druk op ENTER om de standaardwaarde ({standaard_m} m) te gebruiken.")
    print(f"  {'─' * 55}")

    while True:
        prompt = f"  Afstandsnorm (m){f' [{standaard_m}]' if standaard_m else ''}: "
        invoer = input(prompt).strip()
        if not invoer and standaard_m is not None:
            print(f"  → Standaardwaarde {standaard_m} m aangenomen.")
            return standaard_m
        try:
            norm = int(invoer)
            if norm > 0:
                print(f"  → Ingevoerde norm: {norm} m.")
                return norm
        except ValueError:
            pass
        print("  Ongeldige invoer — voer een positief geheel getal in.")


# ──────────────────────────────────────────────
# Classificatie per luchtvaartuig
# ──────────────────────────────────────────────

def _classificeer_luchtvaartuig(lv, df, log):
    """
    Classificeer één luchtvaartuig volledig (3a → 3b → 3c).
    lv: dict met minimaal {"registratie": "PH-xxx", "type": "..."}
    df: register-DataFrame (al geladen)
    Retourneert classificatie-dict.
    """
    ph_code = lv["registratie"].strip().upper()
    signalen = []

    # Stap 3a: PH-code → ICAO-code
    icao = _zoek_icao_in_register(ph_code, df, log)

    if icao:
        log(f"  {ph_code}: ICAO-code gevonden: {icao}")
    else:
        log(f"  {ph_code}: niet gevonden in register.")
        norm_m = _vraag_handmatige_norm(
            f"{ph_code} niet gevonden in ILT luchtvaartregister "
            f"(registratie mogelijk in ander EU-land of onbekend)",
        )
        signalen.append(
            f"{ph_code} niet gevonden in ILT luchtvaartregister. "
            f"Norm {norm_m} m handmatig bepaald door vergunningverlener."
        )
        return {
            "registratie":       ph_code,
            "type_aanvraag":     lv.get("type", ""),
            "icao_code":         None,
            "appendix_categorie": None,
            "vliegtuigtypen_nlr": [],
            "norm_m":            norm_m,
            "norm_bron":         "handmatig_niet_gevonden",
            "signalen":          signalen,
        }

    # Stap 3b: ICAO-code → NLR-tabel
    icao_upper = icao.upper()
    if icao_upper not in NLR_TABEL:
        # Niet in NLR-tabel: 150 m beleidsregel-fallback (MLA / vliegtuig)
        log(f"  {ph_code}: ICAO {icao_upper} niet in NLR-indelingslijst → 150 m (beleidsregel).")
        signalen.append(
            f"ICAO-code {icao_upper} komt niet voor in NLR-indelingslijst CR-96650L. "
            f"Standaard 150 m aangehouden (beleidsregel; vermoedelijk MLA of vliegtuig)."
        )
        return {
            "registratie":       ph_code,
            "type_aanvraag":     lv.get("type", ""),
            "icao_code":         icao_upper,
            "appendix_categorie": None,
            "vliegtuigtypen_nlr": [],
            "norm_m":            150,
            "norm_bron":         "fallback_150m",
            "signalen":          signalen,
        }

    categorie, norm_m = NLR_TABEL[icao_upper]
    typen = NLR_TYPEN.get(icao_upper, [])

    # Stap 3c: PM-categorie → handmatige invoer (standaard 500 m)
    if norm_m is None:
        log(
            f"  {ph_code}: ICAO {icao_upper} → Appendix Categorie {categorie} "
            f"(PM — norm niet vastgesteld in NLR-tabel)."
        )
        norm_m = _vraag_handmatige_norm(
            f"Appendix Categorie {categorie} heeft geen vastgestelde afstandsnorm "
            f"(PM). Beleidsregel adviseert grootste bekende norm.",
            standaard_m=500,
        )
        signalen.append(
            f"Appendix Categorie {categorie} heeft geen vastgestelde norm (PM). "
            f"Norm {norm_m} m bepaald door vergunningverlener. "
            f"Definitieve norm vereist beoordeling geluidsrapport."
        )
        norm_bron = "pm_handmatig"
    else:
        log(
            f"  {ph_code}: ICAO {icao_upper} → Appendix Categorie {categorie} "
            f"→ norm {norm_m} m."
        )
        if typen:
            log(f"    Vliegtuigtype(n): {'; '.join(typen)}")
        norm_bron = "nlr_tabel"

    return {
        "registratie":       ph_code,
        "type_aanvraag":     lv.get("type", ""),
        "icao_code":         icao_upper,
        "appendix_categorie": categorie,
        "vliegtuigtypen_nlr": typen,
        "norm_m":            norm_m,
        "norm_bron":         norm_bron,
        "signalen":          signalen,
    }


# ──────────────────────────────────────────────
# Hoofdfunctie
# ──────────────────────────────────────────────

def run(state_pad: str | Path) -> None:
    state_pad = Path(state_pad)
    state     = json.loads(state_pad.read_text(encoding="utf-8"))
    aanvraag  = state["aanvraag"]

    setup_logging()
    _root_logger = logging.getLogger("tug.02_classificatie")

    luchtvaartuigen = aanvraag.get("luchtvaartuigen", [])
    if not luchtvaartuigen:
        _root_logger.error("FOUT: aanvraag bevat geen luchtvaartuigen.")
        sys.exit(1)

    now = datetime.now()
    log = LogAccumulator("tug.02_classificatie")

    log(f"{'=' * 60}")
    log("TUG-ontheffingen — Stap 3: Classificatie luchtvaartuigen")
    log(f"## Versie: {VERSION}")
    log(f"{'=' * 60}")
    log(f"Aantal luchtvaartuigen in aanvraag: {len(luchtvaartuigen)}")

    # Stap 3a: register laden (eerste poging binnen TTL)
    log("\nStap 3a: Luchtvaartregister laden ...")
    df, meta = _download_register(log, force=False)

    # Classificeer elk luchtvaartuig; herdownload register bij eerste cache-miss
    register_ververst = False
    resultaten = []

    for lv in luchtvaartuigen:
        ph_code = lv["registratie"].strip().upper()
        log(f"\n  Verwerken: {ph_code} (type: {lv.get('type', 'onbekend')})")

        # Snelle pre-check: staat ph_code in het al geladen register?
        reg_kolom, _ = _vind_kolommen(df, log)
        if reg_kolom:
            in_register = (
                df[reg_kolom].astype(str).str.strip().str.upper() == ph_code
            ).any()
        else:
            in_register = False

        if not in_register and not register_ververst:
            log(f"  {ph_code}: niet in lokaal register — register verversen ...")
            df, meta = _download_register(log, force=True)
            register_ververst = True

        resultaten.append(_classificeer_luchtvaartuig(lv, df, log))

    # Stap 3d: norm_toepassing = maximum over alle luchtvaartuigen
    normen = [r["norm_m"] for r in resultaten if r["norm_m"] is not None]
    if not normen:
        log("\nFOUT: Geen geldige afstandsnorm bepaald voor enig luchtvaartuig.")
        state_pad.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        sys.exit(1)

    norm_toepassing = max(normen)
    maatgevend = [r["registratie"] for r in resultaten if r["norm_m"] == norm_toepassing]

    log(f"\nStap 3d: norm_toepassing = {norm_toepassing} m "
        f"(maatgevend luchtvaartuig: {', '.join(maatgevend)})")

    # Alle signalen samenvoegen voor logboek
    alle_signalen = [s for r in resultaten for s in r["signalen"]]
    if alle_signalen:
        log("\nSignaleringen:")
        for s in alle_signalen:
            log(f"  • {s}")

    log(f"\n{'=' * 60}")
    log("Stap 3 voltooid.")
    log(f"{'=' * 60}")

    state["classificatie"] = {
        "luchtvaartuigen":   resultaten,
        "norm_toepassing":   norm_toepassing,
        "register_bestand":  REGISTER_ODS_PAD.name,
        "register_datum":    meta.get("download_datum", ""),
        "log_regels":        log.lines,
    }

    state.setdefault("logboek", []).append({
        "stap":     "02_classificatie",
        "tijdstip": datetime.now().isoformat(),
        "niveau":   "info" if not alle_signalen else "waarschuwing",
        "bericht":  (
            f"Classificatie voltooid: {len(resultaten)} luchtvaartuig(en), "
            f"norm_toepassing={norm_toepassing} m."
            + (f" {len(alle_signalen)} signalering(en)." if alle_signalen else "")
        ),
    })

    state_pad.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.getLogger("tug.02_classificatie").info(f"State geschreven naar {state_pad}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 2:
        setup_logging()
        logging.getLogger("tug.02_classificatie").error(
            "Gebruik: python tug_02_classificatie.py tug_state.json"
        )
        sys.exit(1)
    run(sys.argv[1])
