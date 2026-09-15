"""
install.py -- eenmalige installatie van de TUG-ontheffingen-pipeline

Gebruik:    python install.py

Maakt een virtuele omgeving (.venv) in deze map en installeert daarin de
dependencies uit requirements.txt. Vereist geen adminrechten: alles komt in
deze projectmap terecht, niet in systeempaden.

Elk pakket wordt tegen de hash in requirements.txt gecontroleerd (pip
--require-hashes), inclusief alle transitieve afhankelijkheden. Alleen kant-en-
klare wheels worden geïnstalleerd; de enige uitzondering is odfpy, dat alleen als
broncode bestaat en tegen de eveneens gehashte setuptools uit
requirements-build.txt wordt gebouwd.

Dit is bewust een .py-bestand en geen .sh/.bat: op beheerde laptops is het
uitvoeren van shellscripts vaak geblokkeerd, het draaien van een Python-bestand
via de interpreter niet.
"""

import os
import subprocess
import sys
import venv
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR    = PROJECT_DIR / ".venv"
REQS        = PROJECT_DIR / "requirements.txt"
REQS_BUILD  = PROJECT_DIR / "requirements-build.txt"

# Pakketten die alleen als broncode (sdist) bestaan en dus gebouwd moeten worden.
BRONCODE_PAKKETTEN = ["odfpy"]

# Ondergrens 3.12: pandas 3.0/numpy 2.4/pyproj eisen >=3.11, maar de nieuwste
# pyproj levert geen wheels meer voor 3.11. Bovengrens 3.15: eis van PySide6.
MIN_VERSIE = (3, 12)
MAX_VERSIE = (3, 15)
GETEST_OP  = (3, 12)


def fout(*regels):
    print("\nFOUT: " + regels[0], file=sys.stderr)
    for r in regels[1:]:
        print("      " + r, file=sys.stderr)
    sys.exit(1)


