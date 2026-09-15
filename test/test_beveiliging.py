"""
test_beveiliging.py -- regressietests voor de bevindingen van de security-audit

Elke test hier hoort bij een bevinding uit de audit van 13 september 2026
(obsidian/Kennisbank/Security Audit — TUG-ontheffingen 2026-09-13.md) en bewaakt
dat die niet terugkomt:

- S-01  een uitgevallen bron levert geen conclusie maar een melding, en de bronnen
        waar de toetsing niet zonder kan breken de toetsing af
- S-02  kaarttegels worden alleen als PNG of JPEG geopend
- S-03  verwijzingen uit bronantwoorden: alleen https en toegestane hosts, ook na
        een redirect
- S-04  de aanvraag wordt aan de rand tegen een schema getoetst
- S-06  de state wordt gewist, ook bij een onderbreking, met privérechten
- H-01  antwoorden zijn in omvang begrensd
- H-06  JSON in een scriptblok kan de tag niet sluiten

Er gaat geen verkeer het netwerk op: `requests.request` in tug_http wordt per
test vervangen door een nepnet dat per URL een antwoord of een fout teruggeeft.
"""

import hashlib
import io
import json
import os
import re
import stat
import sys
from base64 import b64encode
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
import requests
from shapely.geometry import MultiPoint

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tug_02_classificatie as klas  # noqa: E402
import tug_03_kaart as kaart  # noqa: E402
import tug_03_ruimtelijk as ruimtelijk  # noqa: E402
import tug_bronnen_bag as bag  # noqa: E402
import tug_bronnen_brt as brt  # noqa: E402
import tug_bronnen_natuur as natuur  # noqa: E402
import tug_bronnen_onderwijs as onderwijs  # noqa: E402
import tug_http  # noqa: E402
import tug_omgeving  # noqa: E402
import tug_run  # noqa: E402
from tug_05_output import _ProcesLogBuilder, genereer_html  # noqa: E402
from tug_aanvraag import controleer_structuur  # noqa: E402
from tug_bronstatus import (  # noqa: E402
    MISLUKT, VEROUDERD, Bronregister, ToetsingAfgebroken, onvolledige_toetsing,
)
from tug_geo import circle_in_rd, json_voor_script, wgs84_to_rd  # noqa: E402
from tug_http import BronFout, haal, haal_json, veilige_bron_url  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent
LAT, LON = 52.531269, 6.135397
X, Y = wgs84_to_rd(LON, LAT)
PUNT = MultiPoint([(X, Y)])
CIRKEL = circle_in_rd(X, Y, 260)


def stil(_regel):
    pass


# ──────────────────────────────────────────────
# Nepnet
# ──────────────────────────────────────────────

class NepAntwoord:
    def __init__(self, status=200, inhoud=b"", headers=None):
        self.status_code = status
        self._inhoud = inhoud
        self.headers = headers or {}
        self.gesloten = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._inhoud), chunk_size):
            yield self._inhoud[i:i + chunk_size]

    def close(self):
        self.gesloten = True


def json_antwoord(data, status=200):
    return NepAntwoord(status, json.dumps(data).encode())


@pytest.fixture
def nepnet(monkeypatch):
    """Registreer per URL-fragment een antwoord (of uitzondering); de rest time-out."""
    regels: list[tuple[str, object]] = []
    aangeroepen: list[str] = []

    def request(_methode, url, **_kw):
        aangeroepen.append(url)
        for fragment, uitkomst in regels:
            if fragment in url:
                if isinstance(uitkomst, BaseException):
                    raise uitkomst
                return uitkomst() if callable(uitkomst) else uitkomst
        raise requests.Timeout("nepnet: geen antwoord")

    monkeypatch.setattr(tug_http.requests, "request", request)

    class Net:
        def zet(self, fragment, uitkomst):
            regels.append((fragment, uitkomst))

        @property
        def urls(self):
            return aangeroepen

    return Net()


# ──────────────────────────────────────────────
# S-03 / H-01 — tug_http
# ──────────────────────────────────────────────

