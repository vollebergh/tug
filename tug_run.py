"""
tug_run.py -- Orchestrator TUG-ontheffingen workflow
Versie: zie git (workflowversie = korte commit-hash, zie tug_config.VERSION)

Gebruik:
    python tug_run.py aanvraag1.json [aanvraag2.json ...]

Elke JSON levert een eigen HTML- en PDF-output. Het veld "naam" in de JSON
wordt opgenomen in de uitvoerbestandsnamen zodat input en output koppelbaar zijn.

Optioneel: herstart één aanvraag vanaf een stap (state-bestand moet al bestaan):
    python tug_run.py tug_state.json --vanaf 05

Exitcodes:
    0  alle aanvragen volledig getoetst
    1  een aanvraag is niet verwerkt (onverwerkbare invoer of een afgebroken stap)
    2  alle aanvragen verwerkt, maar bij minstens één is een toetsingsrelevante bron
       niet volledig geraadpleegd — het rapport meldt welke
    130/143  de run is onderbroken (Ctrl+C / beëindigd)

`tug_state.json` bevat tijdens een run de aanvraag en alle gevonden adressen. Het
wordt na elke aanvraag gewist — na succes, na een afgebroken stap, en ook als de
run zelf wordt onderbroken.
"""

import sys

# Eerst naar de projectomgeving, vóór de imports hieronder: een andere interpreter
# heeft die pakketten mogelijk niet (tug_omgeving gebruikt alleen de standaardbibliotheek).
if __name__ == "__main__":
    from tug_omgeving import zorg_voor_projectomgeving
    zorg_voor_projectomgeving()

import json
import logging
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from tug_aanvraag import controleer_structuur
from tug_bronstatus import beschrijf, onvolledige_toetsing
from tug_config import VERSION
from tug_logging import setup_logging
from tug_omgeving import beveiligingsmeldingen
from tug_opslag import schrijf_state, wis_state

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Initialiseer logging één keer voor de hele pipeline (subprocessen erven niet,
# maar elk subprocess kan eigen setup aanroepen via tug_logging.setup_logging).
setup_logging()
_logger = logging.getLogger("tug.run")

SCRIPT_DIR = Path(__file__).parent
STATE_PAD  = SCRIPT_DIR / "tug_state.json"

# Stappen in volgorde: (script, beschrijving)
STAPPEN = [
    ("tug_01_validatie.py",     "Stap 2  — Validatie aanvraag"),
    ("tug_02_classificatie.py", "Stap 3  — Classificatie luchtvaartuigen"),
    ("tug_03_ruimtelijk.py",    "Stap 4  — Ruimtelijke analyse"),
    ("tug_05_output.py",        "Stap 6  — PDF/HTML-output"),
]

EXIT_ONVOLLEDIG = 2


def _log(tekst: str) -> None:
    _logger.info(f"[tug_run] {tekst}")


class _InvoerFout(Exception):
    """Een invoerbestand of aanvraag is niet verwerkbaar."""


class _Onderbroken(BaseException):
    """De run is van buitenaf beëindigd (SIGTERM, of CTRL_BREAK onder Windows).

    Afgeleid van BaseException, net als KeyboardInterrupt: een `except Exception`
    onderweg mag een stopverzoek niet inslikken.
    """

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


def _stop_bij_signaal() -> None:
    def stop(signum, _frame):
        raise _Onderbroken(signum)

    for naam in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, naam):
            signal.signal(getattr(signal, naam), stop)


