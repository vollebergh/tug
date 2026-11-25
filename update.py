"""
update.py -- de workflow bijwerken naar de nieuwste release op GitHub

Gebruik (met de interface gesloten):
    py -3.12 update.py          (Windows)
    python3 update.py           (Linux)

Bedoeld voor een installatie uit de GitHub-ZIP, zonder git. Een werkkopie met
`.git/` werk je bij met `git pull`; daar weigert dit script.

Wat het doet:
1. Vraagt bij GitHub de nieuwste release op en vergelijkt die met de commit
   waarmee deze map is geïnstalleerd (`tug_config._ARCHIEF_COMMIT`). Alleen een
   release die *na* de geïnstalleerde versie komt, wordt geïnstalleerd; een
   nieuwere installatie wordt nooit teruggezet.
2. Downloadt de ZIP van die release en controleert dat het archief echt die
   commit bevat (ZIP-commentaar en ingevulde `_ARCHIEF_COMMIT`), zonder
   absolute paden, `..`-paden of symbolische koppelingen.
3. Vervangt alleen de bestanden van de workflow zelf. `.venv/`, `output/`,
   `geo/`, `tmp/`, `tug_state.json` en andere eigen bestanden blijven staan.
   Bestanden die uit de workflow zijn verdwenen, worden verwijderd.
4. Bewaart elk bestand dat wordt overschreven of verwijderd eerst in
   `backup/update-<datum>-<oude versie>/`, en zet bij een fout halverwege alles
   terug. Lokaal aangepaste workflowbestanden worden apart gemeld.
5. Draait `install.py` opnieuw als de afhankelijkheden zijn gewijzigd.

Alleen de standaardbibliotheek, net als install.py: het script moet ook werken
als `.venv` ontbreekt of kapot is, en het mag de interpreter uit `.venv` niet
zelf in gebruik hebben terwijl pip daarin pakketten vervangt (Windows vergrendelt
geladen bestanden). Daarom ook niet via tug_http, maar met dezelfde afspraken:
alleen https naar een vaste lijst hosts (ook bij elke redirect), een maximale
omvang per antwoord, en elke mislukking als fout in plaats van stil doorgaan.
De TLS-controle gebruikt hier de certificaten van het besturingssysteem.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

PROJECT_DIR = Path(__file__).resolve().parent

REPO             = "vollebergh/tug"
API              = f"https://api.github.com/repos/{REPO}"
TOEGESTANE_HOSTS = ("api.github.com", "codeload.github.com", "github.com")
USER_AGENT       = "TUG-ontheffingen-update"
TIMEOUT_S        = 20
MAX_BYTES_API    = 5 * 1024 * 1024     # JSON-antwoorden (vergelijking, bestandslijst)
MAX_BYTES_ZIP    = 50 * 1024 * 1024    # archief is ~0,4 MB (november 2026)

# Hier schrijft de update nooit in en hier verwijdert hij niets, ook niet als een
# archief zulke paden zou bevatten: dat archief is dan niet te vertrouwen.
BESCHERMD = frozenset({".git", ".venv", "venv", "output", "geo", "tmp", "backup",
                       "tug_state.json", "aanvragen.json", "obsidian", "archief"})

# Zonder deze bestanden is een archief geen workflow.
VERPLICHT = ("install.py", "update.py", "tug_config.py", "tug_gui.py", "tug_run.py",
             "requirements.txt")

# Bij wijziging hiervan moet install.py opnieuw draaien.
AFHANKELIJKHEDEN = ("requirements.txt", "requirements-build.txt")

_COMMIT_PATROON = re.compile(r'^_ARCHIEF_COMMIT = "([0-9a-f]{7,40})"', re.MULTILINE)
_SHA_PATROON    = re.compile(r"^[0-9a-f]{40}$")


class UpdateFout(Exception):
    """De update kan niet (veilig) worden uitgevoerd; niets is gewijzigd."""


class NietGevonden(UpdateFout):
    """GitHub kent de gevraagde release, commit of vergelijking niet (404)."""


# ──────────────────────────────────────────────
# Netwerk
# ──────────────────────────────────────────────

def veilige_url(url: str) -> str:
    """Geef `url` terug als die https gebruikt en naar een toegestane host wijst."""
    deel = urlparse(url)
    try:
        poort = deel.port
    except ValueError as fout:
        raise UpdateFout(f"ongeldige URL: {url!r}") from fout
    host = (deel.hostname or "").lower()
    if deel.scheme != "https" or poort not in (None, 443) or host not in TOEGESTANE_HOSTS:
        raise UpdateFout(f"verbinding met {deel.scheme}://{host} is niet toegestaan "
                         f"(alleen https naar {', '.join(TOEGESTANE_HOSTS)})")
    return url


class _GetoetsteRedirect(urllib.request.HTTPRedirectHandler):
    """Volg een redirect alleen als het doel zelf ook door `veilige_url` komt."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        veilige_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GetoetsteRedirect)