class TestVeiligeBronUrl:

    @pytest.mark.parametrize("url", [
        "http://service.pdok.nl/x",                 # geen https
        "https://aanvaller.example/x",              # onbekende host
        "https://service.pdok.nl.aanvaller.example/x",
        "https://service.pdok.nl:8443/x",           # afwijkende poort
        "file:///etc/passwd",
        "https://[::1/",                            # ongeldig
    ])
    def test_weigert(self, url):
        with pytest.raises(BronFout):
            veilige_bron_url(url)

    @pytest.mark.parametrize("url", [
        "https://service.pdok.nl/lv/bag/wfs/v2_0",
        "https://www.ilent.nl/documenten/x.ods",    # subdomein van ilent.nl
    ])
    def test_staat_toe(self, url):
        assert veilige_bron_url(url) == url


class TestHaal:

    def test_redirect_naar_vreemde_host_wordt_niet_gevolgd(self, nepnet):
        nepnet.zet("service.pdok.nl", NepAntwoord(302, headers={
            "location": "https://aanvaller.example/buit"}))
        with pytest.raises(BronFout, match="aanvaller.example"):
            haal("https://service.pdok.nl/x", timeout=1, max_bytes=100)
        assert not any("aanvaller" in u for u in nepnet.urls)

    def test_redirect_naar_http_wordt_niet_gevolgd(self, nepnet):
        nepnet.zet("service.pdok.nl", NepAntwoord(301, headers={
            "location": "http://service.pdok.nl/x"}))
        with pytest.raises(BronFout, match="https"):
            haal("https://service.pdok.nl/x", timeout=1, max_bytes=100)

    def test_toegestane_redirect_wordt_gevolgd(self, nepnet):
        nepnet.zet("/oud", NepAntwoord(302, headers={"location": "/nieuw"}))
        nepnet.zet("/nieuw", NepAntwoord(200, b"ok"))
        assert haal("https://service.pdok.nl/oud", timeout=1, max_bytes=100) == b"ok"

    def test_te_groot_volgens_content_length(self, nepnet):
        nepnet.zet("pdok", NepAntwoord(200, b"x", {"content-length": "1000"}))
        with pytest.raises(BronFout, match="groter"):
            haal("https://service.pdok.nl/x", timeout=1, max_bytes=100)

    def test_te_groot_tijdens_het_lezen(self, nepnet):
        nepnet.zet("pdok", NepAntwoord(200, b"x" * 1000))     # geen content-length
        with pytest.raises(BronFout, match="afgebroken"):
            haal("https://service.pdok.nl/x", timeout=1, max_bytes=100)

    @pytest.mark.parametrize("uitkomst", [
        NepAntwoord(503), requests.ConnectionError("weg"), requests.Timeout("traag"),
    ])
    def test_storing_is_een_bronfout(self, nepnet, uitkomst):
        nepnet.zet("pdok", uitkomst)
        with pytest.raises(BronFout):
            haal("https://service.pdok.nl/x", timeout=1, max_bytes=100)

    @pytest.mark.parametrize("inhoud", [b"geen json", b"[1, 2]"])
    def test_json_moet_een_object_zijn(self, nepnet, inhoud):
        nepnet.zet("pdok", NepAntwoord(200, inhoud))
        with pytest.raises(BronFout):
            haal_json("https://service.pdok.nl/x", timeout=1, max_bytes=100)


# ──────────────────────────────────────────────
# S-01 — bronregister
# ──────────────────────────────────────────────

