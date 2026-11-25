"""
test_update.py -- update.py zonder netwerk

Toetst de versievergelijking, de controle van het archief, wat er wordt
overschreven, verwijderd en bewaard, en het terugzetten bij een fout. Het
netwerk is vervangen door een nep-GitHub; er gaat geen verkeer naar buiten.
"""

import io
import json
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import update  # noqa: E402

OUD = "a" * 40
NIEUW = "b" * 40


def tug_config(sha: str) -> bytes:
    return f'_ARCHIEF_COMMIT = "{sha[:7]}"\nVERSION = "x"\n'.encode()


def workflow(sha: str, **extra: bytes) -> dict[str, bytes]:
    bestanden = {naam: f"# {naam}\n".encode() for naam in update.VERPLICHT}
    bestanden["tug_config.py"] = tug_config(sha)
    bestanden.update(extra)
    return bestanden


def maak_zip(bestanden: dict[str, bytes], sha: str = NIEUW, *, commentaar: str | None = None,
             symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(f"vollebergh-tug-{sha[:7]}/", b"")
        for rel, inhoud in bestanden.items():
            z.writestr(f"vollebergh-tug-{sha[:7]}/{rel}", inhoud)
        if symlink:
            info = zipfile.ZipInfo(f"vollebergh-tug-{sha[:7]}/{symlink}")
            info.external_attr = (0o120777 << 16)
            z.writestr(info, b"/etc/passwd")
        z.comment = (sha if commentaar is None else commentaar).encode()
    return buffer.getvalue()


def blob(inhoud: bytes) -> str:
    import hashlib
    return hashlib.sha1(b"blob %d\0" % len(inhoud) + inhoud).hexdigest()  # noqa: S324


def installeer(root: Path, bestanden: dict[str, bytes]) -> None:
    for rel, inhoud in bestanden.items():
        pad = root / rel
        pad.parent.mkdir(parents=True, exist_ok=True)
        pad.write_bytes(inhoud)


@pytest.fixture
def geen_netwerk(monkeypatch):
    def weiger(*_a, **_k):
        raise AssertionError("onverwacht netwerkverzoek")
    monkeypatch.setattr(update, "haal", weiger)
    monkeypatch.setattr(update, "haal_json", weiger)


# ──────────────────────────────────────────────
# Netwerkafspraken
# ──────────────────────────────────────────────

class TestVeiligeUrl:
    @pytest.mark.parametrize("url", [
        "https://api.github.com/repos/vollebergh/tug/releases/latest",
        "https://codeload.github.com/vollebergh/tug/zip/abc",
    ])
    def test_toegestaan(self, url):
        assert update.veilige_url(url) == url

    @pytest.mark.parametrize("url", [
        "http://api.github.com/repos/x",            # geen https
        "https://evil.example/repos/x",              # andere host
        "https://api.github.com.evil.example/x",     # lijkt erop
        "https://raw.githubusercontent.com/x",       # niet op de lijst
        "https://api.github.com:8443/x",             # andere poort
        "file:///etc/passwd",
    ])
    def test_geweigerd(self, url):
        with pytest.raises(update.UpdateFout):
            update.veilige_url(url)

    def test_redirect_naar_andere_host_geweigerd(self):
        verzoek = urllib.request.Request("https://api.github.com/x")
        with pytest.raises(update.UpdateFout):
            update._GetoetsteRedirect().redirect_request(
                verzoek, None, 302, "Found", {}, "https://evil.example/zip")


# ──────────────────────────────────────────────
# Versievergelijking
# ──────────────────────────────────────────────

class TestVergelijk:
    release = update.Release(tag="v2", sha=NIEUW, url="", notities="")

    def test_zelfde_commit_is_actueel(self, geen_netwerk):
        assert update.vergelijk(NIEUW[:7], self.release).status == "actueel"

    def test_zonder_archiefcommit_onbekend(self, geen_netwerk):
        assert update.vergelijk(None, self.release).status == "onbekend"

    @pytest.mark.parametrize(("github", "verwacht"), [
        ("ahead", "nieuwer"), ("behind", "vooruit"), ("identical", "actueel"),
        ("diverged", "afwijkend"),
    ])
    def test_github_vergelijking(self, monkeypatch, github, verwacht):
        gevraagd = []
        def nep(url):
            gevraagd.append(url)
            return {"status": github}
        monkeypatch.setattr(update, "haal_json", nep)
        assert update.vergelijk(OUD[:7], self.release).status == verwacht
        assert gevraagd == [f"{update.API}/compare/{OUD[:7]}...{NIEUW}"]

    def test_onbekende_commit_bij_github_is_afwijkend(self, monkeypatch):
        def nep(url):
            raise update.NietGevonden(url)
        monkeypatch.setattr(update, "haal_json", nep)
        assert update.vergelijk(OUD[:7], self.release).status == "afwijkend"

    def test_geen_release(self, monkeypatch):
        def nep(url):
            raise update.NietGevonden(url)
        monkeypatch.setattr(update, "haal_json", nep)
        with pytest.raises(update.UpdateFout, match="nog geen release"):
            update.nieuwste_release()

    def test_geinstalleerde_commit(self, tmp_path):
        (tmp_path / "tug_config.py").write_bytes(tug_config(OUD))
        assert update.geinstalleerde_commit(tmp_path) == OUD[:7]
        (tmp_path / "tug_config.py").write_text('_ARCHIEF_COMMIT = "$Format:%h$"\n')
        assert update.geinstalleerde_commit(tmp_path) is None

    def test_eigen_tug_config_heeft_placeholder_of_commit(self):
        # Werkkopie: placeholder (None); uit een archief: de ingevulde commit.
        commit = update.geinstalleerde_commit()
        assert commit is None or len(commit) >= 7


# ──────────────────────────────────────────────
# Archief
# ──────────────────────────────────────────────

class TestPakUit:
    def test_geldig(self, tmp_path):
        bestanden = update.pak_uit(maak_zip(workflow(NIEUW, **{"gui/stijl.qss": b"x"})),
                                   NIEUW, tmp_path)
        assert set(bestanden) == set(update.VERPLICHT) | {"gui/stijl.qss"}
        assert (tmp_path / "gui" / "stijl.qss").read_bytes() == b"x"

    def test_verkeerde_commit_in_commentaar(self, tmp_path):
        with pytest.raises(update.UpdateFout, match="verwachte commit"):
            update.pak_uit(maak_zip(workflow(NIEUW), commentaar=OUD), NIEUW, tmp_path)

    def test_ingevulde_versie_moet_passen(self, tmp_path):
        with pytest.raises(update.UpdateFout, match="versie in het archief"):
            update.pak_uit(maak_zip(workflow(OUD)), NIEUW, tmp_path)

    @pytest.mark.parametrize("pad", ["../buiten.py", "a/../../buiten.py", "a//b.py",
                                     "C:/windows.py", "a\\b.py"])
    def test_ongeldig_pad(self, tmp_path, pad):
        with pytest.raises(update.UpdateFout, match="ongeldig pad"):
            update.pak_uit(maak_zip(workflow(NIEUW, **{pad: b"x"})), NIEUW, tmp_path / "doel")
        assert not (tmp_path / "buiten.py").exists()

    def test_symlink(self, tmp_path):
        with pytest.raises(update.UpdateFout, match="symbolische"):
            update.pak_uit(maak_zip(workflow(NIEUW), symlink="link"), NIEUW, tmp_path)

    @pytest.mark.parametrize("pad", [".venv/x.py", "output/rapport.pdf", "tug_state.json",
                                     ".git/config", "geo/cache.gpkg"])
    def test_beschermd_pad(self, tmp_path, pad):
        with pytest.raises(update.UpdateFout, match="beschermd"):
            update.pak_uit(maak_zip(workflow(NIEUW, **{pad: b"x"})), NIEUW, tmp_path)

    def test_verplicht_bestand_ontbreekt(self, tmp_path):
        bestanden = workflow(NIEUW)
        del bestanden["tug_gui.py"]
        with pytest.raises(update.UpdateFout, match="mist tug_gui.py"):
            update.pak_uit(maak_zip(bestanden), NIEUW, tmp_path)

    def test_geen_zip(self, tmp_path):
        with pytest.raises(update.UpdateFout, match="geen geldig ZIP"):
            update.pak_uit(b"<html>", NIEUW, tmp_path)


def test_blob_hash_gelijk_aan_git(tmp_path):
    pad = tmp_path / "a.py"
    pad.write_bytes(b"print('hallo')\n")
    git = subprocess.run(["git", "hash-object", str(pad)],  # noqa: S603, S607
                         capture_output=True, text=True, check=True).stdout.strip()
    assert update.blob_hash(pad) == git


# ──────────────────────────────────────────────
# Plan en uitvoering
# ──────────────────────────────────────────────

@pytest.fixture
def situatie(tmp_path):
    """Installatie van OUD met eigen gebruikersbestanden, en de uitgepakte NIEUW."""
    root = tmp_path / "TUG"
    oud = workflow(OUD, **{"weg.py": b"# weg\n", "sub/weg.py": b"# weg\n",
                           "tug_gui.py": b"# oude gui\n", "README.md": b"oud\n"})
    installeer(root, oud)
    installeer(root, {"output/rapport.pdf": b"pdf", "tug_state.json": b"{}",
                      "geo/cache.gpkg": b"geo", ".venv/pyvenv.cfg": b"cfg",
                      "eigen_notitie.txt": b"van de gebruiker"})
    (root / "README.md").write_bytes(b"lokaal aangepast\n")

    nieuw_bestanden = workflow(NIEUW, **{"tug_gui.py": b"# nieuwe gui\n", "README.md": b"nieuw\n",
                                         "nieuw.py": b"# nieuw\n"})
    staging = tmp_path / "staging"
    nieuw = update.pak_uit(maak_zip(nieuw_bestanden), NIEUW, staging)
    oud_lijst = {rel: blob(inhoud) for rel, inhoud in oud.items()}
    # Zo staat tug_config.py in git: met de placeholder, niet met de ingevulde commit.
    oud_lijst["tug_config.py"] = blob(oud["tug_config.py"].replace(
        OUD[:7].encode(), b"$Format:%h$"))
    return root, nieuw, oud_lijst


class TestPlan:
    def test_ingevulde_commit_geldt_niet_als_aanpassing(self, tmp_path):
        """git bewaart de placeholder; het archief de ingevulde commit."""
        in_git = b'X = 1\n_ARCHIEF_COMMIT = "$Format:%h$"\n'
        pad = tmp_path / "tug_config.py"
        pad.write_bytes(in_git.replace(b"$Format:%h$", OUD[:7].encode()))
        assert update.blob_hash_zoals_in_git("tug_config.py", pad) == blob(in_git)
        pad.write_bytes(in_git.replace(b"X = 1", b"X = 2").replace(b"$Format:%h$", b"abcdef1"))
        assert update.blob_hash_zoals_in_git("tug_config.py", pad) != blob(in_git)

    def test_plan(self, situatie):
        root, nieuw, oud = situatie
        plan = update.maak_plan(root, nieuw, oud)
        assert set(plan.schrijven) == {"tug_config.py", "tug_gui.py", "README.md", "nieuw.py"}
        assert plan.verwijderen == ["sub/weg.py", "weg.py"]
        assert plan.lokaal_aangepast == ["README.md"]
        assert not plan.herinstallatie_nodig

    def test_zonder_oude_lijst_niets_verwijderd(self, situatie):
        root, nieuw, _ = situatie
        plan = update.maak_plan(root, nieuw, None)
        assert plan.verwijderen == [] and plan.lokaal_aangepast == [] and not plan.oud_bekend

    def test_beschermd_pad_uit_oude_lijst_blijft(self, situatie):
        root, nieuw, oud = situatie
        oud["output/rapport.pdf"] = "0" * 40
        assert "output/rapport.pdf" not in update.maak_plan(root, nieuw, oud).verwijderen

    def test_gewijzigde_requirements(self, situatie):
        root, nieuw, oud = situatie
        nieuw["requirements.txt"].write_bytes(b"pandas==9\n")
        assert update.maak_plan(root, nieuw, oud).herinstallatie_nodig

    def test_uitvoeren(self, situatie, tmp_path):
        root, nieuw, oud = situatie
        plan = update.maak_plan(root, nieuw, oud)
        backup = root / "backup" / "u1"
        update.voer_uit(root, nieuw, plan, backup)

        assert (root / "tug_gui.py").read_bytes() == b"# nieuwe gui\n"
        assert (root / "nieuw.py").read_bytes() == b"# nieuw\n"
        assert not (root / "weg.py").exists()
        assert not (root / "sub").exists()                   # lege map opgeruimd
        assert update.geinstalleerde_commit(root) == NIEUW[:7]
        # Eigen bestanden en omgevingen onaangeroerd.
        assert (root / "output" / "rapport.pdf").read_bytes() == b"pdf"
        assert (root / "tug_state.json").read_bytes() == b"{}"
        assert (root / "geo" / "cache.gpkg").read_bytes() == b"geo"
        assert (root / ".venv" / "pyvenv.cfg").read_bytes() == b"cfg"
        assert (root / "eigen_notitie.txt").read_bytes() == b"van de gebruiker"
        # Backup van alles wat is overschreven of verwijderd.
        assert (backup / "tug_gui.py").read_bytes() == b"# oude gui\n"
        assert (backup / "README.md").read_bytes() == b"lokaal aangepast\n"
        assert (backup / "sub" / "weg.py").read_bytes() == b"# weg\n"
        assert not (backup / "nieuw.py").exists()
        assert not list(root.rglob("*.update"))

    def test_fout_halverwege_zet_alles_terug(self, situatie, monkeypatch):
        root, nieuw, oud = situatie
        plan = update.maak_plan(root, nieuw, oud)
        voor = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

        echte_replace = Path.replace
        teller = {"n": 0}
        def vergrendeld(self, doel):
            teller["n"] += 1
            if teller["n"] == 3:
                raise PermissionError(13, "Toegang geweigerd", str(doel))
            return echte_replace(self, doel)
        monkeypatch.setattr(Path, "replace", vergrendeld)

        with pytest.raises(update.UpdateFout, match="teruggezet"):
            update.voer_uit(root, nieuw, plan, root / "backup" / "u1")
        na = {p.relative_to(root): p.read_bytes() for p in root.rglob("*")
              if p.is_file() and "backup" not in p.parts}
        assert na == voor


# ──────────────────────────────────────────────
# Geheel, met nep-GitHub
# ──────────────────────────────────────────────

class NepGitHub:
    def __init__(self, oud: dict[str, bytes], nieuw: dict[str, bytes], status: str = "ahead"):
        self.oud, self.nieuw, self.status = oud, nieuw, status
        self.urls: list[str] = []

    def haal(self, url, *, max_bytes, accept=""):
        self.urls.append(url)
        if url.endswith("/commits/v2"):
            return NIEUW.encode()
        if url.endswith(f"/zipball/{NIEUW}"):
            return maak_zip(self.nieuw)
        raise AssertionError(url)

    def haal_json(self, url):
        self.urls.append(url)
        if url.endswith("/releases/latest"):
            return {"tag_name": "v2", "html_url": "https://github.com/x", "body": "- iets"}
        if "/compare/" in url:
            return {"status": self.status}
        if "/git/trees/" in url:
            return {"truncated": False, "tree": [
                {"path": rel, "type": "blob", "sha": blob(inhoud)}
                for rel, inhoud in self.oud.items()]}
        raise AssertionError(url)


@pytest.fixture
def buiten_venv(monkeypatch):
    monkeypatch.setattr(update.sys, "prefix", update.sys.base_prefix)


def zet_nep(monkeypatch, nep, antwoord="j"):
    monkeypatch.setattr(update, "haal", nep.haal)
    monkeypatch.setattr(update, "haal_json", nep.haal_json)
    monkeypatch.setattr("builtins.input", lambda *_a: antwoord)
    installs = []
    def run(args, **_k):
        installs.append(args)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(update.subprocess, "run", run)
    return installs


class TestBijwerken:
    def test_volledige_update(self, tmp_path, monkeypatch, buiten_venv):
        oud = workflow(OUD, **{"weg.py": b"x"})
        nieuw = workflow(NIEUW, **{"requirements.txt": b"nieuw\n"})
        installeer(tmp_path, oud)
        installs = zet_nep(monkeypatch, NepGitHub(oud, nieuw))

        assert update.bijwerken(tmp_path) == 0
        assert update.geinstalleerde_commit(tmp_path) == NIEUW[:7]
        assert not (tmp_path / "weg.py").exists()
        assert installs == [[sys.executable, str(tmp_path / "install.py")]]

    def test_nee_wijzigt_niets(self, tmp_path, monkeypatch, buiten_venv):
        oud = workflow(OUD)
        installeer(tmp_path, oud)
        installs = zet_nep(monkeypatch, NepGitHub(oud, workflow(NIEUW)), antwoord="n")
        assert update.bijwerken(tmp_path) == 0
        assert update.geinstalleerde_commit(tmp_path) == OUD[:7]
        assert not (tmp_path / "backup").exists() and installs == []

    def test_actueel_downloadt_niets(self, tmp_path, monkeypatch, buiten_venv):
        installeer(tmp_path, workflow(NIEUW))
        nep = NepGitHub({}, {})
        zet_nep(monkeypatch, nep)
        assert update.bijwerken(tmp_path) == 0
        assert not any("zipball" in url for url in nep.urls)

    def test_nieuwere_installatie_wordt_niet_teruggezet(self, tmp_path, monkeypatch, buiten_venv):
        oud = workflow(OUD)
        installeer(tmp_path, oud)
        nep = NepGitHub(oud, workflow(NIEUW), status="behind")
        zet_nep(monkeypatch, nep)
        assert update.bijwerken(tmp_path) == 0
        assert update.geinstalleerde_commit(tmp_path) == OUD[:7]
        assert not any("zipball" in url for url in nep.urls)

    def test_weigert_git_werkkopie(self, tmp_path, buiten_venv, geen_netwerk):
        (tmp_path / ".git").mkdir()
        with pytest.raises(update.UpdateFout, match="git pull"):
            update.bijwerken(tmp_path)

    def test_weigert_python_uit_venv(self, tmp_path, monkeypatch, geen_netwerk):
        monkeypatch.setattr(update.sys, "prefix", "/ergens/.venv")
        monkeypatch.setattr(update.sys, "base_prefix", "/usr")
        with pytest.raises(update.UpdateFout, match="py -3.12"):
            update.bijwerken(tmp_path)


def test_json_moet_object_zijn(monkeypatch):
    monkeypatch.setattr(update, "haal", lambda *_a, **_k: json.dumps([1]).encode())
    with pytest.raises(update.UpdateFout):
        update.haal_json(f"{update.API}/releases/latest")
