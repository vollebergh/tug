"""
test_gui.py -- rooktest op de grafische schil

Toetst niet hoe het venster eruitziet, maar of de panelen samen nog dezelfde
aanvraag opleveren en dezelfde grenzen bewaken als de pipeline erachter. Draait
zonder scherm (offscreen); zonder PySide6 wordt de suite overgeslagen.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

# Moet vóór de eerste Qt-import staan, anders zoekt Qt alsnog een scherm.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6", reason="PySide6 niet geïnstalleerd")

from PySide6.QtCore import QDate, QUrl  # noqa: E402
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import tug_gui as gui  # noqa: E402
from tug_http import BronFout  # noqa: E402
from tug_aanvraag import controleer_structuur  # noqa: E402
from tug_config import MIN_INDIENTERMIJN_DAGEN, TOETSING_TOESLAG_M  # noqa: E402
from tug_geo import puntlocaties  # noqa: E402

LAT, LON = 52.46126, 6.496964


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def eigen_tmp(tmp_path, monkeypatch):
    """De schil leegt tmp/ bij het starten en sluiten; niet de echte projectmap."""
    monkeypatch.setattr(gui, "TMP_DIR", tmp_path / "tmp")


@pytest.fixture
def venster(app):
    v = gui.Hoofdvenster()
    yield v
    v.close()


def vul_volledig(v):
    v.gegevens.veld_naam.setText("Ambt Delden 4 oktober")
    v.toestellen.veld_registratie.setText("PH-ECE")
    v.toestellen.toevoegen()
    v.locaties.toevoegen(LAT, LON)


class TestInvulstatus:

    def test_lege_aanvraag_kan_niet_worden_gegenereerd(self, venster):
        assert not venster.knop_genereer.isEnabled()
        assert venster._ontbrekend() == [
            "omschrijving dossier", "luchtvaartuig", "puntlocatie"
        ]

    def test_volledige_aanvraag_ontgrendelt_de_knop(self, venster):
        vul_volledig(venster)
        assert venster.knop_genereer.isEnabled()
        assert "Gereed" in venster.status.text()


class TestLuchtvaartuigen:

    def test_registratie_wordt_in_hoofdletters_opgeslagen(self, venster):
        venster.toestellen.veld_registratie.setText("ph-ece")
        venster.toestellen.toevoegen()
        assert venster.toestellen.luchtvaartuigen[0]["registratie"] == "PH-ECE"

    def test_hetzelfde_kenmerk_kan_niet_twee_keer(self, venster):
        for _ in range(2):
            venster.toestellen.veld_registratie.setText("PH-ECE")
            venster.toestellen.toevoegen()
        assert len(venster.toestellen.luchtvaartuigen) == 1
        assert "staat al in de lijst" in venster.toestellen.melding.text()

    def test_handmatige_geluidsafstand_in_het_overzicht(self, venster):
        """B12: in de regel van het overzicht een afstand opgeven; 0 = uit register."""
        venster.toestellen.veld_registratie.setText("OO-EYP")
        venster.toestellen.toevoegen()
        rij = venster.toestellen.lijst.itemAt(0).widget()
        assert "afstand_m" not in venster.toestellen.velden()["luchtvaartuigen"][0]
        rij.veld_afstand.setValue(300)
        assert venster.toestellen.velden()["luchtvaartuigen"][0]["afstand_m"] == 300
        rij.veld_afstand.setValue(0)
        assert "afstand_m" not in venster.toestellen.velden()["luchtvaartuigen"][0]

    def test_afwijkend_kenmerk_wordt_gemeld_maar_toegevoegd(self, venster):
        venster.toestellen.veld_registratie.setText("XYZ")
        venster.toestellen.toevoegen()
        assert len(venster.toestellen.luchtvaartuigen) == 1
        assert "lijkt geen registratiekenmerk" in venster.toestellen.melding.text()


class TestPuntlocaties:

    def test_rd_invoer_komt_op_dezelfde_plek_uit(self, venster):
        x, y = gui.naar_rd(LAT, LON)
        venster.locaties.knop_rd.setChecked(True)
        venster.locaties._stelsel_gewijzigd()
        venster.locaties.veld_a.setValue(x)
        venster.locaties.veld_b.setValue(y)
        venster.locaties._handmatig_toevoegen()
        punt = venster.locaties.punten[-1]
        assert abs(punt["lat"] - LAT) < 1e-5
        assert abs(punt["lon"] - LON) < 1e-5

    def test_ver_uit_elkaar_geeft_dezelfde_melding(self, venster):
        venster.locaties.toevoegen(LAT, LON)
        venster.locaties.toevoegen(LAT + 0.01, LON)   # ruim een kilometer verderop
        tekst = venster.locaties.melding_afstand.text()
        assert "als geheel uitgevoerd op alle puntlocaties" in tekst
        assert venster.locaties.melding_afstand.objectName() == "hulp"

    def test_te_ver_uit_elkaar_blokkeert_niet(self, venster):
        """B14: een melding, geen blokkade — de knop blijft beschikbaar."""
        vul_volledig(venster)
        venster.locaties.toevoegen(LAT + 0.01, LON)
        assert venster.knop_genereer.isEnabled()

    def test_afstand_altijd_zichtbaar_bij_meerdere_punten(self, venster):
        venster.locaties.toevoegen(LAT, LON)
        venster.locaties.toevoegen(LAT + 0.0001, LON)   # ruim 10 m
        tekst = venster.locaties.melding_afstand.text()
        assert "Grootste onderlinge afstand" in tekst
        assert "als geheel uitgevoerd op alle puntlocaties" in tekst
        assert venster.locaties.melding_afstand.objectName() == "hulp"

    def test_verwijderen_herstelt_de_melding(self, venster):
        venster.locaties.toevoegen(LAT, LON)
        venster.locaties.toevoegen(LAT + 0.01, LON)
        venster.locaties.verwijderen(venster.locaties.punten[-1]["id"])
        assert venster.locaties.max_onderlinge_afstand() == 0.0
        assert not venster.locaties.melding_afstand.isVisible()


class TestIndientermijn:
    """Het venster moet dezelfde termijn bewaken als tug_01_validatie."""

    def test_te_korte_termijn_wordt_gemeld(self, venster):
        vlucht = venster.gegevens.veld_vlucht.date()
        venster.gegevens.veld_onder.setDate(vlucht.addDays(-(MIN_INDIENTERMIJN_DAGEN - 1)))
        assert "te laat ingediend" in venster.gegevens.melding_datum.text()

    def test_ruime_termijn_geeft_geen_melding(self, venster):
        venster.gegevens.veld_onder.setDate(QDate.currentDate())
        assert not venster.gegevens.melding_datum.isVisible()


class TestAanvraag:

    def test_aanvraag_bevat_precies_de_velden_van_de_pipeline(self, venster):
        vul_volledig(venster)
        aanvraag = venster.aanvraag()
        assert set(aanvraag) == {
            "naam", "soort_ontheffing", "datum_vlucht", "vlucht_udp", "vlucht_start",
            "vlucht_einde", "aantal_vluchten", "luchtvaartuigen", "coord_lat",
            "coord_lon", "datum_ondertekening", "tijdstip_ondertekening", "toeslag_m",
        }

    def test_toeslag_standaard_en_aanpasbaar(self, venster):
        """B21: standaard de vaste toeslag, maar vrij in te stellen."""
        vul_volledig(venster)
        assert venster.aanvraag()["toeslag_m"] == TOETSING_TOESLAG_M
        venster.locaties.veld_toeslag.setValue(25)
        assert venster.aanvraag()["toeslag_m"] == 25
        assert controleer_structuur(venster.aanvraag()) == []

    def test_de_pipeline_accepteert_de_puntlocaties(self, venster):
        """Wat het venster oplevert, moet de validatiestap zonder klacht inlezen."""
        vul_volledig(venster)
        assert puntlocaties(venster.aanvraag()) == [(LAT, LON)]

    def test_verborgen_velden_leveren_hun_standaardwaarden(self, venster):
        """B15: niet in beeld, wel in de aanvraag — met de standaardwaarden."""
        vul_volledig(venster)
        g, t = venster.gegevens, venster.toestellen
        if not gui.TOON_AANVULLENDE_VELDEN:
            for veld in (g.veld_soort, g.veld_aantal, g.veld_tijd, g.veld_udp,
                         g.veld_start, g.veld_einde, t.veld_type):
                assert not veld.isVisible()
        aanvraag = venster.aanvraag()
        assert aanvraag["soort_ontheffing"] == gui.SOORTEN_ONTHEFFING[0]
        assert aanvraag["aantal_vluchten"] == gui.STANDAARD_AANTAL_VLUCHTEN
        assert aanvraag["vlucht_udp"] is True
        assert aanvraag["tijdstip_ondertekening"]
        assert all(lv["type"] == gui.TYPEN_LUCHTVAARTUIG[0]
                   for lv in aanvraag["luchtvaartuigen"])

    def test_udp_uit_levert_start_en_eindtijd(self, venster):
        vul_volledig(venster)
        venster.gegevens.veld_udp.setChecked(False)
        aanvraag = venster.aanvraag()
        assert aanvraag["vlucht_udp"] is False
        assert aanvraag["vlucht_start"] and aanvraag["vlucht_einde"]


class TestKaartBeveiliging:
    """S-05: de ingebedde kaart laadt niets van buiten en navigeert nergens heen."""

    def test_geen_uitzonderingen_voor_lokale_inhoud(self, venster):
        instellingen = venster.kaart.web.settings()
        assert not instellingen.testAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls)
        assert not instellingen.testAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls)

    def test_kaartpagina_bevat_leaflet_zelf(self):
        html = gui.kaart_html({"titel": "</script><!--"})
        assert "unpkg" not in html and "L.map(" in html
        assert "__LEAFLET" not in html and "__CONFIG__" not in html
        assert "</script><!--" not in html

    def test_navigatie_na_laden_gaat_naar_de_systeembrowser(self, app, monkeypatch):
        geopend = []
        monkeypatch.setattr(gui.QDesktopServices, "openUrl",
                            lambda url: geopend.append(url.toString()))
        klik = QWebEnginePage.NavigationType.NavigationTypeLinkClicked
        pagina = gui.KaartPagina()
        assert pagina.acceptNavigationRequest(QUrl(gui.KAART_BASIS_URL), klik, True)
        pagina.geladen = True
        assert not pagina.acceptNavigationRequest(QUrl("https://leafletjs.com/"), klik, True)
        assert not pagina.acceptNavigationRequest(QUrl("file:///etc/passwd"), klik, True)
        assert geopend == ["https://leafletjs.com/"]


class TestAdreszoeker:
    """B18: zoeken op adres loopt via Python; de kaartpagina doet zelf geen verzoeken."""

    def _wacht_op_resultaat(self, app, brug, tekst):
        ontvangen = []
        brug.zoekResultaat.connect(lambda nr, js: ontvangen.append((nr, json.loads(js))))
        brug.zoekAdres(7, tekst)
        for _ in range(200):
            app.processEvents()
            if ontvangen:
                break
            time.sleep(0.01)
        return ontvangen

    def test_treffers_komen_terug_met_volgnummer(self, app, monkeypatch):
        treffer = {"naam": "Denekamp", "soort": "woonplaats", "lat": 52.39, "lon": 7.01, "zoom": 13}
        monkeypatch.setattr(gui, "zoek_locatie",
                            lambda tekst: [treffer] if tekst == "Denekamp" else [])
        ontvangen = self._wacht_op_resultaat(app, gui.Brug(), "Denekamp")
        assert ontvangen == [(7, {"treffers": [treffer]})]

    def test_bronfout_wordt_een_melding(self, app, monkeypatch):
        def mislukt(_tekst):
            raise BronFout("api.pdok.nl/bzk: time-out")
        monkeypatch.setattr(gui, "zoek_locatie", mislukt)
        ontvangen = self._wacht_op_resultaat(app, gui.Brug(), "Denekamp")
        assert ontvangen == [(7, {"fout": "api.pdok.nl/bzk: time-out"})]

    def test_kaartpagina_doet_geen_eigen_netwerkverzoeken(self):
        pagina = (gui.GUI_DIR / "kaart.html").read_text(encoding="utf-8")
        for verboden in ("fetch(", "XMLHttpRequest", "WebSocket", "pdok.nl/bzk"):
            assert verboden not in pagina
        # Namen uit de bron gaan als tekst de pagina in, niet als HTML.
        assert "createTextNode(t.naam)" in pagina


class TestOpruimen:
    """S-06: tijdelijke invoer blijft niet staan, ook niet na een harde afsluiting."""

    def test_tmp_wordt_bij_het_starten_geleegd(self, app):
        gui.TMP_DIR.mkdir(parents=True)
        (gui.TMP_DIR / "achtergebleven.json").write_text("{}")
        v = gui.Hoofdvenster()
        v.close()
        assert not list(gui.TMP_DIR.iterdir())

    def test_dossierveld_vraagt_geen_persoonsnamen(self, venster):
        assert "geen persoonsnamen" in venster.gegevens.veld_naam.placeholderText()
