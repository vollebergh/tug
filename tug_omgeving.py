"""
tug_omgeving.py -- In welke omgeving draait de pipeline, en is die gecontroleerd?

Twee controles bij het starten van `tug_run.py` en `tug_gui.py`:

1. **De projectomgeving.** De pipeline hoort te draaien in de `.venv` die
   `install.py` aanmaakt, met de gepinde en gehashte afhankelijkheden. Wordt een
   script met een andere interpreter gestart (bijvoorbeeld `python` van het PATH),
   dan start het zichzelf opnieuw in `.venv`. Ontbreekt `.venv`, dan volgt een
   waarschuwing; de run zelf blijft dan mogelijk.
2. **De beveiligingscontrole.** `beveiligingscontrole.py` legt zijn uitkomst vast
   in `.beveiligingscontrole.json`. Ontbreekt die, is hij te oud of had hij
   bevindingen, dan meldt de pipeline dat bij de start. Kwetsbaarheden in
   afhankelijkheden verschijnen ook zonder codewijziging; zo blijft een verlopen
   controle niet onopgemerkt.
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from tug_config import BEVEILIGINGSCONTROLE_MAX_DAGEN, BEVEILIGINGSCONTROLE_PAD

PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR    = PROJECT_DIR / ".venv"
_HERSTART   = "TUG_HERSTART_IN_VENV"


def _meld(tekst: str) -> None:
    """Melding vóórdat logging is ingericht (dit draait als eerste in een script)."""
    sys.stderr.write(tekst + "\n")
    sys.stderr.flush()


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def draait_in_projectomgeving() -> bool:
    return Path(sys.prefix).resolve() == VENV_DIR.resolve()


def zorg_voor_projectomgeving() -> None:
    """Start het huidige script opnieuw in `.venv` als het daar niet al draait.

    Een Ctrl+C tijdens de herstarte run gaat naar beide processen; dit proces
    wacht dan tot de run zelf is afgerond en opgeruimd, in plaats van hem af te
    breken.
    """
    if draait_in_projectomgeving():
        return
    python = venv_python()
    if not python.exists() or os.environ.get(_HERSTART):
        _meld(f"WAARSCHUWING: de pipeline draait niet in de projectomgeving {VENV_DIR} "
              f"maar met {sys.executable}. Installeer de omgeving met 'python install.py'; "
              f"alleen daar zijn de afhankelijkheden gepind en gecontroleerd.")
        return
    _meld(f"Herstart in de projectomgeving: {python}")
    proces = subprocess.Popen(  # noqa: S603 — vaste interpreter, eigen scriptargumenten
        [str(python), *sys.argv], env={**os.environ, _HERSTART: "1"},
    )
    while True:
        try:
            sys.exit(proces.wait())
        except KeyboardInterrupt:
            continue


def beveiligingsmeldingen(nu: datetime | None = None) -> list[str]:
    """Waarschuwingen over de laatste beveiligingscontrole; leeg als die in orde is."""
    nu = nu or datetime.now()
    if not BEVEILIGINGSCONTROLE_PAD.exists():
        return ["Er is nog geen beveiligingscontrole vastgelegd. Draai "
                "'.venv/bin/python beveiligingscontrole.py' vóór een productierun."]
    try:
        uitkomst = json.loads(BEVEILIGINGSCONTROLE_PAD.read_text(encoding="utf-8"))
        datum = datetime.fromisoformat(uitkomst["datum"])
        bevindingen = int(uitkomst.get("bevindingen", 0))
    except (OSError, ValueError, KeyError, TypeError):
        return [f"{BEVEILIGINGSCONTROLE_PAD.name} is onleesbaar; draai de beveiligingscontrole "
                f"opnieuw."]

    meldingen = []
    ouderdom = (nu - datum).days
    if ouderdom > BEVEILIGINGSCONTROLE_MAX_DAGEN:
        meldingen.append(f"De laatste beveiligingscontrole is {ouderdom} dagen oud (grens "
                         f"{BEVEILIGINGSCONTROLE_MAX_DAGEN}). Draai beveiligingscontrole.py.")
    if bevindingen:
        meldingen.append(f"De laatste beveiligingscontrole ({datum:%d-%m-%Y}) had "
                         f"{bevindingen} bevinding(en); zie {BEVEILIGINGSCONTROLE_PAD.name}.")
    return meldingen
