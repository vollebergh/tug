"""
tug_01_validatie.py -- Stap 2 TUG-ontheffingen workflow
Versie: zie git (workflowversie = korte commit-hash, zie tug_config.VERSION)

Volledigheidscheck van de aanvraag:
  - Controleert de structuur (tug_aanvraag; fataal bij een onverwerkbare aanvraag)
  - Controleert aanwezigheid van verplichte velden
  - Normaliseert datum_vlucht naar een lijst (str → [str])
  - Controleert of datum_ondertekening minimaal MIN_INDIENTERMIJN_DAGEN vóór
    de vroegste vluchtdatum ligt

Invoer : tug_state.json  (aanvraag)
Uitvoer: tug_state.json  (sectie validatie gevuld; aanvraag.datum_vlucht genormaliseerd)
"""

import json
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from tug_aanvraag import controleer_structuur
from tug_config import (
    MAX_PUNT_AFSTAND_M, MIN_INDIENTERMIJN_DAGEN, REGISTRATIE_PATROON, VERSION,
)
from tug_geo import max_onderlinge_afstand, puntafstand_melding, puntlocaties
from tug_logging import LogAccumulator, setup_logging
from tug_opslag import schrijf_state

VERPLICHTE_VELDEN = [
    "soort_ontheffing",
    "luchtvaartuigen",
    "coord_lat",
    "coord_lon",
    "datum_ondertekening",
    "tijdstip_ondertekening",
]


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


def _controleer_indientermijn(
    datum_ondertekening: date, vluchtdata: list[date],
) -> tuple[bool, str]:
    """Toetst de indientermijn: is er genoeg tijd tussen ondertekening en vlucht?

    De aanvraag moet minimaal MIN_INDIENTERMIJN_DAGEN vóór de vroegste
    vluchtdatum zijn ondertekend. Een kortere termijn is geen technische fout
    maar een gebrek: de pipeline gaat door en het rapport meldt het.

    Geeft (voldoet: bool, melding: str) terug.
    """
    vroegste = min(vluchtdata)
    verschil = vroegste - datum_ondertekening

    if verschil < timedelta(0):
        return False, (
            f"datum_ondertekening ({datum_ondertekening}) ligt ná de vroegste vluchtdatum "
            f"({vroegste}). Een aanvraag dient voor de vluchtdatum te zijn ondertekend."
        )

    if verschil < timedelta(days=MIN_INDIENTERMIJN_DAGEN):
        return False, (
            f"datum_ondertekening ({datum_ondertekening}) ligt minder dan "
            f"{MIN_INDIENTERMIJN_DAGEN} dagen vóór de vroegste vluchtdatum ({vroegste}): "
            f"verschil is {verschil.days} dag(en). De aanvraag dient minimaal "
            f"{MIN_INDIENTERMIJN_DAGEN} dagen vóór de eerste vluchtdatum te zijn ondertekend."
        )

    return True, (
        f"datum_ondertekening ({datum_ondertekening}) is {verschil.days} dag(en) vóór de "
        f"vroegste vluchtdatum ({vroegste}) — voldoet aan de "
        f"{MIN_INDIENTERMIJN_DAGEN}-dageneis."
    )


# ──────────────────────────────────────────────
# Hoofdfunctie
# ──────────────────────────────────────────────

