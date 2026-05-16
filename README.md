# TUG-ontheffingen — Geautomatiseerde inventarisatie

Automatisering van de ruimtelijke analyse voor ontheffingaanvragen **Tijdelijk en Uitzonderlijk Gebruik (TUG)** van het luchtruim, op grond van artikel 8a van de Wet luchtvaart. Ontwikkeld voor de Provincie Overijssel.

De pipeline verwerkt een aanvraag-JSON, classificeert de betrokken luchtvaartuigen, voert een ruimtelijke analyse uit op kwetsbare gebouwen en functies rondom de aanvraaglocatie, en genereert een PDF-rapport en interactieve HTML-kaart.

---

## Inhoudsopgave

- [Vereisten](#vereisten)
- [Installatie](#installatie)
- [Gebruik](#gebruik)
- [Aanvraag-JSON](#aanvraag-json)
- [Architectuur](#architectuur)
- [Gegevensbronnen](#gegevensbronnen)
- [Uitvoer](#uitvoer)
- [Openstaande punten (PM-lijst)](#pm-lijst)

---

## Vereisten

- Python 3.11 of hoger
- Internettoegang (voor PDOK WFS, BAG WFS, DUO, LRK)
- ILT Luchtvaartregister (.ods-bestand, zie [Gegevensbronnen](#gegevensbronnen))

### Python-packages

```
pandas
requests
pyproj
shapely
geopandas      # benodigd voor NNN GeoPackage (optioneel, maar aanbevolen)
fiona          # benodigd voor NNN GML-verwerking
reportlab
```

Installeren:

```bash
pip install pandas requests pyproj shapely geopandas fiona reportlab
```

---

## Installatie

1. Clone of download de repository.
2. Plaats het ILT Luchtvaartregister (.ods) in de projectmap (zie [ILT](#ilt-luchtvaartregister)).
3. Maak de map `geo/` aan — de NNN GeoPackage en DUO-caches worden automatisch aangemaakt bij de eerste run.
4. Maak de map `output/` aan voor PDF- en HTML-uitvoer.

```
TUG-ontheffingen/
├── geo/                  # Automatisch aangemaakt bij eerste run
├── output/               # PDF- en HTML-uitvoer
├── tug_run.py
├── tug_01_validatie.py
├── tug_02_classificatie.py
├── tug_03_bronnen.py
├── tug_03_kaart.py
├── tug_03_ruimtelijk.py
├── tug_05_output.py
└── test_aanvraag_tug.json
```

---

## Gebruik

```bash
python tug_run.py aanvraag.json
```

Herstart vanaf een specifieke stap (als `tug_state.json` al bestaat):

```bash
python tug_run.py tug_state.json --vanaf 05
```

Na een succesvolle run wordt `tug_state.json` automatisch gewist (dataveiligheid).

### Testrunaanvraag

```bash
python tug_run.py test_aanvraag_tug.json
```

---

## Aanvraag-JSON

Minimale vereiste velden:

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
| `datum_vlucht` | string of array | ISO 8601 (`YYYY-MM-DD`), één of meerdere data |
| `vlucht_udp` | boolean | Indien `true`: hele dag, `vlucht_start`/`vlucht_einde` worden genegeerd |
| `luchtvaartuigen` | array | Objecten met `registratie` (PH-code) en `type` |
| `coord_lat` / `coord_lon` | float | WGS84, minimaal 6 decimalen |
| `datum_ondertekening` | string | ISO 8601; dient minimaal 28 dagen vóór de vroegste vluchtdatum te liggen |

---

## Architectuur

De pipeline bestaat uit losse scripts die via `tug_state.json` communiceren.

| Script | Stap | Omschrijving |
|--------|------|--------------|
| `tug_run.py` | Orchestrator | Start en coördineert alle stappen |
| `tug_01_validatie.py` | 2 | Volledigheidscheck verplichte velden, 4-weken-regel |
| `tug_02_classificatie.py` | 3 | PH-code → ICAO → NLR-categorie → toetsingsafstand |
| `tug_03_bronnen.py` | 4 (lib) | Alle databronnen, WFS-calls, BAG, NNN, N2000, LRK, DUO, maneges |
| `tug_03_kaart.py` | 4 (lib) | Kaartrendering (tiles + cirkels + markers) |
| `tug_03_ruimtelijk.py` | 4 | Ruimtelijke analyse: coördineert alle bronnen, bouwt adresrijen |
| `tug_05_output.py` | 6 | PDF-rapport (proceslog + adressenlijst + kaarten) en HTML-kaart |

### Toetsingsafstanden

| Zone | Berekening |
|------|-----------|
| Toetsingsafstand | Norm luidste luchtvaartuig (uit NLR CR-96650L) |
| Margeband | Toetsingsafstand + 75 m |
| Aandachtsgebied maneges | Toetsingsafstand + 375 m |

---

## Gegevensbronnen

### ILT Luchtvaartregister

Wekelijks bijgewerkt .ods-bestand met alle geregistreerde Nederlandse luchtvaartuigen.

Download: https://www.ilent.nl/documenten/lijsten/luchtvaart/databestanden/luchtvaartregister-data

Plaats het bestand in de projectmap. De pipeline detecteert automatisch het meest recente `.ods`-bestand.

### BAG WFS v2.0 (PDOK)

Verblijfsobjecten (adressen, gebruiksdoelen, pandgeometrieën) via:
`https://service.pdok.nl/lv/bag/wfs/v2_0`

### Natura 2000 (PDOK WFS)

On-the-fly ophalen via:
`https://service.pdok.nl/brt/beschermde-gebieden/wfs/v1_0`
Laag: `inspire:PS.ProtectedSite`

### Natuurnetwerk Nederland (NNN)

Lokale GeoPackage-cache (180 dagen TTL), opgebouwd vanuit ATOM-feed:
`https://service.pdok.nl/provincies/natuurnetwerk-nederland/atom/downloads/inspire-pv-ps.nlps-nnn.gml`

> **Let op:** de NNN-geometrie is provinciaal (Overijssel). Locaties net buiten de provincie worden via de N2000-laag automatisch gesignaleerd (N2000 ⊂ NNN).

### Landelijk Register Kinderopvang (LRK)

CSV-export, 7 dagen TTL:
`https://www.landelijkregisterkinderopvang.nl/opendata/export_opendata_lrk.csv`

Gekoppeld aan BAG-verblijfsobjecten via BAG-identificatiecode.

### DUO Open Onderwijsdata

GeoJSON per onderwijstype (gecachet, SHA-256-gehasht):

| Type | URL |
|------|-----|
| PO | `https://onderwijsdata.duo.nl/datastore/dump/dcc9c9a5-6d01-410b-967f-810557588ba4?format=json` |
| SO | `https://onderwijsdata.duo.nl/datastore/dump/8f0f1639-712d-4adb-bb59-cabd43730dc8?format=json` |
| VO | `https://onderwijsdata.duo.nl/datastore/dump/5187f8d5-ff9c-4284-8e06-4311f0354956?format=json` |
| MBO | `https://onderwijsdata.duo.nl/datastore/dump/1a946297-a7ca-48d5-9ae8-19ad73bf8176?format=json` |
| HO | `https://onderwijsdata.duo.nl/datastore/dump/bf1da9c6-c688-4873-91b1-b12c9ac2c132?format=json` |

### Luchthavens (GeoPortaal Overijssel WFS)

`https://services.geodataoverijssel.nl/geoserver/B64_nutsvoorzieningen/wfs`
Laag: `B64_nutsvoorzieningen:B6_Luchthaven_puntlocaties`

### Maneges (PDOK Locatieserver / BRT)

Zoekradius: toetsingsafstand + 375 m. Getoetst op polygoongeometrie (BRT gebouwen).

---

## Uitvoer

Alle uitvoer wordt opgeslagen in `output/`:

| Bestand | Omschrijving |
|---------|--------------|
| `tug_rapport_{timestamp}.pdf` | PDF-rapport: proceslog, adressenlijst, situatie- en omgevingskaart |
| `tug_kaart_{timestamp}.html` | Interactieve Leaflet-kaart met alle geïnventariseerde objecten |

De adressenlijst bevat vier secties:

1. **Wettelijk relevant** — instemmingsverklaring vereist
2. **Margeband** — instemmingsverklaring optioneel
3. **Aandachtslocaties** — maneges en luchthavens
4. **Overig** — weergave op kaart, geen actie vereist

---

## PM-lijst

Openstaande beslissingen en onzekerheden:

| # | Item |
|---|------|
| PM-1 | Rx.Mission exportformaat — veldnamen parsen voor geautomatiseerde intake |
| PM-7 | Gebruiksteller generieke ontheffingen (≥ 12 vluchten/jaar = vergunningplicht) |

---

## Licentie

Intern gebruik Provincie Overijssel. Niet bestemd voor publieke distributie.
