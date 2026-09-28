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
from tug_geo import (  # noqa: E402
    feature_sleutel, max_onderlinge_afstand, naam_slug, puntafstand_melding, puntlocaties,
    wgs84_to_rd,
)
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

    def test_punten_ver_uit_elkaar_zijn_geen_fout(self):
        """B14: ruim 1 km uit elkaar levert een melding op, geen fout."""
        punten = puntlocaties({
            "coord_lat": [BASIS_LAT, BASIS_LAT + 0.01],
            "coord_lon": [BASIS_LON, BASIS_LON],
        })
        afstand = max_onderlinge_afstand(punten)
        assert 1000 < afstand < 1200
        assert f"meer dan {MAX_PUNT_AFSTAND_M} m" in puntafstand_melding(afstand)

    def test_grens_ligt_op_max_punt_afstand(self):
        """Net binnen de grens geen waarschuwing; dit bewaakt dat de grens niet verschuift."""
        graden_per_meter = 1 / 111_320
        net_binnen = (MAX_PUNT_AFSTAND_M - 5) * graden_per_meter
        punten = puntlocaties({
            "coord_lat": [BASIS_LAT, BASIS_LAT + net_binnen],
            "coord_lon": [BASIS_LON, BASIS_LON],
        })
        assert len(punten) == 2
        assert "meer dan" not in puntafstand_melding(max_onderlinge_afstand(punten))

    def test_een_punt_heeft_afstand_nul(self):
        assert max_onderlinge_afstand([(BASIS_LAT, BASIS_LON)]) == 0.0


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


# ──────────────────────────────────────────────
# B13 — geen herleidbare norm breekt de toetsing niet af
# ──────────────────────────────────────────────

class TestNormToepassing:

    def _resultaat(self, registratie, norm):
        return {"registratie": registratie, "norm_m": norm}

    def test_maximum_over_herleidbare_normen(self):
        from tug_02_classificatie import bepaal_norm_toepassing
        from tug_bronstatus import Bronregister
        bronnen = Bronregister()
        norm, herleidbaar = bepaal_norm_toepassing(
            [self._resultaat("PH-A", 150), self._resultaat("OO-B", None),
             self._resultaat("PH-C", 250)], lambda _: None, bronnen)
        assert (norm, herleidbaar) == (250, True)
        assert bronnen.status("afstandsnorm").volledig

    def test_zonder_norm_ruimste_inventarisatie_en_onvolledig(self):
        """Alleen buitenlandse toestellen: doorrekenen tot de ruimste norm, zonder
        conclusie — onvolledig (exitcode 2), niet afgebroken."""
        from tug_02_classificatie import INVENTARISATIE_NORM_M, NLR_TABEL, bepaal_norm_toepassing
        from tug_bronstatus import Bronregister, onvolledige_toetsing
        bronnen = Bronregister()
        norm, herleidbaar = bepaal_norm_toepassing(
            [self._resultaat("OO-EYP", None), self._resultaat("D-ABCD", None)],
            lambda _: None, bronnen)
        assert herleidbaar is False
        assert norm == INVENTARISATIE_NORM_M == max(n for _, n in NLR_TABEL.values() if n)
        state = {"classificatie": {"bronstatus": bronnen.naar_state()}}
        assert [u["sleutel"] for u in onvolledige_toetsing(state)] == ["afstandsnorm"]


def test_luchthavensignalering_tot_5000_m():
    """B17."""
    from tug_config import LUCHTHAVEN_GRENS_M, LUCHTHAVEN_SIGNAAL_M
    assert LUCHTHAVEN_SIGNAAL_M == 5000 > LUCHTHAVEN_GRENS_M


# ──────────────────────────────────────────────
# B12 — handmatige geluidsafstand; B21 — instelbare toeslag
# ──────────────────────────────────────────────

