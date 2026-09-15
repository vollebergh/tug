"""
tug_bronnen_natuur.py -- Natuur-beschermingsgebieden (TUG-ontheffingen)

Bevat signaleringsfuncties voor Natura 2000 (nationaal, on-the-fly via PDOK WFS)
en Natuurnetwerk Nederland (provinciaal, lokale GeoPackage-cache vanuit ATOM-feed).

Natura 2000 breekt de toetsing af als de WFS niet reageert: zonder die bron is
"de puntlocatie ligt niet in een Natura 2000-gebied" geen uitspraak maar een gok.
NNN blijft zichtbaar: lukt het verversen van de cache niet, dan wordt de oude
GeoPackage tot NNN_NOODTERUGVAL_MAX_DAGEN gebruikt met een rode melding, en
daarboven geldt NNN als niet getoetst.
"""

import json
import logging
from datetime import datetime

from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from tug_bronstatus import Bronregister
from tug_config import (
    GEO_DIR,
    MAX_BYTES_N2000, MAX_BYTES_NNN,
    N2000_WFS, N2000_WFS_LAYER, N2000_SIGNAAL_MARGE,
    NNN_GML_URL, NNN_GPKG, NNN_META, NNN_NOODTERUGVAL_MAX_DAGEN, NNN_TTL_DAGEN,
    NNN_SIGNAAL_MARGE,
)
from tug_geo import (
    circle_bbox_wgs84, make_transformer,
    shapely_from_geojson_geom, transform_geom_to_rd, _geom_rings_wgs84,
)
from tug_http import BronFout, download_naar_bestand, haal_json
from tug_types import LogFn, SignaalResultaat

try:
    import geopandas as gpd
    _HAS_GEOPANDAS = True
except ImportError:
    gpd = None
    _HAS_GEOPANDAS = False


_logger = logging.getLogger("tug.bronnen_natuur")


# ──────────────────────────────────────────────
# Natura 2000 — PDOK WFS (RVO), on-the-fly
# ──────────────────────────────────────────────

def _punten_binnen(geom_rd: BaseGeometry, punt_rd: BaseGeometry) -> list[int]:
    """Volgnummers (1-based) van de puntlocaties die binnen geom_rd liggen (B03)."""
    punten = list(punt_rd.geoms) if hasattr(punt_rd, "geoms") else [punt_rd]
    return [i for i, p in enumerate(punten, 1) if geom_rd.intersects(p)]