class TestBronregister:

    def test_slechtste_uitkomst_wint(self):
        r = Bronregister()
        r.mislukt("maneges", "zoekterm faalde")
        r.geraadpleegd("maneges")
        assert r.status("maneges").status == MISLUKT

    def test_afbreekbron_gooit(self):
        with pytest.raises(ToetsingAfgebroken):
            Bronregister().mislukt("natura2000", "WFS weg")

    def test_zonder_gemelde_uitkomst_is_een_bron_niet_geraadpleegd(self):
        r = Bronregister()
        r.controleer_compleet(["maneges"])
        assert r.status("maneges").status == MISLUKT

    def test_onvolledige_toetsing_negeert_weergavebronnen(self):
        r = Bronregister()
        r.mislukt("kaarttegels", "tegel weg")
        r.cache("nnn", 3)
        r.verouderd("lrk", 20, "verversen mislukt")
        state = {"ruimtelijk": {"bronstatus": r.naar_state()}}
        assert [u["sleutel"] for u in onvolledige_toetsing(state)] == ["lrk"]


# ──────────────────────────────────────────────
# S-01 — foutpaden per bron (B24)
# ──────────────────────────────────────────────

class TestAfbrekendeBronnen:
    """Zonder deze bronnen geen rapport: de toetsing breekt af."""

    def test_natura2000(self, nepnet):
        r = Bronregister()
        with pytest.raises(ToetsingAfgebroken):
            natuur.signaleer_natura2000(CIRKEL, stil, punten_rd=PUNT, bronnen=r)
        assert r.status("natura2000").status == MISLUKT

    def test_luchthavens_onbereikbaar(self, nepnet):
        with pytest.raises(ToetsingAfgebroken):
            brt.signaleer_luchthavens(CIRKEL, stil, punten_rd=PUNT, bronnen=Bronregister())

    def test_luchthavenlaag_zonder_luchthavens(self, nepnet):
        nepnet.zet("geodataoverijssel", json_antwoord({"features": []}))
        with pytest.raises(ToetsingAfgebroken, match="nul luchthavens"):
            brt.signaleer_luchthavens(CIRKEL, stil, punten_rd=PUNT, bronnen=Bronregister())

    def test_bag_tweede_pagina_timeout(self, nepnet, monkeypatch):
        pagina = {"features": [{"type": "Feature", "properties": {"identificatie": str(i)},
                                "geometry": {"type": "Point", "coordinates": [LON, LAT]}}
                               for i in range(bag.BAG_PAGE_SIZE)]}
        teller = {"n": 0}

        def antwoord():
            teller["n"] += 1
            if teller["n"] > 1:
                raise requests.Timeout("pagina 2")
            return json_antwoord(pagina)

        monkeypatch.setattr(tug_http.requests, "request", lambda *_a, **_k: antwoord())
        r = Bronregister()
        with pytest.raises(ToetsingAfgebroken):
            bag.haal_verblijfsobjecten(CIRKEL, stil, bronnen=r)
        assert r.status("bag_verblijfsobjecten").status == MISLUKT

    def test_gevelcheck(self, nepnet):
        feat = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [LON, LAT]},
                "properties": {"identificatie": "1", "pandidentificatie": "P1"}}
        with pytest.raises(ToetsingAfgebroken):
            bag.gevel_check([feat], CIRKEL, stil, bronnen=Bronregister())

    def test_ilt_register_zonder_lokaal_bestand(self, nepnet, tmp_path, monkeypatch):
        monkeypatch.setattr(klas, "REGISTER_ODS_PAD", tmp_path / "register.ods")
        monkeypatch.setattr(klas, "REGISTER_META_PAD", tmp_path / "register.meta.json")
        with pytest.raises(ToetsingAfgebroken):
            klas._download_register(stil, bronnen=Bronregister())


