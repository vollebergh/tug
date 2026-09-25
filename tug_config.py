"""
tug_config.py -- Gedeelde configuratie en constanten (TUG-ontheffingen workflow)

Bevat alle constanten, grenswaarden en endpoints die door meerdere
pipeline-modules worden gebruikt. Elke module importeert ze hier vandaan; een
waarde die op twee plaatsen staat, loopt vroeg of laat uiteen.
"""

import subprocess
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────
# Versie- en paddefinities
# ──────────────────────────────────────────────

def _workflow_versie() -> str:
    """Git-commit van de workflowcode; git is de enige versiegeschiedenis.

    Niet-gecommitte wijzigingen aan getrackte bestanden worden gemarkeerd, zodat
    elke output herleidbaar is naar de exacte code. Untracked bestanden tellen
    niet mee (bv. backup/).
    """
    root = Path(__file__).parent
    try:
        # git van het PATH met vaste argumenten; geen invoer van buiten.
        commit = subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"], cwd=root,  # noqa: S607
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        wijzigingen = subprocess.run(  # noqa: S603
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,  # noqa: S607
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "onbekend (geen git)"
    return f"{commit} (niet-gecommitte wijzigingen)" if wijzigingen else commit


VERSION     = _workflow_versie()
MODEL_LABEL = ""

_ROOT      = Path(__file__).parent
OUTPUT_DIR = _ROOT / "output"
GEO_DIR    = _ROOT / "geo"
GUI_DIR    = _ROOT / "gui"   # kaartsjablonen en stijlblad

# ──────────────────────────────────────────────
# Toetsingsafstanden en zones
# ──────────────────────────────────────────────

# Vaste toeslag op de Lden-afstand: toetsingsafstand = Lden-afstand + toeslag.
TOETSING_TOESLAG_M       = 10
# Maximale onderlinge afstand tussen de puntlocaties van één aanvraag (B03).
MAX_PUNT_AFSTAND_M       = 100
MARGE_M                  = 150   # margeband rond toetsingsafstand (gelijk aan KDV_BBOX_EXTRA)
MANEGE_SIGNAAL_MARGE     = 375   # aandachtsgebied maneges (boven toetsingsafstand)
BEGRAAFPLAATS_ZOEK_MARGE = 1500  # extra zoekruimte voor begraafplaats-bbox
PAND_BBOX_ZOEK_MARGE     = 100   # bbox-marge voor pandgeometrie-query (BAG)

# Omhullende van Nederland (lat_min, lat_max, lon_min, lon_max). Ruim genomen:
# de controle vangt verwisselde of buitenlandse coördinaten, niet de landsgrens.
NL_BBOX = (50.0, 54.0, 3.0, 8.0)

# Indientermijn: de aanvraag moet minimaal zoveel dagen vóór de vroegste
# vluchtdatum zijn ondertekend. Korter is niet verboden, maar levert een gebrek
# op in het rapport. Zowel de GUI als de validatiestap toetst hierop.
MIN_INDIENTERMIJN_DAGEN = 28

# Registratiekenmerk luchtvaartuig, bijv. PH-ECE. Alleen een vormcontrole voor
# de invoer; het ILT-register bepaalt of het kenmerk ook bestaat.
REGISTRATIE_PATROON = r"^[A-Z0-9]{1,2}-[A-Z0-9]{2,5}$"

# Maximale lengte van de dossiernaam in een bestandsnaam.
NAAM_SLUG_MAX = 60


def meters(waarde: float) -> str:
    """Meters zonder overbodige decimalen: 10 → '10', 12.5 → '12,5'."""
    return f"{waarde:g}".replace(".", ",")


def toetsing_label(straal: float, toeslag: float = TOETSING_TOESLAG_M) -> str:
    """Kaartlabel voor de toetsingsafstand, met de Lden-afstand en de toeslag apart zichtbaar."""
    lden = straal - toeslag
    return (f"Toetsingsafstand TUG ({lden:.0f} m + {meters(toeslag)} m = "
            f"{meters(round(straal, 1))} m)")


# ──────────────────────────────────────────────
# Externe bronnen — toegestane hosts en grenzen
# ──────────────────────────────────────────────

# Alle uitgaande aanroepen gaan via tug_http en alleen over https naar deze hosts
# (of een subdomein ervan). Een verwijzing in een bronantwoord naar een andere host
# wordt niet gevolgd, ook niet na een redirect.
BRON_HOSTS = (
    "service.pdok.nl",
    "api.pdok.nl",
    "geodata.nationaalgeoregister.nl",
    "services.geodataoverijssel.nl",
    "onderwijsdata.duo.nl",
    "ilent.nl",
    "www.landelijkregisterkinderopvang.nl",
)

# Maximale omvang per antwoord, ruim twee keer de gemeten omvang (september 2026).
# Een bron die meer terugstuurt wordt afgebroken in plaats van ingelezen.
MAX_BYTES_API       = 25 * 1024 * 1024    # WFS-pagina's en JSON-API's
MAX_BYTES_N2000     = 50 * 1024 * 1024    # N2000-polygonen kunnen groot zijn
MAX_BYTES_TEGEL     = 2 * 1024 * 1024     # kaarttegel (gemeten 20–300 KB)
MAX_BYTES_ILT       = 10 * 1024 * 1024    # luchtvaartuigregister (1,3 MB)
MAX_BYTES_LRK       = 30 * 1024 * 1024    # LRK-export (12 MB)
MAX_BYTES_DUO       = 10 * 1024 * 1024    # grootste DUO-dump (2,3 MB)
MAX_BYTES_NNN       = 400 * 1024 * 1024   # NNN-GML (185 MB)

# Noodterugval: kan een verlopen cache niet worden ververst, dan wordt de oude
# versie nog tot deze ouderdom gebruikt — met een rode melding in het rapport en
# exitcode 2. Daarboven geldt de bron als niet geraadpleegd.
ILT_NOODTERUGVAL_MAX_DAGEN = 90
LRK_NOODTERUGVAL_MAX_DAGEN = 30
DUO_NOODTERUGVAL_MAX_DAGEN = 365
NNN_NOODTERUGVAL_MAX_DAGEN = 365

# Uitkomst van de laatste beveiligingscontrole (beveiligingscontrole.py). De
# pipeline waarschuwt bij de start als die ontbreekt, bevindingen heeft of ouder
# is dan BEVEILIGINGSCONTROLE_MAX_DAGEN.
BEVEILIGINGSCONTROLE_PAD      = _ROOT / ".beveiligingscontrole.json"
BEVEILIGINGSCONTROLE_MAX_DAGEN = 31


# ──────────────────────────────────────────────
# BAG / PDOK endpoints
# ──────────────────────────────────────────────

BAG_WFS               = "https://service.pdok.nl/lv/bag/wfs/v2_0"
BAG_PAGE_SIZE         = 1000
PDOK_LOCATION_API     = "https://api.pdok.nl/kadaster/location-api/v1/search"
LOCATIESERVER_REVERSE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
LOCATIESERVER_FREE    = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

# BRT top10nl OGC API — terrein_vlak (begraafplaats-detectie via 'dodenakker')
BRT_TERREIN_VLK_URL       = "https://api.pdok.nl/brt/top10nl/ogc/v1_0/collections/terrein_vlak/items"
BRT_TERREIN_PAGE_SIZE      = 200   # features per pagina
BRT_TERREIN_MAX_PAGES      = 15    # maximaal 3000 features per opvraging
BRT_DODENAKKER_ZOEK_MARGE = 500   # m extra zoekruimte voor BRT-dodenakker-bbox

# ──────────────────────────────────────────────
# Natura 2000 (nationaal, on-the-fly)
# ──────────────────────────────────────────────

N2000_WFS           = "https://service.pdok.nl/rvo/natura2000/wfs/v1_0"
N2000_WFS_LAYER     = "natura2000:natura2000"
N2000_SIGNAAL_MARGE = 500  # m buiten toetsingsafstand voor N2000-signalering

# ──────────────────────────────────────────────
# Natuurnetwerk Nederland (lokale GeoPackage cache)
# ──────────────────────────────────────────────

NNN_GML_URL       = (
    "https://service.pdok.nl/provincies/natuurnetwerk-nederland"
    "/atom/downloads/inspire-pv-ps.nlps-nnn.gml"
)
NNN_GPKG          = GEO_DIR / "nnn_gebieden.gpkg"
NNN_META          = GEO_DIR / "nnn.meta.json"
NNN_TTL_DAGEN     = 180  # herdownload pas na 180 dagen
NNN_SIGNAAL_MARGE = 500  # m buiten toetsingsafstand voor NNN-signalering

# ──────────────────────────────────────────────
# Luchthavens (Overijssel WFS en BRT Top10NL)
# ──────────────────────────────────────────────

LUCHTHAVEN_WFS       = "https://services.geodataoverijssel.nl/geoserver/B64_nutsvoorzieningen/wfs"
LUCHTHAVEN_WFS_LAYER = "B64_nutsvoorzieningen:B6_Luchthaven_puntlocaties"
LUCHTHAVEN_GRENS_M   = 1000  # wettelijke minimumafstand puntlocatie → luchthaven
LUCHTHAVEN_SIGNAAL_M = 5000  # signaleringmarge voor luchthaven (B17)

# Luchthaventerreinen als vlak (B23). De provinciale laag hierboven bevat alleen
# luchthavenregelingen (helihavens, zweefvliegterrein); vliegvelden met een
# luchthavenbesluit, zoals Twente, ontbreken. BRT Top10NL heeft ze als vlak. De API
# filtert niet op type; dat gebeurt in de code. Top10NL is topografisch, niet de
# juridische grens van het luchthavenbesluit, maar ligt ruim om start- en landingsbaan.
LUCHTHAVEN_BRT_URL       = ("https://api.pdok.nl/brt/top10nl/ogc/v1_0/collections/"
                            "functioneel_gebied_vlak/items")
LUCHTHAVEN_BRT_TYPEN     = {
    "vliegveld, luchthaven":     "vliegveld",
    "zweefvliegveldterrein":     "zweefvliegveld",
    "helikopterlandingsterrein": "helikopterlandingsterrein",
}
LUCHTHAVEN_BRT_PAGE_SIZE = 1000
LUCHTHAVEN_BRT_MAX_PAGES = 10
LUCHTHAVEN_KOPPEL_M      = 250   # provinciaal punt ↔ Top10NL-terrein: zelfde luchthaven

# ──────────────────────────────────────────────
# Maneges (BRT zoektermen)
# ──────────────────────────────────────────────

MANEGE_ZOEKTERMEN = [
    "manege", "rijschool", "hippisch", "paardencentrum", "rijvereniging",
    "ponyclub", "ruiterclub", "ruitersportcentrum", "paardensportcentrum",
    "paardensportvereniging", "paardenhouderij", "hippique",
]

# ──────────────────────────────────────────────
# BAG geluidgevoelige gebruiksdoelen
# ──────────────────────────────────────────────

# Logiesfunctie telt bewust niet mee: geen geluidgevoelig gebouw in de zin van art. 3.21 Bkl.
GELUIDGEVOELIGE_DOELEN = {
    "woonfunctie", "onderwijsfunctie", "gezondheidszorgfunctie",
}

# ──────────────────────────────────────────────
# LRK (Landelijk Register Kinderopvang)
# ──────────────────────────────────────────────

LRK_URL        = "https://www.landelijkregisterkinderopvang.nl/opendata/export_opendata_lrk.csv"
LRK_CACHE_DAYS = 7
# De LRK-server weigert de standaard python-requests User-Agent (HTTP 400)
LRK_HEADERS    = {"User-Agent": "Mozilla/5.0 (compatible; TUG-ontheffingen; Provincie Overijssel)"}
KDV_BBOX_EXTRA = MARGE_M  # m extra bbox voor KDV-zoekradius; valt samen met de margeband

# ──────────────────────────────────────────────
# DUO Open Onderwijsdata
# ──────────────────────────────────────────────

DUO_PROVINCIE = "Overijssel"
DUO_TTL_DAGEN = 90
DUO_DATASETS  = {
    "PO":  {"resource_id": "dcc9c9a5-6d01-410b-967f-810557588ba4", "naam_veld": "VESTIGINGSNAAM"},
    "SO":  {"resource_id": "8f0f1639-712d-4adb-bb59-cabd43730dc8", "naam_veld": "VESTIGINGSNAAM"},
    "VO":  {"resource_id": "5187f8d5-ff9c-4284-8e06-4311f0354956", "naam_veld": "VESTIGINGSNAAM"},
    "MBO": {"resource_id": "1a946297-a7ca-48d5-9ae8-19ad73bf8176", "naam_veld": "INSTELLINGSNAAM"},
    "HO":  {"resource_id": "bf1da9c6-c688-4873-91b1-b12c9ac2c132", "naam_veld": "INSTELLINGSNAAM"},
}
_DUO_PO_GROEP     = ["PO"]
_DUO_OVERIG_GROEP = ["SO", "VO", "MBO", "HO"]

SCHOLEN_GEOJSON_PO     = GEO_DIR / "duo_scholen_po.geojson"
SCHOLEN_GEOJSON_OVERIG = GEO_DIR / "duo_scholen_overig.geojson"
SCHOLEN_META           = GEO_DIR / "duo_scholen.meta.json"

# ──────────────────────────────────────────────
# Kaarttegels en PDF
# ──────────────────────────────────────────────

# Achtergrondkaarten (PDOK, open data, geen API-key). Satelliet is primair (B04);
# topografisch is aanvulling, met extra contrast bij het renderen (B05).
KAART_ACHTERGRONDEN: dict[str, dict[str, Any]] = {
    "satelliet": {
        "titel": "luchtfoto",
        "url": "https://service.pdok.nl/hwh/luchtfotorgb/wmts/v1_0/Actueel_orthoHR/EPSG:3857/{z}/{x}/{y}.jpeg",
        "contrast": 1.0,
        "bron": "Luchtfoto: PDOK / Beeldmateriaal Nederland",
    },
    "topografisch": {
        "titel": "topografisch",
        "url": "https://service.pdok.nl/brt/achtergrondkaart/wmts/v2_0/standaard/EPSG:3857/{z}/{x}/{y}.png",
        "contrast": 1.35,
        "bron": "Achtergrond: PDOK BRT-Achtergrondkaart (Kadaster)",
    },
}
_TILE_URL  = KAART_ACHTERGRONDEN["topografisch"]["url"]
_TILE_SIZE = 256

_LOC_CX_FRAC    = 0.40
_LOC_CY_FRAC    = 0.50
_ZOOM_FILL_FRAC = 0.9

_PDF_MARGIN_MM                 = 15
_PDF_DPI                       = 150
_PDF_PAGE_W_MM, _PDF_PAGE_H_MM = 210, 297

_MAANDEN_NL = {
    1: "januari", 2: "februari", 3: "maart", 4: "april",
    5: "mei", 6: "juni", 7: "juli", 8: "augustus",
    9: "september", 10: "oktober", 11: "november", 12: "december",
}
