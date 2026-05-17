"""
tug_01_validatie.py -- Stap 2 TUG-ontheffingen workflow
Versie: 1.0.0  |  2026-05-15

Volledigheidscheck van de aanvraag:
  - Controleert aanwezigheid van verplichte velden
  - Normaliseert datum_vlucht naar een lijst (str → [str])
  - Controleert of datum_ondertekening niet meer dan 4 weken vóór
    de vroegste vluchtdatum ligt

Invoer : tug_state.json  (aanvraag)
Uitvoer: tug_state.json  (sectie validatie gevuld; aanvraag.datum_vlucht genormaliseerd)
"""

import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from tug_logging import LogAccumulator, setup_logging

VERSION = "1.0.0"

VERPLICHTE_VELDEN = [
    "soort_ontheffing",
    "datum_vlucht",
    "luchtvaartuigen",
    "coord_lat",
    "coord_lon",
    "datum_ondertekening",
    "tijdstip_ondertekening",
]

MAX_VOORUIT_WEKEN = 4  # ondertekening mag maximaal 4 weken vóór vroegste vlucht liggen


# ──────────────────────────────────────────────
# Hulpfuncties
# ──────────────────────────────────────────────

def _parse_datum(waarde, veldnaam):
    """Parseer YYYY-MM-DD naar date; retourneert (date, foutmelding_of_None)."""
    try:
        return datetime.strptime(waarde, "%Y-%m-%d").date(), None
    except (ValueError, TypeError):
        return None, f"'{veldnaam}' heeft geen geldige YYYY-MM-DD datum: {waarde!r}"


def _normaliseer_datum_vlucht(aanvraag):
    """
    Zorg dat datum_vlucht altijd een lijst is.
    Retourneert (genormaliseerde_lijst_of_None, foutmelding_of_None).
    """
    waarde = aanvraag.get("datum_vlucht")
    if waarde is None:
        return None, None  # ontbrekend veld wordt al door verplichte-velden-check afgevangen

    if isinstance(waarde, str):
        waarde = [waarde]
    elif not isinstance(waarde, list):
        return None, (
            f"'datum_vlucht' moet een datum-string of een lijst van datums zijn, "
            f"niet {type(waarde).__name__!r}"
        )

    if not waarde:
        return None, "'datum_vlucht' mag geen lege lijst zijn"

    return waarde, None


def _controleer_4_weken(datum_ondertekening: date, vluchtdata: list[date]):
    """
    Controleert of datum_ondertekening minimaal 28 dagen vóór de vroegste
    vluchtdatum ligt. Een kortere termijn levert een waarschuwing op.

    Geeft (geslaagd: bool, melding: str) terug.
    """
    vroegste = min(vluchtdata)
    verschil = vroegste - datum_ondertekening

    if verschil < timedelta(0):
        return False, (
            f"datum_ondertekening ({datum_ondertekening}) ligt ná de vroegste vluchtdatum "
            f"({vroegste}). Een aanvraag dient voor de vluchtdatum te zijn ondertekend."
        )

    if verschil < timedelta(days=MAX_VOORUIT_WEKEN * 7):
        return False, (
            f"datum_ondertekening ({datum_ondertekening}) ligt minder dan "
            f"{MAX_VOORUIT_WEKEN * 7} dagen vóór de vroegste vluchtdatum ({vroegste}): "
            f"verschil is {verschil.days} dag(en). De aanvraag dient minimaal "
            f"{MAX_VOORUIT_WEKEN * 7} dagen vóór de eerste vluchtdatum te zijn ondertekend."
        )

    return True, (
        f"datum_ondertekening ({datum_ondertekening}) is {verschil.days} dag(en) vóór de "
        f"vroegste vluchtdatum ({vroegste}) — voldoet aan de {MAX_VOORUIT_WEKEN * 7}-dageneis."
    )


# ──────────────────────────────────────────────
# Hoofdfunctie
# ──────────────────────────────────────────────

