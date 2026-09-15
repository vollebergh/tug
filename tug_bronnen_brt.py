"""
tug_bronnen_brt.py -- PDOK Location API + Overijssel WFS-bronnen

Bevat signaleringsfuncties voor begraafplaatsen, maneges en luchthavens.
Begraafplaatsen en maneges worden opgespoord via de PDOK Locatieserver (BRT-dataset);
luchthavens via de WFS van GeoPortaal Overijssel.

Elke functie meldt in het bronregister hoe de bevraging is afgelopen. Een
mislukte zoekterm of polygoon laat de rest van de detectie doorlopen, maar de
bron staat dan als niet volledig geraadpleegd in het rapport. De luchthavens zijn
de uitzondering: zonder die bron is het verbod binnen 1.000 m niet te toetsen en
breekt de toetsing af.
"""

import logging

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from tug_bronnen_geocode import pdok_location_haal_polygoon, reverse_geocode_adres_wpl
from tug_bronstatus import Bronregister
from tug_config import (
    BEGRAAFPLAATS_ZOEK_MARGE,
    BRT_DODENAKKER_ZOEK_MARGE,
    BRT_TERREIN_MAX_PAGES,
    BRT_TERREIN_PAGE_SIZE,
    BRT_TERREIN_VLK_URL,
    LUCHTHAVEN_GRENS_M,
    LUCHTHAVEN_SIGNAAL_M,
    LUCHTHAVEN_WFS,
    LUCHTHAVEN_WFS_LAYER,
    MANEGE_SIGNAAL_MARGE,
    MANEGE_ZOEKTERMEN,
    MAX_BYTES_API,
    PDOK_LOCATION_API,
)
from tug_geo import (
    _geom_rings_wgs84,
    circle_bbox_wgs84,
    make_transformer,
    shapely_from_geojson_geom,
    transform_geom_to_rd,
)
from tug_http import BronFout, haal_json
from tug_types import LogFn, SignaalResultaat

_logger = logging.getLogger("tug.bronnen_brt")

# De `next`-link van de BRT OGC API mag alleen naar PDOK zelf verwijzen.
_BRT_HOSTS = ("api.pdok.nl",)


class _Storingen:
    """Telt wat er binnen één detectie misging, voor één samenvattende melding."""

    def __init__(self, n_zoektermen: int):
        self.n_zoektermen = n_zoektermen
        self.zoektermen: list[str] = []
        self.geometrieen = 0
        self.overig: list[str] = []

    def meld(self, bronnen: Bronregister, sleutel: str) -> None:
        delen = []
        if self.zoektermen:
            delen.append(f"{len(self.zoektermen)} van {self.n_zoektermen} zoektermen niet "
                         f"bevraagd ({', '.join(self.zoektermen)})")
        if self.geometrieen:
            delen.append(f"geometrie van {self.geometrieen} kandidaat/kandidaten niet opgehaald")
        delen += self.overig
        if delen:
            bronnen.mislukt(sleutel, "; ".join(delen))
        else:
            bronnen.geraadpleegd(sleutel)


def _location_api_kandidaten(
    zoekterm: str, collectie: str, bbox_str: str, log: LogFn, storingen: _Storingen,
) -> list[dict]:
    """Kandidaten voor één zoekterm; een mislukte bevraging wordt geteld en levert []."""
    params = {"q": zoekterm, f"{collectie}[version]": "1", "bbox": bbox_str, "limit": 50}
    try:
        data = haal_json(PDOK_LOCATION_API, params=params, timeout=15, max_bytes=MAX_BYTES_API)
    except BronFout as fout:
        log(f"  FOUT: PDOK Location API mislukt voor '{zoekterm}': {fout}")
        storingen.zoektermen.append(zoekterm)
        return []
    kandidaten = data.get("features", [])
    log(f"  Zoekterm '{zoekterm}': {len(kandidaten)} kandidaten in bbox.")
    return kandidaten