class TestHandmatigeAfstand:

    def _register(self, norm, bron="niet_in_register"):
        return {"registratie": "OO-EYP", "norm_m": norm, "norm_bron": bron,
                "signalen": ["✗ niet gevonden"]}

    def test_handmatig_gaat_voor_het_register(self):
        from tug_02_classificatie import pas_handmatige_afstand_toe
        r = pas_handmatige_afstand_toe({"registratie": "OO-EYP", "afstand_m": 300.0},
                                       self._register(None), lambda _: None)
        assert (r["norm_m"], r["norm_bron"], r["norm_register_m"]) == (300, "handmatig", None)
        assert not any(s.startswith("✗") for s in r["signalen"])

    def test_afwijking_van_het_register_wordt_gemeld(self):
        from tug_02_classificatie import pas_handmatige_afstand_toe
        r = pas_handmatige_afstand_toe({"registratie": "PH-ECE", "afstand_m": 150},
                                       self._register(250, "nlr_tabel"), lambda _: None)
        assert r["norm_m"] == 150 and "250 m" in r["signalen"][0]

    def test_zonder_afstand_ongewijzigd(self):
        from tug_02_classificatie import pas_handmatige_afstand_toe
        oud = self._register(None)
        assert pas_handmatige_afstand_toe({"registratie": "OO-EYP"}, oud, lambda _: None) is oud

    @pytest.mark.parametrize("afstand, fout", [(0, True), (2001, True), ("300", True),
                                               (True, True), (300, False), (12.5, False)])
    def test_schema_grenzen(self, afstand, fout):
        from tug_aanvraag import controleer_structuur
        aanvraag = {"luchtvaartuigen": [{"registratie": "OO-EYP", "afstand_m": afstand}]}
        assert bool(controleer_structuur(aanvraag)) is fout


class TestToeslag:

    @pytest.mark.parametrize("toeslag, fout", [(-1, True), (501, True), ("10", True),
                                               (0, False), (25, False), (12.5, False)])
    def test_schema_grenzen(self, toeslag, fout):
        from tug_aanvraag import controleer_structuur
        assert bool(controleer_structuur({"toeslag_m": toeslag})) is fout

    def test_standaard_en_opgegeven(self):
        from tug_aanvraag import toeslag_van
        assert toeslag_van({}) == TOETSING_TOESLAG_M
        assert toeslag_van({"toeslag_m": 25}) == 25.0

    def test_straal_telt_opgegeven_toeslag_op(self):
        import tug_03_ruimtelijk as ruimtelijk
        state = {"aanvraag": {"toeslag_m": 25}, "classificatie": {"norm_toepassing": 250}}
        assert ruimtelijk._straal_uit_state(state) == (250.0, 275.0)
        assert toetsing_label(275.0, 25) == "Toetsingsafstand TUG (250 m + 25 m = 275 m)"


# ──────────────────────────────────────────────
# B23 — luchthaventerreinen als vlak
# ──────────────────────────────────────────────

