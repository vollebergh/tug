"""
tug_bronnen_geocode.py -- Reverse geocoding via PDOK Locatieserver

Bevat de functies die coördinaten omzetten naar een adres of woonplaats, en de
generieke polygoon-opvraging via de `href` van een Location API-treffer. Wordt
gebruikt door BAG-postprocessing en de PDOK Location API-bronnen (begraafplaatsen,
maneges).

Adresaanvulling raakt alleen de weergave: het object staat hoe dan ook in de
lijst. Een mislukte aanvulling wordt daarom gemeld onder `adresaanvulling` en
breekt niets af. Een mislukte polygoon-opvraging raakt wél de detectie en gaat
als BronFout terug naar de aanroeper.
"""

import logging
import re
from typing import Any

from pyproj import Transformer

from tug_bronstatus import Bronregister
from tug_config import LOCATIESERVER_REVERSE, MAX_BYTES_API
from tug_geo import extract_lon_lat
from tug_http import BronFout, haal_json
from tug_types import FeatureList, LogFn

_logger = logging.getLogger("tug.bronnen_geocode")

# Hosts waarnaar een `href` uit een Location API-antwoord mag verwijzen.
_PDOK_TOEGESTANE_DOMEINEN = ("api.pdok.nl", "geodata.nationaalgeoregister.nl")


def _reverse_doc(lat: float, lon: float) -> dict[str, Any] | None:
    data = haal_json(
        LOCATIESERVER_REVERSE, params={"lat": lat, "lon": lon, "type": "adres", "rows": 1},
        timeout=10, max_bytes=MAX_BYTES_API,
    )
    docs = data.get("response", {}).get("docs", [])
    return docs[0] if docs else None


# ──────────────────────────────────────────────
# Reverse geocode (enkelvoudig)
# ──────────────────────────────────────────────

def reverse_geocode_adres_wpl(
    lat: float, lon: float, log: LogFn, *, bronnen: Bronregister,
) -> tuple[str, str]:
    """Retourneert (adres_str, pc_wpl_str) via PDOK Locatieserver reverse geocode."""
    try:
        doc = _reverse_doc(lat, lon)
    except BronFout as fout:
        log(f"  WAARSCHUWING: adres bij ({lat:.5f},{lon:.5f}) niet opgehaald: {fout}")
        bronnen.mislukt("adresaanvulling", "adres bij een gesignaleerd object niet opgehaald")
        return "—", ""
    bronnen.geraadpleegd("adresaanvulling")
    if not doc:
        return "—", ""
    straat = doc.get("straatnaam", "")
    huisnr = doc.get("huisnummer", "")
    pc     = doc.get("postcode", "")
    wpl    = doc.get("woonplaatsnaam", "")
    adres  = f"{straat} {huisnr}".strip()
    pc_wpl = f"{pc}  {wpl}".strip()
    if not adres:
        weergave = doc.get("weergavenaam", "")
        if "," in weergave:
            parts  = weergave.rsplit(",", 1)
            adres  = parts[0].strip()
            pc_wpl = pc_wpl or parts[1].strip()
        else:
            adres = weergave
    return adres or "—", pc_wpl


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _wpl_title(s):
    """Corrigeert UPPERCASE plaatsnamen (DUO) naar Titel-casing."""
    return " ".join(w.capitalize() for w in s.split()) if s else s


def _heeft_straatnaam(props):
    return bool(
        props.get("openbare_ruimte") or props.get("openbareruimtenaam")
        or props.get("openbareRuimteNaam") or props.get("straatnaam")
        or props.get("straatNaam") or props.get("naamOpenbareRuimte")
        or props.get("korteNaam")
    )


# ──────────────────────────────────────────────
# Vul woonplaats/straatnaam via reverse geocode op meerdere features
# ──────────────────────────────────────────────

