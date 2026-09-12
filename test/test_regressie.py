"""
test_regressie.py -- regressietest TUG-ontheffingen

Doel: vaststellen of een upgrade van Python of van de libraries de uitkomst van
de pipeline verandert. Niet of de pipeline "werkt", maar of hij hetzelfde
antwoord geeft als op het moment dat de uitkomst nog vertrouwd was.

Gebruik:
    .venv/bin/python test/test_regressie.py            # vergelijk met referentie
    .venv/bin/python test/test_regressie.py --herijk   # referentie opnieuw vastleggen

Werkwijze bij een upgrade: pins aanpassen in requirements.txt, installeren,
deze test draaien. Geen verschillen = veilig. Wel verschillen = de test noemt
per meting de oude en de nieuwe waarde, en dan is het een inhoudelijke
beoordeling of dat een verbetering of een regressie is.

Herijken doe je alleen als een verschil een bewuste, goedgekeurde wijziging is.
Herijken om een onverklaarde afwijking weg te poetsen maakt de test waardeloos.


Twee ontwerpkeuzes bepalen waar deze test wel en niet op reageert.

1. Tolerantie op meterniveau, niet exacte gelijkheid.
   Coördinaten en afstanden worden vergeleken met TOLERANTIE_M marge. Een
   nieuwe PROJ- of shapely-versie die een uitkomst met een fractie van een
   millimeter verschuift, verandert geen enkel document en hoort de test dus
   niet te laten struikelen. Pas een afwijking in de orde van meters raakt de
   signalering en de rapportage.

2. Brondata blijft buiten de vergelijking.
   BAG, LRK, DUO, PDOK en het ILT-luchtvaartuigregister zijn de
   verantwoordelijkheid van de bronhouder en gelden hier als waarheid. Een
   gewijzigd aantal registerregels of een hertekende gebiedsgrens is geen
   regressie en mag geen alarm geven. Waar de bibliotheken toch getoetst
   moeten worden op geometrie en geo-IO, gebeurt dat daarom op synthetische
   geometrie die in deze test zelf wordt opgebouwd: volledig deterministisch
   en onafhankelijk van welke versie van welk bestand er in geo/ staat.

   Van de brongebonden code wordt wél getoetst dat het mechanisme werkt — de
   kolommen worden gevonden, een opzoeking levert een resultaat in het
   verwachte formaat, de signalering geeft de afgesproken structuur terug —
   maar nooit welke waarden de bron op dit moment bevat.
"""

import json
import sys
import tempfile
import traceback
from pathlib import Path

TEST_DIR    = Path(__file__).resolve().parent
PROJECT_DIR = TEST_DIR.parent
GOLDEN      = TEST_DIR / "golden" / "referentie.json"

sys.path.insert(0, str(PROJECT_DIR))

# Toegestane afwijking voor coördinaten en afstanden, in meters. Ruis onder deze
# grens verandert de uitkomst van de workflow niet: afstanden worden in hele
# meters gerapporteerd en de signaleringsgrenzen liggen op honderden meters.
TOLERANTIE_M = 1.0

# Metingen waarop TOLERANTIE_M geldt in plaats van exacte gelijkheid. De sleutel
# is een pad-prefix; alles daaronder (ook lijstelementen) valt eronder.
TOLERANTIES = {
    "transformaties.punt_1_rd":            TOLERANTIE_M,
    "transformaties.punt_2_rd":            TOLERANTIE_M,
    "transformaties.punt_3_rd":            TOLERANTIE_M,
    "transformaties.sluitfout_max_m":      TOLERANTIE_M,
    "geometrie.cirkel_500m_omtrek_m":      TOLERANTIE_M,
    "geometrie.vierkant_rd_zijden_m":      TOLERANTIE_M,
    "geometrie.afstanden_punt_polygoon_m": TOLERANTIE_M,
    "geopackage_io.afstanden_m":           TOLERANTIE_M,
    "geopackage_io.oppervlakken_m2":       500.0,   # m², niet m: 500 m² op ~0,8 ha
    "geometrie.cirkel_500m_oppervlak_m2":  500.0,
}