def signaleer_natura2000(
    circle_rd: BaseGeometry, log: LogFn,
    punten_rd: BaseGeometry | None = None, *, bronnen: Bronregister,
) -> SignaalResultaat:
    """Query Natura 2000-gebieden via PDOK WFS (geen lokale cache, on-the-fly BBOX-query).

    Check: puntlocatie (stijg-/landingsplaats), NIET de toetsingsstraalcirkel.
    Retourneert {'in_straal': [...], 'in_signaal': [...]}
      in_straal  — puntlocatie ligt BINNEN het N2000-gebied
      in_signaal — puntlocatie ligt BUITEN maar op < N2000_SIGNAAL_MARGE m van het N2000-gebied
    """
    # Puntlocatie(s); bij meerdere geldt de dichtstbijzijnde (B03)
    punt_rd    = punten_rd if punten_rd is not None else circle_rd.centroid
    signaal_rd = punt_rd.buffer(N2000_SIGNAAL_MARGE)
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(signaal_rd)
    # PDOK WFS 2.0 + EPSG:4326: axis-volgorde is lat-first (conform de CRS-definitie)
    bbox_str = f"{lat_min},{lon_min},{lat_max},{lon_max},EPSG:4326"

    log(f"  Natura 2000-gebieden ophalen via PDOK WFS "
        f"(puntlocatie ± {N2000_SIGNAAL_MARGE} m, bbox {lat_min:.4f},{lon_min:.4f},"
        f"{lat_max:.4f},{lon_max:.4f}) ...")

    params = {
        "service":      "WFS",
        "version":      "2.0.0",
        "request":      "GetFeature",
        "TYPENAME":     N2000_WFS_LAYER,
        "outputFormat": "application/json",
        "srsName":      "EPSG:4326",
        "SRSNAME":      "urn:ogc:def:crs:EPSG::4326",
        "BBOX":         bbox_str,
    }
    try:
        data = haal_json(N2000_WFS, params=params, timeout=30, max_bytes=MAX_BYTES_N2000)
    except BronFout as fout:
        log(f"  FOUT: Natura 2000 WFS mislukt: {fout}")
        bronnen.mislukt("natura2000", str(fout))
        raise
    kandidaten = data.get("features", [])

    log(f"  N2000: {len(kandidaten)} kandidaat/kandidaten in bbox.")

    in_straal  = []
    in_signaal = []

    for feat in kandidaten:
        geom_dict = feat.get("geometry")
        if not geom_dict:
            continue
        try:
            geom_wgs = shapely_from_geojson_geom(geom_dict)
            geom_rd  = transform_geom_to_rd(geom_wgs)
        except (ValueError, TypeError, KeyError) as fout:
            _logger.warning(f"Natura 2000-gebied met onleesbare geometrie overgeslagen: {fout}")
            continue
        props     = feat.get("properties", {})
        naam      = (props.get("naam") or props.get("NAAM") or props.get("NAME")
                     or props.get("naamN2K") or props.get("gebiedsnaam")
                     or "Onbekend N2000-gebied")
        afstand_p = punt_rd.distance(geom_rd)
        rings     = _geom_rings_wgs84(geom_wgs)
        binnen    = _punten_binnen(geom_rd, punt_rd)
        item      = {"naam": naam, "afstand_m": round(afstand_p), "poly_rings": rings,
                     "punten_binnen": binnen}

        if binnen:
            log(f"  N2000 TREFFER: puntlocatie {', '.join(map(str, binnen))} ligt BINNEN '{naam}'.")
            in_straal.append(item)
        elif afstand_p <= N2000_SIGNAAL_MARGE:
            log(f"  N2000 nabij: puntlocatie op {afstand_p:.0f} m van '{naam}' "
                f"(< {N2000_SIGNAAL_MARGE} m).")
            in_signaal.append(item)

    # Dedupliceer op naam: VR/HR/VR+HR zijn allen N2000 en hebben dezelfde gebiedsnaam.
    # Bij meerdere geometrieën voor dezelfde naam geldt: in_straal heeft voorrang.
    seen = set()
    in_straal_dedup = []
    for item in in_straal:
        if item["naam"] not in seen:
            seen.add(item["naam"])
            in_straal_dedup.append(item)
    in_straal = in_straal_dedup
    in_signaal = [item for item in in_signaal if item["naam"] not in seen]

    log(f"  N2000: puntlocatie binnen {len(in_straal)} gebied(en) | "
        f"{len(in_signaal)} nabij (< {N2000_SIGNAAL_MARGE} m).")
    bronnen.geraadpleegd("natura2000")
    return {"in_straal": in_straal, "in_signaal": in_signaal}


# ──────────────────────────────────────────────
# NNN — ATOM-download + lokale GeoPackage-cache
# ──────────────────────────────────────────────

def _nnn_ouderdom_dagen() -> int | None:
    """Ouderdom van de GeoPackage in dagen (uit de meta, anders de bestandsdatum)."""
    if not NNN_GPKG.exists():
        return None
    if NNN_META.exists():
        try:
            meta = json.loads(NNN_META.read_text(encoding="utf-8"))
            return (datetime.now() - datetime.fromisoformat(meta.get("timestamp", ""))).days
        except (OSError, ValueError, TypeError) as fout:
            # Geen bruikbare meta: terugvallen op de bestandsdatum hieronder.
            _logger.debug(f"NNN-meta onbruikbaar ({fout}); bestandsdatum gebruikt")
    return (datetime.now() - datetime.fromtimestamp(NNN_GPKG.stat().st_mtime)).days


