"""
tug_bronstatus.py -- Wat er van elke externe bron is binnengekomen

Een detectielaag die niets vindt, is in het resultaat niet te onderscheiden van
een detectielaag die stuk is. Daarom meldt elke bronfunctie hier hoe de
bevraging is afgelopen, en leest het rapport uit dit register — niet uit het
aantal treffers — of een conclusie getrokken mag worden.

Per bron ligt vast of een storing de toetsing **afbreekt** (fail-closed) of
**zichtbaar** blijft (fail-visible):

- Afbreken: de BAG-verblijfsobjecten en de gevelcheck (de adressenlijst zelf),
  Natura 2000, de luchthavens en luchthaventerreinen (het verbod binnen 1.000 m) en het ILT-register
  (de toetsingsafstand). Zonder deze bronnen is er geen rapport te maken dat
  klopt; de run is binnen minuten te herhalen.
- Zichtbaar: de overige bronnen, en de afstandsnorm van de luchtvaartuigen
  als die voor geen enkel luchtvaartuig herleidbaar is. Het rapport trekt voor
  zo'n bron geen conclusie maar meldt in rood dat zij niet (volledig) is
  geraadpleegd, en de run eindigt met exitcode 2.

Bronnen die alleen de weergave raken (kaarttegels, gevelcontouren op de kaart,
aanvulling van straatnaam en woonplaats) worden gemeld maar tellen niet mee voor
de volledigheid van de toetsing.
"""

from dataclasses import asdict, dataclass
from typing import Any

from tug_http import BronFout

GERAADPLEEGD = "geraadpleegd"
CACHE        = "cache"               # lokale kopie binnen de bewaartermijn
VEROUDERD    = "verouderde_cache"    # noodterugval: verversen mislukt, bewaartermijn verlopen
MISLUKT      = "mislukt"

# Een slechtere uitkomst overschrijft een betere, nooit andersom.
_RANG = {GERAADPLEEGD: 0, CACHE: 1, VEROUDERD: 2, MISLUKT: 3}


@dataclass(frozen=True)
class Bron:
    label: str
    toetsingsrelevant: bool
    afbreken: bool


BRONNEN: dict[str, Bron] = {
    "ilt_register":         Bron("ILT-luchtvaartuigregister", True, True),
    # Geen externe bron, maar wel een uitkomst waar de conclusie van afhangt:
    # zonder herleidbare norm wordt ruim geïnventariseerd, zonder oordeel (B13).
    "afstandsnorm":         Bron("Afstandsnorm luchtvaartuigen (ILT-register en NLR-tabel)",
                                 True, False),
    "bag_verblijfsobjecten": Bron("BAG-verblijfsobjecten (PDOK WFS)", True, True),
    "bag_gevelcheck":       Bron("BAG-panden voor de gevelcheck (PDOK WFS)", True, True),
    "natura2000":           Bron("Natura 2000 (PDOK WFS)", True, True),
    "luchthavens":          Bron("Luchthavens (GeoPortaal Overijssel WFS)", True, True),
    "luchthaventerreinen":  Bron("Luchthaventerreinen (BRT Top10NL, PDOK)", True, True),
    "nnn":                  Bron("Natuurnetwerk Nederland (PDOK ATOM, lokale cache)", True, False),
    "begraafplaatsen":      Bron("Begraafplaatsen (PDOK Location API en BRT)", True, False),
    "lrk":                  Bron("Kinderopvang (Landelijk Register Kinderopvang)", True, False),
    "duo":                  Bron("Scholen (DUO, gegeocodeerd via PDOK Locatieserver)", True, False),
    "maneges":              Bron("Maneges (PDOK Location API)", True, False),
    "adresaanvulling":      Bron("Straatnaam en woonplaats aanvullen (PDOK Locatieserver)",
                                 False, False),
    "gevelcontouren":       Bron("Gevelcontouren op de kaart (BAG-panden)", False, False),
    "kaarttegels":          Bron("Achtergrondkaarten (PDOK-tegels)", False, False),
}


class ToetsingAfgebroken(BronFout):
    """Een bron waarvan de toetsing niet zonder kan, is niet geraadpleegd."""


@dataclass
class BronUitkomst:
    sleutel: str
    label: str
    status: str
    melding: str = ""
    ouderdom_dagen: int | None = None
    toetsingsrelevant: bool = True

    @property
    def volledig(self) -> bool:
        return self.status in (GERAADPLEEGD, CACHE)


