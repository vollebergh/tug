"""
tug_bronnen_geocode.py -- Reverse geocoding via PDOK Locatieserver

Bevat alle functies voor reverse geocoding van coördinaten naar adressen
en woonplaatsen. Wordt gebruikt door BAG-postprocessing en de PDOK
Location API-gebaseerde bronnen (begraafplaatsen, maneges).
"""

import re

import requests
from pyproj import Transformer

from tug_config import LOCATIESERVER_REVERSE
from tug_geo import extract_lon_lat
from tug_types import FeatureList, LogFn


# ──────────────────────────────────────────────
# Reverse geocode (enkelvoudig)
# ──────────────────────────────────────────────

def reverse_geocode(lat: float, lon: float, log: LogFn) -> str:
    try:
        resp = requests.get(
            LOCATIESERVER_REVERSE,
            params={"lat": lat, "lon": lon, "type": "adres", "rows": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            docs = resp.json().get("response", {}).get("docs", [])
            if docs:
                doc = docs[0]
                weergave = doc.get("weergavenaam")
                if weergave:
                    return weergave
                straat = doc.get("straatnaam", "")
                huisnr = doc.get("huisnummer", "")
                pc = doc.get("postcode", "")
                wpl = doc.get("woonplaatsnaam", "")
                return f"{straat} {huisnr}, {pc} {wpl}".strip(", ")
    except Exception as e:
        log(f"  WAARSCHUWING: reverse geocode mislukt ({lat:.5f},{lon:.5f}): {e}")
    return ""


def reverse_geocode_adres_wpl(lat: float, lon: float, log: LogFn) -> tuple[str, str]:
    """Retourneert (adres_str, pc_wpl_str) via PDOK Locatieserver reverse geocode."""
    try:
        resp = requests.get(
            LOCATIESERVER_REVERSE,
            params={"lat": lat, "lon": lon, "type": "adres", "rows": 1},
            timeout=10,
        )
        if resp.status_code == 200:
            docs = resp.json().get("response", {}).get("docs", [])
            if docs:
                doc    = docs[0]
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
    except Exception as e:
        log(f"  WAARSCHUWING: reverse geocode mislukt ({lat:.5f},{lon:.5f}): {e}")
    return "—", ""


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

def vul_woonplaats_via_reverse_geocode(features: FeatureList, log: LogFn) -> None:
    ontbrekend = [
        f for f in features
        if not (f.get("properties", {}).get("woonplaatsnaam")
                or f.get("properties", {}).get("woonplaatsNaam")
                or f.get("properties", {}).get("woonplaats"))
        or not _heeft_straatnaam(f.get("properties", {}))
    ]
    if not ontbrekend:
        log("  Adres reverse geocode: geen VBO's zonder woonplaats/straatnaam, overgeslagen.")
        return

    cache = {}
    gevuld_wpl = 0
    gevuld_straat = 0
    _rd_transformer = None

    for feat in ontbrekend:
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            lat, lon = extract_lon_lat(feat)
        except Exception:
            continue

        if lon > 1000:
            if _rd_transformer is None:
                _rd_transformer = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)
            wgs_lon, wgs_lat = _rd_transformer.transform(lon, lat)
            lat, lon = wgs_lat, wgs_lon

        cache_key = (round(lat, 7), round(lon, 7))
        if cache_key not in cache:
            try:
                resp = requests.get(
                    LOCATIESERVER_REVERSE,
                    params={"lat": lat, "lon": lon, "type": "adres", "rows": 1},
                    timeout=10,
                )
                gevonden = {"wpl": "", "straat": ""}
                if resp.status_code == 200:
                    docs = resp.json().get("response", {}).get("docs", [])
                    if docs:
                        doc = docs[0]
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
            except Exception:
                cache[cache_key] = {"wpl": "", "straat": ""}

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

    log(f"  Adres reverse geocode: {len(ontbrekend)} VBO's verwerkt via {len(cache)} unieke locaties "
        f"({gevuld_wpl} woonplaats, {gevuld_straat} straatnaam ingevuld).")


# ──────────────────────────────────────────────
# Generieke polygoon-opvraging (BRT via href)
# ──────────────────────────────────────────────

_PDOK_TOEGESTANE_DOMEINEN = ("api.pdok.nl", "geodata.nationaalgeoregister.nl")


def _pdok_location_haal_polygoon(href, log):
    # Domeincheck: accepteer alleen bekende PDOK-domeinen
    try:
        from urllib.parse import urlparse as _urlparse
        hostname = _urlparse(href).hostname or ""
        if not any(hostname == d or hostname.endswith("." + d)
                   for d in _PDOK_TOEGESTANE_DOMEINEN):
            log(f"  WAARSCHUWING: BRT polygoon-href verwijst naar onverwacht domein "
                f"({hostname!r}) — overgeslagen.")
            return None
    except Exception:
        log(f"  WAARSCHUWING: BRT polygoon-href ongeldig ({href!r}) — overgeslagen.")
        return None
    try:
        resp = requests.get(href, timeout=15)
        if resp.status_code != 200:
            log(f"  WAARSCHUWING: BRT polygoon-opvraging gaf status {resp.status_code} ({href})")
            return None
        return resp.json()
    except Exception as e:
        log(f"  WAARSCHUWING: BRT polygoon-opvraging mislukt: {e}")
        return None
