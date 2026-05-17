"""
tug_bronnen_natuur.py -- Natuur-beschermingsgebieden (TUG-ontheffingen)

Bevat signaleringsfuncties voor Natura 2000 (nationaal, on-the-fly via PDOK WFS)
en Natuurnetwerk Nederland (provinciaal, lokale GeoPackage-cache vanuit ATOM-feed).
"""

import json
from datetime import datetime

import requests
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform

from tug_config import (
    GEO_DIR,
    N2000_WFS, N2000_WFS_LAYER, N2000_SIGNAAL_MARGE,
    NNN_GML_URL, NNN_GPKG, NNN_META, NNN_TTL_DAGEN, NNN_SIGNAAL_MARGE,
)
from tug_geo import (
    circle_bbox_wgs84, make_transformer,
    shapely_from_geojson_geom, transform_geom_to_rd, _geom_rings_wgs84,
)
from tug_types import LogFn, SignaalResultaat

try:
    import geopandas as gpd
    _HAS_GEOPANDAS = True
except ImportError:
    gpd = None
    _HAS_GEOPANDAS = False


# ──────────────────────────────────────────────
# Natura 2000 — PDOK WFS (RVO), on-the-fly
# ──────────────────────────────────────────────

def signaleer_natura2000(
    circle_rd: BaseGeometry, straal: float, log: LogFn
) -> SignaalResultaat:
    """Query Natura 2000-gebieden via PDOK WFS (geen lokale cache, on-the-fly BBOX-query).

    Check: puntlocatie (stijg-/landingsplaats), NIET de toetsingsstraalcirkel.
    Retourneert {'in_straal': [...], 'in_signaal': [...]}
      in_straal  — puntlocatie ligt BINNEN het N2000-gebied
      in_signaal — puntlocatie ligt BUITEN maar op < N2000_SIGNAAL_MARGE m van het N2000-gebied
    """
    punt_rd    = circle_rd.centroid
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
        resp = requests.get(N2000_WFS, params=params, timeout=30)
        if resp.status_code != 200:
            log(f"  WAARSCHUWING: N2000 WFS gaf status {resp.status_code} — overgeslagen.")
            return {"in_straal": [], "in_signaal": []}
        kandidaten = resp.json().get("features", [])
    except Exception as e:
        log(f"  WAARSCHUWING: N2000 WFS mislukt: {e}")
        return {"in_straal": [], "in_signaal": []}

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
        except Exception:
            continue
        props     = feat.get("properties", {})
        naam      = (props.get("naam") or props.get("NAAM") or props.get("NAME")
                     or props.get("naamN2K") or props.get("gebiedsnaam")
                     or "Onbekend N2000-gebied")
        afstand_p = punt_rd.distance(geom_rd)
        rings     = _geom_rings_wgs84(geom_wgs)
        item      = {"naam": naam, "afstand_m": round(afstand_p), "poly_rings": rings}

        if geom_rd.contains(punt_rd):
            log(f"  N2000 TREFFER: puntlocatie ligt BINNEN '{naam}'.")
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
    return {"in_straal": in_straal, "in_signaal": in_signaal}


# ──────────────────────────────────────────────
# NNN — ATOM-download + lokale GeoPackage-cache
# ──────────────────────────────────────────────

def _nnn_gpkg_actueel():
    """True als de GeoPackage bestaat én jonger is dan NNN_TTL_DAGEN."""
    if not NNN_GPKG.exists():
        return False
    if NNN_META.exists():
        try:
            meta = json.loads(NNN_META.read_text(encoding="utf-8"))
            ts   = datetime.fromisoformat(meta.get("timestamp", ""))
            return (datetime.now() - ts).days < NNN_TTL_DAGEN
        except Exception:
            pass
    leeftijd = (datetime.now() - datetime.fromtimestamp(NNN_GPKG.stat().st_mtime)).days
    return leeftijd < NNN_TTL_DAGEN


