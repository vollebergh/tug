"""
tug_aanvraag.py -- Het schema van een aanvraag-JSON, getoetst aan de rand

Een aanvraag wordt op twee niveaus gecontroleerd, met elk een eigen gevolg:

- **Structuur (hier, fataal).** Is het een object, kent de pipeline elk veld, en
  heeft elk ingevuld veld het juiste type en een waarde binnen het bereik? Een
  aanvraag die hier niet door komt, wordt niet verwerkt: de stappen erna zouden
  anders halverwege vastlopen, of — erger — met een zinloze waarde doorrekenen.
- **Inhoud (tug_01_validatie, waarschuwing).** Ontbrekende velden, datum- en
  tijdnotatie, het patroon van een registratiekenmerk, de indientermijn. Daarop
  rekent de pipeline door en maakt het gebrek zichtbaar in het rapport.

Het schema staat hier in gewone Python in plaats van in pydantic of jsonschema:
het zijn twaalf velden, de foutmeldingen moeten Nederlands en leesbaar zijn voor
de vergunningverlener, en elke extra afhankelijkheid is extra aanvalsoppervlak.
"""

from typing import Any

from tug_config import TOETSING_TOESLAG_M

SOORTEN_ONTHEFFING = ("locatiegebonden", "generiek")
MAX_TEKST          = 200      # naam, type, tijd- en datumvelden
MAX_LUCHTVAARTUIGEN = 50
MAX_PUNTLOCATIES   = 20
MAX_VLUCHTDATA     = 366
AANTAL_VLUCHTEN    = (1, 9999)
TOESLAG_BEREIK     = (0, 500)    # m rond de puntlocatie, opgeteld bij de Lden-afstand (B21)
AFSTAND_BEREIK     = (1, 2000)   # m, handmatige geluidsafstand per luchtvaartuig (B12)

# Veld → toegestane Python-typen na json.loads. Elk veld mag ook null zijn of
# ontbreken; dat is een inhoudscontrole en geen structuurfout.
_TEKST = (str,)
_VELDEN: dict[str, tuple[type, ...]] = {
    "naam":                   _TEKST,
    "soort_ontheffing":       _TEKST,
    "datum_vlucht":           (str, list),
    "vlucht_udp":             (bool,),
    "vlucht_start":           _TEKST,
    "vlucht_einde":           _TEKST,
    "aantal_vluchten":        (int,),
    "luchtvaartuigen":        (list,),
    "coord_lat":              (int, float, list),
    "coord_lon":              (int, float, list),
    "datum_ondertekening":    _TEKST,
    "tijdstip_ondertekening": _TEKST,
    "toeslag_m":              (int, float),
}
_LUCHTVAARTUIG_TEKSTVELDEN = {"registratie", "type"}
_LUCHTVAARTUIG_VELDEN = _LUCHTVAARTUIG_TEKSTVELDEN | {"afstand_m"}

_TYPENAMEN = {str: "tekst", int: "geheel getal", float: "getal", bool: "ja/nee (true/false)",
              list: "lijst", dict: "object"}


def _typenaam(typen: tuple[type, ...]) -> str:
    return " of ".join(dict.fromkeys(_TYPENAMEN[t] for t in typen))


def _is_type(waarde: Any, typen: tuple[type, ...]) -> bool:
    # bool is in Python een int; een getalveld mag geen true/false zijn, en omgekeerd.
    if isinstance(waarde, bool):
        return bool in typen
    return isinstance(waarde, typen)


def _tekst_te_lang(waarde: Any) -> bool:
    return isinstance(waarde, str) and len(waarde) > MAX_TEKST


def controleer_structuur(aanvraag: Any, label: str = "aanvraag") -> list[str]:
    """Structuurfouten van één aanvraag; een lege lijst betekent: verwerkbaar."""
    if not isinstance(aanvraag, dict):
        return [f"{label}: een aanvraag moet een JSON-object zijn, geen "
                f"{_TYPENAMEN.get(type(aanvraag), type(aanvraag).__name__)}"]

    fouten = []
    onbekend = sorted(set(aanvraag) - set(_VELDEN))
    if onbekend:
        fouten.append(f"{label}: onbekende veld(en) {', '.join(onbekend)} — de pipeline "
                      f"verwerkt die niet; controleer de spelling")

    for veld, typen in _VELDEN.items():
        waarde = aanvraag.get(veld)
        if waarde is None:
            continue            # aanwezigheid is een inhoudscontrole (tug_01_validatie)
        if not _is_type(waarde, typen):
            fouten.append(f"{label}: '{veld}' moet {_typenaam(typen)} zijn, niet "
                          f"{_TYPENAMEN.get(type(waarde), type(waarde).__name__)}")
            continue
        if _tekst_te_lang(waarde):
            fouten.append(f"{label}: '{veld}' is langer dan {MAX_TEKST} tekens")

    fouten += _controleer_lijsten(aanvraag, label)
    return fouten