def _polygoon_van(feat: dict, log: LogFn, storingen: _Storingen) -> dict | None:
    """Geometrie achter een Location API-treffer, of None (een storing wordt geteld)."""
    hrefs = feat.get("properties", {}).get("href", [])
    if not hrefs:
        return None
    href = hrefs[0] if isinstance(hrefs, list) else hrefs
    try:
        poly_feat = pdok_location_haal_polygoon(href)
    except BronFout as fout:
        naam = feat.get("properties", {}).get("display_name", "?")
        log(f"  FOUT: geometrie van '{naam}' niet opgehaald: {fout}")
        storingen.geometrieen += 1
        return None
    return poly_feat.get("geometry") or None


# ──────────────────────────────────────────────
# Begraafplaatsen — PDOK Location API (BRT)
# ──────────────────────────────────────────────

def signaleer_begraafplaatsen(
    circle_rd: BaseGeometry, straal: float, log: LogFn,
    punten_rd: BaseGeometry | None = None, *, bronnen: Bronregister,
) -> SignaalResultaat:
    # Afstanden tot de dichtstbijzijnde puntlocatie (B03)
    centrum = punten_rd if punten_rd is not None else circle_rd.centroid
    circle_zoek_rd  = circle_rd.buffer(BEGRAAFPLAATS_ZOEK_MARGE)
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(circle_zoek_rd)
    bbox_str = f"{lon_min},{lat_min},{lon_max},{lat_max}"

    log(f"  Begraafplaatsen ophalen via PDOK Location API / BRT "
        f"(zoek-bbox = straal+{BEGRAAFPLAATS_ZOEK_MARGE} m = "
        f"{straal + BEGRAAFPLAATS_ZOEK_MARGE:.0f} m; "
        f"intersectie-check op toetsingsafstand {straal:.0f} m) ...")

    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")
    gevonden_ids = set()
    definitief = []
    buiten_straal = []
    zoektermen = ("begraafplaats", "erebegraafplaats")
    storingen = _Storingen(len(zoektermen))

    for zoekterm in zoektermen:
        # Begraafplaatsen staan in de collectie functioneel_gebied (niet gebouw)
        kandidaten = _location_api_kandidaten(
            zoekterm, "functioneel_gebied", bbox_str, log, storingen,
        )
        for feat in kandidaten:
            feat_id = feat.get("id", "")
            if feat_id in gevonden_ids:
                continue
            gevonden_ids.add(feat_id)

            naam      = feat.get("properties", {}).get("display_name", "Onbekende begraafplaats")
            geom_dict = _polygoon_van(feat, log, storingen)
            if not geom_dict:
                continue

            geom_rd     = transform_geom_to_rd(shapely_from_geojson_geom(geom_dict))
            centroid_rd = geom_rd.centroid
            afstand     = centrum.distance(centroid_rd)
            lon_c, lat_c = t_to_wgs.transform(centroid_rd.x, centroid_rd.y)
            rings        = _geom_rings_wgs84(shapely_from_geojson_geom(geom_dict))

            item = {
                "naam":       naam,
                "afstand_m":  round(afstand),
                "adres":      "", "pc_wpl": "",
                "lat":        lat_c, "lon": lon_c,
                "poly_rings": rings,
                "_geom_rd":   geom_rd,
            }

            if circle_rd.intersects(geom_rd):
                item["adres"], item["pc_wpl"] = reverse_geocode_adres_wpl(
                    lat_c, lon_c, log, bronnen=bronnen)
                log(f"  Treffer: '{naam}' — polygoon snijdt toetsingsafstand "
                    f"{straal:.0f} m (centroid op {afstand:.0f} m).")
                definitief.append(item)
            else:
                log(f"  In bbox, buiten toetsingsafstand: '{naam}' "
                    f"(centroid op {afstand:.0f} m).")
                buiten_straal.append(item)

    log(f"\n  {len(definitief)} begraafplaats(en) snijden toetsingsafstand | "
        f"{len(buiten_straal)} in bbox maar buiten straal.")

    # ── Aanvulling: BRT top10nl terrein_vlak (typelandgebruik = 'dodenakker') ──
    brt_definitief, brt_buiten = _haal_brt_dodenakkers(
        circle_rd, straal, centrum, t_to_wgs, gevonden_ids, log, bronnen, storingen,
    )
    definitief    += brt_definitief
    buiten_straal += brt_buiten

    if brt_definitief or brt_buiten:
        log(f"  BRT dodenakker-totaal: {len(brt_definitief)} snijden toetsingsafstand | "
            f"{len(brt_buiten)} in bbox maar buiten straal.")

    log(f"\n  Totaal: {len(definitief)} begraafplaats(en) snijden toetsingsafstand | "
        f"{len(buiten_straal)} in bbox maar buiten straal.")
    storingen.meld(bronnen, "begraafplaatsen")
    return {"definitief": definitief, "buiten_straal": buiten_straal}