def _nnn_download_gml(gml_pad, log):
    """Download NNN GML (~185 MB) met voortgangsindicator. Retourneert True bij succes."""
    log(f"  NNN: GML downloaden van PDOK (~185 MB) ...")
    try:
        resp  = requests.get(NNN_GML_URL, stream=True, timeout=300)
        resp.raise_for_status()
        totaal     = int(resp.headers.get("content-length", 0))
        ontvangen  = 0
        totaal_mb  = totaal / (1024 * 1024) if totaal else 0
        with open(gml_pad, "wb") as fout:
            for chunk in resp.iter_content(chunk_size=131072):
                if chunk:
                    fout.write(chunk)
                    ontvangen += len(chunk)
                    mb = ontvangen / (1024 * 1024)
                    if totaal:
                        pct = ontvangen / totaal * 100
                        print(f"\r  NNN downloaden: {mb:.1f} MB / {totaal_mb:.1f} MB "
                              f"({pct:.0f}%)   ", end="", flush=True)
                    else:
                        print(f"\r  NNN downloaden: {mb:.1f} MB ontvangen   ",
                              end="", flush=True)
        print()
        log(f"  NNN: GML opgeslagen ({ontvangen / (1024 * 1024):.1f} MB).")
        return True
    except Exception as e:
        print()
        log(f"  FOUT: NNN GML downloaden mislukt: {e}")
        return False


def _nnn_detecteer_naam_kolom(gdf, log):
    """Detecteer de gebiedsnaamkolom in de ingelezen NNN GeoDataFrame."""
    kandidaten = [
        "naam", "name", "NAAM", "NAME",
        "siteName", "SITENAAM", "localId", "localid",
        "siteDesignation", "inspireid_localid",
    ]
    for k in kandidaten:
        if k in gdf.columns:
            log(f"  NNN: naamkolom gevonden: '{k}'")
            return k
    # Fallback: eerste tekst-kolom
    for col in gdf.columns:
        if col.lower() != "geometry" and str(gdf[col].dtype) == "object":
            log(f"  NNN: naamkolom niet direct herkend, gebruik '{col}' als fallback.")
            return col
    log("  NNN WAARSCHUWING: geen naamkolom gevonden — gebieden worden genummerd.")
    return None