class TestLuchthaventerreinen:
    """Afstand tot de rand van het terrein, samenvoegen en koppelen, zonder netwerk."""

    @staticmethod
    def _vierkant(lon, lat, d=0.01):
        return {"type": "Polygon", "coordinates": [[[lon, lat], [lon + d, lat],
                [lon + d, lat + d], [lon, lat + d], [lon, lat]]]}

    def _draai(self, monkeypatch, terreinen, regelingen, punt, adres=("Weg 1", "1234 AB Plaats")):
        import tug_bronnen_brt as brt
        from shapely.geometry import MultiPoint
        from tug_bronstatus import Bronregister

        def nep_json(url, **_kw):
            if "functioneel_gebied_vlak" in url:
                return {"features": terreinen, "links": []}
            return {"features": regelingen}
        monkeypatch.setattr(brt, "haal_json", nep_json)
        monkeypatch.setattr(brt, "reverse_geocode_adres_wpl", lambda *_a, **_k: adres)
        x, y = wgs84_to_rd(punt[1], punt[0])
        return brt.signaleer_luchthavens(MultiPoint([(x, y)]).buffer(10), lambda _: None,
                                         punten_rd=MultiPoint([(x, y)]), bronnen=Bronregister())

    def test_punt_op_het_terrein_is_afstand_nul_en_overlap_is_een_terrein(self, monkeypatch):
        lon, lat = 6.88, 52.27
        terreinen = [
            {"properties": {"typefunctioneelgebied": "vliegveld, luchthaven", "naamnl": "Twente"},
             "geometry": self._vierkant(lon, lat, 0.03)},
            {"properties": {"typefunctioneelgebied": "zweefvliegveldterrein", "naamnl": "Twente"},
             "geometry": self._vierkant(lon + 0.005, lat + 0.005, 0.005)},
            {"properties": {"typefunctioneelgebied": "sportterrein", "naamnl": "Veld"},
             "geometry": self._vierkant(lon + 0.05, lat, 0.005)},
        ]
        res = self._draai(monkeypatch, terreinen, [{"properties": {"NAAM": "Ver"},
                          "geometry": {"type": "Point", "coordinates": [5.0, 53.0]}}],
                          (lat + 0.015, lon + 0.015))
        assert [(i["naam"], i["afstand_m"]) for i in res["in_straal"]] == [("Twente", 0)]
        assert "zweefvliegveld" in res["in_straal"][0]["omschrijving"]
        assert res["in_straal"][0]["poly_rings"]

    def test_afstand_tot_de_rand_niet_tot_het_midden(self, monkeypatch):
        """300 m buiten een groot terrein: binnen 1.000 m, ook al ligt het midden kilometers weg."""
        lon, lat = 6.88, 52.27
        terrein = [{"properties": {"typefunctioneelgebied": "vliegveld, luchthaven",
                                   "naamnl": "Groot"}, "geometry": self._vierkant(lon, lat, 0.04)}]
        res = self._draai(monkeypatch, terrein, [{"properties": {"NAAM": "Ver"},
                          "geometry": {"type": "Point", "coordinates": [5.0, 53.0]}}],
                          (lat - 300 / 111_320, lon + 0.02))
        assert len(res["in_straal"]) == 1
        assert 280 <= res["in_straal"][0]["afstand_m"] <= 320

    def test_regeling_geeft_naamloos_terrein_een_naam(self, monkeypatch):
        lon, lat = 6.89, 52.215
        heli = [{"properties": {"typefunctioneelgebied": "helikopterlandingsterrein"},
                 "geometry": self._vierkant(lon, lat, 0.0003)}]
        regeling = [{"properties": {"NAAM": "Helihaven MCT", "OMSCHRIJVING": "helihaven"},
                     "geometry": {"type": "Point", "coordinates": [lon + 0.0001, lat + 0.0001]}}]
        res = self._draai(monkeypatch, heli, regeling, (lat + 0.02, lon))
        alle = res["in_straal"] + res["in_signaal"]
        assert [i["naam"] for i in alle] == ["Helihaven MCT"]
        assert alle[0]["bron"] == "BRT Top10NL + provincie Overijssel"

    def test_naamloos_terrein_krijgt_het_adres(self, monkeypatch):
        lon, lat = 6.64, 52.336
        heli = [{"properties": {"typefunctioneelgebied": "helikopterlandingsterrein"},
                 "geometry": self._vierkant(lon, lat, 0.0003)}]
        res = self._draai(monkeypatch, heli, [{"properties": {"NAAM": "Ver"},
                          "geometry": {"type": "Point", "coordinates": [5.0, 53.0]}}],
                          (lat + 0.02, lon), adres=("Leemslagenweg 40", "7609 PN Almelo"))
        naam = res["in_signaal"][0]["naam"]
        assert naam.startswith("Helikopterlandingsterrein bij Leemslagenweg 40")


class TestZoekLocatie:
    """B18: de adreszoeker van de GUI-kaart, zonder netwerk."""

    def test_treffers_krijgen_punt_en_zoom_en_onbruikbare_vallen_weg(self, monkeypatch):
        import tug_bronnen_geocode as geo
        gevraagd = {}

        def nep_json(url, *, params, **_kw):
            gevraagd.update(params)
            return {"response": {"docs": [
                {"type": "adres", "weergavenaam": "Stationsplein 13A-1, Zwolle",
                 "centroide_ll": "POINT(6.09080195 52.50599301)"},
                {"type": "woonplaats", "weergavenaam": "Denekamp, Dinkelland, Overijssel",
                 "centroide_ll": "POINT(7.01310677 52.3889973)"},
                {"type": "perceel", "weergavenaam": "X", "centroide_ll": "POINT(6 52)"},
                {"type": "adres", "weergavenaam": "Zonder punt"},
            ]}}
        monkeypatch.setattr(geo, "haal_json", nep_json)
        treffers = geo.zoek_locatie("  stationsplein \n zwolle ")
        assert gevraagd["q"] == "stationsplein zwolle"
        assert [t["soort"] for t in treffers] == ["adres", "woonplaats"]
        assert treffers[0]["lat"] == pytest.approx(52.50599301)
        assert treffers[0]["lon"] == pytest.approx(6.09080195)
        assert treffers[0]["zoom"] > treffers[1]["zoom"]

    def test_te_korte_invoer_bevraagt_niets(self, monkeypatch):
        import tug_bronnen_geocode as geo
        monkeypatch.setattr(geo, "haal_json", lambda *_a, **_k: pytest.fail("bevraagd"))
        assert geo.zoek_locatie(" a ") == []

    def test_invoer_wordt_ingekort(self, monkeypatch):
        import tug_bronnen_geocode as geo
        gevraagd = {}
        monkeypatch.setattr(geo, "haal_json",
                            lambda _u, *, params, **_k: gevraagd.update(params) or {})
        geo.zoek_locatie("a" * 500)
        assert len(gevraagd["q"]) == geo.ZOEK_MAX_TEKENS