def haal(url: str, *, max_bytes: int, accept: str = "application/vnd.github+json") -> bytes:
    """Haal `url` op als bytes, begrensd op `max_bytes`.

    404 geeft `NietGevonden`, elke andere mislukking een UpdateFout.
    """
    verzoek = urllib.request.Request(  # noqa: S310 — schema en host getoetst
        veilige_url(url), headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with _opener.open(verzoek, timeout=TIMEOUT_S) as antwoord:
            data = antwoord.read(max_bytes + 1)
    except urllib.error.HTTPError as fout:
        if fout.code == 404:
            raise NietGevonden(url) from fout
        if fout.code == 403 and fout.headers.get("x-ratelimit-remaining") == "0":
            raise UpdateFout("GitHub weigert tijdelijk verzoeken vanaf dit adres "
                             "(limiet bereikt); probeer het over een uur opnieuw") from fout
        raise UpdateFout(f"GitHub gaf HTTP-status {fout.code} voor "
                         f"{urlparse(url).path}") from fout
    except (urllib.error.URLError, OSError) as fout:
        reden = getattr(fout, "reason", fout)
        raise UpdateFout(f"GitHub is niet bereikbaar ({reden})") from fout
    if len(data) > max_bytes:
        raise UpdateFout(f"antwoord van {urlparse(url).path} is groter dan "
                         f"{max_bytes:,} bytes — afgebroken")
    return data


def haal_json(url: str) -> dict[str, Any]:
    try:
        data = json.loads(haal(url, max_bytes=MAX_BYTES_API))
    except ValueError as fout:
        raise UpdateFout(f"antwoord van {urlparse(url).path} is geen geldige JSON") from fout
    if not isinstance(data, dict):
        raise UpdateFout(f"onverwacht antwoord van {urlparse(url).path}")
    return data


# ──────────────────────────────────────────────
# Versies vergelijken
# ──────────────────────────────────────────────

def geinstalleerde_commit(root: Path = PROJECT_DIR) -> str | None:
    """De commit waaruit deze map is uitgepakt, of None als die niet vastligt.

    Gelezen uit de tekst van tug_config.py in plaats van die te importeren: dat
    bestand hoort bij de code die vervangen gaat worden, en importeren voert het uit.
    """
    try:
        tekst = (root / "tug_config.py").read_text(encoding="utf-8")
    except OSError:
        return None
    treffer = _COMMIT_PATROON.search(tekst)
    return treffer.group(1) if treffer else None


@dataclass
class Release:
    tag: str
    sha: str           # volledige commit-hash
    url: str           # releasepagina
    notities: str


@dataclass
class Controle:
    """Uitkomst van de versievergelijking.

    status:
        "actueel"  — de nieuwste release is geïnstalleerd
        "nieuwer"  — er is een nieuwere release; bijwerken kan
        "vooruit"  — deze installatie is nieuwer dan de nieuwste release
        "afwijkend"— geen van beide volgt op de ander, of de geïnstalleerde
                     commit is onbekend bij GitHub; bijwerken kan, na bevestiging
        "onbekend" — de geïnstalleerde versie ligt niet vast (geen archief-commit)
    """
    huidig: str | None
    release: Release
    status: str


def nieuwste_release() -> Release:
    try:
        data = haal_json(f"{API}/releases/latest")
    except NietGevonden as fout:
        raise UpdateFout("er is nog geen release gepubliceerd op GitHub") from fout
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not re.fullmatch(r"[\w.\-]+", tag):
        raise UpdateFout("release zonder geldige tag")
    sha = haal(f"{API}/commits/{tag}", max_bytes=100,
               accept="application/vnd.github.sha").decode("ascii", "replace").strip()
    if not _SHA_PATROON.fullmatch(sha):
        raise UpdateFout(f"commit van release {tag} niet te bepalen")
    return Release(tag=tag, sha=sha, url=str(data.get("html_url") or ""),
                   notities=str(data.get("body") or "").strip())


def vergelijk(huidig: str | None, release: Release) -> Controle:
    if huidig is None:
        return Controle(huidig, release, "onbekend")
    if release.sha.startswith(huidig):
        return Controle(huidig, release, "actueel")
    try:
        data = haal_json(f"{API}/compare/{huidig}...{release.sha}")
    except NietGevonden:
        return Controle(huidig, release, "afwijkend")
    status = {"ahead": "nieuwer", "behind": "vooruit",
              "identical": "actueel"}.get(str(data.get("status")), "afwijkend")
    return Controle(huidig, release, status)


def controleer(root: Path = PROJECT_DIR) -> Controle:
    """Vergelijk deze installatie met de nieuwste release (twee à drie API-verzoeken)."""
    return vergelijk(geinstalleerde_commit(root), nieuwste_release())


# ──────────────────────────────────────────────
# Archief ophalen en toetsen
# ──────────────────────────────────────────────

def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _beschermd(rel: str) -> bool:
    return PurePosixPath(rel).parts[0] in BESCHERMD


def pak_uit(zipdata: bytes, sha: str, doel: Path) -> dict[str, Path]:
    """Toets het archief en pak het uit in `doel`; retourneert {relatief pad: bestand}.

    GitHub zet de commit-hash als ZIP-commentaar en vult via export-subst
    `_ARCHIEF_COMMIT` in; beide moeten bij `sha` passen.
    """
    try:
        archief = zipfile.ZipFile(io.BytesIO(zipdata))
    except zipfile.BadZipFile as fout:
        raise UpdateFout("download is geen geldig ZIP-archief") from fout
    with archief:
        if archief.comment.decode("ascii", "replace").strip() != sha:
            raise UpdateFout("archief hoort niet bij de verwachte commit")
        namen = archief.infolist()
        wortels = {info.filename.split("/", 1)[0] for info in namen}
        if len(wortels) != 1:
            raise UpdateFout("archief heeft geen enkele hoofdmap")
        bestanden: dict[str, Path] = {}
        totaal = 0
        for info in namen:
            if info.is_dir():
                continue
            rel = info.filename.split("/", 1)[1] if "/" in info.filename else ""
            pad = PurePosixPath(rel)
            if (not rel or "\\" in rel or ":" in rel or pad.is_absolute()
                    or any(deel in ("", ".", "..") for deel in rel.split("/"))):
                raise UpdateFout(f"archief bevat een ongeldig pad: {info.filename!r}")
            if _is_symlink(info):
                raise UpdateFout(f"archief bevat een symbolische koppeling: {rel}")
            if _beschermd(rel):
                raise UpdateFout(f"archief bevat een beschermd pad: {rel}")
            totaal += info.file_size
            if totaal > MAX_BYTES_ZIP * 4:
                raise UpdateFout("archief pakt uit tot een onverwacht grote omvang")
            uit = doel.joinpath(*pad.parts)
            uit.parent.mkdir(parents=True, exist_ok=True)
            with archief.open(info) as bron, uit.open("wb") as schrijf:
                shutil.copyfileobj(bron, schrijf)
            bestanden[rel] = uit

    ontbreekt = [naam for naam in VERPLICHT if naam not in bestanden]
    if ontbreekt:
        raise UpdateFout(f"archief mist {', '.join(ontbreekt)}")
    ingevuld = geinstalleerde_commit(doel)
    if ingevuld is None or not sha.startswith(ingevuld):
        raise UpdateFout("versie in het archief past niet bij de verwachte commit")
    return bestanden


def bestandslijst(commit: str) -> dict[str, str] | None:
    """{pad: blob-hash} van de workflowbestanden in `commit`, of None als onbekend.

    Nodig om te weten welke bestanden uit de workflow zijn verdwenen (die mogen
    weg) en welke lokaal zijn aangepast (die worden gemeld).
    """
    try:
        data = haal_json(f"{API}/git/trees/{commit}?recursive=1")
    except NietGevonden:
        return None
    if data.get("truncated") or not isinstance(data.get("tree"), list):
        return None
    return {str(item["path"]): str(item["sha"]) for item in data["tree"]
            if isinstance(item, dict) and item.get("type") == "blob"
            and "path" in item and "sha" in item}


def blob_hash(pad: Path) -> str:
    """Git-blobhash van een bestand, gelijk aan wat `git hash-object` geeft."""
    inhoud = pad.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(inhoud) + inhoud, usedforsecurity=False).hexdigest()


