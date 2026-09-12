"""
test_eenheden.py -- eenheidstests op de beslisregels van de workflow

Aanvulling op test_regressie.py, dat een andere vraag stelt. De regressietest
bewaakt omgevingsdrift: geeft dezelfde code met nieuwe bibliotheken nog hetzelfde
antwoord? Deze suite bewaakt de logica zelf: geeft de code het juiste antwoord?

Getoetst wordt alleen wat zonder netwerk te bepalen is — de regels die beslissen
hoe een aanvraag wordt beoordeeld en hoe een object in het rapport terechtkomt.
Brondata blijft er buiten; features worden hier synthetisch opgebouwd.

Draaien:
    .venv/bin/pytest test
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tug_01_validatie import MIN_INDIENTERMIJN_DAGEN, _controleer_indientermijn  # noqa: E402
from tug_03_kaart import _bereken_zoom  # noqa: E402
from tug_03_ruimtelijk import (  # noqa: E402
    _bouw_adresrijen,
    _bouw_html_markers,
    _bouw_classificatie_context,
    _nnn_via_n2000,
)
from tug_config import MAX_PUNT_AFSTAND_M, TOETSING_TOESLAG_M, toetsing_label  # noqa: E402
from tug_geo import feature_sleutel, naam_slug, puntlocaties, wgs84_to_rd  # noqa: E402
from tug_types import Bevindingen  # noqa: E402


# ──────────────────────────────────────────────
# Hulp: synthetische features
# ──────────────────────────────────────────────

# Willekeurig punt in Overijssel; de absolute ligging doet niet ter zake, alleen
# dat alles in deze tests hetzelfde stelsel gebruikt.
BASIS_LAT, BASIS_LON = 52.46126, 6.496964


def vbo(ident, straat="Dorpsstraat", nummer=1, doelen=("woonfunctie",),
        lat=BASIS_LAT, lon=BASIS_LON):
    """Een BAG-verblijfsobject zoals de WFS het teruggeeft."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "identificatie":   ident,
            "openbare_ruimte": straat,
            "huisnummer":      nummer,
            "postcode":        "8000 AA",
            "woonplaatsnaam":  "Zwolle",
            "gebruiksdoelen":  list(doelen),
        },
    }


def kdv(ident, lat=BASIS_LAT, lon=BASIS_LON):
    """Een KDV-locatie zoals hij na BAG-matching uit het LRK komt."""
    return vbo(ident, straat="Speelstraat", nummer=2, doelen=(), lat=lat, lon=lon)