class TestZichtbareBronnen:
    """Deze bronnen breken niet af, maar staan als niet volledig in het register."""

    def test_maneges(self, nepnet):
        r = Bronregister()
        uit = brt.haal_maneges_pdok(CIRKEL, 250, stil, punten_rd=PUNT, bronnen=r)
        assert uit == {"in_straal": [], "buiten_straal": []}
        assert r.status("maneges").status == MISLUKT

    def test_begraafplaatsen_volgen_geen_vreemde_vervolglink(self, nepnet):
        nepnet.zet("location-api", json_antwoord({"features": []}))
        nepnet.zet("terrein_vlak", json_antwoord({
            "features": [{"properties": {}}] * brt.BRT_TERREIN_PAGE_SIZE,
            "links": [{"rel": "next", "href": "https://aanvaller.example/items?p=2"}],
        }))
        r = Bronregister()
        brt.signaleer_begraafplaatsen(CIRKEL, 250, stil, punten_rd=PUNT, bronnen=r)
        assert r.status("begraafplaatsen").status == MISLUKT
        assert not any("aanvaller" in u for u in nepnet.urls)

    def test_kinderopvang_zonder_cache(self, nepnet, tmp_path, monkeypatch):
        monkeypatch.setattr(onderwijs, "GEO_DIR", tmp_path)
        r = Bronregister()
        uit = onderwijs.haal_kdv_locaties(CIRKEL, stil, bronnen=r)
        assert uit == {"in_straal": [], "in_marge": []}
        assert r.status("lrk").status == MISLUKT
        assert not list(tmp_path.glob("*.part"))

    def test_kinderopvang_verouderde_cache_wordt_gemeld(self, nepnet, tmp_path, monkeypatch):
        monkeypatch.setattr(onderwijs, "GEO_DIR", tmp_path)
        monkeypatch.setattr(onderwijs, "haal_verblijfsobjecten", lambda *_a, **_k: [])
        csv = tmp_path / "lrk_kinderopvang.csv"
        csv.write_text("type_oko;bag_id;naam;adres\nKDV;0123;x;y\n", encoding="utf-8")
        tien_dagen = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(csv, (tien_dagen, tien_dagen))
        r = Bronregister()
        onderwijs.haal_kdv_locaties(CIRKEL, stil, bronnen=r)
        assert r.status("lrk").status == VEROUDERD
        assert r.status("lrk").ouderdom_dagen == 10

    def test_nnn_zonder_cache(self, nepnet, tmp_path, monkeypatch):
        monkeypatch.setattr(natuur, "GEO_DIR", tmp_path)
        monkeypatch.setattr(natuur, "NNN_GPKG", tmp_path / "nnn.gpkg")
        monkeypatch.setattr(natuur, "NNN_META", tmp_path / "nnn.meta.json")
        r = Bronregister()
        uit = natuur.signaleer_nnn(CIRKEL, stil, punten_rd=PUNT, bronnen=r)
        assert uit == {"in_straal": [], "in_signaal": []}
        assert r.status("nnn").status == MISLUKT

    def test_scholen_verouderde_cache_blijft_staan(self, nepnet, tmp_path, monkeypatch):
        monkeypatch.setattr(onderwijs, "GEO_DIR", tmp_path)
        monkeypatch.setattr(onderwijs, "SCHOLEN_META", tmp_path / "meta.json")
        geojson = tmp_path / "po.geojson"
        inhoud = json.dumps({"type": "FeatureCollection", "features": [{"x": 1}]})
        geojson.write_text(inhoud, encoding="utf-8")
        oud = (datetime.now() - timedelta(days=100)).isoformat()
        (tmp_path / "meta.json").write_text(json.dumps({"PO": {"timestamp": oud, "hash": "h"}}))
        r = Bronregister()
        uit = onderwijs._verwerk_scholen_groep("PO", ["PO"], geojson, stil, r)
        assert uit == [{"x": 1}]
        assert r.status("duo").status == VEROUDERD
        assert geojson.read_text(encoding="utf-8") == inhoud   # niet overschreven

    def test_adresaanvulling_is_geen_toetsingsbron(self, nepnet):
        feat = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [LON, LAT]},
                "properties": {"identificatie": "1"}}
        r = Bronregister()
        from tug_bronnen_geocode import vul_woonplaats_via_reverse_geocode
        vul_woonplaats_via_reverse_geocode([feat], stil, bronnen=r)
        state = {"ruimtelijk": {"bronstatus": r.naar_state()}}
        assert r.status("adresaanvulling").status == MISLUKT
        assert onvolledige_toetsing(state) == []


# ──────────────────────────────────────────────
# S-02 — kaarttegels
# ──────────────────────────────────────────────