def blob_hash_zoals_in_git(rel: str, pad: Path) -> str:
    """Blobhash zoals git het bestand opslaat.

    In tug_config.py heeft export-subst bij het uitpakken de commit ingevuld; in
    git staat daar de placeholder. Zonder die terug te zetten zou tug_config.py
    altijd als lokaal aangepast gelden.
    """
    if rel != "tug_config.py":
        return blob_hash(pad)
    tekst = pad.read_bytes()
    terug = re.sub(rb'^_ARCHIEF_COMMIT = "[0-9a-f]{7,40}"', b'_ARCHIEF_COMMIT = "$Format:%h$"',
                   tekst, count=1, flags=re.MULTILINE)
    return hashlib.sha1(b"blob %d\0" % len(terug) + terug, usedforsecurity=False).hexdigest()


# ──────────────────────────────────────────────
# Plan en uitvoering
# ──────────────────────────────────────────────

@dataclass
class Plan:
    schrijven: list[str] = field(default_factory=list)     # nieuw of gewijzigd
    verwijderen: list[str] = field(default_factory=list)   # niet meer in de workflow
    lokaal_aangepast: list[str] = field(default_factory=list)
    oud_bekend: bool = True                                 # bestandslijst oude versie

    @property
    def leeg(self) -> bool:
        return not self.schrijven and not self.verwijderen

    @property
    def herinstallatie_nodig(self) -> bool:
        return any(naam in self.schrijven for naam in AFHANKELIJKHEDEN)


