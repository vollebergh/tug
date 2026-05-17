"""
tug_03_bronnen.py -- Re-export shim voor backward compatibility

De oorspronkelijke ~1400-regel module is gesplitst in 6 specialistische modules
(zie hieronder). Dit bestand re-exporteert alle publieke namen zodat bestaande
imports (`from tug_03_bronnen import ...`) blijven werken.

Gegroepeerd per domein:
    tug_config             — constanten, paden, endpoints, MAANDEN_NL
    tug_geo                — coördinaattransformaties, extract_adres/lon_lat
    tug_bronnen_geocode    — reverse geocoding, polygoon-opvraging
    tug_bronnen_bag        — BAG WFS verblijfsobjecten + gevel-check
    tug_bronnen_brt        — begraafplaatsen, maneges, luchthavens
    tug_bronnen_natuur     — Natura 2000 + Natuurnetwerk Nederland
    tug_bronnen_onderwijs  — kinderopvang (LRK) + scholen (DUO)

Nieuwe code mag direct importeren uit de specifieke modules; deze shim bestaat
alleen om `tug_03_ruimtelijk.py` ongewijzigd te laten draaien.
"""

# ── Configuratie & constanten ────────────────
from tug_config import (
    VERSION, MODEL_LABEL, OUTPUT_DIR, GEO_DIR,
    BAG_WFS, BAG_PAGE_SIZE,
    PDOK_LOCATION_API, LOCATIESERVER_REVERSE, LOCATIESERVER_FREE,
    MARGE_M, MANEGE_SIGNAAL_MARGE, BEGRAAFPLAATS_ZOEK_MARGE, PAND_BBOX_ZOEK_MARGE,
    N2000_WFS, N2000_WFS_LAYER, N2000_SIGNAAL_MARGE,
    NNN_GML_URL, NNN_GPKG, NNN_META, NNN_TTL_DAGEN, NNN_SIGNAAL_MARGE,
    LUCHTHAVEN_WFS, LUCHTHAVEN_WFS_LAYER, LUCHTHAVEN_GRENS_M, LUCHTHAVEN_SIGNAAL_M,
    MANEGE_ZOEKTERMEN, GELUIDGEVOELIGE_DOELEN,
    LRK_URL, LRK_CACHE_DAYS, KDV_BBOX_EXTRA,
    DUO_PROVINCIE, DUO_TTL_DAGEN, DUO_DATASETS,
    _DUO_PO_GROEP, _DUO_OVERIG_GROEP,
    SCHOLEN_GEOJSON_PO, SCHOLEN_GEOJSON_OVERIG, SCHOLEN_META,
    _TILE_URL, _TILE_SIZE,
    _LOC_CX_FRAC, _LOC_CY_FRAC, _ZOOM_FILL_FRAC,
    _PDF_MARGIN_MM, _PDF_DPI, _PDF_PAGE_W_MM, _PDF_PAGE_H_MM,
    _MAANDEN_NL,
)

# ── Coördinaten en extract-helpers ───────────
from tug_geo import (
    _datum_leesbaar,
    make_transformer, wgs84_to_rd,
    circle_in_rd, circle_bbox_wgs84,
    shapely_from_geojson_geom, transform_geom_to_rd, point_wgs84_to_rd,
    _geom_rings_wgs84,
    extract_adres, extract_lon_lat,
)

# ── Reverse geocoding ────────────────────────
from tug_bronnen_geocode import (
    reverse_geocode, reverse_geocode_adres_wpl,
    _wpl_title, _heeft_straatnaam,
    vul_woonplaats_via_reverse_geocode,
    _pdok_location_haal_polygoon,
)

# ── BAG verblijfsobjecten + gevel ────────────
from tug_bronnen_bag import (
    haal_verblijfsobjecten,
    filter_binnen_straal, filter_binnen_marge,
    _parse_doelen, dedupliceer_vbo, filter_geluidgevoelig,
    haal_pand_ids, haal_pand_geometrie_via_bbox, gevel_check,
)

# ── PDOK Location API + Overijssel WFS ───────
from tug_bronnen_brt import (
    signaleer_begraafplaatsen,
    haal_maneges_pdok,
    signaleer_luchthavens,
)

# ── Natuur (N2000 + NNN) ─────────────────────
from tug_bronnen_natuur import (
    signaleer_natura2000,
    signaleer_nnn,
)

# ── Onderwijs (KDV + scholen) ────────────────
from tug_bronnen_onderwijs import (
    haal_kdv_locaties,
    haal_scholen,
)