def venv_python(venv_dir: Path) -> Path:
    """Pad naar de Python-interpreter binnen de venv (platformafhankelijk)."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def controleer_python():
    huidig = sys.version_info[:2]
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")

    if huidig < MIN_VERSIE:
        fout(
            f"Python {huidig[0]}.{huidig[1]} is te oud voor deze pipeline.",
            f"Vereist is minimaal Python {MIN_VERSIE[0]}.{MIN_VERSIE[1]}.",
            "Reden: pandas 3.0, numpy 2.4 en pyproj ondersteunen oudere versies niet.",
            "Installeer Python 3.12 via https://www.python.org/downloads/ en",
            "draai dit script opnieuw met die interpreter.",
        )

    if huidig >= MAX_VERSIE:
        fout(
            f"Python {huidig[0]}.{huidig[1]} is te nieuw: PySide6 vereist "
            f"< {MAX_VERSIE[0]}.{MAX_VERSIE[1]}.",
            "Gebruik Python 3.12.",
        )

    if huidig != GETEST_OP:
        print(
            f"  Let op: getest is Python {GETEST_OP[0]}.{GETEST_OP[1]}. "
            f"Op {huidig[0]}.{huidig[1]} kan pip pakketten willen compileren als er "
            "geen kant-en-klare wheel bestaat; dat vereist een C-compiler."
        )


def maak_venv():
    py = venv_python(VENV_DIR)
    if py.exists():
        print(f"Bestaande omgeving hergebruikt: {VENV_DIR}")
        return py

    if VENV_DIR.exists():
        fout(
            f"{VENV_DIR} bestaat al maar bevat geen werkende Python.",
            "Verwijder of hernoem die map en draai dit script opnieuw.",
        )

    print(f"Omgeving aanmaken: {VENV_DIR} ...")
    try:
        venv.create(VENV_DIR, with_pip=True, upgrade_deps=False)
    except (OSError, subprocess.CalledProcessError) as e:
        fout(
            f"aanmaken van de virtuele omgeving mislukt: {e}",
            "Op Debian/Ubuntu ontbreekt vaak het pakket python3-venv.",
            "Op Windows: herinstalleer Python en vink 'pip' aan bij de onderdelen.",
        )

    if not py.exists():
        fout(f"omgeving aangemaakt maar {py} ontbreekt.")
    print("  Omgeving aangemaakt.")
    return py


def pip_install(py: Path):
    for bestand in (REQS, REQS_BUILD):
        if not bestand.exists():
            fout(f"{bestand.name} niet gevonden naast dit script.")

    print(f"\nBouwgereedschap installeren uit {REQS_BUILD.name} ...")
    resultaat = subprocess.run(  # noqa: S603 — interpreter uit de eigen venv
        [str(py), "-m", "pip", "install", "--require-virtualenv", "--require-hashes",
         "--only-binary=:all:", "-r", str(REQS_BUILD)],
        check=False,
    )
    if resultaat.returncode != 0:
        fout("installeren van het bouwgereedschap mislukt (zie de pip-uitvoer hierboven).")

    print(f"Dependencies installeren uit {REQS.name} (kan enkele minuten duren, "
          "PySide6 is een grote download) ...\n")
    resultaat = subprocess.run(  # noqa: S603 — interpreter uit de eigen venv
        [str(py), "-m", "pip", "install", "--require-virtualenv", "--require-hashes",
         "--no-build-isolation", "--only-binary=:all:",
         *(f"--no-binary={naam}" for naam in BRONCODE_PAKKETTEN),
         "-r", str(REQS)],
        check=False,
    )
    if resultaat.returncode != 0:
        fout(
            "installeren van de dependencies mislukt (zie de pip-uitvoer hierboven).",
            "Bij een bedrijfsnetwerk met proxy kan pip een proxy-instelling nodig hebben:",
            "  python -m pip install --proxy http://proxy:poort --require-hashes "
            "-r requirements.txt",
            "Meldt pip dat een hash niet overeenkomt, dan is het gedownloade pakket niet",
            "het pakket dat is vastgelegd: installeer dan níét zonder hashcontrole.",
            "Bij een SSL-fout is meestal het bedrijfscertificaat de oorzaak; vraag de",
            "IT-afdeling om het CA-bestand en zet dat in de omgevingsvariabele",
            "REQUESTS_CA_BUNDLE / PIP_CERT.",
        )


def controleer_imports(py: Path):
    """Importeer elke library eenmaal: een geslaagde pip-install garandeert niet
    dat een binaire extensie ook laadt (ontbrekende GDAL/Qt-systeembibliotheken)."""
    modules = ["PySide6.QtWidgets", "pandas", "odf", "geopandas", "shapely",
               "pyproj", "pyogrio", "requests", "defusedxml", "reportlab", "PIL"]
    print("\nImports controleren ...")
    code = "import importlib,sys\n" \
           "for m in sys.argv[1:]:\n" \
           "    try:\n" \
           "        importlib.import_module(m)\n" \
           "        print(f'  OK      {m}')\n" \
           "    except Exception as e:\n" \
           "        print(f'  MISLUKT {m}: {e}')\n" \
           "        sys.exit(1)\n"
    resultaat = subprocess.run(  # noqa: S603 — interpreter uit de eigen venv
        [str(py), "-c", code, *modules], check=False)
    if resultaat.returncode != 0:
        fout("een of meer libraries laten zich niet importeren (zie hierboven).")


def main():
    print("=" * 68)
    print("TUG-ontheffingen — installatie")
    print("=" * 68)

    controleer_python()
    py = maak_venv()
    pip_install(py)
    controleer_imports(py)

    rel = py.relative_to(PROJECT_DIR) if py.is_relative_to(PROJECT_DIR) else py
    print("\n" + "=" * 68)
    print("Installatie voltooid.")
    print("=" * 68)
    print("\nDe pipeline starten zonder de omgeving te activeren:")
    print(f"  {rel} tug_gui.py                       (grafische interface)")
    print(f"  {rel} tug_run.py <aanvraag.json>       (batch-verwerking)")


if __name__ == "__main__":
    main()