# ──────────────────────────────────────────────
# BRT top10nl — dodenakker (terrein_vlak)
# ──────────────────────────────────────────────

def _haal_brt_dodenakkers(
    circle_rd, straal, centrum, t_to_wgs, gevonden_ids, log, bronnen: Bronregister,
    storingen: _Storingen,
):
    """Haalt begraafplaatsen op via BRT top10nl OGC API (terrein_vlak, typelandgebruik=dodenakker).

    Retourneert (definitief, buiten_straal) — lijsten met items in hetzelfde
    formaat als signaleer_begraafplaatsen.  Deduplicatie via gevonden_ids (set
    van al door de Kadaster-API gevonden feature-ID's).
    """
    brt_zoek_rd = circle_rd.buffer(BRT_DODENAKKER_ZOEK_MARGE)
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(brt_zoek_rd)
    bbox_str = f"{lon_min},{lat_min},{lon_max},{lat_max}"

    log(f"  Dodenakkers ophalen via BRT top10nl terrein_vlak OGC API "
        f"(bbox-straal ≈ {straal + BRT_DODENAKKER_ZOEK_MARGE:.0f} m) ...")

    alle_features = []
    next_url: str | None = None
    pagina = 0
    nog_meer = False

    while pagina < BRT_TERREIN_MAX_PAGES:
        try:
            if next_url:
                data = haal_json(next_url, timeout=30, max_bytes=MAX_BYTES_API,
                                 toegestane_hosts=_BRT_HOSTS)
            else:
                data = haal_json(
                    BRT_TERREIN_VLK_URL,
                    params={"f": "json", "bbox": bbox_str, "limit": BRT_TERREIN_PAGE_SIZE},
                    timeout=30, max_bytes=MAX_BYTES_API, toegestane_hosts=_BRT_HOSTS,
                )
        except BronFout as fout:
            log(f"  FOUT: BRT terrein_vlak (pagina {pagina + 1}) niet opgehaald: {fout}")
            storingen.overig.append(f"BRT-dodenakkers vanaf pagina {pagina + 1} niet opgehaald")
            nog_meer = False
            break
        feats = data.get("features", [])
        alle_features.extend(feats)
        next_lnk = next(
            (lnk.get("href") for lnk in data.get("links", []) if lnk.get("rel") == "next"),
            None,
        )
        if not next_lnk or len(feats) < BRT_TERREIN_PAGE_SIZE:
            nog_meer = False
            break
        next_url = next_lnk
        pagina  += 1
        nog_meer = True

    if nog_meer:
        log(f"  FOUT: BRT terrein_vlak heeft meer dan {BRT_TERREIN_MAX_PAGES} pagina's; "
            f"niet alle vlakken zijn opgehaald.")
        storingen.overig.append(f"BRT-dodenakkers: meer dan {BRT_TERREIN_MAX_PAGES} pagina's")

    dodenakkers_raw = [
        f for f in alle_features
        if f.get("properties", {}).get("typelandgebruik") == "dodenakker"
    ]
    log(f"  BRT terrein_vlak: {len(alle_features)} features opgehaald, "
        f"{len(dodenakkers_raw)} dodenakker-polygon(en) gevonden.")

    # Converteer naar RD-geometrieën en filter eerder gevonden IDs
    rd_geoms = []
    for feat in dodenakkers_raw:
        props     = feat.get("properties", {})
        lokaal_id = props.get("lokaal_id", "")
        brt_id    = f"brt:{lokaal_id}"
        if brt_id in gevonden_ids:
            continue
        gevonden_ids.add(brt_id)
        geom_dict = feat.get("geometry")
        if geom_dict:
            naam = (props.get("naam") or props.get("naamofficieel")
                    or props.get("naamNL") or "")
            rd_geoms.append((shapely_from_geojson_geom(geom_dict), naam))

    # Cluster aangrenzende/overlappende polygonen tot aaneengesloten begraafplaatsen
    clusters = _cluster_dodenakker_geoms(rd_geoms)
    log(f"  BRT: {len(rd_geoms)} polygonen samengevoegd tot {len(clusters)} cluster(s).")

    definitief    = []
    buiten_straal = []

    for cluster_geom_wgs84, cluster_naam in clusters:
        geom_rd     = transform_geom_to_rd(cluster_geom_wgs84)
        centroid_rd = geom_rd.centroid
        afstand     = centrum.distance(centroid_rd)
        lon_c, lat_c = t_to_wgs.transform(centroid_rd.x, centroid_rd.y)
        rings        = _geom_rings_wgs84(cluster_geom_wgs84)

        item = {
            "naam":       cluster_naam or "Begraafplaats (BRT)",
            "afstand_m":  round(afstand),
            "adres":      "", "pc_wpl": "",
            "lat":        lat_c, "lon": lon_c,
            "poly_rings": rings,
            "_geom_rd":   geom_rd,
        }

        if circle_rd.intersects(geom_rd):
            item["adres"], item["pc_wpl"] = reverse_geocode_adres_wpl(
                lat_c, lon_c, log, bronnen=bronnen)
            if not cluster_naam:
                item["naam"] = (f"Begraafplaats, {item['pc_wpl']}"
                                if item["pc_wpl"] else "Begraafplaats (BRT)")
            log(f"  BRT treffer: '{item['naam']}' — polygoon snijdt toetsingsafstand "
                f"{straal:.0f} m (centroid op {afstand:.0f} m).")
            definitief.append(item)
        else:
            log(f"  BRT in bbox, buiten toetsingsafstand: '{item['naam']}' "
                f"(centroid op {afstand:.0f} m).")
            buiten_straal.append(item)

    return definitief, buiten_straal


