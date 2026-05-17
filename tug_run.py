"""
tug_run.py -- Orchestrator TUG-ontheffingen workflow
Versie: 4.1.0  |  2026-05-15

Gebruik:
    python tug_run.py aanvraag.json

Optioneel: sla een bestaand state-bestand over en herstart vanaf een stap:
    python tug_run.py tug_state.json --vanaf 05
"""

import json
import logging
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

from tug_logging import setup_logging

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


def _log(tekst: str) -> None:
    _logger.info(f"[tug_run] {tekst}")


def _initialiseer_state(aanvraag_pad):
    aanvraag = json.loads(Path(aanvraag_pad).read_text(encoding="utf-8"))
    state    = {
        "aanvraag": aanvraag,
        "logboek":  [{
            "stap":     "run",
            "tijdstip": datetime.now().isoformat(),
            "niveau":   "info",
            "bericht":  f"Pipeline gestart vanuit {Path(aanvraag_pad).name}.",
        }],
    }
    STATE_PAD.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"State geïnitialiseerd: {STATE_PAD.name}")
    return state


def _voer_stap_uit(script_naam, beschrijving):
    script = SCRIPT_DIR / script_naam
    _log(f"Start: {beschrijving} ({script_naam})")
    env = {**os.environ, "PYTHONUTF8": "1"}
    result = subprocess.run(
        [sys.executable, str(script), str(STATE_PAD)],
        capture_output=False,
        env=env,
    )
    if result.returncode != 0:
        _log(f"FOUT: {script_naam} afgebroken (exit {result.returncode}).")
        sys.exit(result.returncode)
    _log(f"Klaar: {beschrijving}")


def _parse_args():
    args     = sys.argv[1:]
    vanaf    = None
    invoer   = None

    i = 0
    while i < len(args):
        if args[i] == "--vanaf" and i + 1 < len(args):
            vanaf = args[i + 1]
            i += 2
        else:
            invoer = args[i]
            i += 1

    return invoer, vanaf


if __name__ == "__main__":
    invoer, vanaf = _parse_args()

    if not invoer:
        _logger.error("Gebruik: python tug_run.py aanvraag.json [--vanaf STAPNUMMER]")
        _logger.error("  aanvraag.json   Pad naar het aanvraagformulier (JSON)")
        _logger.error("  --vanaf 05      Herstart vanaf stap 05 (state-bestand moet al bestaan)")
        sys.exit(1)

    invoer_pad = Path(invoer)

    if vanaf:
        # Herstart: state bestaat al, alleen stappen ≥ vanaf uitvoeren
        if not STATE_PAD.exists():
            _logger.error(f"FOUT: --vanaf vereist een bestaand state-bestand ({STATE_PAD}).")
            sys.exit(1)
        _log(f"Herstart vanaf stap '{vanaf}' — bestaande state wordt hergebruikt.")
        te_draaien = [(s, b) for s, b in STAPPEN if Path(s).stem.split("_")[1] >= vanaf]
    else:
        # Normale start: initialiseer state vanuit aanvraag-JSON
        _initialiseer_state(invoer_pad)
        te_draaien = STAPPEN

    _log(f"Pipeline: {len(te_draaien)} stap(pen) te verwerken.")

    for script_naam, beschrijving in te_draaien:
        _voer_stap_uit(script_naam, beschrijving)

    _log("Pipeline voltooid.")
    _log(f"State: {STATE_PAD}")
    STATE_PAD.write_text("{}", encoding="utf-8")
    _log(f"State gewist ({STATE_PAD.name}) — dataveiligheid.")