def maak_plan(root: Path, nieuw: dict[str, Path], oud: dict[str, str] | None) -> Plan:
    plan = Plan(oud_bekend=oud is not None)
    for rel, bron in sorted(nieuw.items()):
        lokaal = root.joinpath(*PurePosixPath(rel).parts)
        if not lokaal.is_file() or lokaal.read_bytes() != bron.read_bytes():
            plan.schrijven.append(rel)
    for rel in sorted(oud or {}):
        if rel in nieuw or _beschermd(rel):
            continue
        if root.joinpath(*PurePosixPath(rel).parts).is_file():
            plan.verwijderen.append(rel)
    for rel in plan.schrijven + plan.verwijderen:
        lokaal = root.joinpath(*PurePosixPath(rel).parts)
        if (oud and rel in oud and lokaal.is_file()
                and blob_hash_zoals_in_git(rel, lokaal) != oud[rel]):
            plan.lokaal_aangepast.append(rel)
    plan.lokaal_aangepast.sort()
    return plan


def voer_uit(root: Path, nieuw: dict[str, Path], plan: Plan, backup: Path) -> None:
    """Pas het plan toe. Bij een fout wordt de oude toestand teruggezet."""
    def pad(rel: str) -> Path:
        return root.joinpath(*PurePosixPath(rel).parts)

    bestond = {rel for rel in plan.schrijven if pad(rel).is_file()}
    for rel in sorted(bestond | set(plan.verwijderen)):
        kopie = backup.joinpath(*PurePosixPath(rel).parts)
        kopie.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pad(rel), kopie)

    gedaan: list[str] = []
    try:
        for rel in plan.schrijven:
            doel = pad(rel)
            doel.parent.mkdir(parents=True, exist_ok=True)
            tijdelijk = doel.with_name(doel.name + ".update")
            shutil.copyfile(nieuw[rel], tijdelijk)
            tijdelijk.replace(doel)
            gedaan.append(rel)
        for rel in plan.verwijderen:
            pad(rel).unlink()
            gedaan.append(rel)
    except OSError as fout:
        for rel in gedaan:
            if rel in bestond or rel in plan.verwijderen:
                kopie = backup.joinpath(*PurePosixPath(rel).parts)
                shutil.copy2(kopie, pad(rel))
            else:
                pad(rel).unlink(missing_ok=True)
        for rel in plan.schrijven:
            pad(rel).with_name(pad(rel).name + ".update").unlink(missing_ok=True)
        raise UpdateFout(
            f"bijwerken van {fout.filename or 'een bestand'} mislukt ({fout.strerror or fout}); "
            "alle bestanden zijn teruggezet. Is de interface nog open?") from fout

    for rel in plan.verwijderen:
        map_ = pad(rel).parent
        while map_ != root and map_.is_dir() and not any(map_.iterdir()):
            map_.rmdir()
            map_ = map_.parent