def vul_woonplaats_via_reverse_geocode(
    features: FeatureList, log: LogFn, *, bronnen: Bronregister,
) -> None:
    ontbrekend = [
        f for f in features
        if not (f.get("properties", {}).get("woonplaatsnaam")
                or f.get("properties", {}).get("woonplaatsNaam")
                or f.get("properties", {}).get("woonplaats"))
        or not _heeft_straatnaam(f.get("properties", {}))
    ]
    if not ontbrekend:
        log("  Adres reverse geocode: geen VBO's zonder woonplaats/straatnaam, overgeslagen.")
        bronnen.geraadpleegd("adresaanvulling")
        return

    cache: dict[tuple[float, float], dict[str, str]] = {}
    gevuld_wpl = 0
    gevuld_straat = 0
    mislukt = 0
    _rd_transformer = None

    for feat in ontbrekend:
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            lat, lon = extract_lon_lat(feat)
        except (KeyError, IndexError, TypeError, ValueError) as fout:
            _logger.warning(f"Reverse geocode overgeslagen (geen bruikbare geometrie): {fout}")
            continue

        if lon > 1000:
            if _rd_transformer is None:
                _rd_transformer = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)
            wgs_lon, wgs_lat = _rd_transformer.transform(lon, lat)
            lat, lon = wgs_lat, wgs_lon

        cache_key = (round(lat, 7), round(lon, 7))
        if cache_key not in cache:
            gevonden = {"wpl": "", "straat": ""}
            try:
                doc = _reverse_doc(lat, lon)
            except BronFout as fout:
                _logger.warning(f"Reverse geocode mislukt voor {cache_key}: {fout}")
                mislukt += 1
                doc = None
            if doc:
                weergave = doc.get("weergavenaam", "")
                gevonden["straat"] = doc.get("straatnaam", "")
                if not gevonden["straat"] and weergave:
                    m_s = re.match(r'^(.*?)\s+\d', weergave)
                    if m_s:
                        gevonden["straat"] = m_s.group(1).strip()
                gevonden["wpl"] = doc.get("woonplaatsnaam", "")
                if not gevonden["wpl"] and weergave:
                    m_w = re.search(r"\d{4}[A-Z]{2}\s+(.+)$", weergave)
                    if m_w:
                        gevonden["wpl"] = m_w.group(1).strip()
            cache[cache_key] = gevonden

        gevonden = cache[cache_key]
        props = feat["properties"]
        if gevonden["wpl"] and not (
            props.get("woonplaatsnaam") or props.get("woonplaatsNaam")
            or props.get("woonplaats")
        ):
            props["woonplaatsnaam"] = gevonden["wpl"]
            gevuld_wpl += 1
        if gevonden["straat"] and not _heeft_straatnaam(props):
            props["openbareruimtenaam"] = gevonden["straat"]
            gevuld_straat += 1

    log(f"  Adres reverse geocode: {len(ontbrekend)} VBO's verwerkt via "
        f"{len(cache)} unieke locaties "
        f"({gevuld_wpl} woonplaats, {gevuld_straat} straatnaam ingevuld).")
    if mislukt:
        log(f"  WAARSCHUWING: {mislukt} adresaanvulling(en) mislukt; die adressen blijven "
            f"zonder aangevulde straatnaam of woonplaats in de lijst staan.")
        bronnen.mislukt("adresaanvulling", f"{mislukt} locatie(s) niet aangevuld")
    else:
        bronnen.geraadpleegd("adresaanvulling")


# ──────────────────────────────────────────────
# Generieke polygoon-opvraging (BRT via href)
# ──────────────────────────────────────────────

def pdok_location_haal_polygoon(href: str) -> dict[str, Any]:
    """Haal de polygoon achter een Location API-treffer op.

    De `href` komt uit een bronantwoord en wordt alleen gevolgd naar een bekend
    PDOK-domein en over https. Elke fout — ook een verwijzing die niet door die
    controle komt — is een BronFout voor de aanroeper.
    """
    return haal_json(href, timeout=15, max_bytes=MAX_BYTES_API,
                     toegestane_hosts=_PDOK_TOEGESTANE_DOMEINEN)
