"""
tug_types.py -- Gedeelde typen voor publieke API's

Naast de type-aliassen staan hier de twee records die de ruimtelijke analyse
bijeenhouden: `Bevindingen` (wat er in de omgeving is aangetroffen) en
`VboOordeel` (wat dat voor één verblijfsobject betekent).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Een GeoJSON Feature zoals geretourneerd door WFS/PDOK/DUO en intern doorgegeven.
# Heeft minimaal {"type": "Feature", "geometry": {...}, "properties": {...}}.
Feature = dict[str, Any]
FeatureList = list[Feature]

# Een gesignaleerd object dat geen BAG-feature is (begraafplaats, manege,
# luchthaven, natuurgebied): een dict met minimaal naam en afstand_m.
Signalering = dict[str, Any]

# Log-callback die door alle bronnen-functies wordt geaccepteerd.
# Tekst-in, niets-uit; impl. is doorgaans een nested closure die in
# een lokale lijst log_regels accumuleert.
LogFn = Callable[[str], None]

# Resultaat-types voor signaleringsfuncties.
SignaalResultaat = dict[str, list[dict[str, Any]]]

# Een classificatie-context dict, zoals geretourneerd door _bouw_classificatie_context.
ContextDict = dict[str, Any]


# ──────────────────────────────────────────────
# Wat er in de omgeving is aangetroffen
# ──────────────────────────────────────────────

@dataclass
class Bevindingen:
    """Alles wat de bronstap in de omgeving van de puntlocaties heeft gevonden.

    Eén record in plaats van twintig losse variabelen: de presentatiestappen
    (adreslijsten, HTML-markers, kaartrendering) krijgen dit object mee in plaats
    van elk hun eigen selectie van dezelfde argumenten. Een bron toevoegen is
    daarmee één veld erbij, niet vier aanroepen bijwerken.

    De velden volgen de bronstappen van `tug_03_ruimtelijk.run()`.
    """

    # BAG-verblijfsobjecten binnen de toetsingsafstand
    alle_vbo: FeatureList = field(default_factory=list)
    geluidgevoelig_vbo: FeatureList = field(default_factory=list)

    # BAG-verblijfsobjecten in de margeband eromheen
    marge_vbo: FeatureList = field(default_factory=list)
    marge_vbo_overig: FeatureList = field(default_factory=list)

    # Begraafplaatsen (BRT top10nl)
    begraafplaatsen_in_straal: list[Signalering] = field(default_factory=list)
    begraafplaatsen_buiten_straal: list[Signalering] = field(default_factory=list)

    # Maneges (PDOK Location API)
    maneges_in_straal: list[Signalering] = field(default_factory=list)
    maneges_buiten_straal: list[Signalering] = field(default_factory=list)

    # Kinderopvang (LRK) en scholen (DUO)
    kdv_in_straal: FeatureList = field(default_factory=list)
    kdv_in_marge: FeatureList = field(default_factory=list)
    scholen_in_straal: FeatureList = field(default_factory=list)
    scholen_in_marge: FeatureList = field(default_factory=list)

    # Natuurgebieden
    n2000_in_straal: list[Signalering] = field(default_factory=list)
    n2000_in_signaal: list[Signalering] = field(default_factory=list)
    nnn_in_straal: list[Signalering] = field(default_factory=list)
    nnn_in_signaal: list[Signalering] = field(default_factory=list)

    # Luchthavens (WFS Overijssel)
    luchthavens_in_straal: list[Signalering] = field(default_factory=list)
    luchthavens_in_signaal: list[Signalering] = field(default_factory=list)

    def statistieken(self) -> dict[str, int]:
        """Aantallen per categorie, voor het proceslogboek en het PDF-rapport."""
        return {
            "vbo_in_straal":             len(self.alle_vbo),
            "geluidgevoelig":            len(self.geluidgevoelig_vbo),
            "marge_geluidgevoelig":      len(self.marge_vbo),
            "marge_overig":              len(self.marge_vbo_overig),
            "begraafplaatsen_in_straal": len(self.begraafplaatsen_in_straal),
            "maneges_in_straal":         len(self.maneges_in_straal),
            "kdv_in_straal":             len(self.kdv_in_straal),
            "kdv_in_marge":              len(self.kdv_in_marge),
            "scholen_in_straal":         len(self.scholen_in_straal),
            "scholen_in_marge":          len(self.scholen_in_marge),
            "n2000_in_straal":           len(self.n2000_in_straal),
            "n2000_in_signaal":          len(self.n2000_in_signaal),
            "nnn_in_straal":             len(self.nnn_in_straal),
            "nnn_in_signaal":            len(self.nnn_in_signaal),
            "luchthavens_in_straal":     len(self.luchthavens_in_straal),
            "luchthavens_in_signaal":    len(self.luchthavens_in_signaal),
        }

    def kaartobjecten(self) -> FeatureList:
        """De features die een gevelcontour kunnen krijgen (alles met een BAG-pand)."""
        return [
            *self.alle_vbo, *self.marge_vbo, *self.marge_vbo_overig,
            *self.kdv_in_straal, *self.kdv_in_marge,
            *self.scholen_in_straal, *self.scholen_in_marge,
        ]


# ──────────────────────────────────────────────
# Wat dat voor één verblijfsobject betekent
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class VboOordeel:
    """Het oordeel over één verblijfsobject: hoort het in de wettelijke lijst,
    in de margeband, of alleen in het overzicht van overige objecten?

    Dit oordeel wordt één keer geveld (`_bouw_classificatie_context`) en daarna
    alleen nog gelezen — door de adreslijsten van het PDF-rapport, door de
    markers van de HTML-kaart en door de PNG-kaartrendering. Zolang alle drie
    hetzelfde oordeel lezen, kunnen tabel en kaart elkaar niet tegenspreken.
    """

    feature: Feature
    sleutel: str
    categorie: str      # "wettelijk" | "marge" | "overig"
    gebruiksdoel: str   # samengevoegde gebruiksdoelen, of "—"
    extra: str          # toelichting in de kolom Bijzonderheden
