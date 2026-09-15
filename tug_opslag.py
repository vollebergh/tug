"""
tug_opslag.py -- Lokale bestanden met aanvraag- en adresgegevens

`tug_state.json` bevat tijdens een run de aanvraag en alle gevonden adressen, de
tijdelijke aanvraag-JSON van de grafische schil de aanvraag zelf. Beide worden
hier aangemaakt met rechten die alleen de eigenaar toestaan (0o600), ook als het
bestand al bestond met ruimere rechten.
"""

import json
import os
from pathlib import Path
from typing import Any

PRIVE_RECHTEN = 0o600


def schrijf_prive(pad: Path, tekst: str) -> None:
    """Schrijf tekst naar een bestand dat alleen de eigenaar mag lezen.

    De rechten worden gezet vóórdat er inhoud in komt; een bestaand bestand met
    ruimere rechten wordt eerst ingeperkt. Onder Windows kent `os.chmod` alleen
    het alleen-lezen-kenmerk; daar bepaalt het profiel van de gebruiker de toegang.
    """
    fd = os.open(pad, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVE_RECHTEN)
    if hasattr(os, "fchmod"):
        os.fchmod(fd, PRIVE_RECHTEN)
    with os.fdopen(fd, "w", encoding="utf-8") as bestand:
        bestand.write(tekst)


def schrijf_state(pad: Path, state: dict[str, Any]) -> None:
    """Schrijf de pipeline-state (of een aanvraag) als JSON met privérechten."""
    schrijf_prive(pad, json.dumps(state, ensure_ascii=False, indent=2))


def wis_state(pad: Path) -> None:
    """Overschrijf de state met een leeg object — het teken dat er geen run loopt."""
    schrijf_prive(pad, "{}")