def run(state_pad: str | Path) -> None:
    state_pad = Path(state_pad)
    state     = json.loads(state_pad.read_text(encoding="utf-8"))
    aanvraag  = state["aanvraag"]

    fouten           = []  # gereserveerd voor onherstelbare technische fouten
    waarschuw        = []  # pipeline gaat door; worden rood getoond in logboek
    ontbrekende_velden = []  # ontbrekende verplichte velden (subset van waarschuw)

    setup_logging()
    log = LogAccumulator("tug.01_validatie")

    log(f"{'=' * 60}")
    log("TUG-ontheffingen — Stap 2: Validatie aanvraag")
    log(f"## Workflowversie: {VERSION}")
    log(f"{'=' * 60}")

    # ── 0. Structuur ──────────────────────────
    # tug_run toetst dit al bij het inlezen; hier nogmaals, zodat de stap ook
    # losstaand (en bij --vanaf op een bewerkte state) niets onverwerkbaars doorlaat.
    log("\nStap 2-0: Structuur van de aanvraag ...")
    structuurfouten = controleer_structuur(aanvraag)
    if structuurfouten:
        for melding in structuurfouten:
            log(f"  ✗ {melding}")
        fouten.extend(structuurfouten)
        aanvraag = aanvraag if isinstance(aanvraag, dict) else {}
    else:
        log("  ✓ structuur en typen")

    # ── 1. Verplichte velden ──────────────────
    log("\nStap 2a: Volledigheidscheck verplichte velden ...")
    for veld in VERPLICHTE_VELDEN:
        waarde = aanvraag.get(veld)
        if waarde is None:
            melding = f"Verplicht veld ontbreekt of is null: '{veld}'"
            waarschuw.append(melding)
            ontbrekende_velden.append(veld)
            log(f"  ✗ {veld}: ontbreekt")
        elif isinstance(waarde, list) and len(waarde) == 0:
            melding = f"Verplicht veld is een lege lijst: '{veld}'"
            waarschuw.append(melding)
            ontbrekende_velden.append(veld)
            log(f"  ✗ {veld}: lege lijst")
        else:
            log(f"  ✓ {veld}")

    # ── 1b. Speciale controle: datum_vlucht ──
    datum_vlucht_ontbreekt = aanvraag.get("datum_vlucht") is None
    if datum_vlucht_ontbreekt:
        melding = (
            "datum_vlucht is niet ingevuld — vluchtdatum onbekend. "
            "Aanvraag wordt verwerkt; vergunningverlener dient de vluchtdatum\n"
            "handmatig aan te vullen."
        )
        waarschuw.append(melding)
        log("\n  ✗ ONBEKENDE VLUCHTDATUM — datum_vlucht is null of ontbreekt.")
        log(f"  ✗ {melding}")
    else:
        log("  ✓ datum_vlucht")

    # ── 2. Normalisatie datum_vlucht ──────────
    log("\nStap 2b: Normalisatie datum_vlucht ...")
    datum_vlucht_lijst = None
    if datum_vlucht_ontbreekt:
        log("  — datum_vlucht ontbreekt — normalisatie overgeslagen.")
    else:
        datum_vlucht_lijst, norm_fout = _normaliseer_datum_vlucht(aanvraag)
        if norm_fout:
            waarschuw.append(norm_fout)
            log(f"  ✗ {norm_fout}")
        elif datum_vlucht_lijst is not None:
            aanvraag["datum_vlucht"] = datum_vlucht_lijst
            log(f"  ✓ datum_vlucht genormaliseerd naar lijst: {datum_vlucht_lijst}")

    # ── 3. Datum-formaat vluchtdata ───────────
    vluchtdata_parsed = []
    if datum_vlucht_ontbreekt:
        log("\nStap 2c: Validatie vluchtdata — overgeslagen (datum_vlucht ontbreekt).")
    elif datum_vlucht_lijst:
        log("\nStap 2c: Validatie vluchtdata (YYYY-MM-DD) ...")
        for d in datum_vlucht_lijst:
            parsed, fout = _parse_datum(d, "datum_vlucht")
            if fout:
                waarschuw.append(fout)
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
            waarschuw.append(fout)
            log(f"  ✗ {fout}")
        else:
            log(f"  ✓ datum_ondertekening: {datum_ondertekening}")

    # ── 5. Indientermijn ─────────────────────
    termijn_ok = None
    termijn_melding = None
    if datum_ondertekening and vluchtdata_parsed:
        log(f"\nStap 2e: indientermijn (minimaal {MIN_INDIENTERMIJN_DAGEN} dagen "
            f"vóór de vroegste vlucht) ...")
        termijn_ok, termijn_melding = _controleer_indientermijn(
            datum_ondertekening, vluchtdata_parsed
        )
        if termijn_ok:
            log(f"  ✓ {termijn_melding}")
        else:
            waarschuw.append(termijn_melding)
            log(f"  ✗ {termijn_melding}")

    # ── 6. Luchtvaartuigen-structuur ──────────
    lv_lijst = aanvraag.get("luchtvaartuigen")
    if isinstance(lv_lijst, list) and lv_lijst:
        log("\nStap 2f: Validatie luchtvaartuigen ...")
        for i, lv in enumerate(lv_lijst):
            if not isinstance(lv, dict):
                waarschuw.append(f"luchtvaartuigen[{i}] is geen object")
                log(f"  ✗ luchtvaartuigen[{i}]: geen object")
                continue
            registratie = lv.get("registratie")
            if not registratie:
                waarschuw.append(f"luchtvaartuigen[{i}]: 'registratie' ontbreekt")
                log(f"  ✗ luchtvaartuigen[{i}]: 'registratie' ontbreekt")
            elif not re.match(REGISTRATIE_PATROON, str(registratie).strip().upper()):
                melding = (f"luchtvaartuigen[{i}]: registratiekenmerk {registratie!r} heeft niet "
                           f"de vorm van een kenmerk (bijv. PH-ECE)")
                waarschuw.append(melding)
                log(f"  ✗ {melding}")
            else:
                log(f"  ✓ luchtvaartuigen[{i}]: {registratie}")

    # ── 7. Puntlocaties (één of meer) ─────────
    if "coord_lat" in aanvraag and "coord_lon" in aanvraag and not structuurfouten:
        log("\nStap 2g: Validatie puntlocaties ...")
        try:
            punten = puntlocaties(aanvraag)
            for i, (la, lo) in enumerate(punten, 1):
                log(f"  ✓ puntlocatie {i}: lat={la:.6f}, lon={lo:.6f}")
            if len(punten) > 1:
                afstand = max_onderlinge_afstand(punten)
                if afstand > MAX_PUNT_AFSTAND_M:
                    waarschuw.append(puntafstand_melding(afstand))
                    log(f"  ✗ {puntafstand_melding(afstand)}")
                else:
                    log(f"  ✓ {puntafstand_melding(afstand)}")
        except ValueError as e:
            fouten.append(str(e))
            log(f"  ✗ {e}")

    # ── Samenvatting ──────────────────────────
    log(f"\n{'─' * 60}")
    geslaagd = len(fouten) == 0
    if fouten:
        log(f"Validatie MISLUKT — {len(fouten)} fout(en): {'; '.join(fouten)}")
    elif not waarschuw:
        log("Validatie geslaagd — 0 waarschuwing(en).")
    else:
        log(f"Validatie geslaagd — {len(waarschuw)} waarschuwing(en).")
    log(f"{'=' * 60}")

    # ── State bijwerken ───────────────────────
    state["aanvraag"] = aanvraag  # genormaliseerde datum_vlucht opslaan
    state["validatie"] = {
        "geslaagd":               geslaagd,
        "fouten":                 fouten,
        "waarschuwingen":         waarschuw,
        "ontbrekende_velden":     ontbrekende_velden,
        "datum_vlucht_ontbreekt": datum_vlucht_ontbreekt,
        "indientermijn_ok":       termijn_ok,
        "indientermijn_melding":  termijn_melding,
        "log_regels":             log.lines,
    }

    state.setdefault("logboek", []).append({
        "stap":     "01_validatie",
        "tijdstip": datetime.now().isoformat(),
        "niveau":   "waarschuwing" if waarschuw else "info",
        "bericht":  (
            "Validatie geslaagd." if not waarschuw
            else (f"Validatie geslaagd met {len(waarschuw)} waarschuwing(en): "
                  f"{'; '.join(waarschuw[:2])}")
        ),
    })

    schrijf_state(state_pad, state)
    logging.getLogger("tug.01_validatie").info(f"State geschreven naar {state_pad}")
    if fouten:
        logging.getLogger("tug.01_validatie").error(f"FOUT: {'; '.join(fouten)}")
        sys.exit(1)


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