class Bronregister:
    """Houdt per bron de slechtste uitkomst van deze run bij."""

    def __init__(self) -> None:
        self._uitkomsten: dict[str, BronUitkomst] = {}

    def _zet(self, sleutel: str, status: str, melding: str = "",
             ouderdom_dagen: int | None = None) -> None:
        bron = BRONNEN[sleutel]
        bestaand = self._uitkomsten.get(sleutel)
        if bestaand and _RANG[bestaand.status] > _RANG[status]:
            return
        if bestaand and _RANG[bestaand.status] == _RANG[status] and bestaand.melding:
            # Zelfde uitkomst nog eens gemeld: melding aanvullen, niet herhalen.
            if not melding or melding in bestaand.melding.split("; "):
                melding = bestaand.melding
            else:
                melding = f"{bestaand.melding}; {melding}"
        self._uitkomsten[sleutel] = BronUitkomst(
            sleutel, bron.label, status, melding, ouderdom_dagen, bron.toetsingsrelevant,
        )

    def geraadpleegd(self, sleutel: str, melding: str = "") -> None:
        self._zet(sleutel, GERAADPLEEGD, melding)

    def cache(self, sleutel: str, ouderdom_dagen: int, melding: str = "") -> None:
        self._zet(sleutel, CACHE, melding, ouderdom_dagen)

    def verouderd(self, sleutel: str, ouderdom_dagen: int, melding: str) -> None:
        self._zet(sleutel, VEROUDERD, melding, ouderdom_dagen)

    def mislukt(self, sleutel: str, melding: str) -> None:
        """Leg vast dat een bron niet (volledig) is geraadpleegd.

        Voor een bron waar de toetsing niet zonder kan, gooit dit
        ToetsingAfgebroken: de aanroeper hoeft het beleid niet zelf te kennen.
        """
        self._zet(sleutel, MISLUKT, melding)
        if BRONNEN[sleutel].afbreken:
            raise ToetsingAfgebroken(f"{BRONNEN[sleutel].label}: {melding}")

    def status(self, sleutel: str) -> BronUitkomst | None:
        return self._uitkomsten.get(sleutel)

    def controleer_compleet(self, verwacht: list[str]) -> None:
        """Een bron die geen uitkomst meldde, is niet aantoonbaar geraadpleegd."""
        for sleutel in verwacht:
            if sleutel not in self._uitkomsten:
                self.mislukt(sleutel, "geen uitkomst gemeld (programmafout)")

    def naar_state(self) -> list[dict[str, Any]]:
        return [asdict(u) for u in self._uitkomsten.values()]


# ──────────────────────────────────────────────
# Lezen uit de state (voor rapport, runner en schil)
# ──────────────────────────────────────────────

def uitkomsten_uit_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Alle bronuitkomsten uit de stappen classificatie en ruimtelijk, in volgorde."""
    return [
        *state.get("classificatie", {}).get("bronstatus", []),
        *state.get("ruimtelijk", {}).get("bronstatus", []),
    ]


def is_volledig(uitkomst: dict[str, Any]) -> bool:
    return uitkomst.get("status") in (GERAADPLEEGD, CACHE)


def onvolledige_toetsing(state: dict[str, Any]) -> list[dict[str, Any]]:
    """De toetsingsrelevante bronnen die niet volledig zijn geraadpleegd."""
    return [u for u in uitkomsten_uit_state(state)
            if u.get("toetsingsrelevant", True) and not is_volledig(u)]


def kort(uitkomst: dict[str, Any]) -> str:
    """Bron en uitkomst, zonder de technische melding (voor kaartbanner en synthese)."""
    status = uitkomst.get("status")
    dagen = uitkomst.get("ouderdom_dagen")
    if status == MISLUKT and uitkomst.get("sleutel") == "afstandsnorm":
        kern = "niet vastgesteld"
    elif status == MISLUKT:
        kern = "niet (volledig) geraadpleegd"
    elif status == VEROUDERD:
        kern = f"verouderde lokale kopie gebruikt ({dagen} dagen oud)"
    elif status == CACHE:
        kern = f"lokale kopie ({dagen} dagen oud)"
    else:
        kern = "geraadpleegd"
    return f"{uitkomst.get('label', uitkomst.get('sleutel'))}: {kern}"


def beschrijf(uitkomst: dict[str, Any]) -> str:
    """Eén regel voor rapport en runlog: bron, wat er misging en hoe oud de cache is."""
    melding = uitkomst.get("melding") or ""
    return kort(uitkomst) + (f" — {melding}" if melding else "")