def _lees_aanvragen(pad: Path) -> list[dict]:
    """Leest een JSON-bestand en retourneert een lijst van aanvraag-dicts.

    Ondersteunt twee formaten:
      - Enkel object  { ... }        → lijst met één element
      - Array         [ {...}, ... ] → lijst met alle elementen
    """
    try:
        data = json.loads(pad.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as fout:
        raise _InvoerFout(f"{pad}: niet te lezen als JSON ({fout})") from fout
    if isinstance(data, list):
        return data
    return [data]


def _label(aanvraag: object, pad: Path, nr: int) -> str:
    naam = aanvraag.get("naam") if isinstance(aanvraag, dict) else None
    return naam if isinstance(naam, str) and naam.strip() else f"{pad.stem}[{nr}]"


def _initialiseer_state(aanvraag: dict, label: str) -> None:
    """Schrijft een verse state op basis van het aanvraag-dict."""
    state = {
        "aanvraag": aanvraag,
        "logboek":  [{
            "stap":     "run",
            "tijdstip": datetime.now().isoformat(),
            "niveau":   "info",
            "bericht":  f"Pipeline gestart voor aanvraag '{label}'.",
        }],
    }
    schrijf_state(STATE_PAD, state)
    _log(f"State geïnitialiseerd: {STATE_PAD.name}")


class _StapFout(Exception):
    """Wordt gegooid als een pipeline-stap mislukt; bevat de exit-code."""
    def __init__(self, script_naam, returncode):
        self.script_naam = script_naam
        self.returncode  = returncode
        super().__init__(f"{script_naam} afgebroken (exit {returncode})")


def _voer_stap_uit(script_naam, beschrijving):
    script = SCRIPT_DIR / script_naam
    _log(f"Start: {beschrijving} ({script_naam})")
    env = {**os.environ, "PYTHONUTF8": "1"}
    result = subprocess.run(  # noqa: S603 — eigen interpreter en vast stapscript
        [sys.executable, str(script), str(STATE_PAD)],
        capture_output=False,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        _log(f"FOUT: {script_naam} afgebroken (exit {result.returncode}).")
        raise _StapFout(script_naam, result.returncode)
    _log(f"Klaar: {beschrijving}")


def _parse_args():
    args             = sys.argv[1:]
    vanaf            = None
    invoer_bestanden = []

    i = 0
    while i < len(args):
        if args[i] == "--vanaf" and i + 1 < len(args):
            vanaf = args[i + 1]
            i += 2
        else:
            invoer_bestanden.append(args[i])
            i += 1

    return invoer_bestanden, vanaf


def _onvolledige_bronnen() -> list[dict]:
    """De toetsingsrelevante bronnen die in deze run niet volledig zijn geraadpleegd."""
    try:
        return onvolledige_toetsing(json.loads(STATE_PAD.read_text(encoding="utf-8")))
    except (OSError, ValueError) as fout:
        _log(f"FOUT: bronstatus niet te lezen uit {STATE_PAD.name} ({fout}); "
             f"de toetsing geldt als onvolledig.")
        return [{"label": "bronstatus", "status": "mislukt", "melding": "niet te lezen"}]


def _verwerk_aanvraag(aanvraag: dict, label: str, vanaf=None) -> int:
    """Voer de volledige pipeline uit voor één aanvraag-dict.

    Retourneert 0 bij een volledige toetsing en EXIT_ONVOLLEDIG als een
    toetsingsrelevante bron niet volledig is geraadpleegd. Gooit _StapFout als een
    stap mislukt. De state wordt in alle gevallen gewist, ook bij een onderbreking.
    """
    if vanaf:
        if not STATE_PAD.exists():
            raise _InvoerFout(f"--vanaf vereist een bestaand state-bestand ({STATE_PAD}).")
        _log(f"Herstart vanaf stap '{vanaf}' — bestaande state wordt hergebruikt.")
        te_draaien = [(s, b) for s, b in STAPPEN if Path(s).stem.split("_")[1] >= vanaf]
    else:
        _initialiseer_state(aanvraag, label)
        te_draaien = STAPPEN

    _log(f"Workflowversie: {VERSION}")
    _log(f"Pipeline: {len(te_draaien)} stap(pen) te verwerken.")

    geslaagd = False
    try:
        for script_naam, beschrijving in te_draaien:
            _voer_stap_uit(script_naam, beschrijving)
        onvolledig = _onvolledige_bronnen()
        geslaagd = True
    finally:
        wis_state(STATE_PAD)
        _log(f"State gewist ({STATE_PAD.name}"
             f"{'' if geslaagd else ', na fout of onderbreking'}) — dataveiligheid.")

    if onvolledig:
        _log("Pipeline voltooid, maar de toetsing is ONVOLLEDIG:")
        for uitkomst in onvolledig:
            _log(f"  ✗ {beschrijf(uitkomst)}")
        return EXIT_ONVOLLEDIG
    _log("Pipeline voltooid.")
    return 0


def _verzamel_aanvragen(invoer_bestanden: list[str], vanaf) -> tuple[list, list]:
    """(verwerkbare (aanvraag, label)-paren, niet-verwerkbare (label, fout)-paren)."""
    aanvragen, afgewezen = [], []
    if vanaf:
        try:
            state = json.loads(STATE_PAD.read_text(encoding="utf-8"))
            aanvraag = state.get("aanvraag") if isinstance(state, dict) else None
        except (OSError, ValueError) as fout:
            return [], [(STATE_PAD.name, f"state niet te lezen ({fout})")]
        invoer = [(aanvraag, _label(aanvraag, STATE_PAD, 1))]
    else:
        invoer = []
        for bestand in invoer_bestanden:
            pad = Path(bestand)
            try:
                gelezen = _lees_aanvragen(pad)
            except _InvoerFout as fout:
                afgewezen.append((pad.name, str(fout)))
                continue
            invoer += [(a, _label(a, pad, nr)) for nr, a in enumerate(gelezen, 1)]

    for aanvraag, label in invoer:
        fouten = controleer_structuur(aanvraag, label)
        if fouten:
            afgewezen.append((label, "; ".join(fouten)))
        else:
            aanvragen.append((aanvraag, label))
    return aanvragen, afgewezen


def main() -> int:
    invoer_bestanden, vanaf = _parse_args()

    if not invoer_bestanden:
        _logger.error(
            "Gebruik: python tug_run.py aanvraag.json [meer.json ...] [--vanaf STAPNUMMER]"
        )
        _logger.error("  aanvraag.json   Enkel object of array van objecten; "
                      "meerdere bestanden toegestaan")
        _logger.error("  --vanaf 05      Herstart vanaf stap 05 (slechts één aanvraag, "
                      "state moet al bestaan)")
        return 1

    for melding in beveiligingsmeldingen():
        _log(f"WAARSCHUWING: {melding}")

    aanvragen, afgewezen = _verzamel_aanvragen(invoer_bestanden, vanaf)
    for label, reden in afgewezen:
        _log(f"FOUT: '{label}' niet verwerkt — {reden}")

    if vanaf and len(invoer_bestanden) > 1:
        _logger.error("FOUT: --vanaf kan alleen worden gebruikt bij één aanvraag.")
        return 1

    totaal     = len(aanvragen) + len(afgewezen)
    volledig:   list[str] = []
    onvolledig: list[str] = []
    mislukt    = [(label, "onverwerkbare invoer") for label, _ in afgewezen]
    _log(f"Totaal te verwerken aanvragen: {len(aanvragen)} (afgewezen: {len(afgewezen)})")

    for nr, (aanvraag, label) in enumerate(aanvragen, start=1):
        _log(f"=== Aanvraag {nr}/{len(aanvragen)}: {label} ===")
        try:
            code = _verwerk_aanvraag(aanvraag, label, vanaf=vanaf)
        except _InvoerFout as fout:
            _log(f"FOUT: {fout}")
            mislukt.append((label, str(fout)))
            continue
        except _StapFout as fout:
            mislukt.append((label, str(fout)))
            if totaal == 1:
                # Enkelvoudige aanvraag: stoppen met de foutcode van de stap
                return fout.returncode
            _log(f"Aanvraag '{label}' overgeslagen — doorgaan met volgende.")
            continue
        (onvolledig if code == EXIT_ONVOLLEDIG else volledig).append(label)

    if totaal > 1:
        _log("")
        _log(f"{'=' * 60}")
        _log(f"Batchrun voltooid: {len(volledig)} volledig getoetst, {len(onvolledig)} "
             f"onvolledig getoetst, {len(mislukt)} niet verwerkt (van {totaal}).")
        for lbl in volledig:
            _log(f"  ✓ {lbl}")
        for lbl in onvolledig:
            _log(f"  ⚠ {lbl}  (onvolledige toetsing — zie rapport)")
        for lbl, reden in mislukt:
            _log(f"  ✗ {lbl}  ({reden})")
        _log(f"{'=' * 60}")

    if mislukt:
        return 1
    return EXIT_ONVOLLEDIG if onvolledig else 0


if __name__ == "__main__":
    _stop_bij_signaal()
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _log("Run onderbroken (Ctrl+C).")
        sys.exit(130)
    except _Onderbroken as stop:
        _log(f"Run beëindigd (signaal {stop.signum}).")
        sys.exit(143)