class TestKaarttegels:

    def test_eps_tegel_wordt_niet_geopend(self, nepnet):
        eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 1 1\n%%EndComments\n"
        nepnet.zet("service.pdok.nl", NepAntwoord(200, eps))
        r = Bronregister()
        canvas = kaart._haal_tiles(LON, LAT, 15, 256, 256, stil, bronnen=r)
        assert canvas.size == (256, 256)
        assert r.status("kaarttegels").status == MISLUKT

    def test_png_tegel_wordt_geopend(self, nepnet):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (256, 256), (1, 2, 3)).save(buf, format="PNG")
        nepnet.zet("service.pdok.nl", NepAntwoord(200, buf.getvalue()))
        r = Bronregister()
        kaart._haal_tiles(LON, LAT, 15, 256, 256, stil, bronnen=r)
        assert r.status("kaarttegels").status == "geraadpleegd"


# ──────────────────────────────────────────────
# S-01 — rapport
# ──────────────────────────────────────────────

def _pdf_tekst(state):
    regels = []
    bouwer = _ProcesLogBuilder(mock.MagicMock(), state)
    bouwer.schrijf = lambda tekst, *_a, **_k: regels.append(tekst)
    return bouwer, regels


class TestRapport:

    def _state(self, **statussen):
        r = Bronregister()
        for sleutel, status in statussen.items():
            if status == MISLUKT:
                with suppress(ToetsingAfgebroken):   # afbreekbronnen gooien na vastleggen
                    r.mislukt(sleutel, "onbereikbaar")
            else:
                r.geraadpleegd(sleutel)
        return {"ruimtelijk": {"bronstatus": r.naar_state(), "punten": [[LAT, LON]],
                               "straal": 260}}

    def test_mislukte_natura2000_geeft_geen_conclusie(self):
        bouwer, regels = _pdf_tekst(self._state(natura2000=MISLUKT))
        bouwer._h4_n2000()
        tekst = " ".join(regels)
        assert "Geen conclusie" in tekst
        assert "ligt niet binnen" not in tekst

    def test_geraadpleegde_natura2000_geeft_wel_een_conclusie(self):
        bouwer, regels = _pdf_tekst(self._state(natura2000="geraadpleegd"))
        bouwer._h4_n2000()
        assert "Puntlocatie ligt niet binnen een Natura 2000-gebied." in regels

    def test_mislukte_nnn_geeft_geen_conclusie(self):
        bouwer, regels = _pdf_tekst(self._state(nnn=MISLUKT))
        bouwer._h5_nnn()
        assert "ligt niet binnen" not in " ".join(regels)

    def test_onvolledige_toetsing_staat_bovenaan(self):
        bouwer, regels = _pdf_tekst(self._state(maneges=MISLUKT))
        bouwer._h0_opening()
        assert "⚠ Onvolledige toetsing" in regels

    def test_state_zonder_bronstatus_geldt_niet_als_volledig(self):
        bouwer, regels = _pdf_tekst({"ruimtelijk": {"punten": [[LAT, LON]], "straal": 260}})
        bouwer._h0_opening()
        bouwer._h4_n2000()
        tekst = " ".join(regels)
        assert "Bronstatus ontbreekt" in tekst and "ligt niet binnen" not in tekst


# ──────────────────────────────────────────────
# H-06 — JSON in een scriptblok
# ──────────────────────────────────────────────

