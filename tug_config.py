"""
tug_config.py -- Gedeelde configuratie en constanten (TUG-ontheffingen workflow)

Bevat alle constanten die door meerdere pipeline-modules worden gebruikt.
Importeer hier vanuit zowel tug_03_bronnen.py als tug_05_output.py om
dubbele definities en silent inconsistenties te voorkomen.
"""

from pathlib import Path

# ──────────────────────────────────────────────
# Versie- en paddefinities
# ──────────────────────────────────────────────

VERSION     = "4.5.0"
MODEL_LABEL = ""

_ROOT      = Path(__file__).parent
OUTPUT_DIR = _ROOT / "output"
GEO_DIR    = _ROOT / "geo"

# ──────────────────────────────────────────────
# Toetsingsafstanden en zones
# ──────────────────────────────────────────────

MARGE_M                  = 75    # margeband rond toetsingsafstand
MANEGE_SIGNAAL_MARGE     = 375   # aandachtsgebied maneges (boven toetsingsafstand)
BEGRAAFPLAATS_ZOEK_MARGE = 1500  # extra zoekruimte voor begraafplaats-bbox
PAND_BBOX_ZOEK_MARGE     = 100   # bbox-marge voor pandgeometrie-query (BAG)

# ──────────────────────────────────────────────
# BAG / PDOK endpoints
# ──────────────────────────────────────────────

BAG_WFS               = "https://service.pdok.nl/lv/bag/wfs/v2_0"
BAG_PAGE_SIZE         = 1000
PDOK_LOCATION_API     = "https://api.pdok.nl/kadaster/location-api/v1/search"
LOCATIESERVER_REVERSE = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse"
LOCATIESERVER_FREE    = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

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

GELUIDGEVOELIGE_DOELEN = {
    "woonfunctie", "onderwijsfunctie", "gezondheidszorgfunctie", "logiesfunctie",
}

# ──────────────────────────────────────────────
# LRK (Landelijk Register Kinderopvang)
# ──────────────────────────────────────────────

LRK_URL        = "https://www.landelijkregisterkinderopvang.nl/opendata/export_opendata_lrk.csv"
LRK_CACHE_DAYS = 7
KDV_BBOX_EXTRA = 150  # m extra bbox voor KDV-zoekradius

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

_TILE_URL  = "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"
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
