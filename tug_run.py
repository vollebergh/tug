"""
tug_run.py -- Orchestrator TUG-ontheffingen workflow
Versie: zie git (workflowversie = korte commit-hash, zie tug_config.VERSION)

Gebruik:
    python tug_run.py aanvraag1.json [aanvraag2.json ...]

Elke JSON levert een eigen HTML- en PDF-output. Het veld "naam" in de JSON
wordt opgenomen in de uitvoerbestandsnamen zodat input en output koppelbaar zijn.

Optioneel: herstart één aanvraag vanaf een stap (state-bestand moet al bestaan):
    python tug_run.py tug_state.json --vanaf 05
"""

import json
import logging
import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

from tug_config import VERSION
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


def _lees_aanvragen(pad: Path) -> list[dict]:
    """Leest een JSON-bestand en retourneert een lijst van aanvraag-dicts.

    Ondersteunt twee formaten:
      - Enkel object  { ... }        → lijst met één element
      - Array         [ {...}, ... ] → lijst met alle elementen
    """
    data = json.loads(pad.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return [data]


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
    STATE_PAD.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
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
    result = subprocess.run(
        [sys.executable, str(script), str(STATE_PAD)],
        capture_output=False,
        env=env,
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


def _verwerk_aanvraag(aanvraag: dict, label: str, vanaf=None) -> None:
    """Voer de volledige pipeline uit voor één aanvraag-dict.
    Gooit _StapFout als een stap mislukt (state wordt alsnog gewist).
    """
    if vanaf:
        if not STATE_PAD.exists():
            _logger.error(f"FOUT: --vanaf vereist een bestaand state-bestand ({STATE_PAD}).")
            sys.exit(1)
        _log(f"Herstart vanaf stap '{vanaf}' — bestaande state wordt hergebruikt.")
        te_draaien = [(s, b) for s, b in STAPPEN if Path(s).stem.split("_")[1] >= vanaf]
    else:
        _initialiseer_state(aanvraag, label)
        te_draaien = STAPPEN

    _log(f"Workflowversie: {VERSION}")
    _log(f"Pipeline: {len(te_draaien)} stap(pen) te verwerken.")

    try:
        for script_naam, beschrijving in te_draaien:
            _voer_stap_uit(script_naam, beschrijving)
    except _StapFout:
        STATE_PAD.write_text("{}", encoding="utf-8")
        _log(f"State gewist na fout ({STATE_PAD.name}) — dataveiligheid.")
        raise

    _log("Pipeline voltooid.")
    STATE_PAD.write_text("{}", encoding="utf-8")
    _log(f"State gewist ({STATE_PAD.name}) — dataveiligheid.")


if __name__ == "__main__":
    invoer_bestanden, vanaf = _parse_args()

    if not invoer_bestanden:
        _logger.error(
            "Gebruik: python tug_run.py aanvraag.json [meer.json ...] [--vanaf STAPNUMMER]"
        )
        _logger.error("  aanvraag.json   Enkel object of array van objecten; meerdere bestanden toegestaan")
        _logger.error("  --vanaf 05      Herstart vanaf stap 05 (slechts één aanvraag, state moet al bestaan)")
        sys.exit(1)

    # Uitvouwen: elk bestand kan een enkel object of een array bevatten
    aanvragen: list[tuple[dict, str]] = []  # (aanvraag-dict, label)
    for invoer in invoer_bestanden:
        pad = Path(invoer)
        for aanvraag in _lees_aanvragen(pad):
            label = aanvraag.get("naam") or pad.stem
            aanvragen.append((aanvraag, label))

    if vanaf and len(aanvragen) > 1:
        _logger.error("FOUT: --vanaf kan alleen worden gebruikt bij één aanvraag.")
        sys.exit(1)

    totaal   = len(aanvragen)
    geslaagd = []
    mislukt  = []
    _log(f"Totaal te verwerken aanvragen: {totaal}")

    for nr, (aanvraag, label) in enumerate(aanvragen, start=1):
        _log(f"=== Aanvraag {nr}/{totaal}: {label} ===")
        try:
            _verwerk_aanvraag(aanvraag, label, vanaf=vanaf)
            geslaagd.append(label)
        except _StapFout as e:
            mislukt.append((label, str(e)))
            if totaal == 1:
                # Enkelvoudige aanvraag: origineel gedrag — stoppen met foutcode
                sys.exit(e.returncode)
            _log(f"Aanvraag '{label}' overgeslagen — doorgaan met volgende.")

    # ── Eindsamenvatting (alleen bij batch) ──
    if totaal > 1:
        _log(f"")
        _log(f"{'=' * 60}")
        _log(f"Batchrun voltooid: {len(geslaagd)}/{totaal} geslaagd, {len(mislukt)} mislukt.")
        for lbl in geslaagd:
            _log(f"  ✓ {lbl}")
        for lbl, fout in mislukt:
            _log(f"  ✗ {lbl}  ({fout})")
        _log(f"{'=' * 60}")
        if mislukt:
            sys.exit(1)
