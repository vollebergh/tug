"""
beveiligingscontrole.py -- De beveiligingscontrole van de pipeline in één commando

Gebruik:
    .venv/bin/python beveiligingscontrole.py

Draait achter elkaar:

1. **pip-audit** op de geïnstalleerde pakketten van `.venv` — bekende
   kwetsbaarheden, ook in transitieve afhankelijkheden. Legt tegelijk een
   softwarestuklijst (SBOM, CycloneDX) vast.
2. **Omgeving tegen lockfile** — elk geïnstalleerd pakket moet met exact die
   versie in requirements-dev.txt of requirements-build.txt staan, en omgekeerd.
3. **ruff** met de projectregels (inclusief de beveiligingsregels S).
4. **bandit** — alleen medium en hoog tellen als bevinding.
5. **mypy** met de projectconfiguratie.
6. **gitleaks** over de volledige git-geschiedenis.

De uitkomst komt in `.beveiligingscontrole.json` (datum, commit, bevindingen per
onderdeel); `tug_run.py` en de grafische schil waarschuwen als die ontbreekt,
ouder is dan een maand of bevindingen bevat. Een onderdeel dat niet kan draaien
(gereedschap ontbreekt, geen netwerk) telt als bevinding: een controle die niet
heeft plaatsgevonden is geen geslaagde controle.

Exitcode 0 zonder bevindingen, 1 met.
"""

import json
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from datetime import datetime
from importlib import metadata
from pathlib import Path

from tug_config import BEVEILIGINGSCONTROLE_PAD, VERSION
from tug_opslag import schrijf_prive

PROJECT_DIR = Path(__file__).resolve().parent
SBOM_PAD    = PROJECT_DIR / ".beveiligingscontrole-sbom.cdx.json"
LOCKFILES   = ("requirements-dev.txt", "requirements-build.txt")
VENV_BIN    = Path(sys.executable).parent


def _draai(opdracht: list[str], timeout: int = 900) -> subprocess.CompletedProcess:
    return subprocess.run(
        opdracht, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=timeout,
        check=False,
    )


def _gereedschap(naam: str) -> str | None:
    kandidaat = VENV_BIN / naam
    if kandidaat.exists():
        return str(kandidaat)
    return shutil.which(naam)


def _normaliseer(naam: str) -> str:
    return re.sub(r"[-_.]+", "-", naam).lower()


def controleer_afhankelijkheden() -> dict:
    """pip-audit op de geïnstalleerde pakketten, met SBOM."""
    pip_audit = _gereedschap("pip-audit")
    if not pip_audit:
        return {"bevindingen": 1, "melding": "pip-audit niet gevonden in .venv"}
    site = sysconfig.get_paths()["purelib"]
    with tempfile.TemporaryDirectory() as tmp:
        rapport = Path(tmp) / "audit.json"
        uit = _draai([pip_audit, "--path", site, "-f", "json", "-o", str(rapport),
                      "--progress-spinner", "off"])
        if not rapport.exists():
            return {"bevindingen": 1,
                    "melding": f"pip-audit kon niet draaien: {uit.stderr.strip()[-300:]}"}
        data = json.loads(rapport.read_text(encoding="utf-8"))
        _draai([pip_audit, "--path", site, "-f", "cyclonedx-json", "-o", str(SBOM_PAD),
                "--progress-spinner", "off"])

    kwetsbaar = {}
    for pakket in data.get("dependencies", []):
        ids = sorted({v["id"] for v in pakket.get("vulns", [])})
        if ids:
            kwetsbaar[f"{pakket['name']} {pakket['version']}"] = ids
    return {
        "bevindingen": sum(len(ids) for ids in kwetsbaar.values()),
        "pakketten_gecontroleerd": len(data.get("dependencies", [])),
        "kwetsbaar": kwetsbaar,
        "sbom": SBOM_PAD.name if SBOM_PAD.exists() else None,
    }


def controleer_lockfile() -> dict:
    """Komt de geïnstalleerde omgeving exact overeen met de lockfiles?"""
    vastgelegd: dict[str, str] = {}
    for bestand in LOCKFILES:
        for regel in (PROJECT_DIR / bestand).read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", regel)
            if m:
                vastgelegd[_normaliseer(m.group(1))] = m.group(2)
    geinstalleerd = {_normaliseer(d.metadata["Name"]): d.version
                     for d in metadata.distributions()}

    afwijkingen = []
    for naam, versie in sorted(geinstalleerd.items()):
        if naam not in vastgelegd:
            afwijkingen.append(f"{naam} {versie} geïnstalleerd maar niet in de lockfile")
        elif vastgelegd[naam] != versie:
            afwijkingen.append(f"{naam} {versie} geïnstalleerd, lockfile zegt {vastgelegd[naam]}")
    # Omgekeerd alleen voor pakketten die op dit platform horen (markers staan in
    # de lockfile; colorama en tzdata zijn bijvoorbeeld alleen voor Windows).
    return {"bevindingen": len(afwijkingen), "afwijkingen": afwijkingen}