# ──────────────────────────────────────────────
# Vaste invoer
# ──────────────────────────────────────────────

# Twee puntlocaties van één aanvraag, minder dan 100 m uit elkaar (B03).
PUNTEN_AANVRAAG = [
    (52.25930771424353, 6.750056994102494),
    (52.25944306802954, 6.75065499410406),
]

# Drie punten verspreid over Nederland, ruim binnen het RD-geldigheidsgebied.
IJKPUNTEN = [
    (52.0907374, 5.1214201),   # Utrecht, Domtoren
    (53.2193835, 6.5665018),   # Groningen, Martinitoren
    (51.4416090, 5.4697225),   # Eindhoven, centrum
]

# Synthetische gebieden in RD, opgebouwd rond een vast middelpunt. Deze
# vervangen de rol die PDOK-gebieden zouden spelen: ze toetsen shapely, pyproj,
# geopandas en pyogrio even goed, maar veranderen nooit onder de test.
MIDDELPUNT_RD = (233000.0, 582000.0)


def _synthetische_gebieden():
    """Drie vierkanten op bekende afstand van MIDDELPUNT_RD.

    Afstand punt→vierkant is met de hand na te rekenen: bij een vierkant met
    zijde 2a waarvan het middelpunt dx naar rechts ligt, is de afstand van de
    oorsprong tot de rand dx - a.
    """
    from shapely.geometry import Polygon
    cx, cy = MIDDELPUNT_RD

    def vierkant(dx, dy, halve_zijde):
        x, y = cx + dx, cy + dy
        h    = halve_zijde
        return Polygon([(x - h, y - h), (x + h, y - h),
                        (x + h, y + h), (x - h, y + h)])

    return {
        # middelpunt 300 m naar rechts, halve zijde 100 → rand op 200 m
        "nabij":   vierkant(300.0, 0.0, 100.0),
        # middelpunt 1500 m naar boven, halve zijde 200 → rand op 1300 m
        "ver":     vierkant(0.0, 1500.0, 200.0),
        # om het middelpunt heen: punt ligt erbinnen, afstand 0
        "omvat":   vierkant(0.0, 0.0, 250.0),
    }


# ──────────────────────────────────────────────
# Metingen — bibliotheekgedrag
# ──────────────────────────────────────────────

def meet_transformaties():
    """pyproj/PROJ: WGS84 ↔ RD New op drie ijkpunten."""
    from tug_geo import wgs84_to_rd, make_transformer

    uit = {}
    naar_wgs   = make_transformer("EPSG:28992", "EPSG:4326")
    sluitfouten = []

    for i, (lat, lon) in enumerate(IJKPUNTEN, 1):
        x, y = wgs84_to_rd(lon, lat)
        # Op millimeter vastgelegd omdat dat informatief is bij het lezen van
        # de referentie; de vergelijking gebeurt met TOLERANTIE_M marge.
        uit[f"punt_{i}_rd"] = [round(x, 3), round(y, 3)]

        lon_r, lat_r = naar_wgs.transform(x, y)
        x_r, y_r     = wgs84_to_rd(lon_r, lat_r)
        sluitfouten.append(((x_r - x) ** 2 + (y_r - y) ** 2) ** 0.5)

    # De grootste sluitfout van de heen-en-terugtransformatie. Hoort ver onder
    # een meter te blijven; loopt hij op tot meters, dan is de keten veranderd.
    uit["sluitfout_max_m"] = round(max(sluitfouten), 6)
    return uit