def _controleer_lijsten(aanvraag: dict[str, Any], label: str) -> list[str]:
    fouten = []

    toeslag = aanvraag.get("toeslag_m")
    if isinstance(toeslag, int | float) and not isinstance(toeslag, bool):
        laag, hoog = TOESLAG_BEREIK
        if not laag <= toeslag <= hoog:
            fouten.append(f"{label}: 'toeslag_m' moet tussen {laag} en {hoog} m liggen, "
                          f"niet {toeslag}")

    aantal = aanvraag.get("aantal_vluchten")
    if isinstance(aantal, int) and not isinstance(aantal, bool):
        laag, hoog = AANTAL_VLUCHTEN
        if not laag <= aantal <= hoog:
            fouten.append(f"{label}: 'aantal_vluchten' moet tussen {laag} en {hoog} liggen, "
                          f"niet {aantal}")

    data = aanvraag.get("datum_vlucht")
    if isinstance(data, list):
        if len(data) > MAX_VLUCHTDATA:
            fouten.append(f"{label}: 'datum_vlucht' bevat meer dan {MAX_VLUCHTDATA} data")
        for i, datum in enumerate(data):
            if not isinstance(datum, str) or len(datum) > MAX_TEKST:
                fouten.append(f"{label}: 'datum_vlucht'[{i}] moet een datum als tekst zijn")

    toestellen = aanvraag.get("luchtvaartuigen")
    if isinstance(toestellen, list):
        if len(toestellen) > MAX_LUCHTVAARTUIGEN:
            fouten.append(f"{label}: meer dan {MAX_LUCHTVAARTUIGEN} luchtvaartuigen")
        for i, lv in enumerate(toestellen):
            plek = f"{label}: luchtvaartuigen[{i}]"
            if not isinstance(lv, dict):
                fouten.append(f"{plek} moet een object zijn met 'registratie' en eventueel "
                              f"'type' en 'afstand_m'")
                continue
            onbekend = sorted(set(lv) - _LUCHTVAARTUIG_VELDEN)
            if onbekend:
                fouten.append(f"{plek}: onbekende veld(en) {', '.join(onbekend)}")
            for veld in _LUCHTVAARTUIG_TEKSTVELDEN & set(lv):
                if lv[veld] is not None and not isinstance(lv[veld], str):
                    fouten.append(f"{plek}: '{veld}' moet tekst zijn")
                elif _tekst_te_lang(lv[veld]):
                    fouten.append(f"{plek}: '{veld}' is langer dan {MAX_TEKST} tekens")
            afstand = lv.get("afstand_m")
            if afstand is not None:
                laag, hoog = AFSTAND_BEREIK
                if not _is_type(afstand, (int, float)):
                    fouten.append(f"{plek}: 'afstand_m' moet een getal zijn")
                elif not laag <= afstand <= hoog:
                    fouten.append(f"{plek}: 'afstand_m' moet tussen {laag} en {hoog} m "
                                  f"liggen, niet {afstand}")

    for veld in ("coord_lat", "coord_lon"):
        waarde = aanvraag.get(veld)
        if isinstance(waarde, list):
            if not waarde:
                fouten.append(f"{label}: '{veld}' mag geen lege lijst zijn")
            if len(waarde) > MAX_PUNTLOCATIES:
                fouten.append(f"{label}: '{veld}' bevat meer dan {MAX_PUNTLOCATIES} puntlocaties")
            if any(isinstance(w, bool) or not isinstance(w, int | float) for w in waarde):
                fouten.append(f"{label}: '{veld}' moet een lijst van getallen zijn")

    soort = aanvraag.get("soort_ontheffing")
    if isinstance(soort, str) and soort not in SOORTEN_ONTHEFFING:
        fouten.append(f"{label}: 'soort_ontheffing' moet "
                      f"{' of '.join(repr(s) for s in SOORTEN_ONTHEFFING)} zijn, niet {soort!r}")
    return fouten


def toeslag_van(aanvraag: dict[str, Any]) -> float:
    """De toeslag rond de puntlocatie in meters: opgegeven in de aanvraag, anders
    TOETSING_TOESLAG_M. Het bereik is bij het inlezen al gecontroleerd (B21)."""
    waarde = aanvraag.get("toeslag_m")
    if isinstance(waarde, int | float) and not isinstance(waarde, bool):
        return float(waarde)
    return float(TOETSING_TOESLAG_M)