# ──────────────────────────────────────────────
# Opdrachtregel
# ──────────────────────────────────────────────

def _vraag(tekst: str, standaard: bool) -> bool:
    keuze = " [J/n] " if standaard else " [j/N] "
    try:
        antwoord = input(tekst + keuze).strip().lower()
    except EOFError:
        return standaard
    return standaard if not antwoord else antwoord in ("j", "ja", "y", "yes")


def _lijst(kop: str, items: list[str], max_regels: int = 15) -> None:
    if not items:
        return
    print(f"  {kop} ({len(items)}):")
    for rel in items[:max_regels]:
        print(f"    {rel}")
    if len(items) > max_regels:
        print(f"    … en {len(items) - max_regels} meer")


def bijwerken(root: Path = PROJECT_DIR) -> int:
    """Voer de update interactief uit; retourneert de exitcode."""
    if (root / ".git").exists():
        raise UpdateFout("dit is een git-werkkopie; werk die bij met `git pull`")
    if sys.prefix != sys.base_prefix:
        raise UpdateFout(
            "start dit script niet met de Python uit .venv, maar met de gewone Python "
            "(Windows: `py -3.12 update.py`); anders kan pip die omgeving niet bijwerken")

    print("Nieuwste release opvragen bij GitHub …")
    controle = controleer(root)
    rel = controle.release
    print(f"  geïnstalleerd: {controle.huidig or 'onbekend'}")
    print(f"  nieuwste:      {rel.tag} ({rel.sha[:7]})")

    if controle.status == "actueel":
        print("\nJe hebt de nieuwste versie al. Er is niets gewijzigd.")
        return 0
    if controle.status == "vooruit":
        print("\nDeze installatie is nieuwer dan de nieuwste release; er wordt niets "
              "teruggezet.")
        return 0
    if controle.status in ("afwijkend", "onbekend"):
        print("\nDe geïnstalleerde versie volgt niet aantoonbaar vóór deze release "
              "(onbekende of afwijkende commit).")
        if not _vraag("Toch release " + rel.tag + " installeren?", standaard=False):
            print("Afgebroken; er is niets gewijzigd.")
            return 0

    if rel.notities:
        print(f"\nWat is er nieuw in {rel.tag}:")
        for regel in rel.notities.splitlines()[:30]:
            print(f"  {regel}")
        if rel.url:
            print(f"  ({rel.url})")

    with tempfile.TemporaryDirectory(prefix="tug-update-") as tijdelijk:
        print(f"\nRelease {rel.tag} downloaden …")
        zipdata = haal(f"{API}/zipball/{rel.sha}", max_bytes=MAX_BYTES_ZIP,
                       accept="application/zip")
        nieuw = pak_uit(zipdata, rel.sha, Path(tijdelijk))
        oud = bestandslijst(controle.huidig) if controle.huidig else None
        plan = maak_plan(root, nieuw, oud)

        if plan.leeg:
            print("De bestanden zijn al gelijk aan deze release. Er is niets gewijzigd.")
            return 0

        print(f"\nDe update wijzigt {len(plan.schrijven)} bestand(en) en verwijdert er "
              f"{len(plan.verwijderen)}.")
        _lijst("bijwerken of toevoegen", plan.schrijven)
        _lijst("verwijderen (niet meer in de workflow)", plan.verwijderen)
        if plan.lokaal_aangepast:
            print("\n  LET OP: deze bestanden waren hier lokaal aangepast; die aanpassingen")
            print("  worden overschreven (een kopie komt in de backupmap):")
            for naam in plan.lokaal_aangepast:
                print(f"    {naam}")
        if not plan.oud_bekend:
            print("\n  De vorige versie is niet bekend: verouderde bestanden blijven staan.")
        if plan.herinstallatie_nodig:
            print("\n  De afhankelijkheden zijn gewijzigd: daarna draait install.py opnieuw.")

        print("\nSluit eerst de TUG-interface als die nog open is.")
        if not _vraag("Bijwerken?", standaard=True):
            print("Afgebroken; er is niets gewijzigd.")
            return 0

        stempel = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = root / "backup" / f"update-{stempel}-{controle.huidig or 'onbekend'}"
        voer_uit(root, nieuw, plan, backup)

    print(f"\nWorkflow bijgewerkt naar {rel.tag} ({rel.sha[:7]}).")
    if backup.exists():
        print(f"Oude bestanden staan in {backup.relative_to(root)}")

    if plan.herinstallatie_nodig:
        print("\ninstall.py draaien voor de gewijzigde afhankelijkheden …\n")
        resultaat = subprocess.run(  # noqa: S603 — eigen interpreter, eigen script
            [sys.executable, str(root / "install.py")], cwd=root, check=False)
        if resultaat.returncode != 0:
            print("\nFOUT: install.py is niet gelukt. De code is wel bijgewerkt; los de",
                  file=sys.stderr)
            print("      melding hierboven op en draai daarna `py -3.12 install.py`.",
                  file=sys.stderr)
            return 1
    return 0


def main() -> int:
    print("=" * 68)
    print("TUG-ontheffingen — bijwerken")
    print("=" * 68)
    try:
        code = bijwerken()
    except UpdateFout as fout:
        print(f"\nFOUT: {fout}", file=sys.stderr)
        code = 1
    # Gestart via een snelkoppeling sluit het venster anders meteen.
    if sys.stdin.isatty():
        with contextlib.suppress(EOFError):
            input("\nDruk op Enter om af te sluiten.")
    return code


if __name__ == "__main__":
    sys.exit(main())