def _nnn_maak_gpkg(log: LogFn) -> None:
    """Download NNN GML, converteer naar GeoPackage in EPSG:28992. Gooit BronFout."""
    if not _HAS_GEOPANDAS:
        raise BronFout("geopandas is niet geïnstalleerd")

    GEO_DIR.mkdir(parents=True, exist_ok=True)
    gml_pad = GEO_DIR / "_nnn_tmp.gml"

    log("  NNN: GML downloaden van PDOK (~185 MB) ...")
    ontvangen = download_naar_bestand(
        NNN_GML_URL, gml_pad, max_bytes=MAX_BYTES_NNN, timeout=(30, 300),
        label="NNN downloaden",
    )
    log(f"  NNN: GML opgeslagen ({ontvangen / (1024 * 1024):.1f} MB).")

    try:
        # Laagnamen uitlezen (INSPIRE GML kan meerdere lagen bevatten)
        import pyogrio
        lagen = [rij[0] for rij in pyogrio.list_layers(str(gml_pad))]
        log(f"  NNN GML: {len(lagen)} laag/lagen gevonden: {lagen}")
        laag = lagen[0] if lagen else None

        log("  NNN: GML inlezen met geopandas (INSPIRE GML, kan 1–3 minuten duren) ...")
        kwargs = {"layer": laag} if laag else {}
        gdf = gpd.read_file(str(gml_pad), **kwargs)
        log(f"  NNN: {len(gdf)} gebieden ingelezen | CRS = {gdf.crs}.")
        if gdf.empty:
            raise BronFout("de NNN-GML bevat geen gebieden")

        log("  NNN: coördinaten omzetten naar RD New (EPSG:28992) ...")
        gdf_rd = gdf.to_crs("EPSG:28992")

        # Alleen geometrie: de INSPIRE-bron levert geen gebiedsnamen, maar provinciale
        # categorie-aanduidingen ("bestaande natuur"), en bij de helft van de gebieden
        # niets. Die zijn voor de signalering niet van belang.
        gdf_out = gdf_rd[["geometry"]].copy()

        log(f"  NNN: GeoPackage opslaan ({NNN_GPKG.name}) ...")
        tijdelijk = NNN_GPKG.with_name(NNN_GPKG.name + ".part")
        tijdelijk.unlink(missing_ok=True)
        gdf_out.to_file(str(tijdelijk), driver="GPKG", layer="nnn")
        tijdelijk.replace(NNN_GPKG)
        kb = NNN_GPKG.stat().st_size // 1024
        log(f"  NNN: GeoPackage opgeslagen ({kb} KB, {len(gdf_out)} gebieden).")
    except (OSError, RuntimeError, ValueError, KeyError) as fout:
        raise BronFout(f"NNN-GML niet te verwerken ({fout.__class__.__name__}: {fout})") from fout
    finally:
        gml_pad.unlink(missing_ok=True)
        # GDAL schrijft een .gfs-schemabestand naast de GML; een achtergebleven
        # .gfs wordt bij een volgende run op een nieuwere GML opgedrongen.
        gml_pad.with_suffix(".gfs").unlink(missing_ok=True)

    NNN_META.write_text(
        json.dumps(
            {"timestamp": datetime.now().isoformat(), "n_gebieden": len(gdf_out)},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )


def _nnn_cache_gereed(log: LogFn, bronnen: Bronregister) -> bool:
    """Zorg voor een bruikbare GeoPackage en meld de cachestatus. False = niet toetsbaar."""
    ouderdom = _nnn_ouderdom_dagen()
    if ouderdom is not None and ouderdom < NNN_TTL_DAGEN:
        log(f"  NNN: GeoPackage actueel ({NNN_GPKG.name}, {ouderdom} dag(en) oud).")
        bronnen.cache("nnn", ouderdom)
        return True

    log(f"  NNN: GeoPackage ontbreekt of ouder dan {NNN_TTL_DAGEN} dagen — bijwerken ...")
    try:
        _nnn_maak_gpkg(log)
    except BronFout as fout:
        if ouderdom is not None and ouderdom <= NNN_NOODTERUGVAL_MAX_DAGEN:
            log(f"  FOUT: NNN bijwerken mislukt ({fout}) — verouderde GeoPackage van "
                f"{ouderdom} dagen gebruikt.")
            bronnen.verouderd("nnn", ouderdom, f"verversen mislukt: {fout}")
            return True
        log(f"  FOUT: NNN bijwerken mislukt ({fout}) — geen bruikbare GeoPackage; "
            f"NNN niet getoetst.")
        bronnen.mislukt("nnn", str(fout))
        return False
    bronnen.geraadpleegd("nnn", "GeoPackage opnieuw opgebouwd")
    return True


def signaleer_nnn(
    circle_rd: BaseGeometry, log: LogFn,
    punten_rd: BaseGeometry | None = None, *, bronnen: Bronregister,
) -> SignaalResultaat:
    """Laad NNN GeoPackage (download + verwerk indien nodig) en check intersectie.

    Retourneert {'in_straal': [...], 'in_signaal': [...]}
      in_straal  — polygoon overlapt met toetsingsstraal
      in_signaal — polygoon overlapt met toetsingsstraal + NNN_SIGNAAL_MARGE,
                   maar raakt toetsingsstraal niet aan
    """
    if not _HAS_GEOPANDAS:
        log("  FOUT: geopandas niet beschikbaar — NNN niet getoetst. "
            "Installeer de omgeving opnieuw met install.py.")
        bronnen.mislukt("nnn", "geopandas is niet geïnstalleerd")
        return {"in_straal": [], "in_signaal": []}

    log("  NNN: cache-status controleren ...")
    if not _nnn_cache_gereed(log, bronnen):
        return {"in_straal": [], "in_signaal": []}

    # Puntlocatie(s); bij meerdere geldt de dichtstbijzijnde (B03)
    punt_rd    = punten_rd if punten_rd is not None else circle_rd.centroid
    signaal_rd = punt_rd.buffer(NNN_SIGNAAL_MARGE)
    minx, miny, maxx, maxy = signaal_rd.bounds
    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")

    log(f"  NNN: gebieden ophalen uit GeoPackage "
        f"(bbox = punt+{NNN_SIGNAAL_MARGE} m) ...")
    try:
        gdf = gpd.read_file(str(NNN_GPKG), layer="nnn", bbox=(minx, miny, maxx, maxy))
        log(f"  NNN: {len(gdf)} kandidaat/kandidaten in bbox.")
    except (OSError, RuntimeError, ValueError) as fout:
        log(f"  FOUT: NNN GeoPackage lezen mislukt: {fout}")
        bronnen.mislukt("nnn", f"GeoPackage niet leesbaar: {fout}")
        return {"in_straal": [], "in_signaal": []}

    in_straal  = []
    in_signaal = []

    for _, rij in gdf.iterrows():
        geom_rd = rij.geometry
        if geom_rd is None or geom_rd.is_empty:
            continue
        afstand_p = punt_rd.distance(geom_rd)
        geom_wgs  = shapely_transform(lambda x, y: t_to_wgs.transform(x, y), geom_rd)
        rings     = _geom_rings_wgs84(geom_wgs)
        binnen    = _punten_binnen(geom_rd, punt_rd)
        item      = {"naam": "NNN-gebied", "afstand_m": round(afstand_p),
                     "poly_rings": rings, "punten_binnen": binnen}

        if binnen:
            log(f"  NNN TREFFER: puntlocatie {', '.join(map(str, binnen))} "
                f"ligt BINNEN NNN-gebied.")
            in_straal.append(item)
        elif afstand_p <= NNN_SIGNAAL_MARGE:
            log(f"  NNN nabij: puntlocatie op {afstand_p:.0f} m van NNN-gebied "
                f"(< {NNN_SIGNAAL_MARGE} m).")
            in_signaal.append(item)

    log(f"  NNN: {len(in_straal)} puntlocatie binnen gebied | "
        f"{len(in_signaal)} nabij (< {NNN_SIGNAAL_MARGE} m).")
    return {"in_straal": in_straal, "in_signaal": in_signaal}