def _cluster_dodenakker_geoms(
    rd_geoms: list[tuple],
    buffer_m: float = 5.0,
) -> list[tuple]:
    """Groepeert aangrenzende dodenakker-WGS84-geometrieën tot clusters.

    Geeft een lijst van (unioned_wgs84_geom, naam) terug, één per aaneengesloten
    begraafplaats-cluster.  Samenvoegen gebeurt op basis van WGS84-geometrieën
    omgezet naar RD voor de bufferberekening, en terug naar WGS84 voor de output.
    """
    if not rd_geoms:
        return []

    # Stap 1: maak RD-geometrieën met kleine buffer om randgevallen te sluiten
    buffered_rd = []
    for geom_wgs84, naam in rd_geoms:
        geom_rd = transform_geom_to_rd(geom_wgs84)
        buffered_rd.append((geom_rd.buffer(buffer_m), geom_wgs84, naam))

    # Stap 2: unie-gebaseerde clustering via union-find
    n = len(buffered_rd)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    for i in range(n):
        for j in range(i + 1, n):
            if buffered_rd[i][0].intersects(buffered_rd[j][0]):
                union(i, j)

    # Stap 3: groepeer per cluster
    clusters: dict[int, list] = {}
    for i, (_, geom_wgs84, naam) in enumerate(buffered_rd):
        root = find(i)
        clusters.setdefault(root, []).append((geom_wgs84, naam))

    # Stap 4: unie per cluster → (wgs84_geom, naam)
    result = []
    for members in clusters.values():
        wgs84_geoms = [g for g, _ in members]
        namen = [n for _, n in members if n]
        merged = unary_union(wgs84_geoms)
        naam   = namen[0] if namen else ""
        result.append((merged, naam))

    return result


