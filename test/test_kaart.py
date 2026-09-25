"""
test_kaart.py -- rooktest op de kaartrendering

De kaart is de enige presentatievorm die niet als tekst te vergelijken is. Deze
tests controleren daarom niet hoe hij eruitziet, maar dat hij met alle soorten
lagen zonder fout tot stand komt en dat hij hetzelfde oordeel leest als de
adreslijsten en de HTML-markers.

De tegels komen normaal van PDOK; hier worden ze vervangen door een effen vlak,
zodat de test zonder netwerk draait en niets meet wat van een bron afhangt.
"""

import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tug_03_kaart as kaart  # noqa: E402
from tug_bronstatus import Bronregister  # noqa: E402
from tug_03_ruimtelijk import _bouw_classificatie_context  # noqa: E402
from tug_types import Bevindingen  # noqa: E402

LAT, LON = 52.46126, 6.496964
RING = [[[LON - 0.0005, LAT - 0.0005], [LON + 0.0005, LAT - 0.0005],
         [LON + 0.0005, LAT + 0.0005], [LON - 0.0005, LAT + 0.0005]]]


@pytest.fixture(autouse=True)
def geen_netwerk(monkeypatch):
    """Vervang de tegelhaler door een effen vlak."""
    def effen_vlak(_clon, _clat, _zoom, breedte, hoogte, _log, tile_url=None, *, bronnen):
        return Image.new("RGB", (breedte, hoogte), (120, 140, 120))

    monkeypatch.setattr(kaart, "_haal_tiles", effen_vlak)


def vbo(ident, straat, nr, doelen=("woonfunctie",), contour=None, pand=None):
    feat = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [LON, LAT]},
        "properties": {"identificatie": ident, "openbare_ruimte": straat, "huisnummer": nr,
                       "postcode": "8000 AA", "woonplaatsnaam": "Zwolle",
                       "gebruiksdoelen": list(doelen)},
    }
    if contour:
        feat["_contour"], feat["_pand_id"] = contour, pand
    return feat


def signalering(naam, afstand=0.0):
    return {"naam": naam, "adres": "", "pc_wpl": "", "afstand_m": afstand,
            "lat": LAT, "lon": LON, "poly_rings": RING, "omschrijving": ""}


@pytest.fixture
def alles():
    """Een Bevindingen-record waarin elke soort laag vertegenwoordigd is."""
    return Bevindingen(
        alle_vbo=[vbo("A1", "Dorpsstraat", 1, contour=RING, pand="P1"),
                  vbo("A2", "Loods", 9, ("industriefunctie",))],
        geluidgevoelig_vbo=[vbo("A1", "Dorpsstraat", 1)],
        marge_vbo=[vbo("M1", "Randweg", 5)],
        marge_vbo_overig=[vbo("M2", "Schuur", 7, ("industriefunctie",))],
        kdv_in_straal=[vbo("K1", "Speelstraat", 2, ())],
        begraafplaatsen_in_straal=[signalering("Begraafplaats")],
        maneges_in_straal=[signalering("Manege", 100)],
        n2000_in_straal=[signalering("Heide")],
        nnn_in_signaal=[signalering("NNN", 300)],
        luchthavens_in_straal=[signalering("Veld", 200)],
    )


def render(bev, **kwargs):
    context = _bouw_classificatie_context(bev)
    opties = {
        "bev": bev, "oordelen": context["oordelen"], "toon_legenda": True,
        "log": lambda _regel: None, "achtergrond": "satelliet", "punten": [(LAT, LON)],
        "toetsing_rings": RING, "signaal_rings": RING, "bronnen": Bronregister(),
    }
    opties.update(kwargs)
    return kaart._render_kaart(LON, LAT, 15, 800, 600, 510.0, **opties)


class TestRendering:

    def test_alle_lagen_renderen_zonder_fout(self, alles):
        img = render(alles)
        assert img.size == (800, 600)

    def test_er_wordt_daadwerkelijk_op_getekend(self, alles):
        """Een effen vlak zou betekenen dat geen enkele laag is getekend."""
        leeg = render(Bevindingen(), toon_legenda=False)
        vol  = render(alles, toon_legenda=False)
        assert len(vol.getcolors(maxcolors=100_000)) > len(leeg.getcolors(maxcolors=100_000))

    def test_lege_bevindingen_leveren_nog_steeds_een_kaart(self):
        img = render(Bevindingen())
        assert img.size == (800, 600)

    def test_toeslagcirkel_rond_puntlocatie(self):
        """B20: de toeslag rond de puntlocatie staat op de kaart."""
        context = _bouw_classificatie_context(Bevindingen())
        img = kaart._render_kaart(
            LON, LAT, 19, 800, 600, 510.0, bev=Bevindingen(), oordelen=context["oordelen"],
            toon_legenda=False, log=lambda _regel: None, punten=[(LAT, LON)],
            bronnen=Bronregister())
        kleuren = {kleur for _n, kleur in img.getcolors(maxcolors=100_000)}
        assert kaart.KLEUR_TOESLAG in kleuren

    @pytest.mark.parametrize("achtergrond", ["satelliet", "topografisch"])
    def test_beide_achtergronden(self, alles, achtergrond):
        assert render(alles, achtergrond=achtergrond).size == (800, 600)


class TestOordeelWordtGelezen:
    """De kaart hangt alleen nog kleur en tekenvolgorde aan het oordeel; hij
    velt het niet zelf. Deze tests bewaken dat die scheiding blijft bestaan."""

    def test_kaart_leest_dezelfde_banden_als_de_classificatie(self, alles):
        oordelen = _bouw_classificatie_context(alles)["oordelen"]
        assert set(oordelen) == {"straal", "marge", "marge_overig"}
        gebruikt = {o.categorie for band in oordelen.values() for o in band}
        assert gebruikt <= {"wettelijk", "marge", "overig"}

    def test_uitgesloten_verblijfsobject_wordt_niet_getekend(self):
        """Wat de begraafplaatsuitsluiting uit de tabel haalt, hoort ook van de
        kaart weg te blijven — anders spreken kaart en rapport elkaar tegen."""
        from shapely.geometry import Point

        from tug_geo import wgs84_to_rd

        x, y = wgs84_to_rd(LON, LAT)
        begraafplaats = signalering("Begraafplaats")
        begraafplaats["_geom_rd"] = Point(x, y).buffer(60)
        binnen = vbo("B1", "Grafpad", 4)

        bev = Bevindingen(alle_vbo=[binnen], geluidgevoelig_vbo=[binnen],
                          begraafplaatsen_in_straal=[begraafplaats])
        oordelen = _bouw_classificatie_context(bev)["oordelen"]
        assert oordelen["straal"] == []
        assert render(bev).size == (800, 600)
