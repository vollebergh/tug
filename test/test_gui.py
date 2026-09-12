"""
test_gui.py -- rooktest op de grafische schil

Toetst niet hoe het venster eruitziet, maar of de panelen samen nog dezelfde
aanvraag opleveren en dezelfde grenzen bewaken als de pipeline erachter. Draait
zonder scherm (offscreen); zonder PySide6 wordt de suite overgeslagen.
"""

import os
import sys
from pathlib import Path

import pytest

# Moet vóór de eerste Qt-import staan, anders zoekt Qt alsnog een scherm.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("PySide6", reason="PySide6 niet geïnstalleerd")

from PySide6.QtCore import QDate  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import tug_gui as gui  # noqa: E402
from tug_config import MAX_PUNT_AFSTAND_M, MIN_INDIENTERMIJN_DAGEN  # noqa: E402
from tug_geo import puntlocaties  # noqa: E402

LAT, LON = 52.46126, 6.496964


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


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

    def test_te_ver_uit_elkaar_wordt_gemeld(self, venster):
        venster.locaties.toevoegen(LAT, LON)
        venster.locaties.toevoegen(LAT + 0.01, LON)   # ruim een kilometer verderop
        assert venster.locaties.max_onderlinge_afstand() > MAX_PUNT_AFSTAND_M
        assert "uit elkaar" in venster.locaties.melding_afstand.text()

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
            "coord_lon", "datum_ondertekening", "tijdstip_ondertekening",
        }

    def test_de_pipeline_accepteert_de_puntlocaties(self, venster):
        """Wat het venster oplevert, moet de validatiestap zonder klacht inlezen."""
        vul_volledig(venster)
        assert puntlocaties(venster.aanvraag()) == [(LAT, LON)]

    def test_udp_uit_levert_start_en_eindtijd(self, venster):
        vul_volledig(venster)
        venster.gegevens.veld_udp.setChecked(False)
        aanvraag = venster.aanvraag()
        assert aanvraag["vlucht_udp"] is False
        assert aanvraag["vlucht_start"] and aanvraag["vlucht_einde"]