# ──────────────────────────────────────────────
# Maneges — PDOK Location API (BRT)
# ──────────────────────────────────────────────

def haal_maneges_pdok(
    circle_rd: BaseGeometry, straal: float, log: LogFn,
    punten_rd: BaseGeometry | None = None, *, bronnen: Bronregister,
) -> SignaalResultaat:
    # Afstanden tot de dichtstbijzijnde puntlocatie (B03)
    centrum = punten_rd if punten_rd is not None else circle_rd.centroid
    signaal_cirkel = circle_rd.buffer(MANEGE_SIGNAAL_MARGE)
    lon_min, lat_min, lon_max, lat_max = circle_bbox_wgs84(signaal_cirkel)
    bbox_str = f"{lon_min},{lat_min},{lon_max},{lat_max}"
    aandacht_straal_tot = straal + MANEGE_SIGNAAL_MARGE

    log(f"  Maneges ophalen via PDOK Location API / BRT "
        f"(aandachtsgebied = straal + {MANEGE_SIGNAAL_MARGE} m = "
        f"{aandacht_straal_tot:.0f} m) ...")

    t_to_wgs = make_transformer("EPSG:28992", "EPSG:4326")
    gevonden_ids = set()
    in_straal = []
    buiten_straal = []
    storingen = _Storingen(len(MANEGE_ZOEKTERMEN))

    for zoekterm in MANEGE_ZOEKTERMEN:
        kandidaten = _location_api_kandidaten(zoekterm, "gebouw", bbox_str, log, storingen)
        for feat in kandidaten:
            feat_id = feat.get("id", "")
            if feat_id in gevonden_ids:
                continue
            gevonden_ids.add(feat_id)

            naam      = feat.get("properties", {}).get("display_name", "Onbekende manege")
            geom_dict = _polygoon_van(feat, log, storingen)
            if not geom_dict:
                continue

            geom_rd     = transform_geom_to_rd(shapely_from_geojson_geom(geom_dict))
            centroid_rd = geom_rd.centroid
            afstand     = centrum.distance(centroid_rd)
            lon_c, lat_c = t_to_wgs.transform(centroid_rd.x, centroid_rd.y)

            item = {
                "naam":      naam,
                "afstand_m": round(afstand),
                "adres":     "", "pc_wpl": "",
                "lat":       lat_c, "lon": lon_c,
            }

            if signaal_cirkel.intersects(geom_rd):
                item["adres"], item["pc_wpl"] = reverse_geocode_adres_wpl(
                    lat_c, lon_c, log, bronnen=bronnen)
                log(f"  Treffer ('{zoekterm}'): '{naam}' — polygoon snijdt aandachtsgebied "
                    f"(centroid op {afstand:.0f} m).")
                in_straal.append(item)
            else:
                log(f"  In bbox, buiten aandachtsgebied ('{zoekterm}'): '{naam}' "
                    f"(centroid op {afstand:.0f} m).")
                buiten_straal.append(item)

    log(f"  {len(in_straal)} manege(s) binnen aandachtsgebied | "
        f"{len(buiten_straal)} buiten aandachtsgebied.")
    storingen.meld(bronnen, "maneges")
    return {"in_straal": in_straal, "buiten_straal": buiten_straal}


# ──────────────────────────────────────────────
# Luchthavens — GeoPortaal Overijssel WFS
# ──────────────────────────────────────────────

