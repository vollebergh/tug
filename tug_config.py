"""
tug_config.py -- Gedeelde configuratie en constanten (TUG-ontheffingen workflow)

Bevat alle constanten die door meerdere pipeline-modules worden gebruikt.
Importeer hier vanuit zowel tug_03_bronnen.py als tug_05_output.py om
dubbele definities en silent inconsistenties te voorkomen.
"""

import subprocess
from pathlib import Path

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
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=root,
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        wijzigingen = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
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

# ──────────────────────────────────────────────
# Toetsingsafstanden en zones
# ──────────────────────────────────────────────

TOETSING_TOESLAG_M       = 10    # vaste toeslag op de Lden-afstand; toetsingsafstand = Lden-afstand + toeslag
MAX_PUNT_AFSTAND_M       = 100   # max. onderlinge afstand tussen puntlocaties van één aanvraag (B03)
MARGE_M                  = 150   # margeband rond toetsingsafstand (gelijk aan KDV_BBOX_EXTRA)
MANEGE_SIGNAAL_MARGE     = 375   # aandachtsgebied maneges (boven toetsingsafstand)
BEGRAAFPLAATS_ZOEK_MARGE = 1500  # extra zoekruimte voor begraafplaats-bbox
PAND_BBOX_ZOEK_MARGE     = 100   # bbox-marge voor pandgeometrie-query (BAG)

def toetsing_label(straal: float) -> str:
    """Kaartlabel voor de toetsingsafstand, met de Lden-afstand en de toeslag apart zichtbaar."""
    lden = straal - TOETSING_TOESLAG_M
    return f"Toetsingsafstand TUG ({lden:.0f} m + {TOETSING_TOESLAG_M} m = {straal:.0f} m)"


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
# Luchthavens (Overijssel WFS)
# ──────────────────────────────────────────────

LUCHTHAVEN_WFS       = "https://services.geodataoverijssel.nl/geoserver/B64_nutsvoorzieningen/wfs"
LUCHTHAVEN_WFS_LAYER = "B64_nutsvoorzieningen:B6_Luchthaven_puntlocaties"
LUCHTHAVEN_GRENS_M   = 1000  # wettelijke minimumafstand puntlocatie → luchthaven
LUCHTHAVEN_SIGNAAL_M = 2000  # signaleringmarge voor luchthaven

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
KAART_ACHTERGRONDEN = {
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