def run(state_pad: str | Path) -> None:
    state_pad = Path(state_pad)
    state     = json.loads(state_pad.read_text(encoding="utf-8"))
    aanvraag  = state["aanvraag"]

    fouten    = []  # stoppen de pipeline
    waarschuw = []  # pipeline gaat door, maar worden gelogd

    setup_logging()
    log = LogAccumulator("tug.01_validatie")

    log(f"{'=' * 60}")
    log("TUG-ontheffingen — Stap 2: Validatie aanvraag")
    log(f"## Versie: {VERSION}")
    log(f"{'=' * 60}")

    # ── 1. Verplichte velden ──────────────────
    log("\nStap 2a: Volledigheidscheck verplichte velden ...")
    for veld in VERPLICHTE_VELDEN:
        waarde = aanvraag.get(veld)
        if waarde is None:
            fouten.append(f"Verplicht veld ontbreekt of is null: '{veld}'")
            log(f"  ✗ {veld}: ontbreekt")
        elif isinstance(waarde, list) and len(waarde) == 0:
            fouten.append(f"Verplicht veld is een lege lijst: '{veld}'")
            log(f"  ✗ {veld}: lege lijst")
        else:
            log(f"  ✓ {veld}")

    # ── 2. Normalisatie datum_vlucht ──────────
    log("\nStap 2b: Normalisatie datum_vlucht ...")
    datum_vlucht_lijst, norm_fout = _normaliseer_datum_vlucht(aanvraag)
    if norm_fout:
        fouten.append(norm_fout)
        log(f"  ✗ {norm_fout}")
    elif datum_vlucht_lijst is not None:
        aanvraag["datum_vlucht"] = datum_vlucht_lijst
        log(f"  ✓ datum_vlucht genormaliseerd naar lijst: {datum_vlucht_lijst}")

    # ── 3. Datum-formaat vluchtdata ───────────
    vluchtdata_parsed = []
    if datum_vlucht_lijst:
        log("\nStap 2c: Validatie vluchtdata (YYYY-MM-DD) ...")
        for d in datum_vlucht_lijst:
            parsed, fout = _parse_datum(d, "datum_vlucht")
            if fout:
                fouten.append(fout)
                log(f"  ✗ {fout}")
            else:
                vluchtdata_parsed.append(parsed)
                log(f"  ✓ {d}")

    # ── 4. Datum-formaat ondertekening ────────
    datum_ondertekening = None
    if aanvraag.get("datum_ondertekening"):
        log("\nStap 2d: Validatie datum_ondertekening ...")
        datum_ondertekening, fout = _parse_datum(
            aanvraag["datum_ondertekening"], "datum_ondertekening"
        )
        if fout:
            fouten.append(fout)
            log(f"  ✗ {fout}")
        else:
            log(f"  ✓ datum_ondertekening: {datum_ondertekening}")

    # ── 5. 4-weken-regel ─────────────────────
    vierw_ok = None
    vierw_melding = None
    if datum_ondertekening and vluchtdata_parsed:
        log(f"\nStap 2e: 4-weken-regel (max. {MAX_VOORUIT_WEKEN} weken voor vroegste vlucht) ...")
        vierw_ok, vierw_melding = _controleer_4_weken(datum_ondertekening, vluchtdata_parsed)
        if vierw_ok:
            log(f"  ✓ {vierw_melding}")
        else:
            waarschuw.append(vierw_melding)
            log(f"  ✗ {vierw_melding}")

    # ── 6. Luchtvaartuigen-structuur ──────────
    lv_lijst = aanvraag.get("luchtvaartuigen")
    if isinstance(lv_lijst, list) and lv_lijst:
        log("\nStap 2f: Validatie luchtvaartuigen ...")
        for i, lv in enumerate(lv_lijst):
            if not isinstance(lv, dict):
                fouten.append(f"luchtvaartuigen[{i}] is geen object")
                log(f"  ✗ luchtvaartuigen[{i}]: geen object")
                continue
            if not lv.get("registratie"):
                fouten.append(f"luchtvaartuigen[{i}]: 'registratie' ontbreekt")
                log(f"  ✗ luchtvaartuigen[{i}]: 'registratie' ontbreekt")
            else:
                log(f"  ✓ luchtvaartuigen[{i}]: {lv['registratie']}")

    # ── Samenvatting ──────────────────────────
    log(f"\n{'─' * 60}")
    geslaagd = len(fouten) == 0
    if geslaagd:
        log(f"Validatie geslaagd — {len(waarschuw)} waarschuwing(en).")
    else:
        log(f"Validatie MISLUKT — {len(fouten)} fout(en), {len(waarschuw)} waarschuwing(en).")
        for f in fouten:
            log(f"  FOUT: {f}")
    log(f"{'=' * 60}")

    # ── State bijwerken ───────────────────────
    state["aanvraag"] = aanvraag  # genormaliseerde datum_vlucht opslaan
    state["validatie"] = {
        "geslaagd":         geslaagd,
        "fouten":           fouten,
        "waarschuwingen":   waarschuw,
        "4_weken_ok":       vierw_ok,
        "4_weken_melding":  vierw_melding,
        "log_regels":       log.lines,
    }

    state.setdefault("logboek", []).append({
        "stap":     "01_validatie",
        "tijdstip": __import__("datetime").datetime.now().isoformat(),
        "niveau":   "fout" if not geslaagd else ("waarschuwing" if waarschuw else "info"),
        "bericht":  (
            "Validatie geslaagd." if geslaagd
            else f"Validatie mislukt: {'; '.join(fouten)}"
        ),
    })

    state_pad.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    if not geslaagd:
        logging.getLogger("tug.01_validatie").error(
            "Pipeline gestopt: aanvraag voldoet niet aan de vereisten."
        )
        sys.exit(1)

    logging.getLogger("tug.01_validatie").info(f"State geschreven naar {state_pad}")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 2:
        setup_logging()
        logging.getLogger("tug.01_validatie").error(
            "Gebruik: python tug_01_validatie.py tug_state.json"
        )
        sys.exit(1)
    run(sys.argv[1])