class TestJsonVoorScript:

    @pytest.mark.parametrize("naam", ["</script><script>alert(1)</script>",
                                      "<!--<script>", "a & b", "regel scheiding"])
    def test_rondreis_zonder_gevaarlijke_tekens(self, naam):
        tekst = json_voor_script({"naam": naam})
        assert not re.search(r"[<>&  ]", tekst)
        assert json.loads(tekst) == {"naam": naam}

    def test_html_export_bevat_geen_scriptafsluiting_uit_de_data(self, tmp_path, monkeypatch):
        import tug_05_output as uitvoer
        monkeypatch.setattr(uitvoer, "OUTPUT_DIR", tmp_path)
        kwaad = "</script><!--<script>"
        state = {
            "aanvraag": {"naam": kwaad},
            "ruimtelijk": {
                "lat": LAT, "lon": LON, "straal": 260, "datum_leesbaar": kwaad,
                "timestamp": "20260914_120000", "punten": [[LAT, LON]],
                "html_markers": [{"lat": LAT, "lon": LON, "categorie": "overig",
                                  "adres": kwaad, "pc_wpl": "", "gebruiksdoel": kwaad,
                                  "extra": ""}],
                "html_polygonen": [], "bronstatus": [],
            },
        }
        html = genereer_html(state, stil).read_text(encoding="utf-8")
        data = html.split("<script>\n", 1)[1].split("var map", 1)[0]
        assert "</script" not in data and "<!--" not in data
        assert "var ONVOLLEDIG=[];" in data


# ──────────────────────────────────────────────
# S-04 — schema aan de rand
# ──────────────────────────────────────────────

GELDIG = {
    "naam": "Zwartsluis 26 september", "soort_ontheffing": "locatiegebonden",
    "datum_vlucht": ["2026-10-20"], "vlucht_udp": True, "vlucht_start": None,
    "vlucht_einde": None, "aantal_vluchten": 12,
    "luchtvaartuigen": [{"registratie": "PH-ECE", "type": "heli"}],
    "coord_lat": [LAT], "coord_lon": [LON],
    "datum_ondertekening": "2026-09-01", "tijdstip_ondertekening": "16:29",
}


class TestSchema:

    def test_geldige_aanvraag(self):
        assert controleer_structuur(GELDIG) == []

    def test_ontbrekende_velden_zijn_geen_structuurfout(self):
        assert controleer_structuur({"naam": "x"}) == []

    @pytest.mark.parametrize(("wijziging", "verwacht"), [
        ({"straal_override": -500}, "onbekende veld"),
        ({"aantal_vluchten": True}, "aantal_vluchten"),
        ({"aantal_vluchten": 0}, "tussen"),
        ({"coord_lat": [LAT, "52.5"]}, "lijst van getallen"),
        ({"luchtvaartuigen": ["PH-ECE"]}, "moet een object zijn"),
        ({"luchtvaartuigen": [{"registratie": 12}]}, "moet tekst zijn"),
        ({"soort_ontheffing": "tijdelijk"}, "soort_ontheffing"),
        ({"naam": "x" * 500}, "langer dan"),
    ])
    def test_structuurfouten(self, wijziging, verwacht):
        fouten = controleer_structuur({**GELDIG, **wijziging})
        assert any(verwacht in f for f in fouten), fouten

    def test_geen_object(self):
        assert controleer_structuur(["lijst"])

    def test_toetsingsafstand_komt_alleen_uit_de_classificatie(self):
        with pytest.raises(SystemExit):
            ruimtelijk._straal_uit_state({"aanvraag": {"straal_override": 500}})
        with pytest.raises(SystemExit):
            ruimtelijk._straal_uit_state({"classificatie": {"norm_toepassing": -10}})
        assert ruimtelijk._straal_uit_state({"classificatie": {"norm_toepassing": 250}}) == \
            (250.0, 260.0)

    def test_luchtvaartuig_zonder_kenmerk_krijgt_geen_norm(self):
        import pandas as pd
        df = pd.DataFrame({"Registration": ["PH-ECE"], "ICAO": ["R44"]})
        uit = klas._classificeer_luchtvaartuig({"type": "heli"}, df, stil)
        assert uit["norm_m"] is None and uit["norm_bron"] == "geen_registratie"


# ──────────────────────────────────────────────
# S-06 — opruimen en rechten
# ──────────────────────────────────────────────