def controleer_ruff() -> dict:
    ruff = _gereedschap("ruff")
    if not ruff:
        return {"bevindingen": 1, "melding": "ruff niet gevonden"}
    uit = _draai([ruff, "check", ".", "--output-format", "json"])
    meldingen = json.loads(uit.stdout or "[]")
    return {"bevindingen": len(meldingen),
            "regels": sorted({m["code"] for m in meldingen if m.get("code")})}


def controleer_bandit() -> dict:
    bandit = _gereedschap("bandit")
    if not bandit:
        return {"bevindingen": 1, "melding": "bandit niet gevonden"}
    uit = _draai([bandit, "-c", "pyproject.toml", "-r", ".", "-f", "json", "-q"])
    try:
        resultaten = json.loads(uit.stdout)["results"]
    except (ValueError, KeyError):
        return {"bevindingen": 1, "melding": f"bandit-uitvoer onleesbaar: {uit.stderr[-300:]}"}
    zwaar = [f"{r['test_id']} {r['filename']}:{r['line_number']}" for r in resultaten
             if r["issue_severity"] in ("MEDIUM", "HIGH")]
    return {"bevindingen": len(zwaar), "medium_hoog": zwaar,
            "laag": sum(r["issue_severity"] == "LOW" for r in resultaten)}


def controleer_mypy() -> dict:
    mypy = _gereedschap("mypy")
    if not mypy:
        return {"bevindingen": 1, "melding": "mypy niet gevonden"}
    uit = _draai([mypy])
    fouten = [r for r in uit.stdout.splitlines() if ": error:" in r]
    return {"bevindingen": len(fouten), "fouten": fouten[:20]}


def controleer_geheimen() -> dict:
    gitleaks = shutil.which("gitleaks")
    if not gitleaks:
        return {"bevindingen": 1,
                "melding": "gitleaks niet gevonden op het PATH (zie README §19.5)"}
    with tempfile.TemporaryDirectory() as tmp:
        rapport = Path(tmp) / "gitleaks.json"
        _draai([gitleaks, "git", ".", "--redact", "--no-banner",
                "--report-format", "json", "--report-path", str(rapport)])
        if not rapport.exists():
            return {"bevindingen": 1, "melding": "gitleaks leverde geen rapport op"}
        lekken = json.loads(rapport.read_text(encoding="utf-8") or "[]")
    return {"bevindingen": len(lekken),
            "lekken": [f"{lek['RuleID']} {lek['File']}:{lek['StartLine']}" for lek in lekken]}


ONDERDELEN = {
    "afhankelijkheden": controleer_afhankelijkheden,
    "lockfile":         controleer_lockfile,
    "ruff":             controleer_ruff,
    "bandit":           controleer_bandit,
    "mypy":             controleer_mypy,
    "gitleaks":         controleer_geheimen,
}


def main() -> int:
    print(f"Beveiligingscontrole — workflowversie {VERSION}")
    uitkomst: dict = {"datum": datetime.now().isoformat(timespec="seconds"),
                      "commit": VERSION, "onderdelen": {}}
    for naam, controle in ONDERDELEN.items():
        print(f"  {naam} ...", end=" ", flush=True)
        try:
            resultaat = controle()
        except (OSError, subprocess.SubprocessError, ValueError) as fout:
            resultaat = {"bevindingen": 1, "melding": f"kon niet draaien: {fout}"}
        uitkomst["onderdelen"][naam] = resultaat
        print("geen bevindingen" if not resultaat["bevindingen"]
              else f"{resultaat['bevindingen']} bevinding(en)")
        for sleutel in ("melding", "kwetsbaar", "afwijkingen", "medium_hoog", "fouten", "lekken"):
            if resultaat.get(sleutel):
                print(f"      {sleutel}: {json.dumps(resultaat[sleutel], ensure_ascii=False)}")

    uitkomst["bevindingen"] = sum(o["bevindingen"] for o in uitkomst["onderdelen"].values())
    schrijf_prive(BEVEILIGINGSCONTROLE_PAD, json.dumps(uitkomst, ensure_ascii=False, indent=2))
    print(f"\nTotaal: {uitkomst['bevindingen']} bevinding(en) — vastgelegd in "
          f"{BEVEILIGINGSCONTROLE_PAD.name}")
    return 1 if uitkomst["bevindingen"] else 0


if __name__ == "__main__":
    sys.exit(main())