def school(vbo_id, lat=BASIS_LAT, lon=BASIS_LON):
    """Een DUO-schoolvestiging: eigen structuur, gekoppeld via vbo_id."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "naam":          "De Regenboog",
            "onderwijstype": "PO",
            "adres":         "Schoolpad 3",
            "postcode":      "8000 CC",
            "woonplaats":    "ZWOLLE",
            "vbo_id":        vbo_id,
        },
    }


def begraafplaats(naam="Oude begraafplaats", lat=BASIS_LAT, lon=BASIS_LON, straal_m=60):
    """Een begraafplaats met een RD-polygoon eromheen, zoals de BRT-stap hem levert."""
    from shapely.geometry import Point

    x, y = wgs84_to_rd(lon, lat)
    return {
        "naam": naam, "adres": "Kerkweg 1", "pc_wpl": "8000 BB  Zwolle",
        "afstand_m": 0, "lat": lat, "lon": lon, "poly_rings": [],
        "_geom_rd": Point(x, y).buffer(straal_m),
    }


def bevindingen(**kwargs):
    """Een Bevindingen-record waarin alleen de genoemde bronnen gevuld zijn."""
    return Bevindingen(**kwargs)


# ──────────────────────────────────────────────
# Puntlocaties (B03)
# ──────────────────────────────────────────────

class TestPuntlocaties:

    def test_enkel_getal_wordt_lijst_met_een_punt(self):
        assert puntlocaties({"coord_lat": BASIS_LAT, "coord_lon": BASIS_LON}) == [
            (BASIS_LAT, BASIS_LON)
        ]

    def test_lijsten_worden_paarsgewijs_gekoppeld(self):
        punten = puntlocaties({
            "coord_lat": [BASIS_LAT, BASIS_LAT + 0.0002],
            "coord_lon": [BASIS_LON, BASIS_LON],
        })
        assert len(punten) == 2
        assert punten[0] == (BASIS_LAT, BASIS_LON)

    def test_ongelijke_lengte_is_een_fout(self):
        with pytest.raises(ValueError, match="evenveel"):
            puntlocaties({"coord_lat": [BASIS_LAT, BASIS_LAT], "coord_lon": [BASIS_LON]})

    def test_verwisselde_lat_lon_wordt_herkend(self):
        with pytest.raises(ValueError, match="niet in Nederland"):
            puntlocaties({"coord_lat": BASIS_LON, "coord_lon": BASIS_LAT})

    def test_punten_te_ver_uit_elkaar(self):
        # ~0,01 graad breedte is ruim 1 km, dus ver buiten de toegestane afstand.
        with pytest.raises(ValueError, match="uit elkaar"):
            puntlocaties({
                "coord_lat": [BASIS_LAT, BASIS_LAT + 0.01],
                "coord_lon": [BASIS_LON, BASIS_LON],
            })

    def test_grens_ligt_op_max_punt_afstand(self):
        """Net binnen de grens mag; dit bewaakt dat de grens niet verschuift."""
        graden_per_meter = 1 / 111_320
        net_binnen = (MAX_PUNT_AFSTAND_M - 5) * graden_per_meter
        punten = puntlocaties({
            "coord_lat": [BASIS_LAT, BASIS_LAT + net_binnen],
            "coord_lon": [BASIS_LON, BASIS_LON],
        })
        assert len(punten) == 2


# ──────────────────────────────────────────────
# Indientermijn (28 dagen)
# ──────────────────────────────────────────────

class TestIndientermijn:

    def test_ruim_op_tijd(self):
        ok, melding = _controleer_indientermijn(date(2026, 1, 1), [date(2026, 3, 1)])
        assert ok is True
        assert "voldoet" in melding

    def test_precies_op_de_grens_is_voldoende(self):
        """28 dagen is de eis; precies 28 dagen voldoet nog."""
        ok, _ = _controleer_indientermijn(date(2026, 1, 1), [date(2026, 1, 29)])
        assert ok is True

    def test_een_dag_te_laat(self):
        ok, melding = _controleer_indientermijn(date(2026, 1, 2), [date(2026, 1, 29)])
        assert ok is False
        assert "27 dag(en)" in melding

    def test_ondertekening_na_de_vlucht(self):
        ok, melding = _controleer_indientermijn(date(2026, 2, 1), [date(2026, 1, 29)])
        assert ok is False
        assert "ná de vroegste vluchtdatum" in melding

    def test_vroegste_vluchtdatum_telt(self):
        """Bij meerdere vluchtdata is de vroegste maatgevend, niet de eerste in de lijst."""
        ok, _ = _controleer_indientermijn(
            date(2026, 1, 1), [date(2026, 6, 1), date(2026, 1, 10)]
        )
        assert ok is False

    def test_eis_is_achtentwintig_dagen(self):
        assert MIN_INDIENTERMIJN_DAGEN == 28


# ──────────────────────────────────────────────
# Toetsingsafstand en kaartschaal
# ──────────────────────────────────────────────

class TestToetsingsafstand:

    def test_label_toont_lden_en_toeslag_apart(self):
        label = toetsing_label(510)
        assert "500" in label and str(TOETSING_TOESLAG_M) in label and "510" in label

    def test_grotere_straal_geeft_lager_zoomniveau(self):
        dichtbij = _bereken_zoom(BASIS_LAT, 300, 0.9, 1200)
        veraf    = _bereken_zoom(BASIS_LAT, 3000, 0.9, 1200)
        assert veraf < dichtbij

    def test_zoom_blijft_binnen_het_tegelbereik(self):
        assert 1 <= _bereken_zoom(BASIS_LAT, 1, 0.9, 1200) <= 19
        assert 1 <= _bereken_zoom(BASIS_LAT, 10_000_000, 0.9, 1200) <= 19


# ──────────────────────────────────────────────
# Bestandsnaam-slug
# ──────────────────────────────────────────────

class TestNaamSlug:

    def test_spaties_en_leestekens_worden_onderstrepingen(self):
        assert naam_slug("Ambt Delden 4 oktober") == "Ambt_Delden_4_oktober"

    def test_opeenvolgende_scheidingstekens_vallen_samen(self):
        """Spaties vallen samen tot één onderstreping; een koppelteken blijft."""
        assert naam_slug("a  --  b") == "a_--_b"

    def test_randen_worden_geschoond(self):
        assert naam_slug("  /pad/ ") == "pad"

    def test_lege_naam_geeft_lege_slug(self):
        assert naam_slug("") == ""
        assert naam_slug(None) == ""

    def test_lengte_is_begrensd(self):
        assert len(naam_slug("x" * 200)) <= 60


# ──────────────────────────────────────────────
# Featuresleutel (identiteit tussen lijsten)
# ──────────────────────────────────────────────

class TestFeatureSleutel:

    def test_bag_identificatie_is_de_sleutel(self):
        assert feature_sleutel(vbo("0193010000123456")) == "0193010000123456"

    def test_school_valt_terug_op_vbo_id(self):
        assert feature_sleutel(school("0193010000999999")) == "0193010000999999"

    def test_sleutel_overleeft_een_json_ronde(self):
        """De kern van de zaak: dezelfde inhoud moet dezelfde sleutel geven,
        ook als het object onderweg gekopieerd of geserialiseerd is."""
        origineel = vbo("0193010000123456")
        kopie     = json.loads(json.dumps(origineel))
        assert feature_sleutel(kopie) == feature_sleutel(origineel)

    def test_feature_zonder_identificatie_krijgt_stabiele_afgeleide_sleutel(self):
        naamloos = {"type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [BASIS_LON, BASIS_LAT]},
                    "properties": {"naam": "Iets zonder id"}}
        kopie = json.loads(json.dumps(naamloos))
        assert feature_sleutel(naamloos) == feature_sleutel(kopie)

    def test_verschillende_features_krijgen_verschillende_sleutels(self):
        assert feature_sleutel(vbo("A1")) != feature_sleutel(vbo("B2"))


# ──────────────────────────────────────────────
# Classificatie: wettelijk / marge / overig
# ──────────────────────────────────────────────

def rijen_van(bev):
    wettelijk, marge, aandacht, overig = _bouw_adresrijen(bev)
    return {"wettelijk": wettelijk, "marge": marge, "aandacht": aandacht, "overig": overig}


def adressen(rijen):
    return sorted(r["adres"] for r in rijen)


class TestClassificatie:

    def test_geluidgevoelig_vbo_in_straal_is_wettelijk(self):
        f = vbo("A1", "Dorpsstraat", 1)
        bev = bevindingen(alle_vbo=[f], geluidgevoelig_vbo=[f])
        rijen = rijen_van(bev)
        assert adressen(rijen["wettelijk"]) == ["Dorpsstraat 1"]
        assert rijen["overig"] == []

    def test_niet_geluidgevoelig_vbo_in_straal_is_overig(self):
        f = vbo("A1", "Loods", 9, doelen=("industriefunctie",))
        bev = bevindingen(alle_vbo=[f], geluidgevoelig_vbo=[])
        rijen = rijen_van(bev)
        assert adressen(rijen["overig"]) == ["Loods 9"]
        assert rijen["wettelijk"] == []

    def test_kdv_maakt_een_vbo_wettelijk_ook_zonder_geluidgevoelig_doel(self):
        f = vbo("A1", "Speelstraat", 2, doelen=("bijeenkomstfunctie",))
        bev = bevindingen(alle_vbo=[f], geluidgevoelig_vbo=[], kdv_in_straal=[kdv("A1")])
        rijen = rijen_van(bev)
        assert adressen(rijen["wettelijk"]) == ["Speelstraat 2"]

    def test_kdv_wordt_samengevoegd_en_niet_dubbel_opgevoerd(self):
        """Eén adres, één rij: het KDV-label komt bij het gebruiksdoel van het VBO."""
        f = vbo("A1", "Speelstraat", 2, doelen=("woonfunctie",))
        bev = bevindingen(alle_vbo=[f], geluidgevoelig_vbo=[f], kdv_in_straal=[kdv("A1")])
        rijen = rijen_van(bev)
        assert len(rijen["wettelijk"]) == 1
        doel = rijen["wettelijk"][0]["gebruiksdoel"]
        assert "woonfunctie" in doel and "KDV" in doel

    def test_kdv_zonder_eigen_vbo_krijgt_een_eigen_rij(self):
        bev = bevindingen(alle_vbo=[], geluidgevoelig_vbo=[], kdv_in_straal=[kdv("Z9")])
        rijen = rijen_van(bev)
        assert len(rijen["wettelijk"]) == 1
        assert "KDV" in rijen["wettelijk"][0]["gebruiksdoel"]

    def test_school_wordt_samengevoegd_met_het_vbo(self):
        f = vbo("A1", "Schoolpad", 3, doelen=("onderwijsfunctie",))
        bev = bevindingen(alle_vbo=[f], geluidgevoelig_vbo=[f], scholen_in_straal=[school("A1")])
        rijen = rijen_van(bev)
        assert len(rijen["wettelijk"]) == 1
        assert "school" in rijen["wettelijk"][0]["gebruiksdoel"]

    def test_vbo_in_margeband_is_marge(self):
        f = vbo("M1", "Randweg", 5)
        bev = bevindingen(alle_vbo=[], geluidgevoelig_vbo=[], marge_vbo=[f])
        rijen = rijen_van(bev)
        assert adressen(rijen["marge"]) == ["Randweg 5"]

    def test_niet_geluidgevoelig_in_margeband_is_overig(self):
        f = vbo("M2", "Schuur", 7, doelen=("industriefunctie",))
        bev = bevindingen(alle_vbo=[], geluidgevoelig_vbo=[], marge_vbo_overig=[f])
        rijen = rijen_van(bev)
        assert adressen(rijen["overig"]) == ["Schuur 7"]
        assert rijen["marge"] == []

    def test_kdv_in_margeband_tilt_een_overig_vbo_naar_marge(self):
        f = vbo("M3", "Speelstraat", 8, doelen=("bijeenkomstfunctie",))
        bev = bevindingen(alle_vbo=[], geluidgevoelig_vbo=[],
                          marge_vbo_overig=[f], kdv_in_marge=[kdv("M3")])
        rijen = rijen_van(bev)
        assert adressen(rijen["marge"]) == ["Speelstraat 8"]
        assert rijen["overig"] == []

    def test_manege_is_aandacht_geen_wettelijk(self):
        manege = {"naam": "De Hoefslag", "adres": "Ruiterweg 2", "pc_wpl": "8000 DD  Zwolle",
                  "afstand_m": 200, "lat": BASIS_LAT, "lon": BASIS_LON}
        bev = bevindingen(alle_vbo=[], geluidgevoelig_vbo=[], maneges_in_straal=[manege])
        rijen = rijen_van(bev)
        assert len(rijen["aandacht"]) == 1
        assert rijen["wettelijk"] == []


class TestBegraafplaatsUitsluiting:
    """Een adres binnen een begraafplaatspolygoon hoort niet nog eens los in het
    rapport: de begraafplaatsrij dekt het al."""

    def test_vbo_binnen_de_polygoon_verdwijnt_uit_de_adreslijsten(self):
        # Bewust een ander adres dan de begraafplaats zelf, anders is niet te
        # zien welke van de twee rijen er staat.
        binnen = vbo("B1", "Grafpad", 4)
        bev = bevindingen(alle_vbo=[binnen], geluidgevoelig_vbo=[binnen],
                          begraafplaatsen_in_straal=[begraafplaats()])
        rijen = rijen_van(bev)
        assert "Grafpad 4" not in adressen(rijen["wettelijk"])
        assert "Grafpad 4" not in adressen(rijen["overig"])
        # Alleen de begraafplaatsrij blijft over.
        assert adressen(rijen["wettelijk"]) == ["Kerkweg 1"]

    def test_de_begraafplaats_zelf_staat_er_wel(self):
        bev = bevindingen(alle_vbo=[], geluidgevoelig_vbo=[],
                          begraafplaatsen_in_straal=[begraafplaats()])
        rijen = rijen_van(bev)
        assert len(rijen["wettelijk"]) == 1
        assert "begraafplaats" in rijen["wettelijk"][0]["gebruiksdoel"]

    def test_vbo_buiten_de_polygoon_blijft_staan(self):
        ver_weg = vbo("B2", "Verweg", 1, lat=BASIS_LAT + 0.01)
        bev = bevindingen(alle_vbo=[ver_weg], geluidgevoelig_vbo=[ver_weg],
                          begraafplaatsen_in_straal=[begraafplaats()])
        rijen = rijen_van(bev)
        assert "Verweg 1" in adressen(rijen["wettelijk"])

    def test_uitsluiting_geldt_ook_voor_de_kaartmarkers(self):
        """Dit is de kern van bevinding B-01: tabel en kaart mogen niet uiteenlopen."""
        binnen = vbo("B1", "Grafpad", 4)
        bev = bevindingen(alle_vbo=[binnen], geluidgevoelig_vbo=[binnen],
                          begraafplaatsen_in_straal=[begraafplaats()])
        markers, _ = _bouw_html_markers(bev)
        assert "Grafpad 4" not in [m["adres"] for m in markers]


class TestTabelEnKaartLopenGelijk:
    """De PDF-tabel, de HTML-markers en de PNG-kaart moeten hetzelfde oordeel
    laten zien. Eén gedeelde bron van waarheid is precies wat dit bewaakt."""

    @staticmethod
    def _volledige_bevindingen():
        gev      = vbo("A1", "Dorpsstraat", 1)
        niet_gev = vbo("A2", "Loods", 9, doelen=("industriefunctie",))
        kdv_vbo  = vbo("A3", "Speelstraat", 2, doelen=("bijeenkomstfunctie",))
        marge    = vbo("M1", "Randweg", 5)
        marge_ov = vbo("M2", "Schuur", 7, doelen=("industriefunctie",))
        return bevindingen(
            alle_vbo=[gev, niet_gev, kdv_vbo],
            geluidgevoelig_vbo=[gev],
            marge_vbo=[marge],
            marge_vbo_overig=[marge_ov],
            kdv_in_straal=[kdv("A3")],
        )

    def test_dezelfde_adressen_zijn_wettelijk_in_tabel_en_op_de_kaart(self):
        bev = self._volledige_bevindingen()
        wettelijk, _, _, _ = _bouw_adresrijen(bev)
        markers, _ = _bouw_html_markers(bev)
        uit_tabel  = sorted(r["adres"] for r in wettelijk)
        uit_kaart  = sorted(m["adres"] for m in markers if m["categorie"] == "wettelijk")
        assert uit_tabel == uit_kaart

    def test_dezelfde_adressen_staan_in_de_margeband(self):
        bev = self._volledige_bevindingen()
        _, marge, _, _ = _bouw_adresrijen(bev)
        markers, _ = _bouw_html_markers(bev)
        assert sorted(r["adres"] for r in marge) == sorted(
            m["adres"] for m in markers if m["categorie"] == "marge"
        )

    def test_gebruiksdoel_is_in_tabel_en_kaart_gelijk(self):
        bev = self._volledige_bevindingen()
        wettelijk, _, _, _ = _bouw_adresrijen(bev)
        markers, _ = _bouw_html_markers(bev)
        per_adres_tabel = {r["adres"]: r["gebruiksdoel"] for r in wettelijk}
        per_adres_kaart = {m["adres"]: m["gebruiksdoel"] for m in markers
                           if m["categorie"] == "wettelijk"}
        assert per_adres_tabel == per_adres_kaart

    def test_oordeel_wordt_een_keer_geveld_en_door_beide_gelezen(self):
        """Beide presentatiefuncties moeten met een meegegeven context hetzelfde
        antwoord geven als wanneer ze hem zelf laten berekenen."""
        bev = self._volledige_bevindingen()
        context = _bouw_classificatie_context(bev)
        assert _bouw_adresrijen(bev, context=context) == _bouw_adresrijen(bev)
        assert _bouw_html_markers(bev, context=context) == _bouw_html_markers(bev)


class TestNatuurnetwerkAanname:
    """Natura 2000 ligt per definitie binnen het Natuurnetwerk. Valt de locatie in
    N2000 maar meldt de NNN-cache niets, dan is de cache verouderd en nemen we de
    NNN-treffer aan."""

    def test_n2000_treffer_zonder_nnn_treffer_vult_nnn_aan(self):
        n2000 = [{"naam": "Sallandse Heuvelrug", "afstand_m": 0.0, "punten_binnen": [1]}]
        aangevuld = _nnn_via_n2000(n2000, [])
        assert len(aangevuld) == 1
        assert "Sallandse Heuvelrug" in aangevuld[0]["naam"]
        assert aangevuld[0]["afstand_m"] == 0.0

    def test_bestaande_nnn_treffer_blijft_ongemoeid(self):
        n2000 = [{"naam": "Sallandse Heuvelrug", "afstand_m": 0.0}]
        nnn   = [{"naam": "NNN-gebied 12", "afstand_m": 0.0}]
        assert _nnn_via_n2000(n2000, nnn) == nnn

    def test_zonder_n2000_treffer_gebeurt_er_niets(self):
        assert _nnn_via_n2000([], []) == []