class TestStateOpruimen:

    @pytest.fixture
    def state_pad(self, tmp_path, monkeypatch):
        pad = tmp_path / "tug_state.json"
        monkeypatch.setattr(tug_run, "STATE_PAD", pad)
        return pad

    @pytest.mark.parametrize("fout", [KeyboardInterrupt(), tug_run._Onderbroken(15),
                                      tug_run._StapFout("x.py", 1)])
    def test_state_gewist_bij_elke_afloop(self, state_pad, fout):
        with mock.patch.object(tug_run, "_voer_stap_uit", side_effect=fout), \
                pytest.raises(type(fout)):
            tug_run._verwerk_aanvraag(GELDIG, "test")
        assert json.loads(state_pad.read_text()) == {}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-rechten")
    def test_state_heeft_privérechten(self, state_pad):
        state_pad.write_text("{}")
        state_pad.chmod(0o664)
        with mock.patch.object(tug_run, "_voer_stap_uit"):
            tug_run._verwerk_aanvraag(GELDIG, "test")
        assert stat.S_IMODE(state_pad.stat().st_mode) == 0o600

    def test_onvolledige_bron_geeft_exitcode_2(self, state_pad):
        r = Bronregister()
        r.mislukt("maneges", "weg")

        def stap(script, _beschrijving):
            if script.startswith("tug_05"):
                state = json.loads(state_pad.read_text())
                state["ruimtelijk"] = {"bronstatus": r.naar_state()}
                state_pad.write_text(json.dumps(state))

        with mock.patch.object(tug_run, "_voer_stap_uit", side_effect=stap):
            assert tug_run._verwerk_aanvraag(GELDIG, "test") == tug_run.EXIT_ONVOLLEDIG
        assert json.loads(state_pad.read_text()) == {}

    def test_onverwerkbare_aanvraag_wordt_afgewezen(self, tmp_path):
        pad = tmp_path / "aanvragen.json"
        pad.write_text(json.dumps([GELDIG, {**GELDIG, "straal_override": 900}]))
        goed, fout = tug_run._verzamel_aanvragen([str(pad)], None)
        assert len(goed) == 1 and len(fout) == 1


# ──────────────────────────────────────────────
# H-03 — beveiligingscontrole
# ──────────────────────────────────────────────

class TestBeveiligingsmeldingen:

    def _schrijf(self, pad, dagen_oud, bevindingen):
        datum = (datetime.now() - timedelta(days=dagen_oud)).isoformat()
        pad.write_text(json.dumps({"datum": datum, "bevindingen": bevindingen}))

    def test_ontbrekend(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tug_omgeving, "BEVEILIGINGSCONTROLE_PAD", tmp_path / "x.json")
        assert tug_omgeving.beveiligingsmeldingen()

    def test_recent_en_schoon(self, tmp_path, monkeypatch):
        pad = tmp_path / "x.json"
        monkeypatch.setattr(tug_omgeving, "BEVEILIGINGSCONTROLE_PAD", pad)
        self._schrijf(pad, 3, 0)
        assert tug_omgeving.beveiligingsmeldingen() == []

    @pytest.mark.parametrize(("dagen", "bevindingen"), [(40, 0), (3, 2)])
    def test_te_oud_of_met_bevindingen(self, tmp_path, monkeypatch, dagen, bevindingen):
        pad = tmp_path / "x.json"
        monkeypatch.setattr(tug_omgeving, "BEVEILIGINGSCONTROLE_PAD", pad)
        self._schrijf(pad, dagen, bevindingen)
        assert len(tug_omgeving.beveiligingsmeldingen()) == 1


# ──────────────────────────────────────────────
# S-05 — meegeleverde Leaflet is dezelfde als de vastgepinde
# ──────────────────────────────────────────────

class TestLeaflet:

    @pytest.mark.parametrize("bestand", ["leaflet.js", "leaflet.css"])
    def test_meegeleverd_bestand_heeft_de_sri_hash_van_de_export(self, bestand):
        inhoud = (PROJECT / "gui" / "vendor" / "leaflet" / bestand).read_bytes()
        sri = "sha384-" + b64encode(hashlib.sha384(inhoud).digest()).decode()
        export = (PROJECT / "gui" / "kaart_export.html").read_text(encoding="utf-8")
        assert f'integrity="{sri}"' in export