def meet_geometrie():
    """shapely: buffer, oppervlak, omtrek, geometrie-transform, afstanden."""
    from shapely.geometry import Point, Polygon
    from tug_geo import (circle_in_rd, circle_bbox_wgs84, wgs84_to_rd,
                         transform_geom_to_rd, _geom_rings_wgs84)

    lat, lon = PUNTEN_AANVRAAG[0]
    x, y     = wgs84_to_rd(lon, lat)

    cirkel = circle_in_rd(x, y, 500.0)
    uit = {
        # Het aantal punten legt vast met hoeveel segmenten shapely een cirkel
        # benadert (quad_segs). Verandert die default, dan verschuift ook de
        # signalering aan de rand — dus exact vergelijken, geen tolerantie.
        "cirkel_500m_aantal_punten":  len(cirkel.exterior.coords),
        "cirkel_500m_oppervlak_m2":   round(cirkel.area),
        "cirkel_500m_omtrek_m":       round(cirkel.length, 3),
    }

    # bbox in graden; omgerekend naar meters zodat de tolerantie in meters geldt.
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(cirkel)
    uit["cirkel_bbox_breedte_m"] = round(
        Point(*wgs84_to_rd(lon_max, lat_min)).distance(
            Point(*wgs84_to_rd(lon_min, lat_min))), 3)
    uit["cirkel_bbox_hoogte_m"] = round(
        Point(*wgs84_to_rd(lon_min, lat_max)).distance(
            Point(*wgs84_to_rd(lon_min, lat_min))), 3)

    # Vierkant van 0,01° in WGS84 naar RD: controleert transform_geom_to_rd.
    vierkant    = Polygon([(lon, lat), (lon + 0.01, lat),
                           (lon + 0.01, lat + 0.01), (lon, lat + 0.01)])
    vierkant_rd = transform_geom_to_rd(vierkant)
    hoeken      = list(vierkant_rd.exterior.coords)[:4]
    uit["vierkant_rd_zijden_m"] = [
        round(Point(hoeken[i]).distance(Point(hoeken[(i + 1) % 4])), 3)
        for i in range(4)
    ]

    ringen = _geom_rings_wgs84(vierkant)
    uit["ringen_aantal"]       = len(ringen)
    uit["ringen_punten"]       = len(ringen[0])
    uit["ringen_leeg_bij_punt"] = _geom_rings_wgs84(Point(lon, lat)) == []

    # Afstanden punt → synthetische polygonen; met de hand na te rekenen.
    punt    = Point(*MIDDELPUNT_RD)
    gebieden = _synthetische_gebieden()
    uit["afstanden_punt_polygoon_m"] = {
        naam: round(punt.distance(geom), 3)
        for naam, geom in sorted(gebieden.items())
    }
    uit["punt_binnen_omvat"] = gebieden["omvat"].intersects(punt)
    uit["punt_binnen_nabij"] = gebieden["nabij"].intersects(punt)
    return uit


