"""
tug_http.py -- Eén toegangsweg naar de externe bronnen

Elke uitgaande aanroep van de pipeline loopt via deze module. Zo gelden drie
afspraken overal, in plaats van per bronmodule herhaald (en soms vergeten):

1. **Alleen https naar bekende hosts.** Een URL — ook een verwijzing uit een
   bronantwoord, en elke stap van een redirect — moet `https` gebruiken en naar
   een host uit `tug_config.BRON_HOSTS` wijzen. Redirects worden daarom met de
   hand gevolgd en per stap opnieuw getoetst.
2. **Een maximale omvang.** Antwoorden worden gestreamd en afgebroken zodra ze
   groter worden dan de grens die de aanroeper opgeeft; een gecomprimeerd
   antwoord telt met zijn uitgepakte omvang.
3. **Mislukken is een uitzondering, geen leeg resultaat.** Netwerkfout, time-out,
   onverwachte status, te groot antwoord of onleesbare JSON: altijd `BronFout`.
   Of de pipeline daarop afbreekt of zichtbaar doorgaat, beslist de aanroeper
   via het bronregister (tug_bronstatus) — niet deze module.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from tug_config import BRON_HOSTS
from tug_logging import Voortgang

MAX_REDIRECTS = 5
_BLOK         = 64 * 1024


class BronFout(Exception):
    """Een externe bron leverde geen bruikbaar antwoord."""


def _kort(url: str) -> str:
    """URL zonder querystring, voor foutmeldingen die in het rapport terechtkomen."""
    deel = urlparse(url)
    return f"{deel.hostname or '?'}{deel.path}"


def veilige_bron_url(url: str, toegestane_hosts: tuple[str, ...] = BRON_HOSTS) -> str:
    """Geef `url` terug als die https gebruikt en naar een toegestane host wijst.

    Een host is toegestaan als hij gelijk is aan een vermelding of er een
    subdomein van is. Gooit BronFout bij elke afwijking.
    """
    try:
        deel = urlparse(url)
        poort = deel.port
    except ValueError as fout:
        raise BronFout(f"ongeldige URL: {url!r}") from fout
    if deel.scheme != "https":
        raise BronFout(f"alleen https is toegestaan, niet {deel.scheme or 'geen schema'!r}: "
                       f"{_kort(url)}")
    if poort not in (None, 443):
        raise BronFout(f"afwijkende poort {poort} niet toegestaan: {_kort(url)}")
    host = (deel.hostname or "").lower()
    if not host or not any(host == h or host.endswith("." + h) for h in toegestane_hosts):
        raise BronFout(f"host {host!r} staat niet op de lijst van toegestane bronhosts")
    return url


@contextmanager
def _open(
    url: str, *, params: dict[str, Any] | None, headers: dict[str, str] | None,
    timeout: float | tuple[float, float], toegestane_hosts: tuple[str, ...],
    methode: str = "GET",
) -> Iterator[requests.Response]:
    """Open een verbinding en volg redirects met de hand, elke stap getoetst."""
    huidige = veilige_bron_url(url, toegestane_hosts)
    for _ in range(MAX_REDIRECTS + 1):
        try:
            resp = requests.request(
                methode, huidige, params=params, headers=headers, timeout=timeout,
                stream=True, allow_redirects=False,
            )
        except requests.RequestException as fout:
            raise BronFout(f"{_kort(huidige)} niet bereikbaar "
                           f"({fout.__class__.__name__})") from fout
        if resp.is_redirect:
            doel = urljoin(huidige, resp.headers.get("location", ""))
            resp.close()
            huidige = veilige_bron_url(doel, toegestane_hosts)
            params = None   # zitten al in de redirect-URL
            continue
        try:
            if resp.status_code != 200:
                raise BronFout(f"{_kort(huidige)} gaf HTTP-status {resp.status_code}")
            yield resp
        finally:
            resp.close()
        return
    raise BronFout(f"{_kort(url)}: meer dan {MAX_REDIRECTS} redirects")


def _controleer_lengte(resp: requests.Response, max_bytes: int, url: str) -> int | None:
    lengte = resp.headers.get("content-length", "")
    if lengte.isdigit():
        if int(lengte) > max_bytes:
            raise BronFout(f"{_kort(url)}: antwoord van {int(lengte):,} bytes is groter dan "
                           f"de grens van {max_bytes:,} bytes")
        return int(lengte)
    return None


def _stream(resp: requests.Response, max_bytes: int, url: str) -> Iterator[bytes]:
    ontvangen = 0
    try:
        for blok in resp.iter_content(chunk_size=_BLOK):
            ontvangen += len(blok)
            if ontvangen > max_bytes:
                raise BronFout(f"{_kort(url)}: antwoord groter dan de grens van "
                               f"{max_bytes:,} bytes — afgebroken")
            yield blok
    except requests.RequestException as fout:
        raise BronFout(f"{_kort(url)}: verbinding verbroken tijdens het lezen "
                       f"({fout.__class__.__name__})") from fout


def haal(
    url: str, *, max_bytes: int, timeout: float | tuple[float, float],
    params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
    toegestane_hosts: tuple[str, ...] = BRON_HOSTS,
) -> bytes:
    """Haal een antwoord op als bytes, begrensd op `max_bytes`."""
    with _open(url, params=params, headers=headers, timeout=timeout,
               toegestane_hosts=toegestane_hosts) as resp:
        _controleer_lengte(resp, max_bytes, url)
        return b"".join(_stream(resp, max_bytes, url))


def haal_json(
    url: str, *, max_bytes: int, timeout: float | tuple[float, float],
    params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
    toegestane_hosts: tuple[str, ...] = BRON_HOSTS,
) -> dict[str, Any]:
    """Haal een JSON-object op. Een ander JSON-type dan een object is ook een BronFout."""
    ruw = haal(url, max_bytes=max_bytes, timeout=timeout, params=params,
               headers=headers, toegestane_hosts=toegestane_hosts)
    try:
        data = json.loads(ruw)
    except ValueError as fout:
        raise BronFout(f"{_kort(url)}: antwoord is geen geldige JSON") from fout
    if not isinstance(data, dict):
        raise BronFout(f"{_kort(url)}: JSON-object verwacht, {type(data).__name__} ontvangen")
    return data


def bestaat(
    url: str, *, timeout: float | tuple[float, float],
    toegestane_hosts: tuple[str, ...] = BRON_HOSTS,
) -> bool:
    """HEAD-verzoek: True bij status 200 (na getoetste redirects), anders False.

    Bedoeld voor het zoeken naar een bestand waarvan de naam niet vastligt; een
    ontbrekend bestand is daar een gewone uitkomst, geen fout. Een URL die niet
    door `veilige_bron_url` komt, blijft wél een BronFout.
    """
    veilige_bron_url(url, toegestane_hosts)
    try:
        with _open(url, params=None, headers=None, timeout=timeout,
                   toegestane_hosts=toegestane_hosts, methode="HEAD"):
            return True
    except BronFout:
        return False


def download_naar_bestand(
    url: str, doel: Path, *, max_bytes: int, timeout: float | tuple[float, float],
    headers: dict[str, str] | None = None, label: str = "downloaden",
    toegestane_hosts: tuple[str, ...] = BRON_HOSTS,
) -> int:
    """Download naar `doel` via een tijdelijk `.part`-bestand; retourneert het aantal bytes.

    Het doelbestand wordt pas vervangen als de download volledig is, zodat een
    afgebroken download een bestaande cache niet beschadigt.
    """
    tijdelijk = doel.with_name(doel.name + ".part")
    ontvangen = 0
    try:
        with _open(url, params=None, headers=headers, timeout=timeout,
                   toegestane_hosts=toegestane_hosts) as resp:
            totaal = _controleer_lengte(resp, max_bytes, url)
            with tijdelijk.open("wb") as uit, Voortgang() as voortgang:
                for blok in _stream(resp, max_bytes, url):
                    uit.write(blok)
                    ontvangen += len(blok)
                    mb = ontvangen / (1024 * 1024)
                    if totaal:
                        voortgang(f"  {label}: {mb:.1f} MB / {totaal / (1024 * 1024):.1f} MB "
                                  f"({ontvangen / totaal * 100:.0f}%)")
                    else:
                        voortgang(f"  {label}: {mb:.1f} MB ontvangen")
        tijdelijk.replace(doel)
    except BaseException:
        tijdelijk.unlink(missing_ok=True)
        raise
    return ontvangen