def signaleer_luchthavens(
    circle_rd: BaseGeometry, log: LogFn, punten_rd: BaseGeometry | None = None,
    *, bronnen: Bronregister,
) -> SignaalResultaat:
    """Haalt luchthavenpuntlocaties op via GeoPortaal Overijssel WFS (on-the-fly, geen cache).

    Retourneert {'in_straal': [...], 'in_signaal': [...]}
      in_straal  — aanvraaglocatie binnen LUCHTHAVEN_GRENS_M (1.000 m) van luchthaven
                   → puntlocatie NIET toegestaan
      in_signaal — aanvraaglocatie op 1.000–2.000 m van luchthaven
                   → signalering

    De laag bevat alle luchthavens van de provincie. Een mislukte bevraging, en
    ook een antwoord zonder één enkele luchthaven, breekt de toetsing af: het
    verbod binnen 1.000 m is dan niet getoetst.
    """
    punt_rd  = punten_rd if punten_rd is not None else circle_rd.centroid
    t_to_rd  = make_transformer("EPSG:4326", "EPSG:28992")

    log(f"  Luchthavens: WFS opvragen ({LUCHTHAVEN_WFS_LAYER}) ...")
    params = {
        "SERVICE":      "WFS",
        "VERSION":      "2.0.0",
        "REQUEST":      "GetFeature",
        "TYPENAMES":    LUCHTHAVEN_WFS_LAYER,
        "OUTPUTFORMAT": "application/json",
        "SRSNAME":      "EPSG:4326",
    }
    try:
        fc = haal_json(LUCHTHAVEN_WFS, params=params, timeout=30, max_bytes=MAX_BYTES_API)
    except BronFout as fout:
        log(f"  FOUT: Luchthavens WFS ophalen mislukt: {fout}")
        bronnen.mislukt("luchthavens", str(fout))
        raise

    features = fc.get("features", [])
    log(f"  Luchthavens: {len(features)} locaties ontvangen.")
    if not features:
        log("  FOUT: de luchthavenlaag bevat geen enkele locatie — laag leeg of gewijzigd.")
        bronnen.mislukt("luchthavens", "de WFS-laag gaf nul luchthavens terug")

    in_straal  = []
    in_signaal = []

    for feat in features:
        props = feat.get("properties", {}) or {}
        geom  = feat.get("geometry", {}) or {}
        if geom.get("type") != "Point":
            continue
        coords  = geom.get("coordinates", [])
        if len(coords) < 2:
            continue
        lon_lh, lat_lh = coords[0], coords[1]
        lh_x, lh_y     = t_to_rd.transform(lon_lh, lat_lh)
        lh_pt_rd       = Point(lh_x, lh_y)
        afstand        = round(punt_rd.distance(lh_pt_rd))

        naam         = props.get("NAAM") or "Onbekend"
        omschrijving = props.get("OMSCHRIJVING") or ""
        gebruik      = props.get("GEBRUIK") or ""
        beperking    = props.get("BEPERKING") or ""
        exploitant   = props.get("EXPLOITANT") or ""
        adres_lh     = props.get("ADRES") or ""

        item = {
            "naam":         naam,
            "omschrijving": omschrijving,
            "gebruik":      gebruik,
            "beperking":    beperking,
            "exploitant":   exploitant,
            "adres":        adres_lh,
            "afstand_m":    afstand,
            "lat":          lat_lh,
            "lon":          lon_lh,
        }

        if afstand <= LUCHTHAVEN_GRENS_M:
            log(f"  LUCHTHAVEN CONFLICT: '{naam}' op {afstand} m van aanvraaglocatie "
                f"(< {LUCHTHAVEN_GRENS_M} m — NIET TOEGESTAAN).")
            in_straal.append(item)
        elif afstand <= LUCHTHAVEN_SIGNAAL_M:
            log(f"  Luchthaven signalering: '{naam}' op {afstand} m van aanvraaglocatie "
                f"(< {LUCHTHAVEN_SIGNAAL_M} m).")
            in_signaal.append(item)
        else:
            log(f"  Luchthaven buiten signaalgebied: '{naam}' op {afstand} m.")

    log(f"  Luchthavens: {len(in_straal)} binnen {LUCHTHAVEN_GRENS_M} m | "
        f"{len(in_signaal)} binnen {LUCHTHAVEN_SIGNAAL_M} m.")
    bronnen.geraadpleegd("luchthavens")
    return {"in_straal": in_straal, "in_signaal": in_signaal}
