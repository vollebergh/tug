# TUG-ontheffingen — Geautomatiseerde inventarisatie

Automatisering van de ruimtelijke analyse voor ontheffingaanvragen **Tijdelijk en Uitzonderlijk Gebruik (TUG)** van het luchtruim buiten luchthavens, op grond van artikel 8a.51 van de Wet luchtvaart. Ontwikkeld voor de Provincie Overijssel.

Elke provincie hanteert zijn eigen TUG-beleid en beoordelingscriteria. Dit project is daarom een maatwerkautomatisering voor de Provincie Overijssel en niet zonder aanpassingen toepasbaar op andere provincies.

De pipeline verwerkt een aanvraag-JSON, classificeert de betrokken luchtvaartuigen via het ILT Luchtvaartregister en de NLR-categorietabel, voert een ruimtelijke analyse uit op alle wettelijk relevante objecten en functies rondom de aanvraaglocatie, en genereert een PDF-rapport met proceslog en adressenlijst en een interactieve HTML-kaart. De HTML-kaart is bedoeld voor de vergunningverlener om in te zoomen op de aanvraaglocatie en de geïnventariseerde objecten in hun ruimtelijke context te beoordelen.

---

## Inhoudsopgave

- [Vereisten](#vereisten)
- [Installatie](#installatie)
- [Gebruik](#gebruik)
- [Aanvraag-JSON](#aanvraag-json)
- [Validatieregels](#validatieregels)
- [Architectuur](#architectuur)
- [Toetsingslogica](#toetsingslogica)
- [Gegevensbronnen & caching](#gegevensbronnen--caching)
- [Uitvoer](#uitvoer)
- [tug_state.json](#tug_statejson)
- [Kwaliteitsborging](#kwaliteitsborging)

---

## Vereisten

- Python 3.11 of hoger (geen adminrechten vereist voor installatie via `pip`)

### Python-packages

```
pandas
requests
pyproj
shapely
odfpy
Pillow
reportlab
geopandas      # aanbevolen — vereist voor NNN GeoPackage
fiona          # aanbevolen — vereist voor NNN GeoPackage
```

Installeren:

```bash
pip install pandas requests pyproj shapely odfpy Pillow reportlab geopandas fiona
```

> **Zonder geopandas/fiona** draait de pipeline wel, maar de NNN-analyse (Natuurnetwerk Nederland) wordt overgeslagen. N2000-signalering via PDOK WFS blijft altijd actief.

---

## Installatie

1. Clone of download de repository.
2. Geen verdere configuratie nodig — mappen `geo/` en `output/` worden automatisch aangemaakt bij de eerste run.

### Bestanden die bij eerste run worden gedownload

De pipeline downloadt bij de eerste run externe bestanden automatisch naar `geo/` en cachet deze voor hergebruik. 

| Bestand | Bron | Grootte | TTL |
|---------|------|---------|-----|
| `luchtvaartuigregister_ilt.ods` | ILT (gescrapet van bronpagina) | ± 1,3 MB | 30 dagen |
| `lrk_kinderopvang.csv` | Landelijk Register Kinderopvang | ± 12 MB | 7 dagen |
| `nnn_gebieden.gpkg` | PDOK ATOM-feed (GML → GeoPackage) | ± 111 MB | 180 dagen |
| `duo_scholen_po.geojson` | DUO Open Onderwijsdata PO (gefilterd op Overijssel) | ± 143 KB | 90 dagen |
| `duo_scholen_overig.geojson` | DUO Open Onderwijsdata SO/VO/MBO/HO (gefilterd op Overijssel) | ± 63 KB | 90 dagen |

> De NNN GeoPackage (111 MB) wordt alleen aangemaakt als `geopandas` en `fiona` beschikbaar zijn. De overige bestanden worden altijd gedownload.

```
TUG-ontheffingen/
├── geo/                        # Automatisch aangemaakt; caches voor ILT, DUO, LRK, NNN
├── output/                     # PDF- en HTML-uitvoer
├── tug_run.py                  # Orchestrator
├── tug_01_validatie.py         # Stap 2 — volledigheidscheck
├── tug_02_classificatie.py     # Stap 3 — vliegtuigclassificatie
├── tug_03_bronnen.py           # Stap 4 — re-export shim (backward compat)
├── tug_03_kaart.py             # Stap 4 — kaartrendering (library)
├── tug_03_ruimtelijk.py        # Stap 4 — ruimtelijke analyse (orchestrator)
├── tug_05_output.py            # Stap 6 — PDF/HTML-output
├── tug_config.py               # Gedeelde constanten (URLs, drempelwaarden, paden)
├── tug_types.py                # Type-aliassen (Feature, FeatureList, LogFn, …)
├── tug_logging.py              # LogAccumulator + setup_logging
├── tug_geo.py                  # Coördinaattransformaties en extract-helpers
├── tug_bronnen_bag.py          # BAG WFS, gevel-check, deduplicatie
├── tug_bronnen_brt.py          # Begraafplaatsen, maneges, luchthavens (PDOK/WFS)
├── tug_bronnen_geocode.py      # Reverse geocoding (PDOK Locatieserver)
├── tug_bronnen_natuur.py       # Natura 2000, NNN (GeoPackage-cache)
├── tug_bronnen_onderwijs.py    # LRK (KDV) + DUO-scholen (5 datasets)
└── test_aanvraag_tug.json      # Testaanvraag (negatief testgeval)
```

---

## Gebruik

### Standaard run

```bash
python tug_run.py aanvraag.json
```

### Herstart vanaf een specifieke stap

Als `tug_state.json` al bestaat (vorige run afgebroken of handmatig bewaard):

```bash
python tug_run.py tug_state.json --vanaf 05
```

Geldige stapnummers: `01`, `02`, `03`, `05` (overeenkomstig de scriptnamen).

### Testrun

```bash
python tug_run.py test_aanvraag_tug.json
```

Het testbestand bevat een negatief testgeval: `datum_ondertekening` ligt 19 dagen vóór de vroegste vluchtdatum. De pipeline signaleert dat de aanvraag korter dan 28 dagen vóór de eerste vluchtdatum is ingediend en genereert een waarschuwing (niet fataal).

### Dataveiligheid

Na een succesvolle run wordt `tug_state.json` automatisch overschreven met `{}`. Tussentijdse resultaten blijven beschikbaar zolang de run loopt.

---

## Aanvraag-JSON

### Vereiste velden

```json
{
  "soort_ontheffing": "locatiegebonden",
  "datum_vlucht": ["2026-06-20", "2026-07-01"],
  "vlucht_udp": true,
  "vlucht_start": null,
  "vlucht_einde": null,
  "aantal_vluchten": 12,
  "luchtvaartuigen": [
    {"registratie": "PH-ANK", "type": "heli"},
    {"registratie": "PH-ECE", "type": "heli"}
  ],
  "coord_lat": 52.405354,
  "coord_lon": 6.129962,
  "datum_ondertekening": "2026-06-01",
  "tijdstip_ondertekening": "16:29"
}
```

| Veld | Type | Toelichting |
|------|------|-------------|
| `soort_ontheffing` | string | `"locatiegebonden"` of `"generiek"` |
| `datum_vlucht` | string of array | YYYY-MM-DD; één datum of een array van meerdere data |
| `vlucht_udp` | boolean | Indien `true`: universal daylight period; `vlucht_start`/`vlucht_einde` worden genegeerd |
| `luchtvaartuigen` | array | Objecten met minimaal `registratie` (PH-code); `type` facultatief |
| `coord_lat` / `coord_lon` | float | WGS84, minimaal 6 decimalen |
| `datum_ondertekening` | string | YYYY-MM-DD; zie 4-weken-regel |
| `tijdstip_ondertekening` | string | `HH:MM` formaat |

---

## Validatieregels

`tug_01_validatie.py` controleert de aanvraag op de volgende punten:

| Controle | Gedrag bij fout |
|----------|-----------------|
| Aanwezigheid van alle 7 verplichte velden | **Fataal** — pipeline stopt |
| `datum_vlucht` als string → automatisch omgezet naar `[string]` | Normalisatie (geen fout) |
| Datum-formaat YYYY-MM-DD voor alle vluchtdata | **Fataal** |
| Datum-formaat YYYY-MM-DD voor `datum_ondertekening` | **Fataal** |
| Structuur `luchtvaartuigen`: array van objecten met `registratie` | **Fataal** |
| **4-weken-regel**: ondertekening ≥ 28 dagen vóór vroegste vluchtdatum | **Waarschuwing** (niet fataal) |
| `datum_ondertekening` ná vluchtdatum | **Fataal** |

---

## Architectuur

### Modulaire opbouw

```
aanvraag.json
    ↓  tug_run.py (orchestrator, v4.1.0)
    ↓
    ├── tug_01_validatie.py   (v1.0.0)   Stap 2 — volledigheidscheck + 4-weken-regel
    ↓
    ├── tug_02_classificatie.py (v1.0.0)  Stap 3 — PH-code → ICAO → NLR → toetsingsafstand
    ↓
    ├── tug_03_ruimtelijk.py  (v4.5.0)   Stap 4 — ruimtelijke analyse (orchestrator)
    │       ├── tug_03_bronnen.py         re-export shim (backward compat)
    │       ├── tug_bronnen_bag.py        BAG WFS, gevel-check, deduplicatie
    │       ├── tug_bronnen_brt.py        begraafplaatsen, maneges, luchthavens
    │       ├── tug_bronnen_natuur.py     N2000, NNN GeoPackage
    │       ├── tug_bronnen_onderwijs.py  LRK (KDV) + DUO-scholen
    │       ├── tug_bronnen_geocode.py    reverse geocoding
    │       ├── tug_geo.py               coördinaatfuncties, extract-helpers
    │       └── tug_03_kaart.py          PIL-kaartrendering (tiles + lagen)
    ↓
    └── tug_05_output.py      (v4.5.0)   Stap 6 — PDF-rapport + HTML-kaart

tug_config.py   gedeelde constanten (URLs, drempelwaarden, bestandspaden)
tug_types.py    type-aliassen (Feature, FeatureList, LogFn, …)
tug_logging.py  LogAccumulator + setup_logging
tug_state.json  communicatie tussen stappen; gewist na succesvolle run
```

### Modulerollen

| Module | Rol | Afhankelijkheden |
|--------|-----|-----------------|
| `tug_run.py` | Orchestrator; start stappen via `subprocess` | — |
| `tug_01_validatie.py` | Volledigheidscheck; normaliseert `datum_vlucht` | — |
| `tug_02_classificatie.py` | ILT-register ophalen/cachen; NLR-tabel opzoeken | `pandas`, `odfpy` |
| `tug_03_bronnen.py` | Re-export shim (alle `from tug_03_bronnen import ...` blijven werken) | alle tug_bronnen_* |
| `tug_bronnen_bag.py` | BAG WFS, pandgeometrie, gevel-check, deduplicatie | `pyproj`, `shapely` |
| `tug_bronnen_brt.py` | Begraafplaatsen, maneges, luchthavens (PDOK Location API / WFS) | `shapely` |
| `tug_bronnen_geocode.py` | Reverse geocoding (PDOK Locatieserver) | — |
| `tug_bronnen_natuur.py` | Natura 2000 (PDOK WFS), NNN (GeoPackage-cache, ATOM-feed) | `geopandas`, `fiona` |
| `tug_bronnen_onderwijs.py` | LRK (KDV-koppeling), DUO-scholen (5 datasets) | `pandas` |
| `tug_geo.py` | Coördinaattransformaties, bbox-berekeningen, extract-helpers | `pyproj`, `shapely` |
| `tug_config.py` | Alle gedeelde constanten (één bron van waarheid) | — |
| `tug_logging.py` | `LogAccumulator`-klasse; routing naar `logging`-module | — |
| `tug_types.py` | Type-aliassen voor annotaties | — |
| `tug_03_kaart.py` | Kaarttegels ophalen; cirkels en markers tekenen | `Pillow` |
| `tug_03_ruimtelijk.py` | Coördineert alle bronnen; bouwt adresrijen en kaartlagen | alle hierboven |
| `tug_05_output.py` | ReportLab PDF; Leaflet HTML-kaart | `reportlab` |

### Classificatie van luchtvaartuigen

De NLR-tabel (CR-96650L, Suppl. 1, oktober 2022) bevat per ICAO-type een appendix en een afstandsnorm. De afstandsnormen volgen uit art. 6 lid 3 van de [Beleidsregel TUG Overijssel](https://lokaleregelgeving.overheid.nl/CVDR329333/). Bij meerdere luchtvaartuigen geldt de hoogste norm (`norm_toepassing`).

| Appendix | Norm | Voorbeeldtypen |
|----------|------|----------------|
| 010 | 250 m | EC120, R66, B407, AS50 |
| 011 | 150 m | R22, R44, H269 |
| 012 | 350 m | A139, S76, B412 |
| 013/015/016/017 | handmatig | handmatig vaststellen |
| 014 | 500 m | H60, Chinook, Puma, AS332 |

Bij appendix 013/015/016/017 zijn de normen niet vastgesteld in de NLR-tabel. De pipeline signaleert deze categorie in het proceslog en past een standaard van 500 m toe. De vergunningverlener bepaalt handmatig de juiste norm.

---

## Toetsingslogica

### Zones

| Zone | Berekening | Gebruik |
|------|-----------|---------|
| Toetsingsafstand | Norm luidste luchtvaartuig (NLR-tabel) | Geluidgevoelige gebouwen, begraafplaatsen, kinderopvangverblijf, scholen |
| Margeband | Toetsingsafstand + 75 m | Optionele signalering overige gebouwen |
| Aandachtsgebied maneges | Toetsingsafstand + 375 m | Manegesignalering |

### Categorieën adressenlijst

| Sectie | Inhoud | Actie |
|--------|--------|-------|
| **Wettelijk relevant** | Geluidgevoelige gebouwen binnen toetsingsafstand + begraafplaatsen (polygoon snijdt toetsingsafstand) + kinderopvangverblijf + scholen | Instemmingsverklaring vereist |
| **Margeband** | Overige verblijfsobjecten in de margeband | Instemmingsverklaring optioneel |
| **Aandachtslocaties** | Maneges (aandachtsgebied) + luchthavens | Signalering; geen instemmingsvereiste |
| **Overig** | Overige objecten buiten margeband | Weergave op kaart; geen actie |

### Geluidgevoelige functies (BAG)

Woonbestemming, onderwijsfunctie, gezondheidszorgfunctie, logiesfunctie (proxy voor gezondheidszorgfunctie met bed).

### Margeband

De margeband (toetsingsafstand + 75 m) heeft een tweeledig doel. Ten eerste kunnen grote gebouwen een BAG-adres hebben dat buiten de toetsingsafstand valt, terwijl de gevel van dat gebouw er nog wel binnen snijdt; de gevelcheck via pandgeometrie vangt dit op. Ten tweede kan het voorkomen dat een piloot niet exact opstijgt of landt op de aangevraagde locatie. Gebouwen die binnen de margeband maar buiten de toetsingsafstand vallen worden daarom apart gesignaleerd, zodat de vergunningverlener kan beoordelen of ook hiervoor instemming wenselijk is.

### Begraafplaatsen

Begraafplaatsen worden via twee parallelle bronnen opgespoord:

1. **PDOK Locatieserver** — zoekt op "begraafplaats" en "erebegraafplaats" binnen straal + 1.500 m. Retourneert benoemde objecten met polygoongeometrie.
2. **BRT top10nl OGC API** — bevraagt de `terrein_vlak`-collectie op `typelandgebruik = 'dodenakker'` binnen een bbox van straal + 500 m. Vangt begraafplaatsen op die niet als benoemd object in de Locatieserver zijn geïndexeerd. Aangrenzende deelvlakken (buffer 5 m) worden samengevoegd tot clusters.

Wettelijk relevant als de polygoon de **toetsingsafstand** snijdt (niet de margeband). Centroid-afstand kan groter zijn dan de toetsingsafstand.

### Natura 2000 en NNN

N2000 wordt on-the-fly via PDOK WFS opgevraagd (nationaal). NNN via een lokale GeoPackage (Overijssel). Omdat N2000 een subset is van NNN, wordt bij een N2000-treffer automatisch ook een NNN-treffer aangenomen — ook als de locatie net buiten de Overijsselse provinciegrenzen valt en niet in de GeoPackage staat.

### Maneges

Er zijn geen landelijk dekkende polygoongeometrieën van manegegebieden beschikbaar. De grootste manege van Nederland (Exloo) heeft oefenterreinen op circa 340 meter van het BAG-adres. In theorie kan een manege daardoor binnen de toetsingsafstand vallen terwijl het BAG-adres tot 340 meter buiten die afstand ligt. Om dit op te vangen wordt een aandachtsgebied van toetsingsafstand + 375 m gehanteerd: maneges waarvan het BAG-adres binnen dit aandachtsgebied valt worden gesignaleerd. De vergunningverlener beoordeelt vervolgens of de feitelijke terreinsituatie aanleiding geeft tot verdere actie.

### Luchthavens

| Grens | Afstand |
|-------|---------|
| Wettelijke minimumafstand | 1000 m |
| Signaleringmarge | 2000 m |

---

## Gegevensbronnen & caching

Alle externe bestanden worden gecachet in `geo/`. De pipeline controleert de TTL bij elke run en herdownloadt automatisch als de cache verlopen is.

| Bron | Gebruik | TTL | Cachebestand |
|------|---------|-----|--------------|
| ILT Luchtvaartregister | PH-code → ICAO | 30 dagen | `geo/luchtvaartuigregister_ilt.ods` |
| DUO Open Onderwijsdata | Scholen PO/SO/VO/MBO/HO | 90 dagen | `geo/duo_scholen_po.geojson`, `geo/duo_scholen_overig.geojson` |
| LRK (Landelijk Register Kinderopvang) | KDV-locaties via BAG-koppeling | 7 dagen | In-memory (geen lokale cache) |
| NNN GeoPackage | Natuurnetwerk Nederland (Overijssel) | 180 dagen | `geo/nnn_gebieden.gpkg` |
| BAG WFS v2.0 | Verblijfsobjecten, gebruiksdoelen, pandgeometrieën | On-the-fly | — |
| N2000 WFS | Natura 2000-gebieden | On-the-fly | — |
| PDOK Location API | Begraafplaatsen (benoemde objecten), maneges | On-the-fly | — |
| BRT top10nl OGC API | Begraafplaatsen (dodenakker-vlakken, clustering) | On-the-fly | — |
| Luchthavens (GeoPortaal Overijssel) | Luchthaven puntlocaties | On-the-fly | — |

### ILT Luchtvaartregister

Wekelijks bijgewerkt .ods-bestand. De pipeline scrapet de ILT-pagina op de huidige download-URL en downloadt bij eerste run of verlopen TTL.

Bronpagina: https://www.ilent.nl/documenten/lijsten/luchtvaart/databestanden/luchtvaartregister-data

### BAG WFS v2.0 (PDOK)

Verblijfsobjecten via WFS-query met `propertyName`-filter op 9 velden:

| Veld | Gebruik |
|------|---------|
| `identificatie` | Koppeling met KDV (LRK `bag_id`) en DUO (`vbo_id`) |
| `gebruiksdoel` | Geluidgevoelige functiebepaling |
| `openbare_ruimte`, `huisnummer`, `huisletter`, `toevoeging`, `postcode`, `woonplaats` | Adresopbouw |
| `pandidentificatie` | Pandgeometrie voor gevelafstandscheck |

Endpoint: `https://service.pdok.nl/lv/bag/wfs/v2_0`

### Natura 2000 (PDOK WFS)

On-the-fly, nationaal dekkend:
`https://service.pdok.nl/rvo/natura2000/wfs/v1_0` — laag `natura2000:natura2000`

Signalering: treffer binnen toetsingsafstand én binnen signaleerzone (+500 m) wordt onderscheiden.

### Natuurnetwerk Nederland (NNN)

Lokale GeoPackage-cache, opgebouwd vanuit ATOM-feed. De GeoPackage bevat alleen geometrieën van provincie Overijssel. Locaties net buiten de grens worden via N2000-auto-detect ondervangen.

ATOM-feed: `https://service.pdok.nl/provincies/natuurnetwerk-nederland/atom/downloads/inspire-pv-ps.nlps-nnn.gml`

### Landelijk Register Kinderopvang (LRK)

CSV-download gekoppeld aan BAG via `bag_id`. KDV-locaties met verblijfsfunctie (kinderen met bed) worden wettelijk relevant geacht.

`https://www.landelijkregisterkinderopvang.nl/opendata/export_opendata_lrk.csv`

### DUO Open Onderwijsdata

GeoJSON per onderwijstype. De pipeline filtert op provincie Overijssel en koppelt via `vbo_id` aan BAG.

| Type | URL |
|------|-----|
| PO | `https://onderwijsdata.duo.nl/datastore/dump/dcc9c9a5-6d01-410b-967f-810557588ba4?format=json` |
| SO | `https://onderwijsdata.duo.nl/datastore/dump/8f0f1639-712d-4adb-bb59-cabd43730dc8?format=json` |
| VO | `https://onderwijsdata.duo.nl/datastore/dump/5187f8d5-ff9c-4284-8e06-4311f0354956?format=json` |
| MBO | `https://onderwijsdata.duo.nl/datastore/dump/1a946297-a7ca-48d5-9ae8-19ad73bf8176?format=json` |
| HO | `https://onderwijsdata.duo.nl/datastore/dump/bf1da9c6-c688-4873-91b1-b12c9ac2c132?format=json` |

### Maneges (PDOK Location API / BRT)

De pipeline zoekt maneges via de PDOK Location API met een reeks zoektermen (manege, rijschool, hippisch, paardencentrum, rijvereniging e.a.) binnen een zoekradius van toetsingsafstand + 375 m. De API retourneert gecombineerde resultaten uit de BRT en het BAG. Voor elk resultaat wordt via de BRT gebouwenlaag de polygoongeometrie opgevraagd om de werkelijke ligging te bepalen. Resultaten zonder polygoon (puntlocaties) worden op centroid-afstand beoordeeld.

### BRT top10nl OGC API (PDOK)

Begraafplaatsen die niet als benoemd object in de PDOK Locatieserver staan worden opgespoord via de BRT top10nl `terrein_vlak`-collectie. De API wordt bevraagd op `typelandgebruik = 'dodenakker'` binnen een bounding box van straal + 500 m. De collectie gebruikt cursor-gebaseerde paginering (geen `offset`-parameter).

Endpoint: `https://api.pdok.nl/brt/top10nl/ogc/v1_0/collections/terrein_vlak/items`

Één begraafplaats kan in de BRT uit meerdere afzonderlijke vlakken bestaan. Vlakken die elkaar binnen 5 m overlappen of raken worden samengevoegd tot één cluster via een union-find-algoritme. Het samengestelde polygoon wordt vervolgens getoetst aan de toetsingsafstand.

### Luchthavens (GeoPortaal Overijssel WFS)

`https://services.geodataoverijssel.nl/geoserver/B64_nutsvoorzieningen/wfs`  
Laag: `B64_nutsvoorzieningen:B6_Luchthaven_puntlocaties`

---

## Uitvoer

Alle bestanden worden opgeslagen in `output/`:

| Bestand | Omschrijving |
|---------|--------------|
| `tug_rapport_{timestamp}.pdf` | PDF-rapport: proceslog (12 paragrafen) + adressenlijst + situatie- en omgevingskaart |
| `tug_kaart_{timestamp}.html` | Interactieve Leaflet-kaart met alle geïnventariseerde objecten en zones |

### PDF-proceslog

Het proceslog documenteert per analysestap de uitgevoerde controles, gebruikte bronnen, gevonden objecten en toegepaste regels. Paragrafen worden rood gemarkeerd bij bevindingen die actie vereisen (NNN, N2000, strakke termijn, PM-categorie).

### Adressenlijst

Vier secties per aanvraag:

1. **Wettelijk relevant** — instemmingsverklaring vereist (geluidgevoelig + begraafplaats + KDV + school)
2. **Margeband** — instemmingsverklaring optioneel
3. **Aandachtslocaties** — maneges en luchthavens (geen instemmingsvereiste)
4. **Overig** — weergave op kaart, geen actie vereist

---

## tug_state.json

De state wordt aangemaakt door `tug_run.py` en uitgebreid door elke stap. Na succesvolle voltooiing wordt het bestand leeggemaakt (`{}`).

| Sectie | Gevuld door | Inhoud |
|--------|-------------|--------|
| `aanvraag` | `tug_run.py` | Originele aanvraag-JSON (met genormaliseerde `datum_vlucht`) |
| `validatie` | `tug_01_validatie.py` | Fouten, waarschuwingen, 4_weken_ok, log_regels |
| `classificatie` | `tug_02_classificatie.py` | Per luchtvaartuig: ICAO, appendix, norm; `norm_toepassing` |
| `ruimtelijk` | `tug_03_ruimtelijk.py` | Adresrijen (wettelijk/marge/aandacht/overig), PNG-kaartpaden |
| `logboek` | Alle scripts | Tijdgestempelde log-entries per stap |

---

## Kwaliteitsborging

### Code review

De codebase is doorgelicht aan de hand van een gelaagd review-protocol op basis van de volgende frameworks:

- **SOLID-principes** (Single Responsibility, Open/Closed, …)
- **Code Smells** — Martin Fowler, *Refactoring* (2e druk)
- **Clean Code** — Robert C. Martin
- **Python Coding Conventions** — PEP 8 en moderne Python 3.11+

Bevindingen (11 in totaal) zijn dezelfde sessie verholpen. De belangrijkste ingreep was het opsplitsen van een 1398-regel god-module (`tug_03_bronnen.py`) in zes gespecialiseerde modules. Zie ook `Codebase Review — TUG-ontheffingen` in de projectdocumentatie.

### Beveiligingsreview

De pipeline is gecontroleerd op veelvoorkomende kwetsbaarheden aan de hand van de [OWASP Cheat Sheet Series](https://cheatsheetseries.owasp.org/) (CC BY-SA 4.0). De review is uitgevoerd met de Claude Code [security-review skill](https://github.com/dougwithseismic/claude-code-skills).

Dreigingsmodel: lokale CLI-tool zonder netwerkinterface; alle externe aanroepen gaan naar Nederlandse overheids-API's (PDOK, ILT, RvIG, DUO). Geen direct exploiteerbare kwetsbaarheden aangetroffen. Verholpen:

| Bevinding | Maatregel |
|-----------|-----------|
| JSON-in-script-tag (`</script>`-injectie, VERIFY-001) | `<\/`-escaping toegepast op alle `json.dumps()`-aanroepen in HTML-output |
| Gescrapete ILT-URL zonder domeincheck (VERIFY-002) | Domeinvalidatie: alleen `*.ilent.nl` geaccepteerd |
| PDOK-href zonder URL-validatie (OBS-001) | Allowlist: alleen `api.pdok.nl` en `geodata.nationaalgeoregister.nl` |
| ODS ZIP zonder bom-beveiliging (OBS-002) | Limiet 50 MB uitgecomprimeerd in `_ods_data_hash()` |
| `print()` buiten logging-systeem (OBS-004) | Vervangen door `logging.getLogger(...).warning()` |

---

## Licentie

Intern gebruik Provincie Overijssel. Niet bestemd voor publieke distributie.