def meet_geopackage_io():
    """geopandas + pyogrio: GeoPackage schrijven, terugleggen, bbox-filteren.

    Dit dekt dezelfde keten als de NNN-signalering (schrijven in EPSG:28992,
    inlezen met een bbox-filter, afstanden bepalen) maar op geometrie die hier
    wordt opgebouwd, zodat PDOK-wijzigingen de test niet raken.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    gebieden = _synthetische_gebieden()
    namen    = sorted(gebieden)
    gdf = gpd.GeoDataFrame(
        {"naam": namen},
        geometry=[gebieden[n] for n in namen],
        crs="EPSG:28992",
    )

    punt = Point(*MIDDELPUNT_RD)
    uit  = {}

    with tempfile.TemporaryDirectory() as tmp:
        pad = Path(tmp) / "synthetisch.gpkg"
        gdf.to_file(str(pad), driver="GPKG", layer="gebieden")

        import pyogrio
        uit["lagen"] = [rij[0] for rij in pyogrio.list_layers(str(pad))]
        info = pyogrio.read_info(str(pad), layer="gebieden")
        uit["aantal_features"] = int(info["features"])
        uit["crs"]             = str(info["crs"])

        # Volledige inleesronde: komt alles terug zoals het erin ging?
        terug = gpd.read_file(str(pad), layer="gebieden")
        uit["namen_terug"]     = sorted(terug["naam"].tolist())
        uit["oppervlakken_m2"] = {
            r["naam"]: round(r.geometry.area)
            for _, r in terug.sort_values("naam").iterrows()
        }
        uit["afstanden_m"] = {
            r["naam"]: round(punt.distance(r.geometry), 3)
            for _, r in terug.sort_values("naam").iterrows()
        }

        # bbox-filter van 500 m rond het punt: 'ver' (rand op 1300 m) hoort
        # buiten de selectie te vallen, de andere twee erbinnen.
        minx, miny, maxx, maxy = punt.buffer(500.0).bounds
        gefilterd = gpd.read_file(str(pad), layer="gebieden",
                                  bbox=(minx, miny, maxx, maxy))
        uit["bbox_500m_namen"] = sorted(gefilterd["naam"].tolist())

    return uit


# ──────────────────────────────────────────────
# Metingen — projectlogica (geen brondata)
# ──────────────────────────────────────────────

def meet_puntlocaties():
    """Validatie van puntlocaties, inclusief de onderlinge-afstandsgrens (B03)."""
    from tug_geo import puntlocaties

    def vang(aanvraag):
        try:
            return {"ok": True, "aantal": len(puntlocaties(aanvraag))}
        except ValueError as e:
            return {"ok": False, "fout": str(e)}

    lats = [la for la, _ in PUNTEN_AANVRAAG]
    lons = [lo for _, lo in PUNTEN_AANVRAAG]

    return {
        "geldig_twee_punten": vang({"coord_lat": lats, "coord_lon": lons}),
        "geldig_enkel_getal": vang({"coord_lat": lats[0], "coord_lon": lons[0]}),
        "te_ver_uit_elkaar":  vang({"coord_lat": [52.0907374, 52.1907374],
                                    "coord_lon": [5.1214201, 5.1214201]}),
        "buiten_nederland":   vang({"coord_lat": [5.1214201],
                                    "coord_lon": [52.0907374]}),
        "lengte_mismatch":    vang({"coord_lat": [52.09, 52.10],
                                    "coord_lon": [5.12]}),
        "lege_lijst":         vang({"coord_lat": [], "coord_lon": []}),
        "geen_getal":         vang({"coord_lat": ["abc"], "coord_lon": [5.12]}),
    }


def meet_datumlogica():
    """Termijnbewaking: de 28-dageneis en de normalisatie van datum_vlucht."""
    from datetime import date
    import tug_01_validatie as v

    def indientermijn(onder, vluchten):
        ok, melding = v._controleer_indientermijn(
            date.fromisoformat(onder), [date.fromisoformat(d) for d in vluchten]
        )
        return {"ok": ok, "melding": melding}

    return {
        "ruim_op_tijd":      indientermijn("2025-09-04", ["2025-10-04"]),
        "precies_28_dagen":  indientermijn("2025-09-06", ["2025-10-04"]),
        "een_dag_te_kort":   indientermijn("2025-09-07", ["2025-10-04"]),
        "na_de_vlucht":      indientermijn("2025-10-05", ["2025-10-04"]),
        "vroegste_van_meer": indientermijn("2025-09-04", ["2025-11-01", "2025-10-04"]),
        "normalisatie_string": v._normaliseer_datum_vlucht({"datum_vlucht": "2025-10-04"}),
        "normalisatie_lijst":  v._normaliseer_datum_vlucht({"datum_vlucht": ["2025-10-04"]}),
        "normalisatie_leeg":   v._normaliseer_datum_vlucht({"datum_vlucht": []}),
        "normalisatie_getal":  v._normaliseer_datum_vlucht({"datum_vlucht": 20251004}),
        "parse_geldig":        [str(x) for x in v._parse_datum("2025-10-04", "veld")],
        "parse_ongeldig":      [str(x) for x in v._parse_datum("04-10-2025", "veld")],
    }


def meet_normtabel():
    """De NLR-indelingslijst staat in de code, niet in een bron: exact vergelijken."""
    import tug_02_classificatie as c
    return {
        "aantal_icao_codes": len(c.NLR_TABEL),
        "volledige_tabel":   {k: list(c.NLR_TABEL[k]) for k in sorted(c.NLR_TABEL)},
    }


def meet_config():
    """Afgeleide constanten en labelopmaak."""
    from tug_config import (toetsing_label, MAX_PUNT_AFSTAND_M, MARGE_M,
                            N2000_SIGNAAL_MARGE, NNN_SIGNAAL_MARGE,
                            TOETSING_TOESLAG_M)
    return {
        "toetsing_label_160": toetsing_label(160),
        "toetsing_label_510": toetsing_label(510),
        "toetsing_toeslag_m": TOETSING_TOESLAG_M,
        "marge_m":            MARGE_M,
        "max_punt_afstand_m": MAX_PUNT_AFSTAND_M,
        "n2000_marge_m":      N2000_SIGNAAL_MARGE,
        "nnn_marge_m":        NNN_SIGNAAL_MARGE,
    }


# ──────────────────────────────────────────────
# Metingen — mechanisme op brondata, zonder de waarden vast te leggen
# ──────────────────────────────────────────────

def meet_register_mechanisme():
    """pandas + odfpy: laat het ILT-register zich lezen en de kolommen vinden?

    Legt bewust géén rijaantallen of ICAO-codes vast: dat is brondata en die is
    van de bronhouder. Getoetst wordt alleen of het leesmechanisme werkt en of
    een opzoeking een resultaat in het verwachte formaat oplevert.
    """
    import re
    import pandas as pd
    import tug_02_classificatie as c

    if not c.REGISTER_ODS_PAD.exists():
        return {"_overgeslagen": f"{c.REGISTER_ODS_PAD.name} ontbreekt"}

    df = pd.read_excel(c.REGISTER_ODS_PAD, engine="odf", header=0, dtype=str)
    reg_kolom, icao_kolom = c._vind_kolommen(df, lambda _: None)

    uit = {
        "bestand_leesbaar":        True,
        "heeft_rijen":             len(df) > 0,
        "registratiekolom_gevonden": reg_kolom is not None,
        "icaokolom_gevonden":      icao_kolom is not None,
        "kolommen_zijn_tekst":     all(isinstance(k, str) for k in df.columns),
    }

    if reg_kolom and icao_kolom:
        reg_kolom_str = df[reg_kolom].astype(str).str.strip()
        is_ph         = reg_kolom_str.str.match(r"^PH-\w+$", na=False)

        # Kies een rij waarvan zowel de registratie als de ICAO-cel gevuld is.
        # Niet "de eerste PH-rij": in het register staan rijen zonder ICAO-code,
        # en dan zou de meting op brondata struikelen in plaats van op de code.
        gevuld = df[is_ph & df[icao_kolom].notna()]
        if len(gevuld):
            registratie = reg_kolom_str[gevuld.index[0]]
            icao = c._zoek_icao_in_register(registratie, df, lambda _: None)
            uit["opzoeking_geeft_resultaat"] = icao is not None
            uit["opzoeking_formaat_ok"] = bool(
                icao and re.match(r"^[A-Z0-9]{2,5}$", icao)
            )

        # Een rij zonder ICAO-code hoort None te geven, niet de tekst "nan".
        # Alleen toetsen als zulke rijen bestaan; hoeveel het zijn is brondata.
        leeg = df[is_ph & df[icao_kolom].isna()]
        if len(leeg):
            uit["lege_icao_geeft_none"] = c._zoek_icao_in_register(
                reg_kolom_str[leeg.index[0]], df, lambda _: None) is None

        uit["onbekende_registratie_geeft_none"] = (
            c._zoek_icao_in_register("PH-ZZZZ9", df, lambda _: None) is None
        )
    return uit


def meet_classificatie_zonder_norm():
    """Geen afstandsnorm aannemen als die niet herleidbaar is.

    Op een synthetisch register, zodat de meting niet afhangt van wat het ILT
    op dit moment publiceert. Vastgelegd wordt het besluit van de code: een
    onbekende registratie en een PM-categorie leveren norm_m = None met een
    eigen norm_bron en een rode signalering — nooit een aangenomen norm.
    """
    import pandas as pd
    import tug_02_classificatie as c

    df = pd.DataFrame({
        "Registration (kolomkop)": ["PH-TEST1", "PH-TEST2"],
        "ICAO type (kolomkop)":    ["R44", "NH90"],  # 011 = 150 m, 017 = PM
    })
    def stil(_regel):
        pass

    uit = {}
    for sleutel, registratie in (("bekend",       "PH-TEST1"),
                                 ("pm_categorie", "PH-TEST2"),
                                 ("onbekend",     "PH-TEST9")):
        r = c._classificeer_luchtvaartuig(
            {"registratie": registratie, "type": "heli"}, df, stil
        )
        uit[sleutel] = {
            "norm_m":         r["norm_m"],
            "norm_bron":      r["norm_bron"],
            "aantal_signalen": len(r["signalen"]),
            "signaal_rood":   all(s.startswith("✗") for s in r["signalen"]),
        }
    return uit


def meet_signalering_structuur():
    """De NNN-signalering op de lokale GeoPackage: structuur, niet de uitkomst.

    Welke gebieden er liggen en hoe ver weg is PDOK-data en dus geen regressie.
    Wat hier wél vastligt zijn de invarianten van de code: de vorm van het
    resultaat, de sleutels per treffer, dat afstanden hele meters zijn en binnen
    de marge vallen, en dat het label vast is.
    """
    from shapely.geometry import Point, MultiPoint
    import tug_bronnen_natuur as n
    from tug_config import NNN_GPKG, NNN_SIGNAAL_MARGE
    from tug_geo import make_transformer

    if not NNN_GPKG.exists():
        return {"_overgeslagen": f"{NNN_GPKG.name} ontbreekt"}

    t = make_transformer("EPSG:4326", "EPSG:28992")
    punten = MultiPoint([Point(*t.transform(lon, lat))
                         for lat, lon in PUNTEN_AANVRAAG])

    res   = n.signaleer_nnn(punten.buffer(NNN_SIGNAAL_MARGE),
                            lambda _: None, punten_rd=punten)
    items = res["in_straal"] + res["in_signaal"]

    return {
        "sleutels":              sorted(res),
        "beide_zijn_lijst":      all(isinstance(res[k], list) for k in res),
        "itemsleutels":          sorted(items[0]) if items else [],
        "labels_vast":           {i["naam"] for i in items} <= {"NNN-gebied"},
        "afstanden_hele_meters": all(isinstance(i["afstand_m"], int) for i in items),
        "afstanden_binnen_marge": all(i["afstand_m"] <= NNN_SIGNAAL_MARGE
                                      for i in res["in_signaal"]),
        "ringen_aanwezig":       all(bool(i["poly_rings"]) for i in items),
        "binnen_heeft_afstand_nul": all(i["afstand_m"] == 0
                                        for i in res["in_straal"]),
    }


METINGEN = {
    "transformaties":        meet_transformaties,
    "geometrie":             meet_geometrie,
    "geopackage_io":         meet_geopackage_io,
    "puntlocaties":          meet_puntlocaties,
    "datumlogica":           meet_datumlogica,
    "normtabel":             meet_normtabel,
    "config":                meet_config,
    "register_mechanisme":   meet_register_mechanisme,
    "classificatie_zonder_norm": meet_classificatie_zonder_norm,
    "signalering_structuur": meet_signalering_structuur,
}


# ──────────────────────────────────────────────
# Vergelijken
# ──────────────────────────────────────────────

def _tolerantie_voor(pad):
    """Tolerantie van de langst passende prefix in TOLERANTIES, of None."""
    kandidaten = [(len(k), v) for k, v in TOLERANTIES.items()
                  if pad == k or pad.startswith(k + ".") or pad.startswith(k + "[")]
    return max(kandidaten)[1] if kandidaten else None


def verzamel():
    """Draai alle metingen. Een mislukte meting wordt vastgelegd, niet verzwegen."""
    resultaat = {}
    for naam, functie in METINGEN.items():
        print(f"  meten: {naam} ...", flush=True)
        try:
            resultaat[naam] = functie()
        except Exception as e:
            resultaat[naam] = {"_fout": f"{type(e).__name__}: {e}"}
            print(f"    FOUT in meting '{naam}': {type(e).__name__}: {e}")
            traceback.print_exc(limit=3)
    return resultaat


def vergelijk(oud, nieuw, pad=""):
    """Loop twee geneste structuren na en geef alle verschillen terug.

    Numerieke waarden onder een pad met tolerantie mogen binnen die marge
    afwijken; al het andere moet exact gelijk zijn.
    """
    verschillen = []
    getallen    = (int, float)

    if isinstance(oud, bool) or isinstance(nieuw, bool):
        # bool is een subklasse van int; True mag nooit als 1 doorglippen.
        if oud is not nieuw:
            verschillen.append((pad, oud, nieuw))
        return verschillen

    if isinstance(oud, getallen) and isinstance(nieuw, getallen):
        tol = _tolerantie_voor(pad)
        if tol is not None:
            if abs(oud - nieuw) > tol:
                verschillen.append((f"{pad} (buiten tolerantie {tol})", oud, nieuw))
        elif oud != nieuw:
            verschillen.append((pad, oud, nieuw))
        return verschillen

    if type(oud) is not type(nieuw):
        return [(pad, oud, nieuw)]

    if isinstance(oud, dict):
        for sleutel in sorted(set(oud) | set(nieuw)):
            sub = f"{pad}.{sleutel}" if pad else sleutel
            if sleutel not in oud:
                verschillen.append((sub, "<ontbrak>", nieuw[sleutel]))
            elif sleutel not in nieuw:
                verschillen.append((sub, oud[sleutel], "<ontbreekt nu>"))
            else:
                verschillen += vergelijk(oud[sleutel], nieuw[sleutel], sub)
    elif isinstance(oud, list):
        if len(oud) != len(nieuw):
            verschillen.append((f"{pad} (lengte)", len(oud), len(nieuw)))
        else:
            for i, (o, n) in enumerate(zip(oud, nieuw)):
                verschillen += vergelijk(o, n, f"{pad}[{i}]")
    elif oud != nieuw:
        verschillen.append((pad, oud, nieuw))

    return verschillen


def omgeving():
    """Vastleggen waarmee de referentie is gemaakt, zodat een verschil te plaatsen is."""
    import importlib.metadata as md
    pakketten = ["pandas", "numpy", "geopandas", "shapely", "pyproj",
                 "pyogrio", "odfpy", "lxml", "reportlab", "pillow", "requests"]
    versies = {}
    for p in pakketten:
        try:
            versies[p] = md.version(p)
        except Exception:
            versies[p] = "niet geïnstalleerd"
    try:
        import pyproj
        versies["PROJ"] = pyproj.proj_version_str
    except Exception:
        pass
    try:
        import pyogrio
        versies["GDAL"] = pyogrio.__gdal_version_string__
    except Exception:
        pass
    return {"python": ".".join(map(str, sys.version_info[:3])), "pakketten": versies}


def main():
    herijk = "--herijk" in sys.argv

    print("=" * 70)
    print("TUG-ontheffingen — regressietest")
    print("=" * 70)

    nu = {"omgeving": omgeving(), "metingen": verzamel()}

    # Door JSON heen halen voordat er iets mee gebeurt: de referentie komt uit
    # JSON, waar een tuple een lijst en een set geen set meer is. Zonder deze
    # stap vergelijk je verschillende typen en faalt elke meting die zo'n waarde
    # teruggeeft. Dit controleert tegelijk dat alles serialiseerbaar is.
    nu = json.loads(json.dumps(nu, ensure_ascii=False, default=str))

    mislukt = [n for n, m in nu["metingen"].items()
               if isinstance(m, dict) and "_fout" in m]
    overgeslagen = [n for n, m in nu["metingen"].items()
                    if isinstance(m, dict) and "_overgeslagen" in m]

    if herijk:
        if mislukt:
            print(f"\nGEWEIGERD: herijken kan niet terwijl metingen mislukken: "
                  f"{', '.join(mislukt)}")
            print("Los die eerst op; een referentie met fouten erin is waardeloos.")
            return 1
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(nu, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        print(f"\nReferentie vastgelegd: {GOLDEN.relative_to(PROJECT_DIR)}")
        if overgeslagen:
            print(f"Let op: overgeslagen metingen (ontbrekende cachebestanden): "
                  f"{', '.join(overgeslagen)}")
        for regel in _omgeving_regels(nu["omgeving"]):
            print(f"  {regel}")
        return 0

    if not GOLDEN.exists():
        print(f"\nGeen referentie gevonden ({GOLDEN.relative_to(PROJECT_DIR)}).")
        print("Leg er eerst een vast, zolang de uitkomst nog vertrouwd is:")
        print("  .venv/bin/python test/test_regressie.py --herijk")
        return 1

    referentie = json.loads(GOLDEN.read_text(encoding="utf-8"))

    print("\nOmgeving toen → nu:")
    for regel in _omgeving_verschillen(referentie.get("omgeving", {}), nu["omgeving"]):
        print(f"  {regel}")

    verschillen = vergelijk(referentie.get("metingen", {}), nu["metingen"])

    print()
    if overgeslagen:
        print(f"OVERGESLAGEN: {', '.join(overgeslagen)} "
              f"(ontbrekende cachebestanden in geo/)")
    if mislukt:
        print(f"MISLUKT: {', '.join(mislukt)}")

    if not verschillen:
        print(f"GEEN VERSCHILLEN — de uitkomst is gelijk aan de referentie "
              f"(tolerantie {TOLERANTIE_M} m op coördinaten en afstanden).")
        return 1 if mislukt else 0

    print(f"{len(verschillen)} VERSCHIL(LEN) met de referentie:\n")
    for pad, oud, nieuw in verschillen:
        print(f"  {pad}")
        print(f"      referentie : {oud!r}")
        print(f"      nu         : {nieuw!r}")
    print("\nBeoordeel per verschil of dit een verbetering of een regressie is.")
    print("Is het een goedgekeurde wijziging, leg de referentie dan opnieuw vast:")
    print("  .venv/bin/python test/test_regressie.py --herijk")
    return 1


def _omgeving_regels(omg):
    regels = [f"python {omg['python']}"]
    regels += [f"{p} {v}" for p, v in sorted(omg["pakketten"].items())]
    return regels


def _omgeving_verschillen(toen, nu):
    regels = []
    if toen.get("python") != nu.get("python"):
        regels.append(f"python {toen.get('python')} → {nu.get('python')}")
    else:
        regels.append(f"python {nu.get('python')} (ongewijzigd)")

    toen_p, nu_p = toen.get("pakketten", {}), nu.get("pakketten", {})
    gewijzigd = [f"{p}: {toen_p.get(p, '?')} → {nu_p[p]}"
                 for p in sorted(nu_p) if toen_p.get(p) != nu_p[p]]

    regels += gewijzigd or ["pakketversies ongewijzigd"]
    return regels


if __name__ == "__main__":
    sys.exit(main())