def _nnn_maak_gpkg(log):
    """Download NNN GML, converteer naar GeoPackage in EPSG:28992. True bij succes."""
    if not _HAS_GEOPANDAS:
        log("  FOUT: geopandas niet geïnstalleerd — NNN-verwerking overgeslagen. "
            "Installeer via: pip install geopandas")
        return False

    GEO_DIR.mkdir(parents=True, exist_ok=True)
    gml_pad = GEO_DIR / "_nnn_tmp.gml"

    if not _nnn_download_gml(gml_pad, log):
        return False

    # Laagnamen uitlezen (INSPIRE GML kan meerdere lagen bevatten)
    try:
        import fiona
        lagen = fiona.listlayers(str(gml_pad))
        log(f"  NNN GML: {len(lagen)} laag/lagen gevonden: {lagen}")
        laag = lagen[0] if lagen else None
    except Exception as e:
        log(f"  NNN: lagen uitlezen mislukt ({e}) — eerste laag wordt gebruikt.")
        laag = None

    log("  NNN: GML inlezen met geopandas (INSPIRE GML, kan 1–3 minuten duren) ...")
    try:
        kwargs = {"layer": laag} if laag else {}
        gdf = gpd.read_file(str(gml_pad), **kwargs)
        log(f"  NNN: {len(gdf)} gebieden ingelezen | CRS = {gdf.crs}.")
        log(f"  NNN: beschikbare kolommen: {list(gdf.columns)}")
    except Exception as e:
        log(f"  FOUT: NNN GML lezen mislukt: {e}")
        gml_pad.unlink(missing_ok=True)
        return False

    naam_col = _nnn_detecteer_naam_kolom(gdf, log)

    log("  NNN: coördinaten omzetten naar RD New (EPSG:28992) ...")
    try:
        gdf_rd = gdf.to_crs("EPSG:28992")
    except Exception as e:
        log(f"  FOUT: CRS-conversie EPSG:3035 → EPSG:28992 mislukt: {e}")
        gml_pad.unlink(missing_ok=True)
        return False

    if naam_col:
        gdf_out = gdf_rd[[naam_col, "geometry"]].copy()
        gdf_out = gdf_out.rename(columns={naam_col: "naam"})
    else:
        gdf_out = gdf_rd[["geometry"]].copy()
        gdf_out["naam"] = [f"NNN-gebied {i + 1}" for i in range(len(gdf_out))]
    gdf_out["naam"] = gdf_out["naam"].fillna("NNN-gebied (naamloos)").astype(str)

    log(f"  NNN: GeoPackage opslaan ({NNN_GPKG.name}) ...")
    try:
        gdf_out.to_file(str(NNN_GPKG), driver="GPKG", layer="nnn")
        kb = NNN_GPKG.stat().st_size // 1024
        log(f"  NNN: GeoPackage opgeslagen ({kb} KB, {len(gdf_out)} gebieden).")
    except Exception as e:
        log(f"  FOUT: NNN GeoPackage opslaan mislukt: {e}")
        gml_pad.unlink(missing_ok=True)
        return False

    try:
        gml_pad.unlink()
        log("  NNN: tijdelijk GML-bestand verwijderd.")
    except Exception:
        pass

    NNN_META.write_text(
        json.dumps(
            {"timestamp": datetime.now().isoformat(), "n_gebieden": len(gdf_out)},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return True


def signaleer_nnn(
    circle_rd: BaseGeometry, straal: float, log: LogFn
) -> SignaalResultaat:
    """Laad NNN GeoPackage (download + verwerk indien nodig) en check intersectie.

    Retourneert {'in_straal': [...], 'in_signaal': [...]}
      in_straal  — polygoon overlapt met toetsingsstraal
      in_signaal — polygoon overlapt met toetsingsstraal + NNN_SIGNAAL_MARGE,
                   maar raakt toetsingsstraal niet aan
    """
    if not _HAS_GEOPANDAS:
        log("  NNN: geopandas niet beschikbaar — overgeslagen. "
            "Installeer via: pip install geopandas")
        return {"in_straal": [], "in_signaal": []}

    log("  NNN: cache-status controleren ...")
    if not _nnn_gpkg_actueel():
        log(f"  NNN: GeoPackage ontbreekt of ouder dan {NNN_TTL_DAGEN} dagen — bijwerken ...")
        if not _nnn_maak_gpkg(log):
            log("  NNN: aanmaken mislukt — NNN-signalering overgeslagen.")
            return {"in_straal": [], "in_signaal": []}
    else:
        meta_str = ""
        if NNN_META.exists():
            try:
                meta = json.loads(NNN_META.read_text(encoding="utf-8"))
                ts   = datetime.fromisoformat(meta["timestamp"])
                meta_str = (f", {meta.get('n_gebieden','?')} gebieden, "
                            f"aangemaakt {ts.strftime('%Y-%m-%d')}")
            except Exception:
                pass
        log(f"  NNN: GeoPackage actueel ({NNN_GPKG.name}{meta_str}).")

    punt_rd    = circle_rd.centroid
    signaal_rd = punt_rd.buffer(NNN_SIGNAAL_MARGE)
    minx, miny, maxx, maxy = signaal_rd.bounds
    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")

    log(f"  NNN: gebieden ophalen uit GeoPackage "
        f"(bbox = punt+{NNN_SIGNAAL_MARGE} m) ...")
    try:
        gdf = gpd.read_file(str(NNN_GPKG), layer="nnn", bbox=(minx, miny, maxx, maxy))
        log(f"  NNN: {len(gdf)} kandidaat/kandidaten in bbox.")
    except Exception as e:
        log(f"  FOUT: NNN GeoPackage lezen mislukt: {e}")
        return {"in_straal": [], "in_signaal": []}

    in_straal  = []
    in_signaal = []

    for _, rij in gdf.iterrows():
        geom_rd = rij.geometry
        if geom_rd is None or geom_rd.is_empty:
            continue
        naam      = str(rij.get("naam", "NNN-gebied"))
        afstand_p = punt_rd.distance(geom_rd)
        geom_wgs  = shapely_transform(lambda x, y: t_to_wgs.transform(x, y), geom_rd)
        rings     = _geom_rings_wgs84(geom_wgs)
        item      = {"naam": naam, "afstand_m": round(afstand_p), "poly_rings": rings}

        if geom_rd.contains(punt_rd):
            log(f"  NNN TREFFER: puntlocatie ligt BINNEN '{naam}'.")
            in_straal.append(item)
        elif afstand_p <= NNN_SIGNAAL_MARGE:
            log(f"  NNN nabij: puntlocatie op {afstand_p:.0f} m van '{naam}' "
                f"(< {NNN_SIGNAAL_MARGE} m).")
            in_signaal.append(item)

    log(f"  NNN: {len(in_straal)} puntlocatie binnen gebied | "
        f"{len(in_signaal)} nabij (< {NNN_SIGNAAL_MARGE} m).")
    return {"in_straal": in_straal, "in_signaal": in_signaal}
