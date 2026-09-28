# TUG-ontheffingen — geautomatiseerde ruimtelijke toetsing

Automatisering van de ruimtelijke analyse voor ontheffingaanvragen **Tijdelijk en Uitzonderlijk
Gebruik (TUG)** van terreinen buiten luchthavens, op grond van artikel 8a.51 van de Wet luchtvaart.
Ontwikkeld voor en in opdracht van de Provincie Overijssel.

De pipeline leest een aanvraag, classificeert de opgegeven luchtvaartuigen via het ILT
Luchtvaartuigregister en de NLR-indelingslijst, bepaalt daaruit de toetsingsafstand, inventariseert
alle wettelijk beschermde objecten en functies rond de opstijg- en landingslocatie, en levert een
PDF-rapport met proceslogboek, adressenlijst en kaarten plus een interactieve HTML-kaart.

Elke provincie hanteert eigen TUG-beleid en eigen beoordelingscriteria. Dit is daarom een
maatwerkautomatisering voor Overijssel en niet zonder aanpassing bruikbaar voor andere provincies.

> **Over dit document.** Het dient twee doelen. **Deel A** beschrijft wat het systeem doet, welke
> gegevens het verwerkt, waar het faalt en hoe de mens in het proces staat — geschreven om zonder
> technische kennis te lezen en bruikbaar als procesbeschrijving voor een algoritmetoets en een
> DPIA. **Deel B** is de technische documentatie voor wie de pipeline draait of onderhoudt.
> **Deel C** gaat over kwaliteitsborging en beheer.

---

## Inhoudsopgave

**Deel A — Wat dit systeem is en hoe het wordt gebruikt**

1. [Doel en juridisch kader](#1-doel-en-juridisch-kader)
2. [Positie in het vergunningproces](#2-positie-in-het-vergunningproces)
3. [Reikwijdte — wat het systeem wel en niet doet](#3-reikwijdte--wat-het-systeem-wel-en-niet-doet)
4. [Werking in hoofdlijnen](#4-werking-in-hoofdlijnen)
5. [Gegevensverwerking](#5-gegevensverwerking)
6. [Menselijke tussenkomst en controlemomenten](#6-menselijke-tussenkomst-en-controlemomenten)
7. [Risico's, beperkingen en wat daartegenover staat](#7-risicos-beperkingen-en-wat-daartegenover-staat)
8. [Transparantie en verantwoording](#8-transparantie-en-verantwoording)

**Deel B — Technische documentatie**

9. [Installatie en omgeving](#9-installatie-en-omgeving)
10. [Gebruik](#10-gebruik)
11. [Grafische schil](#11-grafische-schil)
12. [Aanvraag-JSON](#12-aanvraag-json)
13. [Validatieregels](#13-validatieregels)
14. [Classificatie van luchtvaartuigen](#14-classificatie-van-luchtvaartuigen)
15. [Toetsingslogica](#15-toetsingslogica)
16. [Gegevensbronnen en caching](#16-gegevensbronnen-en-caching)
17. [Architectuur](#17-architectuur)
18. [Uitvoer](#18-uitvoer)

**Deel C — Beheer**

19. [Kwaliteitsborging](#19-kwaliteitsborging)
20. [Onderhoud en houdbaarheid](#20-onderhoud-en-houdbaarheid)
21. [Licentie en eigenaarschap](#21-licentie-en-eigenaarschap)

---
---

# Deel A — Wat dit systeem is en hoe het wordt gebruikt

## 1. Doel en juridisch kader

Wie met een luchtvaartuig wil opstijgen of landen buiten een luchthaven heeft daarvoor een
ontheffing nodig van gedeputeerde staten (art. 8a.51 Wet luchtvaart). De provincie beoordeelt zo'n
aanvraag onder meer op de geluidbelasting voor de omgeving: rond de opstijg- en landingsplaats geldt
een afstandsnorm, en van geluidgevoelige gebouwen binnen die afstand moet de aanvrager een
instemmingsverklaring overleggen.

Het bepalen van welke objecten binnen die afstand liggen is handwerk dat zich slecht verhoudt tot
zorgvuldigheid: het vergt het combineren van vijf à tien landelijke registers per aanvraag, en een
gemist adres betekent een omwonende die niet om instemming is gevraagd. Dat deel — en alleen dat
deel — automatiseert deze pipeline.

**Toegepast normenkader**

| Bron | Wat eruit volgt |
|---|---|
| Art. 8a.51 Wet luchtvaart | Ontheffingplicht voor tijdelijk en uitzonderlijk gebruik |
| [Beleidsregel TUG Overijssel](https://lokaleregelgeving.overheid.nl/CVDR329333/), art. 6 lid 3 | Afstandsnormen per luchtvaartuigcategorie; bescherming van begraafplaatsen en maneges; minimumafstand tot luchthavens |
| Art. 3.21 Besluit kwaliteit leefomgeving (Bkl) | Welke gebouwen geluidgevoelig zijn: woon-, onderwijs- en gezondheidszorgfunctie, en bijeenkomstfunctie voor kinderopvang met bedgebied |
| NLR-indelingslijst CR-96650L Suppl. 1 (okt. 2022) | ICAO-typeaanduiding → appendixcategorie → afstandsnorm |

De logiesfunctie telt bewust **niet** als geluidgevoelig gebouw in de zin van art. 3.21 Bkl.

## 2. Positie in het vergunningproces

Het vergunningproces kent zes stappen. De pipeline automatiseert stap 2 tot en met 4 en produceert
het materiaal voor stap 6; stap 1 en 5 zijn mensenwerk.

| Processtap | Automatiseringsgraad | Uitvoerder |
|---|---|---|
| 1. Aanvraag binnenkomst | Handmatig | Aanvrager dient in via het webformulier; de vergunningverlener neemt de gegevens over |
| 2. Volledigheidscheck | Volledig | `tug_01_validatie.py` |
| 3. Classificatie luchtvaartuigen | Volledig | `tug_02_classificatie.py` |
| 4. Ruimtelijke analyse | Grotendeels | `tug_03_ruimtelijk.py` |
| 5. Instemmingscheck | Handmatig, buiten de pipeline | Aanvrager verzamelt, vergunningverlener beoordeelt |
| 6. Concept-beschikking | Materiaal geautomatiseerd | `tug_05_output.py` levert rapport en kaarten; de beschikking zelf, de review en de ondertekening blijven bij de vergunningverlener |

> De scriptnummers volgen de processtappen, niet hun eigen volgorde: `tug_01_validatie.py` is
> processtap 2, `tug_02_classificatie.py` is stap 3, `tug_03_ruimtelijk.py` is stap 4 en
> `tug_05_output.py` hoort bij stap 6.

**Het systeem neemt geen besluit.** Het stelt vast welke objecten binnen welke afstand liggen en
legt dat vast; of dat tot een ontheffing, aanvullende voorwaarden of een weigering leidt, beoordeelt
de vergunningverlener. Er is geen sprake van geautomatiseerde besluitvorming in de zin van art. 22
AVG: er rolt geen beslissing uit, en het resultaat wordt in alle gevallen door een mens gelezen,
beoordeeld en ondertekend voordat er iets richting aanvrager of omgeving gaat.

## 3. Reikwijdte — wat het systeem wel en niet doet

**Wel**

- Locatiegebonden aanvragen met een of meer opgegeven puntlocaties.
- Controle op volledigheid van de aanvraag en op de indieningstermijn.
- Classificatie van de opgegeven luchtvaartuigen en bepaling van de maatgevende afstandsnorm.
- Inventarisatie van geluidgevoelige gebouwen, kinderopvanglocaties, scholen, begraafplaatsen,
  maneges en luchthavens rond de locatie.
- Signalering van Natura 2000- en Natuurnetwerk Nederland-gebieden.
- Een rapport met proceslogboek, adressenlijst en kaarten, plus een interactieve kaart.

**Niet**

- **Het besluit.** Zie hierboven.
- **Instemmingsverklaringen.** Het verzamelen ervan is aan de aanvrager, het beoordelen aan de
  vergunningverlener. De pipeline levert alleen de lijst van objecten waarvoor instemming nodig is.
- **Heteluchtballonnen.** Die zijn via een verklaring van geen bedenkingen van de burgemeester
  vrijgesteld van de TUG-ontheffing en vallen buiten het proces.
- **Drones.** De voorschriften daarvoor zijn nog niet vastgesteld.
- **Generieke (niet-locatiegebonden) ontheffingen.** Zonder locatie is er geen ruimtelijke toetsing
  mogelijk.
- **De veiligheidsbeoordeling van de locatie.** Dat is een rijksbevoegdheid (ILT), geen
  provinciale.
- **Koppeling met het zaaksysteem.** De aanvraaggegevens worden handmatig overgenomen of via de
  grafische schil ingevoerd; het zaaksysteem kan het formulier niet los exporteren.
- **Niet-geregistreerde maneges.** Maneges die feitelijk in gebruik zijn maar niet als zodanig zijn
  vastgelegd, hebben geen bescherming op grond van de Beleidsregel en worden niet opgespoord.

## 4. Werking in hoofdlijnen

De pipeline is een **deterministische regelketen**. Er zit geen machine learning in, geen
statistisch model, geen scoring en geen training op gegevens. Elke uitkomst volgt uit een expliciet
opgeschreven regel of uit een meting op geometrie, en is met de hand na te rekenen. Dezelfde
aanvraag levert bij dezelfde brondata en dezelfde codeversie dezelfde uitkomst.

Het systeem beoordeelt **locaties en gebouwfuncties, geen personen**. Er wordt niet geprofileerd,
geen gedrag voorspeld en geen persoon gerangschikt of gescoord.

De keten:

```
aanvraag (soort, data, luchtvaartuigen, puntlocatie(s), ondertekening)
    │
    ├─ 1. volledigheid en termijn          → waarschuwingen in het proceslog
    │
    ├─ 2. per luchtvaartuig:
    │      PH-registratie → ILT-register → ICAO-type → NLR-tabel → afstandsnorm
    │      de grootste norm van alle luchtvaartuigen is maatgevend
    │
    ├─ 3. toetsingsafstand = maatgevende norm + toeslag (standaard 10 m)
    │      cirkel(s) rond de puntlocatie(s) in RD New (EPSG:28992)
    │
    ├─ 4. wat ligt daarbinnen?
    │      BAG-verblijfsobjecten (gevel, niet adrespunt) · kinderopvang via LRK ·
    │      scholen via DUO · begraafplaatsen en maneges via BRT · luchthavens ·
    │      Natura 2000 en NNN als signalering
    │
    └─ 5. categoriseren en vastleggen
           wettelijk relevant · margeband · aandachtslocatie · overig
           → PDF-rapport + HTML-kaart
```

De afstandsnorm komt dus **niet** uit een schatting maar uit twee openbare tabellen achter elkaar.
Waar een van die twee geen antwoord geeft, neemt het systeem geen norm aan — zie
[hoofdstuk 14](#14-classificatie-van-luchtvaartuigen).

## 5. Gegevensverwerking

Feitelijke beschrijving van welke gegevens waar vandaan komen, waar ze terechtkomen en hoe lang ze
blijven staan.

### 5.1 Wat er wordt verwerkt

| Gegeven | Herkomst | Komt terecht in | Blijft staan |
|---|---|---|---|
| Soort ontheffing, vluchtdata, tijdvenster, aantal vluchten | Aanvraag | Proceslogboek in het PDF | In het PDF zolang dat bewaard wordt |
| Registratiekenmerken luchtvaartuigen (PH-…) | Aanvraag | Proceslogboek, PDF | Idem |
| Puntlocatie(s) (WGS84 en RD) | Aanvraag | Proceslogboek, PDF, kaarten; bbox-parameter in de bevragingen van externe bronnen | Idem |
| Datum en tijdstip ondertekening | Aanvraag | Proceslogboek, termijncontrole | Idem |
| Dossieromschrijving (`naam`) | Vrij invoerveld — bedoeld voor een zaaknummer of plaats en datum, niet voor een naam van aanvrager of omwonende | Bestandsnamen van rapport en kaart, proceslogboek | Idem |
| Adresgegevens in de omgeving: straat, huisnummer, postcode, woonplaats, gebruiksdoel, verblijfsobject- en pandidentificatie | BAG WFS (Kadaster) | Adressenlijst in het PDF, HTML-kaart | Idem |
| Pandgeometrie (gevelcontour) | BAG WFS | Kaarten in PDF en HTML | Idem |
| Kinderopvanglocaties (BAG-koppeling, type opvang) | LRK (open data) | Adressenlijst; volledige landelijke bronbestand in `geo/` | Cache 7 dagen |
| Schoolvestigingen (naam, adres, verblijfsobject-id) | DUO Open Onderwijsdata | Adressenlijst; voorbewerkt bestand voor Overijssel in `geo/` | Cache 90 dagen |
| Luchtvaartuigregister (registratie, type, ICAO-code) | ILT | `geo/luchtvaartuigregister_ilt.ods` | Tot de ILT een nieuwe weekversie publiceert |
| Begraafplaatsen, maneges, luchthavens (naam, geometrie, dichtstbijzijnd adres) | PDOK Location API / BRT Top10NL / GeoPortaal Overijssel | Adressenlijst, kaarten | Niet gecached |
| Natura 2000-gebieden (naam, geometrie) | PDOK WFS | Proceslogboek, kaarten | Niet gecached |
| NNN-gebieden (alleen geometrie) | PDOK ATOM-feed | Proceslogboek, kaarten | Cache 180 dagen |

### 5.2 Wat er niet wordt verwerkt

- **Geen NAW- of contactgegevens van de aanvrager.** De aanvraagstructuur kent die velden niet; naam,
  adres, KvK-nummer, telefoonnummer en e-mailadres van de aanvrager blijven in het zaaksysteem.
- **Geen namen van bewoners of eigenaren.** De BAG levert adressen, gebruiksdoelen en geometrie —
  geen personen. Het systeem raadpleegt geen eigendoms- of kadastrale registers.
- **Geen instemmingsverklaringen.** Die worden buiten de pipeline om verzameld en beoordeeld.
- **Geen houdergegevens uit het luchtvaartuigregister.** Het register wordt uitsluitend gelezen op
  de kolommen `Registration` en `ICAO-code`. Het openbare bestand bevat geen houdersnaam.

De adressenlijst bevat dus wel adresgegevens van derden — omwonenden. Dat is geen bijvangst maar het
product zelf: de aanvrager moet op precies die adressen om instemming vragen.

### 5.3 Waar gegevens naartoe gaan

Alle bevraagde bronnen zijn open data van Nederlandse overheidsorganisaties: PDOK/Kadaster, RVO,
ILT, DUO en het Landelijk Register Kinderopvang, plus het GeoPortaal van de provincie Overijssel.
Er zijn geen accounts, API-sleutels of commerciële diensten in gebruik. De pipeline zelf legt alleen
verbinding met deze bronnen: elke aanroep gaat over https naar een vaste lijst van hosts, en een
verwijzing in een bronantwoord naar een andere host wordt niet gevolgd.

Eén uitzondering buiten de pipeline om: de HTML-kaart haalt bij het openen in de browser de
kaartbibliotheek Leaflet op bij `unpkg.com`, vastgepind op versie en inhoud (Subresource Integrity).
Die dienst ziet daarbij het IP-adres van wie de kaart opent, geen aanvraaggegevens. De kaart in de
grafische schil gebruikt een meegeleverde kopie en maakt die verbinding niet.

Wat die bronnen te zien krijgen is per bevraging een **bounding box of coördinaat rond de
aanvraaglocatie**, en bij begraafplaatsen en maneges een zoekterm. Gegevens over de aanvrager of de
aanvraag zelf verlaten de machine niet. De adreszoeker op de kaart van de grafische schil stuurt de
ingetypte zoektekst naar de PDOK Locatieserver (`api.pdok.nl`); wat daar wordt ingetypt, verlaat de
machine dus wél. De tekst wordt nergens bewaard. De knop *Bug rapporteren* verstuurt zelf niets: hij opent een
concept in het eigen e-mailprogramma, en wat daarin wordt meegestuurd bepaalt de gebruiker.

### 5.4 Opslag en verwijdering

| Wat | Waar | Wat ermee gebeurt |
|---|---|---|
| Tussentijdse procesdata | `tug_state.json` | Na elke aanvraag overschreven met `{}` — na succes, na een afgebroken stap en ook als de run wordt onderbroken (Ctrl+C, beëindigd). Alleen leesbaar voor de eigen gebruiker |
| Invoerbestand van de grafische schil | `tmp/` | Geleegd na afloop van elke run, bij het sluiten van de schil (een lopende run wordt dan eerst gestopt) en bij het opstarten. Alleen leesbaar voor de eigen gebruiker |
| Rapport en kaart | `output/` | Blijven staan tot iemand ze verplaatst of verwijdert; de grafische schil verplaatst ze desgevraagd naar een gekozen map |
| Bronbestanden | `geo/` | Blijven staan tot de bewaartermijn (TTL) verloopt en het bestand wordt vervangen |
| Runlogboek | Terminalvenster | Wordt niet naar een bestand geschreven |

De versiebeheerrepository bevat uitsluitend code: `output/`, `geo/`, `tug_state.json`, `tmp/`,
`aanvragen.json`, de projectnotities en het archief zijn uitgesloten. Er komt geen aanvraag- of
adresdata in de repository terecht.

## 6. Menselijke tussenkomst en controlemomenten

De pipeline is zo gebouwd dat zij **niet stilvalt op een onvolledige aanvraag**, maar ook **niets
aanneemt wat zij niet kan onderbouwen**. Beide keuzes leiden het werk terug naar de
vergunningverlener in plaats van naar een impliciete aanname.

| Moment | Wat het systeem doet | Wat de vergunningverlener doet |
|---|---|---|
| Ontbrekend of onjuist gevormd veld in de aanvraag | Waarschuwing in rood in het proceslogboek; rekent door | Beoordeelt of de aanvraag moet worden aangevuld |
| Aanvraag korter dan 28 dagen voor de vlucht ondertekend | Waarschuwing; rekent door | Beoordeelt de termijnoverschrijding |
| Luchtvaartuig niet in het ILT-register, of ICAO-code zonder vastgestelde norm | Neemt **geen** norm aan; laat het luchtvaartuig buiten de toetsing en meldt dat in een rood blok | Bepaalt of het luchtvaartuig in de ontheffing wordt opgenomen en welke norm daarvoor geldt |
| Voor geen enkel luchtvaartuig een norm | Inventariseert tot de ruimste norm (500 m), trekt geen conclusie, meldt dat in rood; exitcode 2 | Bepaalt de norm en beoordeelt de adressenlijst daarop |
| Object in de margeband (tot 150 m buiten de toetsingsafstand) | Toont het op de kaart, niet in de adressenlijst | Beoordeelt of ook daar instemming wenselijk is |
| Manege in het aandachtsgebied | Signaleert de manege | Beoordeelt de feitelijke terreinsituatie |
| Natura 2000 of NNN geraakt | Signaleert gebied en afstand | Beoordeelt of de vluchtomschrijving aanleiding geeft tot doorverwijzing naar een passende beoordeling |
| Luchthaven binnen 1.000 m | Meldt "niet toegestaan" | Neemt het besluit |
| Een bron waar de toetsing niet zonder kan valt uit (BAG, gevelcheck, Natura 2000, luchthavens en luchthaventerreinen, ILT-register) | Breekt de toetsing af: geen rapport | Draait de aanvraag opnieuw zodra de bron bereikbaar is |
| Een andere bron valt uit, of alleen een verouderde lokale kopie is beschikbaar | Maakt het rapport, trekt voor die bron **geen** conclusie en meldt dat in rood bovenaan, in de paragraaf en boven de adressenlijst; de run eindigt met exitcode 2 | Beoordeelt of de aanvraag opnieuw moet worden doorgerekend |

De pipeline breekt op een onvolledige aanvraag niet af, maar wel in twee gevallen waarin er niets
betrouwbaars is om op door te rekenen: de aanvraag is structureel onverwerkbaar (geen object, een
onbekend veld, een waarde van het verkeerde type of buiten het bereik, of een puntlocatie buiten
Nederland, zie [§12](#12-aanvraag-json)); of een bron waar de toetsing niet zonder kan is
onbereikbaar. De onderlinge afstand tussen puntlocaties is nooit een stop. Is voor géén van de luchtvaartuigen een
afstandsnorm herleidbaar, dan rekent zij door als inventarisatie zonder conclusie (exitcode 2, zie
[§14](#14-classificatie-van-luchtvaartuigen)).

## 7. Risico's, beperkingen en wat daartegenover staat

De asymmetrie van dit systeem bepaalt het ontwerp: een **gemist object** (vals negatief) betekent
een omwonende die niet om instemming is gevraagd en een beschikking die op onvolledige informatie
rust. Een **te veel gesignaleerd object** (vals positief) kost de aanvrager extra werk en verder
niets. Waar een keuze zich voordoet, is die daarom consequent naar ruim signaleren gemaakt: een
toeslag van standaard 10 m op de afstandsnorm, een margeband van 150 m, een aandachtsgebied van 375 m rond
maneges, en zoekvensters die veel ruimer zijn dan de toetsingsafstand.

| Risico | Oorzaak | Gevolg | Wat daartegenover staat |
|---|---|---|---|
| School of kinderopvang niet als geluidgevoelig herkend | Het BAG-gebruiksdoel is niet betrouwbaar: scholen staan er vaak in als bijeenkomstfunctie | Vals negatief | Twee aanvullende registers: LRK voor kinderopvang (directe koppeling op BAG-identificatie) en DUO voor scholen. Bij een treffer in die registers is dat register leidend boven het BAG-gebruiksdoel |
| Manege niet gevonden | De detectie zoekt op objectnaam in de BRT; naamloze of generiek benoemde maneges ontbreken | Vals negatief, maar zonder rechtsgevolg | Twaalf zoektermen en een ruim aandachtsgebied. Niet-geregistreerde maneges hebben geen bescherming op grond van de Beleidsregel |
| Schoolgebouw op de verkeerde plek gepositioneerd | DUO levert geen BAG-koppeling; het adres wordt gegeocodeerd en het hoogst scorende verblijfsobject gebruikt | Afstand tot een groot schoolcomplex kan enkele tientallen meters afwijken | Deduplicatie tegen de BAG-treffers; het gebouw verschijnt hoe dan ook in de lijst zodra het binnen de afstand ligt |
| Gebouw net buiten de cirkel terwijl de gevel er nog binnen valt | Een BAG-adrespunt ligt niet op de gevel | Vals negatief | De toetsing gebeurt op de **pandgeometrie**, niet op het adrespunt: een pand telt mee zodra de gevel de cirkel snijdt |
| Piloot landt niet exact op het opgegeven coördinaat | Het formulier vraagt één coördinaat; de praktijk wijkt af | Objecten net buiten de cirkel blijven ongezien | Margeband van 150 m, apart gemarkeerd op de kaarten |
| Begraafplaats onterecht gesignaleerd | Er wordt geen onderscheid gemaakt tussen actieve en gesloten begraafplaatsen | Vals positief | Bewuste keuze; het alternatief (handmatige lijsten van gesloten begraafplaatsen) is niet betrouwbaar bij te houden |
| NNN-gebied net buiten Overijssel niet gesignaleerd | De NNN-cache bevat alleen de provinciale begrenzing | Vals negatief bij grenslocaties | Alle Natura 2000-gebieden zijn ook NNN: bij een N2000-treffer wordt de NNN-treffer aangenomen |
| Externe bron valt uit of wijzigt | Registers en API's zijn van hun bronhouders en veranderen zonder aankondiging | Stille onderbreking van een detectielaag | Reëel gebleken risico: het kinderopvangregister weigerde op enig moment de standaard opvraging, en een begraafplaats-collectie was na een refactor stil leeg. Daartegenover: elke bron meldt hoe haar bevraging is afgelopen, en het rapport leest dáárnaar in plaats van naar het aantal treffers. Een uitgevallen kernbron breekt de toetsing af; bij een andere bron trekt het rapport geen conclusie en meldt het de uitval in rood, met exitcode 2. Een luchthavenlaag zonder één luchthaven en een LRK-bestand zonder kinderopvang gelden als uitgevallen, niet als "niets gevonden". Een verouderde lokale kopie wordt tot een vaste grens gebruikt en met haar ouderdom vermeld — zie [§16](#16-gegevensbronnen-en-caching) |
| Luchtvaartuig pas net geregistreerd | De ILT publiceert het register wekelijks; een registratie van na de laatste publicatie staat er nog niet in | Onterechte melding "niet in register" | Elke run gebruikt de actuele weekversie (publicatiedatum in de bestandsnaam), dus de achterstand is hooguit een week; daarbinnen kan de vergunningverlener een handmatige geluidsafstand invullen |
| Bibliotheekversie verandert de uitkomst | Een minor release van de geometrie- of projectiebibliotheken kan een randgeval anders afhandelen; bij een grens van 500 m is het verschil tussen 499 en 501 m het verschil tussen wel en niet melden | Stille verandering in een juridisch document | Exact vastgepinde versies en een regressietest die 67 waarden vergelijkt met een vastgelegde nulmeting — zie [hoofdstuk 19](#19-kwaliteitsborging) |
| Brondata is onjuist | Registers bevatten fouten | Onjuiste uitkomst | Buiten de invloedssfeer van de pipeline: de bronhouder is verantwoordelijk voor de integriteit van zijn data en die data geldt hier als gegeven. Het proceslogboek benoemt per stap welke bron is geraadpleegd, zodat een fout herleidbaar is tot de bron |

Twee beperkingen die geen mitigatie kennen en als zodanig gelden: de detectie van
**niet-geregistreerde maneges** en de dekking van **toekomstige, vergunde maar nog niet
gerealiseerde functies** uit omgevingsplannen. Voor dat laatste is geen landelijk dekkende bron
beschikbaar zolang niet alle gemeenten hun omgevingsplan hebben gedigitaliseerd.

## 8. Transparantie en verantwoording

**Het proceslogboek is de uitlegbaarheidsvoorziening.** Elk rapport bevat een logboek van twaalf
paragrafen dat per stap vastlegt welke bron is bevraagd, met welk endpoint en welke parameters, wat
dat opleverde en welke regel daarop is toegepast. Paragrafen die aandacht vragen — een geraakt
natuurgebied, een krappe indieningstermijn, een luchtvaartuig zonder norm, een ontbrekend veld —
worden in rood weergegeven. Bovenaan staat of alle bronnen volledig zijn geraadpleegd; is dat niet
zo, dan noemt het rapport welke, en trekt het voor die bronnen geen conclusie.

| Paragraaf | Inhoud |
|---|---|
| 1 | Inputparameters van de aanvraag; ontbrekende velden |
| 2 | Classificatie van de luchtvaartuigen en de maatgevende toetsingsafstand |
| 3 | Omzetting WGS84 → RD en de berekende zones |
| 4 | Natura 2000 |
| 5 | Natuurnetwerk Nederland |
| 6 | Verblijfsobjecten via BAG WFS |
| 7 | Begraafplaatsen |
| 8 | Kinderopvanglocaties |
| 9 | Scholen |
| 10 | Maneges |
| 11 | Luchthavens |
| 12 | Synthese, aantallen per categorie en verwijzing naar de uitvoerbestanden |

**Herleidbaarheid tot de exacte code.** Elk voortbrengsel — runlogboek, PDF, HTML-kaart en de
tussentijdse state — draagt de workflowversie: de korte commit-hash van de code waarmee het is
gemaakt, met de toevoeging *(niet-gecommitte wijzigingen)* wanneer er lokaal gewijzigde bestanden
zijn. Van elk rapport is daarmee vast te stellen met welke versie van de regels het is opgesteld.

**Reproduceerbaarheid.** Dezelfde aanvraag opnieuw doorrekenen levert hetzelfde resultaat, behoudens
wijzigingen in de brondata. De vastgepinde bibliotheekversies en de regressietest borgen dat de
omgeving zelf geen stille verschillen introduceert.

**Verifieerbaarheid van elk afzonderlijk adres.** De adressenlijst vermeldt per object waarom het
er staat: het gebruiksdoel uit de BAG, of de bron die het object als beschermd aanmerkt
(`kinderdagverblijf met bedverblijf (KDV)`, `school – PO (DUO)`, `begraafplaats – {naam}`,
`manege – {naam}`). Een lezer kan elke regel terugvoeren op een bron.

---
---

# Deel B — Technische documentatie

## 9. Installatie en omgeving

### 9.1 Vereisten

- **Python 3.12.** Ondergrens én bovengrens zijn scherp: onder 3.12 levert de vastgepinde
  geo-stack geen kant-en-klare pakketten meer, vanaf 3.15 ondersteunt PySide6 niet. Op een andere
  3.x-versie kan de installatie pakketten willen compileren, waarvoor een C-compiler nodig is.
- **Geen adminrechten.** Alles komt in een virtuele omgeving binnen de projectmap terecht.
- Voor de grafische schil onder Linux: de systeembibliotheken `libxcb-cursor0`,
  `libxkbcommon-x11-0`, `libxcb-icccm4`, `libxcb-keysyms1` en `libxcb-xkb1`. Ontbreken die, dan
  meldt Qt alleen dat het xcb-platformplugin niet geladen kan worden.

### 9.2 Installeren

```bash
python install.py
```

Het script controleert de Python-versie, maakt `.venv/` aan in de projectmap, installeert
`requirements.txt` en controleert daarna of elke bibliotheek zich ook werkelijk laat importeren —
een geslaagde installatie garandeert niet dat een binaire uitbreiding laadt, daar kunnen Qt- of
GDAL-systeembibliotheken voor ontbreken. Het script is idempotent: een tweede run hergebruikt de
bestaande omgeving.

**Elk pakket wordt tegen zijn hash gecontroleerd**, ook alle indirecte afhankelijkheden, en er
worden alleen kant-en-klare pakketten (wheels) geïnstalleerd. De enige uitzondering is `odfpy`, dat
alleen als broncode bestaat; dat wordt gebouwd met de eveneens gehashte `setuptools` uit
`requirements-build.txt` in plaats van met een bouwomgeving die pip zelf ongecontroleerd ophaalt.
Meldt pip dat een hash niet overeenkomt, installeer dan níét zonder controle: het gedownloade
pakket is dan niet het pakket dat is vastgelegd.

Bewust een `.py`-bestand en geen `.sh` of `.bat`: op beheerde laptops is het uitvoeren van
shellscripts vaak geblokkeerd, het draaien van een Python-bestand via de interpreter niet. Bij een
proxy of een bedrijfs-CA-certificaat benoemt de foutmelding wat er moet gebeuren.

Daarna starten zonder de omgeving te activeren:

```bash
.venv/bin/python tug_gui.py
.venv/bin/python tug_run.py aanvraag.json
```

**De projectomgeving is de enige omgeving.** Wordt `tug_run.py` of `tug_gui.py` met een andere
interpreter gestart — bijvoorbeeld `python` van het PATH — dan start het script zichzelf opnieuw in
`.venv` en meldt dat. Alleen daar zijn de afhankelijkheden gepind, gehasht en gecontroleerd.

### 9.3 Dependencies

De afhankelijkheden liggen in twee lagen vast. `requirements.in` noemt de twaalf directe
afhankelijkheden, **exact** gepind (`==`) en met de reden erbij: de pipeline produceert documenten
met een juridische functie, en dezelfde aanvraag moet op elke machine dezelfde afstanden en dezelfde
classificatie opleveren. `requirements.txt` wordt daaruit gegenereerd en bevat **alle** pakketten,
ook de indirecte, elk met versie én hash.

| Groep | Pakketten |
|---|---|
| Grafische schil | `PySide6` |
| Tabellen en registers | `pandas`, `numpy`, `odfpy` (leest het ILT-register in ODS-formaat), `defusedxml` (veilige XML-verwerking voor `odfpy`) |
| Geo | `geopandas`, `shapely`, `pyproj`, `pyogrio` (geo-IO onder geopandas) |
| Bronnen ophalen | `requests` |
| Uitvoer | `reportlab` (PDF), `pillow` (kaartafbeeldingen) |

| Bestand | Inhoud |
|---|---|
| `requirements.in` → `requirements.txt` | De pipeline |
| `requirements-dev.in` → `requirements-dev.txt` | Pipeline plus controlegereedschap: pytest, ruff, bandit, mypy, pip-audit, pre-commit |
| `requirements-build.in` → `requirements-build.txt` | `setuptools`, alleen om `odfpy` te bouwen |

**Bijwerken** is een bewuste handeling: pas de versie in het `.in`-bestand aan (kies geen release die
jonger is dan een week), genereer de lockfiles opnieuw, installeer en draai de regressietest
(zie [hoofdstuk 19](#19-kwaliteitsborging)):

```bash
uv pip compile --universal --python-version 3.12 --generate-hashes requirements.in -o requirements.txt
uv pip compile --universal --python-version 3.12 --generate-hashes -c requirements.txt requirements-dev.in -o requirements-dev.txt
uv pip compile --universal --python-version 3.12 --generate-hashes requirements-build.in -o requirements-build.txt
python install.py
.venv/bin/python test/test_regressie.py
```

`odfpy` wordt sinds 2020 niet meer onderhouden. Het werkt, en leest de XML van het register veilig
via `defusedxml`; wordt het ooit onbruikbaar, dan is de pandas-engine `calamine` het alternatief.

Zonder `geopandas`/`pyogrio` geldt de NNN-analyse als niet uitgevoerd en meldt het rapport dat in
rood. Zonder `PySide6` werkt alleen de grafische schil niet.

### 9.4 Wat er bij de eerste run wordt gedownload

De pipeline maakt `geo/` en `output/` zelf aan en haalt de bronbestanden op zodra ze nodig zijn.

| Bestand | Bron | Grootte | Bewaartermijn |
|---|---|---|---|
| `luchtvaartuigregister_ilt.ods` | ILT (link wordt van de bronpagina gelezen) | ± 1,3 MB | Tot een nieuwe weekversie |
| `lrk_kinderopvang.csv` | Landelijk Register Kinderopvang | ± 12 MB | 7 dagen |
| `nnn_gebieden.gpkg` | PDOK ATOM-feed (GML → GeoPackage) | ± 111 MB | 180 dagen |
| `duo_scholen_po.geojson` | DUO, basisonderwijs, gefilterd op Overijssel | ± 143 KB | 90 dagen |
| `duo_scholen_overig.geojson` | DUO, SO/VO/MBO/HO, gefilterd op Overijssel | ± 63 KB | 90 dagen |

De opbouw van de NNN-GeoPackage duurt ongeveer 30 seconden: downloaden, GML lezen, projecteren naar
RD en wegschrijven.

## 10. Gebruik

### 10.1 Eén of meer aanvragen

```bash
python tug_run.py aanvraag.json
```

```bash
python tug_run.py aanvraag1.json aanvraag2.json aanvraag3.json
```

Een bestand mag één aanvraagobject (`{...}`) of een array van aanvragen (`[{...}, {...}]`) bevatten;
beide vormen mogen door elkaar worden meegegeven. Elke aanvraag levert een eigen PDF en HTML.

Elke aanvraag wordt bij het inlezen tegen het schema van [§12](#12-aanvraag-json) getoetst; een
onverwerkbare aanvraag wordt met de reden gemeld en overgeslagen. Bij een batch logt de runner een
falende aanvraag en gaat door met de volgende; aan het einde volgt een samenvatting met volledig
getoetste, onvolledig getoetste en niet verwerkte aanvragen.

| Exitcode | Betekenis |
|---|---|
| 0 | Alle aanvragen volledig getoetst |
| 1 | Minstens één aanvraag niet verwerkt: onverwerkbare invoer of een afgebroken stap (bij één aanvraag: de exitcode van die stap) |
| 2 | Alles verwerkt, maar bij minstens één aanvraag is een bron niet volledig geraadpleegd of is voor geen enkel luchtvaartuig een afstandsnorm herleidbaar; het rapport meldt wat |
| 130 / 143 | De run is onderbroken (Ctrl+C) of beëindigd |

Een exitcode 0 betekent dus ook echt dat elke bron is geraadpleegd. Bij de start meldt de runner
bovendien als de laatste beveiligingscontrole ontbreekt, ouder is dan een maand of bevindingen had
(zie [§19.5](#195-beveiligingsreview)).

### 10.2 Herstart vanaf een stap

Met een handmatig bewaarde state — na elke run, ook een afgebroken, wordt `tug_state.json` gewist:

```bash
python tug_run.py tug_state.json --vanaf 05
```

Geldige stapnummers zijn `01`, `02`, `03` en `05`, overeenkomstig de scriptnamen. Alleen bruikbaar
bij één aanvraag tegelijk.

### 10.3 Dataveiligheid tijdens de run

`tug_state.json` bevat tijdens de run de volledige aanvraag met alle gevonden adressen. Het bestand
is alleen leesbaar voor de eigen gebruiker en wordt na elke aanvraag overschreven met `{}`: na
succes, wanneer een stap afbreekt, en ook wanneer de run wordt onderbroken met Ctrl+C of van buitenaf
wordt beëindigd. De lopende stap wordt dan eerst gestopt, zodat die de state niet alsnog vult.

## 11. Grafische schil

```bash
python tug_gui.py
```

Een venster (1400 × 1000) waarin één aanvraag wordt samengesteld en direct wordt doorgerekend. De
schil vervangt de opdrachtregel niet en voert zelf niets uit: zij schrijft een aanvraagbestand en
start daarmee `tug_run.py`, dat de enige uitvoerder blijft.

**Indeling** — links (⅓) de invoer, rechts (⅔) de kaart.

| Onderdeel | Toelichting |
|---|---|
| Aanvraaggegevens | Omschrijving dossier, vluchtdatum en datum ondertekening |
| Luchtvaartuigen | Registratiekenmerk invoeren en toevoegen met `+` of Enter; het overzicht eronder groeit mee en heeft per regel een veld voor een handmatige geluidsafstand en een verwijderknop |
| Puntlocaties | Klikken op de kaart plaatst een pin, klikken op een pin verwijdert hem. Handmatige invoer in WGS84 of RD. Elke pin verschijnt in het overzicht met beide coördinaatstelsels |
| Kaart | Dezelfde Leaflet-opzet en dezelfde PDOK-tegels als de export, zodat er geen tweede kaartimplementatie uit de pas kan lopen. Schakelbaar tussen topografisch en luchtfoto; startbeeld midden-Overijssel op zoomniveau 11 |
| Zoeken op adres | Zoekveld linksboven op de kaart: adres, postcode, straat of plaats. Na een korte typpauze verschijnen tot acht treffers (PDOK Locatieserver, `suggest`); pijltjes en Enter of een klik kiezen er één. De kaart vliegt ernaartoe en zet een oranje markering, maar plaatst **geen** puntlocatie — dat blijft een klik op de kaart. Escape wist het veld en de markering |
| Bug rapporteren | Knop met insect-icoon rechtsboven in de kop. Opent in het eigen e-mailprogramma een concept aan de beheerder met de workflowversie in het onderwerp (`bugreport Workflow TUG-ontheffingen Overijssel - <versie>`) en invulinstructies in de tekst: stappen, verwachting, wat er gebeurde, foutmelding of schermafbeelding, bijlagen (aanvraag-JSON, PDF/HTML) met een afweging over persoonsgegevens; datum, besturingssysteem en versie zijn al ingevuld. Er wordt niets automatisch verstuurd. Daarnaast opent altijd een venster met dezelfde velden (Aan, Onderwerp, Tekst), elk met een kopieerknop, om in een ander e-mailprogramma of webmail te plakken. Dat venster verschijnt altijd, omdat de schil niet kan zien of het concept werkelijk opende: onder Linux slaagt `xdg-open` ook als de mailreader daarna faalt (XFCE: *Failed to execute default Mail Reader*), en Windows 11 zonder ingestelde mail-app toont eerst een keuzevenster |

**De kaart is afgeschermd.** Leaflet komt uit `gui/vendor/leaflet` (dezelfde versie en inhoud als de
vastgepinde versie in de HTML-export; een test bewaakt dat) en wordt in de pagina ingevoegd; de
pagina laadt geen externe scripts en kan geen lokale bestanden lezen. Klikken op een link, zoals de
bronvermelding, opent de systeembrowser: de kaartweergave zelf navigeert nergens heen, zodat geen
externe pagina bij de koppeling met Python kan. Ook de adreszoeker verandert daar niets aan: de pagina geeft de zoektekst
via de koppeling aan Python, dat de Locatieserver bevraagt via `tug_http` (alleen https, vaste
hostlijst, begrensde omvang) en de treffers terugstuurt. De pagina doet zelf geen netwerkverzoeken
en zet namen uit het antwoord als tekst in de lijst, nooit als HTML.

**Genereren** schrijft de aanvraag naar `tmp/<omschrijving>_<timestamp>.json`, start `tug_run.py`
daarmee en vraagt daarná pas om een exportmap — de pipeline wacht dus niet op de gebruiker. Tijdens
de run toont een voortgangsvenster de uitvoer per stap. Na afloop worden de nieuwe bestanden uit
`output/` naar de gekozen map verplaatst en geopend; wordt de mapkeuze geannuleerd, dan blijven ze
in `output/` staan. Was de toetsing onvolledig (exitcode 2), dan zegt het eindbericht dat in rood en
verwijst het naar de melding bovenaan het rapport.

Daarna wordt `tmp/` geleegd, ook na een afgebroken run — de gebruikte invoer blijft vastgelegd in het
proceslogboek van het rapport. Wordt het venster gesloten terwijl de pipeline nog loopt, dan stopt de
schil de pipeline (die zelf de state wist) en leegt `tmp/`. Ook bij het opstarten wordt `tmp/`
geleegd, voor het geval een eerdere sessie hard is afgesloten.

**Uitleg bij de velden.** Een klein "i" naast een kop of veld toont de uitleg direct bij aanwijzen
of aanklikken, ook als het venster niet actief is.

**Bewaakte invoer.** De knop blijft grijs zolang omschrijving, luchtvaartuig of puntlocatie
ontbreekt. De omschrijving komt in de bestandsnamen terecht; het veld vraagt daarom om een zaaknummer
of plaats en datum, niet om een persoonsnaam. De schil waarschuwt bij minder dan 28 dagen tussen ondertekening en vlucht. Bij meerdere
puntlocaties toont zij altijd de melding met de grootste onderlinge afstand en dat de toetsing als
geheel wordt uitgevoerd op alle puntlocaties; genereren blijft mogelijk.

**Handmatige geluidsafstand.** Elke regel in het overzicht van luchtvaartuigen heeft een veld dat
op "uit register" staat. Vul daar een afstand in voor een luchtvaartuig dat het ILT-register niet
kent, zoals een buitenlandse registratie; die waarde gaat als `afstand_m` mee en vóór het register.

**Marge rond puntlocatie.** Onder de puntlocaties staat de marge rond de puntlocatie (in de aanvraag `toeslag_m`; standaard 10 m, 0 – 500 m),
die bij de geluidsafstand wordt opgeteld. Het rapport noemt een afwijkende waarde als zodanig. Een kleinere toeslag verkleint de marge die de toetsing naar ruim signaleren laat hellen (§7).

**Verborgen velden.** Velden die de toetsing niet sturen, staan niet in beeld maar worden wel met
een vaste waarde in de aanvraag weggeschreven. Schema, validatie en rapport blijven daardoor
ongewijzigd; het proceslogboek toont deze waarden, dus lees ze als standaardwaarde en niet als
opgave van de aanvrager.

| Veld in de aanvraag | Weggeschreven waarde |
|---|---|
| `soort_ontheffing` | `locatiegebonden` |
| `aantal_vluchten` | `50` |
| `tijdstip_ondertekening` | het tijdstip waarop het venster werd geopend |
| `vlucht_udp`, `vlucht_start`, `vlucht_einde` | `true`, `null`, `null` (vluchten binnen de uniforme daglichtperiode) |
| `type` per luchtvaartuig | `heli` |

Weer tonen kan door `TOON_AANVULLENDE_VELDEN = True` te zetten boven in `tug_gui.py`.

**Datumvelden** staan op "vanaf heden". Een ondertekening in het verleden invoeren kan door
`ONDERTEKENING_VANAF_HEDEN = False` te zetten boven in `tug_gui.py`.

**Starten onder een VNC-sessie zonder GLX** (zoals op de ontwikkelmachine) vereist
software-rendering:

```bash
DISPLAY=:1 XAUTHORITY=$HOME/.Xauthority QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu --disable-gpu-compositing --in-process-gpu" python tug_gui.py
```

Zonder die vlaggen sluit het proces af met `GLX is not present`.

## 12. Aanvraag-JSON

```json
{
  "naam": "Locatienaam datum",
  "soort_ontheffing": "locatiegebonden",
  "datum_vlucht": ["2026-06-20", "2026-07-01"],
  "vlucht_udp": true,
  "vlucht_start": null,
  "vlucht_einde": null,
  "aantal_vluchten": 12,
  "luchtvaartuigen": [
    {"registratie": "PH-ANK", "type": "heli"},
    {"registratie": "OO-EYP", "type": "heli", "afstand_m": 300}
  ],
  "coord_lat": [52.405354, 52.405612],
  "coord_lon": [6.129962, 6.130331],
  "toeslag_m": 10,
  "datum_ondertekening": "2026-06-01",
  "tijdstip_ondertekening": "16:29"
}
```

| Veld | Type | Toelichting |
|---|---|---|
| `naam` | string | Omschrijving van het dossier — een zaaknummer of plaats en datum, geen persoonsnaam; komt in de bestandsnamen en het proceslogboek, zodat invoer en uitvoer koppelbaar zijn |
| `soort_ontheffing` | string | `"locatiegebonden"` of `"generiek"` |
| `datum_vlucht` | string of array | `YYYY-MM-DD`; één datum of meerdere. Een enkele string wordt genormaliseerd naar een array |
| `vlucht_udp` | boolean | Bij `true` geldt de uniforme daglichtperiode en worden `vlucht_start` en `vlucht_einde` genegeerd |
| `vlucht_start` / `vlucht_einde` | string of null | `HH:MM` |
| `aantal_vluchten` | getal | — |
| `luchtvaartuigen` | array | Objecten met `registratie` (PH-kenmerk), facultatief `type`, en facultatief `afstand_m`: een handmatig vastgestelde geluidsafstand in meters die vóór het register gaat (zie [§14](#14-classificatie-van-luchtvaartuigen)); geen andere velden |
| `coord_lat` / `coord_lon` | float of lijst | WGS84, minimaal zes decimalen. Bij meerdere puntlocaties twee even lange lijsten, waarbij de n-de breedtegraad bij de n-de lengtegraad hoort |
| `toeslag_m` | getal | Toeslag in meters rond de puntlocatie, opgeteld bij de geluidsafstand; ontbreekt het veld, dan 10 m |
| `datum_ondertekening` | string | `YYYY-MM-DD` |
| `tijdstip_ondertekening` | string | `HH:MM` |

**Schema.** Bij het inlezen (`tug_aanvraag.py`) wordt de structuur getoetst. Een aanvraag die daar niet
door komt, wordt niet verwerkt — de stappen erna zouden anders halverwege vastlopen of met een
zinloze waarde doorrekenen. Ontbrekende of lege velden zijn géén structuurfout; die meldt de
validatie als waarschuwing ([§13](#13-validatieregels)).

| Structuurcontrole (fataal) | Grens |
|---|---|
| De aanvraag is een JSON-object | — |
| Alleen de velden uit de tabel hierboven; elk ander veld wordt geweigerd | Ook het vroegere `straal_override`: de toetsingsafstand komt uitsluitend uit de classificatie |
| Elk ingevuld veld heeft het type uit de tabel; `true`/`false` telt niet als getal | — |
| `soort_ontheffing` | `locatiegebonden` of `generiek` |
| `aantal_vluchten` | 1 – 9.999 |
| Tekstvelden | Maximaal 200 tekens |
| `luchtvaartuigen` | Maximaal 50; elk een object met alleen `registratie` en `type` (tekst) en `afstand_m` (getal, 1 – 2.000 m) |
| `toeslag_m` | 0 – 500 m |
| `coord_lat` / `coord_lon` als lijst | Niet leeg, maximaal 20 puntlocaties, alleen getallen |
| `datum_vlucht` als lijst | Maximaal 366 data, elk als tekst |

**Meerdere puntlocaties.** Bij twee of meer locaties geven schil, validatielog, proceslogboek (§3) en
HTML-kaart (balk bovenaan en legenda) altijd dezelfde melding, ongeacht de afstand: *"Grootste
onderlinge afstand tussen de puntlocaties: … m. De toetsing wordt als geheel uitgevoerd op alle
puntlocaties."* Er is geen afstandsgrens en de toetsing gaat altijd door. Het toetsingsgebied omvat
de cirkels rond alle locaties samen, afstanden gelden tot de dichtstbijzijnde locatie, en er blijft
één adressenlijst per aanvraag. Het rapport vermeldt bij een natuurtreffer
welke puntlocatie het gebied raakt.

**Batch-invoer** is dezelfde structuur als array:

```json
[
  { "naam": "Locatie A 15 juni" },
  { "naam": "Locatie B 22 juni" }
]
```

## 13. Validatieregels

Uitgevoerd door `tug_01_validatie.py`. Het uitgangspunt: **de validatie stopt de pipeline niet op
een onvolledige aanvraag**, maar maakt elk gebrek zichtbaar. Zodra het aanvraagformulier de velden
zelf afdwingt, verdwijnen deze waarschuwingen in de praktijk vanzelf.

| Controle | Gedrag bij afwijking |
|---|---|
| Aanwezigheid van `soort_ontheffing`, `luchtvaartuigen`, `coord_lat`, `coord_lon`, `datum_ondertekening`, `tijdstip_ondertekening` | Waarschuwing per veld |
| Aanwezigheid van `datum_vlucht` | Waarschuwing "vluchtdatum onbekend"; de vergunningverlener vult de datum handmatig aan |
| `datum_vlucht` als string | Genormaliseerd naar een array (geen melding) |
| Datumformaat `YYYY-MM-DD` voor vluchtdata en ondertekening | Waarschuwing |
| Ondertekening minimaal 28 dagen vóór de vroegste vluchtdatum | Waarschuwing met het werkelijke aantal dagen |
| Ondertekening ná de vroegste vluchtdatum | Waarschuwing |
| Structuur van de aanvraag ([§12](#12-aanvraag-json)) | **Fataal** — aanvraag niet verwerkt |
| Elk luchtvaartuig heeft een `registratie` | Waarschuwing per element; zonder kenmerk krijgt het luchtvaartuig geen norm en blijft het buiten de toetsing |
| `registratie` heeft de vorm van een kenmerk (bijv. `PH-ECE`) | Waarschuwing; het register bepaalt daarna of het kenmerk bestaat |
| Puntlocaties: even lange lijsten, binnen Nederland | **Fataal** |
| Meerdere puntlocaties | Melding met de grootste onderlinge afstand, zonder grens; rekent door |

Alle waarschuwingen komen rood in het proceslogboek van het rapport te staan, en het rapport wordt
ook bij een onvolledige aanvraag geproduceerd.

## 14. Classificatie van luchtvaartuigen

Twee opzoekingen achter elkaar, per opgegeven luchtvaartuig:

1. **PH-kenmerk → ICAO-typeaanduiding**, in het ILT Luchtvaartuigregister. Het register wordt
   bij elke run gecontroleerd op een nieuwe weekversie van de ILT en alleen dan opnieuw gedownload;
   zie [§16.1](#161-ilt-luchtvaartuigregister). Een toestel dat na de laatste publicatie is
   ingeschreven, staat er dus hooguit een week nog niet in.
2. **ICAO-typeaanduiding → appendixcategorie → afstandsnorm**, in de NLR-indelingslijst. Die tabel
   staat in de code (`NLR_TABEL` in `tug_02_classificatie.py`) en wordt met de hand bijgewerkt
   wanneer NLR of ILT de lijst wijzigt.

| Appendix | Norm | Voorbeeldtypen |
|---|---|---|
| 010 | 250 m | EC120, R66, B407, AS350 |
| 011 | 150 m | R22, R44, H269 |
| 012 | 350 m | A139, S76, B412 |
| 013 / 015 / 016 / 017 | niet vastgesteld | A109, EC135, EC145, NH90, UH-1 |
| 014 | 500 m | H60, Chinook, Puma, AS332 |

### Wat er gebeurt als een opzoeking geen norm oplevert

De pipeline neemt **geen afstandsnorm aan die zij niet kan onderbouwen**.

| Situatie | Uitkomst | In het rapport |
|---|---|---|
| ICAO-code met vastgestelde norm | Norm uit de NLR-tabel | Normale regel |
| Appendixcategorie 013, 015, 016 of 017 | **Geen norm**; het luchtvaartuig blijft buiten de toetsing | Rood, met een apart blok dat de reden benoemt |
| ICAO-code niet in de NLR-indelingslijst | 150 m op grond van de Beleidsregel; vermoedelijk een MLA of vliegtuig | Regel met bronvermelding `beleidsregel 150 m` |
| PH-kenmerk niet in het ILT-register (bijvoorbeeld een buitenlandse registratie) | **Geen norm**; het luchtvaartuig blijft buiten de toetsing | Rood, met een apart blok dat de reden benoemt |
| `afstand_m` opgegeven bij het luchtvaartuig | De opgegeven afstand, vóór register en NLR-tabel; het luchtvaartuig telt mee | Regel "handmatig opgegeven in de aanvraag", met ernaast wat register en NLR-tabel opleveren; een afwijking wordt benoemd |

De handmatige afstand is geen aanname van de pipeline maar een vaststelling van de
vergunningverlener, bijvoorbeeld uit het geluidsrapport van een buitenlands toestel; het rapport
maakt dat onderscheid zichtbaar.

Blijft er geen enkel luchtvaartuig met een herleidbare norm over (bijvoorbeeld alleen buitenlandse
registraties), dan stopt de pipeline **niet**. Ook dan neemt zij geen norm aan: zij inventariseert
de omgeving tot de ruimste norm uit de NLR-tabel (500 m + toeslag), zodat niets buiten beeld blijft,
en trekt geen conclusie. Het rapport meldt in rood bovenaan dat de afstandsnorm niet is vastgesteld,
de adressenlijst heet dan een inventarisatie en geen toetsing, en de run eindigt met exitcode 2.
Bij een kenmerk zonder `PH-` meldt het logboek dat het niet in het ILT-register te verwachten is:
het register kent alleen Nederlandse registraties. In alle andere gevallen rekent zij
door met de luchtvaartuigen die wél een norm hebben, en vermeldt het rapport expliciet welke
luchtvaartuigen niet zijn meegewogen. De vergunningverlener bepaalt vervolgens — op basis van het
geluidsrapport van het betreffende toestel — welke norm geldt, of houdt het toestel buiten de
ontheffing.

**Bij meerdere luchtvaartuigen** geldt de grootste norm als maatgevend voor de hele aanvraag.

## 15. Toetsingslogica

### 15.1 Zones

| Zone | Berekening | Waarvoor |
|---|---|---|
| Toetsingsafstand | Maatgevende norm + toeslag (standaard 10 m, `toeslag_m`) | Geluidgevoelige gebouwen, begraafplaatsen, kinderopvang, scholen |
| Margeband | Toetsingsafstand + 150 m | Gevelcontrole en signalering op de kaart; niet in de adressenlijst |
| Aandachtsgebied maneges | Toetsingsafstand + 375 m | Manegesignalering; de margeband telt hier niet bovenop |
| Signaleringszone natuur | Toetsingsafstand + 500 m | Natura 2000 en NNN |

Het kaartlabel toont de opbouw: *"Toetsingsafstand TUG (250 m + 10 m = 260 m)"*. Alle buffers worden
in RD New (EPSG:28992) aangemaakt, omdat afstanden daarin in meters kloppen; de aanvraagcoördinaten
in WGS84 worden daarvoor omgezet.

### 15.2 Categorieën in de adressenlijst

| Sectie | Inhoud | Gevolg |
|---|---|---|
| **Wettelijk relevant** | Geluidgevoelige gebouwen waarvan de gevel de toetsingsafstand snijdt, begraafplaatsen waarvan de polygoon de toetsingsafstand snijdt, kinderopvanglocaties en scholen binnen de toetsingsafstand | Instemmingsverklaring vereist |
| **Aandachtslocaties** | Maneges binnen het aandachtsgebied, luchthavens | Signalering; geen instemmingsvereiste |
| **Overig** | Overige objecten in beeld | Alleen weergave op de kaart |

De **margeband** staat alleen op de kaarten en niet in de adressenlijst. Zij heeft twee doelen: een
groot gebouw kan een adrespunt buiten de toetsingsafstand hebben terwijl de gevel er nog binnen
valt, en een piloot landt niet altijd exact op het opgegeven coördinaat. De vergunningverlener kan
daardoor zien of ook daar instemming wenselijk is.

### 15.3 Geluidgevoelige functies

Uit het BAG-gebruiksdoel tellen mee: **woonfunctie, onderwijsfunctie en gezondheidszorgfunctie**.
De logiesfunctie telt niet mee. Omdat het BAG-gebruiksdoel niet betrouwbaar is voor scholen en
kinderopvang, zijn DUO en LRK voor die twee categorieën leidend: een object dat daar voorkomt gaat
naar de wettelijk relevante sectie, ook als het BAG-gebruiksdoel iets anders zegt.

De toets gebeurt op de **pandgeometrie**, niet op het adrespunt: een verblijfsobject is wettelijk
relevant zodra de gevel van het pand de toetsingsafstand snijdt.

### 15.4 Begraafplaatsen

Twee parallelle sporen, omdat geen van beide alleen volstaat:

1. **PDOK Location API** (collectie `functioneel_gebied`), op de zoektermen "begraafplaats" en
   "erebegraafplaats", binnen de toetsingsafstand + 1.500 m. Dat zoekvenster is ruim omdat de API op
   het middelpunt van een object zoekt: een grote begraafplaats kan een middelpunt op honderden
   meters hebben terwijl de rand vlak bij de locatie ligt.
2. **BRT Top10NL** (`terrein_vlak` met `typelandgebruik = 'dodenakker'`), binnen de toetsingsafstand
   + 500 m, voor begraafplaatsen die niet als benoemd object zijn geïndexeerd. Eén begraafplaats kan
   uit meerdere aangrenzende vlakken bestaan; vlakken die elkaar binnen 5 m raken worden samengevoegd
   tot één geheel voordat de afstand wordt bepaald.

Wettelijk relevant zodra de **polygoon** de toetsingsafstand snijdt. Er wordt geen onderscheid
gemaakt tussen actieve en gesloten begraafplaatsen.

### 15.5 Maneges

Er bestaan geen landelijk dekkende geometrieën van manegeterreinen. De grootste manege van Nederland
heeft oefenterreinen op ongeveer 340 m van het BAG-adres; een manege kan dus binnen de
toetsingsafstand liggen terwijl het adres er ver buiten valt. Daarom een aandachtsgebied van
toetsingsafstand + 375 m, waarbinnen maneges worden gesignaleerd zonder instemmingsvereiste. De
vergunningverlener beoordeelt de feitelijke terreinsituatie.

De detectie zoekt op objectnaam met twaalf termen (manege, rijschool, hippisch, paardencentrum,
rijvereniging, ponyclub, ruiterclub, ruitersportcentrum, paardensportcentrum, paardensportvereniging,
paardenhouderij, hippique), omdat de API geen OR-zoekopdracht en geen filter op gebouwtype kent.

### 15.6 Luchthavens

| Grens | Afstand | Gevolg |
|---|---|---|
| Wettelijke minimumafstand | 1.000 m | Niet toegestaan |
| Signaleringsmarge | 5.000 m | Signalering |

**Bronnen.** Luchthavens komen uit twee bronnen, die elk de toetsing afbreken als ze uitvallen:

1. **BRT Top10NL** (`functioneel_gebied_vlak`) levert de terreinen als vlak: vliegvelden,
   zweefvliegvelden en helikopterlandingsterreinen, ook over de provinciegrens (Teuge, Hoogeveen).
   De API filtert niet op type; de code selecteert `vliegveld, luchthaven`, `zweefvliegveldterrein`
   en `helikopterlandingsterrein`. Vlakken die elkaar raken, zoals het vliegveld en het
   zweefvliegveld van Twente, worden één terrein.
2. **GeoPortaal Overijssel** (`B6_Luchthaven_puntlocaties`) levert de provinciale
   luchthavenregelingen als punt, met naam en gebruik. Die laag bevat alleen regelingen
   (helihavens, zweefvliegterrein Lemelerveld), géén vliegvelden met een luchthavenbesluit zoals
   Twente. Een regelingspunt binnen 250 m van een Top10NL-terrein geeft dat terrein zijn naam;
   anders telt het als eigen luchthaven.

**Afstand tot de rand.** De afstand is die van de dichtstbijzijnde puntlocatie tot de rand van het
terrein — 0 m als de puntlocatie erbinnen ligt — en niet tot een middelpunt; bij een terrein van
ruim 380 ha scheelt dat kilometers. Top10NL is een topografische registratie en niet de juridische
grens uit een luchthavenbesluit, maar het terreinvlak ligt ruim om start- en landingsbaan.
Helikopterlandingsterreinen hebben in Top10NL geen naam; zonder provinciale regeling krijgen ze
het dichtstbijzijnde adres als naam. MLA- en ultralightterreinen en tijdelijke TUG-terreinen staan
in geen van beide bronnen.

### 15.7 Natura 2000 en Natuurnetwerk Nederland

Beide zijn **signaleringslagen, geen weigeringsgrond**. Ligt de locatie binnen of nabij zo'n gebied,
dan kan de vluchtomschrijving aanleiding zijn de aanvrager te wijzen op een passende beoordeling.

Natura 2000 wordt landelijk en rechtstreeks bevraagd en levert echte gebiedsnamen; die gaan met
naam en afstand het rapport in. NNN komt uit een lokale kopie voor Overijssel en levert **geen
bruikbare namen**: het naamveld is bij ruim de helft van de gebieden leeg en de gevulde waarden zijn
vooral provinciale categorie-aanduidingen. Dat is een eigenschap van de bron — NNN is een
planologische begrenzing, geen register van benoemde gebieden. De signalering gebruikt daarom het
vaste label "NNN-gebied": *"puntlocatie op 14 m van NNN-gebied (< 500 m)"*.

Omdat alle Natura 2000-gebieden ook NNN zijn, wordt bij een N2000-treffer automatisch een
NNN-treffer aangenomen, ook wanneer de locatie net buiten de provinciegrens valt en niet in de
lokale kopie voorkomt.

## 16. Gegevensbronnen en caching

Alle bronnen zijn open data; er zijn geen API-sleutels of accounts in gebruik. Bestanden worden in
`geo/` bewaard en automatisch opnieuw opgehaald zodra de bewaartermijn is verlopen.

**Eén toegangsweg.** Elke aanroep loopt via `tug_http.py`, dat drie afspraken afdwingt: alleen
`https` en alleen naar de hosts in `tug_config.BRON_HOSTS` — ook na een redirect en ook voor een
verwijzing uit een bronantwoord; een maximale omvang per antwoord (ruim twee keer de gemeten omvang;
een groter antwoord wordt afgebroken); en een mislukte bevraging is een fout, geen leeg resultaat.

**Wat een storing betekent.** Elke bron meldt in een bronregister (`tug_bronstatus.py`) hoe haar
bevraging is afgelopen. Dat register komt in de state, het rapport en de exitcode.

| Bron | Bij uitval |
|---|---|
| BAG-verblijfsobjecten, BAG-panden voor de gevelcheck, Natura 2000, luchthavens en luchthaventerreinen, ILT-register | **Toetsing afgebroken**, geen rapport. Deze bronnen bepalen de adressenlijst, de natuurtoets, het luchthavenverbod en de toetsingsafstand |
| NNN, begraafplaatsen, kinderopvang, scholen, maneges | Rapport met rode melding en **geen conclusie** voor die bron; exitcode 2 |
| Adresaanvulling, gevelcontouren op de kaart, kaarttegels | Gemeld als weergaveprobleem; geen invloed op de toetsing |

Een bron die aantoonbaar stuk is, telt als uitgevallen: een luchthavenlaag zonder één luchthaven,
een LRK-bestand zonder kinderopvang, een DUO-bestand zonder vestigingen in Overijssel.

**Noodterugval op een lokale kopie.** Lukt het verversen van een verlopen cache niet, dan wordt de
oude kopie nog gebruikt tot een vaste grens en meldt het rapport haar ouderdom in rood (exitcode 2).
Daarboven geldt de bron als uitgevallen. Een half ververste kopie wordt nooit weggeschreven.

| Bron | Waarvoor | Bewaartermijn | Noodterugval tot | Cachebestand |
|---|---|---|---|---|
| ILT Luchtvaartuigregister | PH-kenmerk → ICAO-code | Tot de ILT een nieuwe weekversie publiceert (elke run gecontroleerd) | 90 dagen na publicatie | `geo/luchtvaartuigregister_ilt.ods` |
| DUO Open Onderwijsdata | Scholen PO/SO/VO/MBO/HO | 90 dagen | 365 dagen | `geo/duo_scholen_po.geojson`, `geo/duo_scholen_overig.geojson` |
| Landelijk Register Kinderopvang | Kinderopvang met bedverblijf | 7 dagen | 30 dagen | `geo/lrk_kinderopvang.csv` |
| Natuurnetwerk Nederland | NNN-geometrie Overijssel | 180 dagen | 365 dagen | `geo/nnn_gebieden.gpkg` |
| BAG WFS v2.0 (PDOK) | Verblijfsobjecten, gebruiksdoelen, pandgeometrie | Direct | — | — |
| PDOK Locatieserver | Reverse en forward geocoding | Direct | — | — |
| PDOK Location API | Begraafplaatsen, maneges | Direct | — | — |
| BRT Top10NL OGC API | Dodenakkervlakken, gebouw- en gebiedspolygonen | Direct | — | — |
| Natura 2000 WFS (PDOK/RVO) | Natura 2000-gebieden | Direct | — | — |
| GeoPortaal Overijssel WFS | Luchthavenregelingen (punten) | Direct | — | — |
| BRT Top10NL `functioneel_gebied_vlak` | Luchthaventerreinen (vlakken) | Direct | — | — |

### 16.1 ILT Luchtvaartuigregister

De ILT plaatst wekelijks een nieuw bestand online, met de publicatiedatum in de bestandsnaam
(`luchtvaartuigregister-ilt-datas2-JJJJ-MM-DD.ods`). Er is geen API: ook de zoekfunctie op ilent.nl
leest dit bestand in de browser in. Bij elke run leest de pipeline de actuele downloadlink van de
[bronpagina](https://www.ilent.nl/documenten/lijsten/luchtvaart/databestanden/luchtvaartregister-data),
met een terugval die de bestandsnaam op datum reconstrueert. Alleen `https`-adressen op `*.ilent.nl`
worden geaccepteerd.

Is de publicatiedatum van de lokale kopie (vastgelegd in `geo/luchtvaartregister.meta.json`) gelijk
aan die van de link, dan wordt de lokale kopie gebruikt; anders wordt de nieuwe versie gedownload.
Zo werkt elke run met de actuele weekversie en blijft het downloaden beperkt tot eens per week.
Lukt het opzoeken of downloaden niet, dan volgt de noodterugval (tot 90 dagen na publicatie).
Het rapport vermeldt publicatie- en downloaddatum.

Het bestand is een ODS-archief; de tabelnaam wisselt en wordt genegeerd. Vóór het opslaan wordt het
archief gecontroleerd (geldig ODS, uitgepakte omvang begrensd). Gelezen kolommen: `Registration` en
`ICAO-code`.

### 16.2 BAG WFS v2.0 (PDOK)

Endpoint: `https://service.pdok.nl/lv/bag/wfs/v2_0`, featuretypes `bag:verblijfsobject` en
`bag:pand`.

| Opgevraagd veld | Waarvoor |
|---|---|
| `identificatie` | Koppeling met LRK en DUO |
| `gebruiksdoel` | Bepaling geluidgevoeligheid |
| `openbare_ruimte`, `huisnummer`, `huisletter`, `toevoeging`, `postcode`, `woonplaats` | Adresopbouw |
| `pandidentificatie` | Ophalen van de pandgeometrie voor de gevelcontrole |

De veldnamen wijken af van de camelCase-varianten die in sommige BAG-documentatie staan.
Verblijfsobjecten worden in WGS84 opgevraagd, pandgeometrieën komen terug in RD.

### 16.3 Landelijk Register Kinderopvang

`https://www.landelijkregisterkinderopvang.nl/opendata/export_opendata_lrk.csv` — ongeveer 12 MB en
31.800 rijen, gefilterd op opvangtype KDV en gekoppeld via `bag_id` aan de BAG-identificatie. De
server weigert de standaard opvraging van de HTTP-bibliotheek met een foutcode; er wordt daarom een
browser-achtige identificatie meegestuurd. Mislukt de download alsnog, dan valt de pipeline terug op
de bestaande cache tot de noodterugvalgrens, en meldt dat.

### 16.4 DUO Open Onderwijsdata

Vijf datasets (PO, SO, VO, MBO, HO) via `https://onderwijsdata.duo.nl/datastore/dump/{resource-id}`,
gefilterd op provincie Overijssel. DUO levert geen BAG-koppeling; adressen worden via de PDOK
Locatieserver omgezet naar een verblijfsobject-identificatie en een RD-punt. Het basisonderwijs
wordt apart gecacheerd omdat het vaker muteert; de overige vier zijn samengevoegd. Invalidatie
gebeurt op een SHA-256-hash van het bronbestand. Het GeoJSON-bestand wordt pas vervangen als alle
datasets van de groep volledig zijn opgehaald en gegeocodeerd.

### 16.5 Natuurnetwerk Nederland

Er is geen rechtstreekse bevragingsdienst. De GML (± 185 MB) wordt eenmalig gedownload, omgezet naar
RD en als GeoPackage bewaard. Het bestand bevat uitsluitend geometrie — 46.792 gebieden — en geen
namen; zie [15.7](#157-natura-2000-en-natuurnetwerk-nederland).

### 16.6 Overige endpoints

```
PDOK Locatieserver   https://api.pdok.nl/bzk/locatieserver/search/v3_1/{reverse,free}
PDOK Location API    https://api.pdok.nl/kadaster/location-api/v1/search
BRT Top10NL          https://api.pdok.nl/brt/top10nl/ogc/v1_0/collections/terrein_vlak/items
Natura 2000          https://service.pdok.nl/rvo/natura2000/wfs/v1_0  (laag natura2000:natura2000)
NNN (ATOM)           https://service.pdok.nl/provincies/natuurnetwerk-nederland/atom/downloads/inspire-pv-ps.nlps-nnn.gml
Luchthavens          https://services.geodataoverijssel.nl/geoserver/B64_nutsvoorzieningen/wfs
Luchthaventerreinen  https://api.pdok.nl/brt/top10nl/ogc/v1_0/collections/functioneel_gebied_vlak/items
Kaarttegels          https://service.pdok.nl/hwh/luchtfotorgb/... en https://service.pdok.nl/brt/achtergrondkaart/...
```

Alle endpoints staan in `tug_config.py`; dat bestand is de enige plek waar URL's, toegestane hosts,
omvanggrenzen, noodterugvalgrenzen, drempelwaarden en marges zijn vastgelegd.

## 17. Architectuur

```
tug_gui.py  (optionele grafische schil)
    │  schrijft tmp/<naam>_<timestamp>.json en roept tug_run.py aan
    ▼
aanvraag.json  (enkel object of array)
    │  tug_run.py — orchestrator: schema, stappen als apart proces, exitcode
    │  [per aanvraag]
    ├── tug_01_validatie.py       processtap 2 — volledigheid en termijn
    ├── tug_02_classificatie.py   processtap 3 — register, NLR-tabel, toetsingsafstand
    ├── tug_03_ruimtelijk.py      processtap 4 — ruimtelijke analyse
    │       ├── tug_bronnen_bag.py        BAG WFS, gevelcontrole, deduplicatie
    │       ├── tug_bronnen_brt.py        begraafplaatsen, maneges, luchthavens
    │       ├── tug_bronnen_natuur.py     Natura 2000, NNN
    │       ├── tug_bronnen_onderwijs.py  kinderopvang en scholen
    │       ├── tug_bronnen_geocode.py    reverse en forward geocoding
    │       ├── tug_geo.py                coördinaattransformaties en geometrie
    │       └── tug_03_kaart.py           kaartopbouw uit tegels en lagen
    └── tug_05_output.py          processtap 6 — PDF-rapport en HTML-kaart
    │
    ▼  tug_state.json gewist (altijd, ook bij een onderbreking)

tug_config.py       alle constanten, endpoints, hosts, grenzen en invoerregels; workflowversie
tug_http.py         de enige toegangsweg naar externe bronnen
tug_bronstatus.py   bronregister: hoe elke bevraging afliep, en wat dat betekent
tug_aanvraag.py     schema van de aanvraag-JSON
tug_opslag.py       state en tijdelijke invoer met privérechten
tug_omgeving.py     herstart in .venv; waarschuwing bij een verlopen beveiligingscontrole
tug_types.py        type-aliassen; Bevindingen en VboOordeel
tug_logging.py      logboekopbouw, terminaluitvoer en voortgangstellers
beveiligingscontrole.py  pip-audit, lockfile, ruff, bandit, mypy en gitleaks in één commando
gui/                kaart en stijlblad van de schil, sjabloon van de HTML-export, gui/vendor/leaflet
beheer/             systemd-timer voor de maandelijkse beveiligingscontrole
test/               regressietest, eenheidstests, beveiligingstests en rooktest op de schil
pyproject.toml      configuratie van ruff, bandit, mypy, pytest en uv
requirements*.in    directe afhankelijkheden; requirements*.txt gegenereerd, met hashes
```

De stappen communiceren via `tug_state.json`: elke stap leest de state, vult zijn eigen sectie aan
en schrijft het bestand terug. Omdat elke stap een apart proces is, kan een afgebroken run met
`--vanaf` worden hervat zonder het voorgaande werk te herhalen.

### 17.1 Modulerollen

| Module | Rol |
|---|---|
| `tug_run.py` | Orchestrator; toetst de invoer, start de stappen, bepaalt de exitcode, wist de state |
| `tug_01_validatie.py` | Volledigheid, datumformaten, termijn, puntlocaties |
| `tug_02_classificatie.py` | Register ophalen en cachen, NLR-opzoeking, maatgevende norm |
| `tug_03_ruimtelijk.py` | Coördineert alle bronnen, bouwt adresrijen en kaartlagen |
| `tug_bronnen_bag.py` | BAG-verblijfsobjecten en -panden, gevelcontrole, deduplicatie |
| `tug_bronnen_brt.py` | Begraafplaatsen, maneges, luchthavens |
| `tug_bronnen_natuur.py` | Natura 2000 en de NNN-GeoPackage |
| `tug_bronnen_onderwijs.py` | Kinderopvang en scholen |
| `tug_bronnen_geocode.py` | Adressen opzoeken bij coördinaten en omgekeerd |
| `tug_geo.py` | Coördinaattransformaties, puntlocatienormalisatie, bbox-berekeningen |
| `tug_03_kaart.py` | Kaarttegels ophalen, zones en objecten tekenen |
| `tug_05_output.py` | PDF-rapport en interactieve kaart |
| `tug_config.py` | Constanten, endpoints, toegestane hosts, omvang- en noodterugvalgrenzen, marges, workflowversie |
| `tug_http.py` | Alle uitgaande aanroepen: https en toegestane hosts (ook na redirects), omvanggrens, fouten als uitzondering |
| `tug_bronstatus.py` | Bronregister; per bron of een storing de toetsing afbreekt of zichtbaar blijft |
| `tug_aanvraag.py` | Structuurcontrole van de aanvraag |
| `tug_opslag.py` | Schrijven van state en tijdelijke invoer met rechten voor alleen de eigenaar |
| `tug_omgeving.py` | Herstart in de projectomgeving; meldingen over de laatste beveiligingscontrole |
| `tug_logging.py` | Logboekregels verzamelen voor het rapport en kleuren in de terminal |
| `tug_gui.py` | Grafische schil; stelt de aanvraag samen en start `tug_run.py` |
| `tug_types.py` | Type-aliassen en de twee records van de ruimtelijke stap: `Bevindingen` (wat er in de omgeving is aangetroffen) en `VboOordeel` (wat dat per verblijfsobject betekent) |

### 17.2 tug_state.json

| Sectie | Gevuld door | Inhoud |
|---|---|---|
| `aanvraag` | `tug_run.py` | De aanvraag, met genormaliseerde vluchtdata |
| `validatie` | `tug_01_validatie.py` | Fouten, waarschuwingen, termijncontrole, logregels |
| `classificatie` | `tug_02_classificatie.py` | Per luchtvaartuig ICAO-code, appendix, norm en bron; maatgevende norm; luchtvaartuigen zonder norm |
| `classificatie.bronstatus` | `tug_02_classificatie.py` | Uitkomst van het ophalen van het ILT-register |
| `ruimtelijk` | `tug_03_ruimtelijk.py` | Adresrijen per categorie, kaartlagen, paden naar de gerenderde kaarten, `bronstatus` per bron |
| `logboek` | Alle stappen | Tijdgestempelde regels per stap |

Het bestand wordt na elke aanvraag overschreven met `{}` en is alleen leesbaar voor de eigenaar.

## 18. Uitvoer

Beide bestanden komen in `output/`:

| Bestand | Inhoud |
|---|---|
| `tug_rapport_{naam}_{timestamp}.pdf` | Proceslogboek in twaalf paragrafen, adressenlijst, en situatie- en omgevingskaart — elk zowel als luchtfoto als topografisch, dus vier kaartpagina's |
| `tug_kaart_{naam}_{timestamp}.html` | Interactieve kaart met alle geïnventariseerde objecten en zones; luchtfoto als ondergrond, omschakelbaar naar de topografische kaart |

Op alle kaarten staat rond elke puntlocatie een lichtblauwe cirkel met de marge rond de puntlocatie (standaard 10 m,
zie `toeslag_m`) die bij de Lden-afstand wordt opgeteld; bij een toeslag van 0 m vervalt de cirkel. Op de omgevingskaart is die cirkel op kaartschaal kleiner dan het
kruissymbool; daar wordt hij met een minimale doorsnede getekend, zodat hij zichtbaar blijft.

`{naam}` is het `naam`-veld uit de aanvraag, teruggebracht tot bestandsnaamveilige tekens en maximaal
zestig posities. Ontbreekt het veld, dan vervalt dat deel van de bestandsnaam.

**Onvolledige toetsing.** Is een bron niet volledig geraadpleegd, dan staat dat in rood bovenaan het
proceslogboek, in de paragraaf van die bron (die dan geen conclusie trekt), boven de adressenlijst en
als rode balk op de HTML-kaart. Per paragraaf staat ook wanneer een lokale kopie is gebruikt, met
haar ouderdom.

**Adressenlijst.** Drie secties — wettelijk relevant, aandachtslocaties, overig — met per regel
adres, postcode en woonplaats, het gebruiksdoel of de bron die het object aanmerkt, en waar van
toepassing de afstand. De margeband staat niet in de lijst.

**Kaarten.** De luchtfoto is de primaire achtergrond; de topografische kaart wordt met verhoogd
contrast gerenderd als aanvulling. BAG-objecten worden als gevelcontour getekend, gekleurd naar de
zwaarste categorie waarin ze vallen. Elke puntlocatie krijgt een kruis, de toetsingsafstand een rode
omtrek en het aandachtsgebied een gele, beide met een label.

---
---

# Deel C — Beheer

## 19. Kwaliteitsborging

Vier soorten controle, met elk een eigen vraag. De regressietest vraagt of de code na een upgrade
nog hetzelfde antwoord geeft; de eenheidstests vragen of dat antwoord juist is; ruff, bandit en mypy
vragen of de code aan de eigen stijl-, type- en beveiligingsafspraken voldoet; de
beveiligingscontrole vraagt of de afhankelijkheden en de geschiedenis nog schoon zijn.

```bash
.venv/bin/python -m pip install --require-hashes --no-build-isolation \
    --only-binary=:all: --no-binary=odfpy -r requirements-dev.txt   # eenmalig
.venv/bin/python test/test_regressie.py                   # omgevingsdrift
.venv/bin/pytest test                                     # beslisregels, bronfouten en schil
.venv/bin/ruff check .                                    # stijl, fouten en beveiligingsregels
.venv/bin/mypy                                            # typecontrole
.venv/bin/python beveiligingscontrole.py                  # alles, plus pip-audit en gitleaks
```

### 19.1 Regressietest

```bash
.venv/bin/python test/test_regressie.py
```

De test stelt niet vast of de pipeline *werkt*, maar of zij **hetzelfde antwoord geeft** als op het
moment dat de uitkomst is gecontroleerd. Zij meet zeventig waarden in tien groepen —
coördinaattransformaties, geometrie, geo-bestandsinvoer met bbox-filter, puntlocatievalidatie,
datumlogica, de NLR-normtabel, afgeleide constanten, het opzoekmechanisme van het register, de
afhandeling van luchtvaartuigen zonder norm en de structuur van de natuursignalering — en vergelijkt
die met een vastgelegde nulmeting in `test/golden/referentie.json`. Die nulmeting legt ook vast met
welke Python-, PROJ-, GDAL- en pakketversies zij is gemaakt, zodat een verschil te plaatsen is.

Twee ontwerpkeuzes bepalen waar de test wel en niet op reageert:

1. **Tolerantie op meterniveau** (1 m) in plaats van exacte gelijkheid. Afstanden worden in hele
   meters gerapporteerd en de signaleringsgrenzen liggen op honderden meters; ruis onder een meter
   verandert geen document. Waarden worden preciezer vastgelegd dan vergeleken.
2. **Brondata blijft buiten de vergelijking.** BAG, LRK, DUO, PDOK en het ILT-register zijn van hun
   bronhouders; een gewijzigd rijaantal of een hertekende gebiedsgrens is geen regressie. Waar de
   bibliotheken toch op geometrie en geo-invoer getoetst moeten worden, gebeurt dat op
   **synthetische geometrie** die de test zelf opbouwt en die met de hand na te rekenen is. Van
   brongebonden code wordt alleen getoetst dat het *mechanisme* werkt — kolommen gevonden, opzoeking
   in het verwachte formaat, afgesproken retourstructuur — nooit welke waarden de bron nu bevat.

**Werkwijze bij een upgrade:** pins aanpassen, installeren, test draaien. Geen verschillen betekent
veilig; wel verschillen betekent per meting beoordelen of het een verbetering of een regressie is.
`--herijk` legt de nulmeting opnieuw vast en is uitsluitend bedoeld voor een bewuste, goedgekeurde
wijziging — herijken om een onverklaarde afwijking weg te poetsen maakt de test waardeloos. Het
script weigert te herijken zolang een meting mislukt.

**Wat niet gedekt is:** de PDF-generatie (het eindproduct zelf, en daarmee het grootste gat: een
upgrade van de PDF-bibliotheek kan de opmaak stil veranderen) en de bronparsers — dekking daarvan
vereist bevroren voorbeeldresponsen.

### 19.2 Eenheidstests

```bash
.venv/bin/pytest test
```

`test/test_eenheden.py` toetst de beslisregels zelf, op synthetische invoer en zonder netwerk: de
puntlocatieregels, de indientermijn, de toetsingsafstand en het zoomniveau, de bestandsnaam-slug, de
featuresleutel, en de classificatie van verblijfsobjecten in wettelijk relevant, margeband en
overig — inclusief de begraafplaatsuitsluiting en de aanname dat een Natura 2000-treffer ook een
NNN-treffer is. Verder: de melding over de onderlinge afstand tussen puntlocaties (gelijk voor elke afstand), de
handmatige geluidsafstand en haar schemagrenzen, de instelbare marge rond de puntlocatie, het
doorrekenen zonder herleidbare norm, en de luchthaventerreinen (samenvoegen van overlappende
vlakken, afstand tot de rand, koppeling met de provinciale regeling, naamgeving) op een
nagebootste bron.

Eén groep tests bewaakt daarbij iets dat eerder is misgegaan: dat de PDF-tabel, de HTML-markers en
de PNG-kaart **hetzelfde oordeel** laten zien. Alle drie lezen het oordeel dat
`_bouw_classificatie_context()` één keer velt; de tests vergelijken de uitkomsten van de
presentatiefuncties rechtstreeks met elkaar.

`test/test_beveiliging.py` bewaakt de uitkomsten van de beveiligingsaudit. Met een nepnetwerk — er
gaat geen verkeer naar buiten — laat hij elke bron uitvallen en controleert hij dat de kernbronnen
de toetsing afbreken, dat de overige bronnen als niet geraadpleegd in het register en in het rapport
komen (zonder conclusie), dat een verouderde lokale kopie met haar ouderdom wordt gemeld en niet
wordt overschreven, dat redirects en verwijzingen naar vreemde hosts of over `http` niet worden
gevolgd, dat te grote antwoorden worden afgebroken, dat een EPS-bestand niet als kaarttegel wordt
geopend, dat JSON in de HTML-kaart de scripttag niet kan sluiten, dat een onverwerkbare aanvraag
wordt geweigerd, en dat de state bij elke afloop — ook Ctrl+C — wordt gewist met privérechten.

`test/test_gui.py` is een rooktest op de schil: hij bouwt het venster zonder scherm op
(`QT_QPA_PLATFORM=offscreen`), vult het, en controleert dat de knop pas vrijkomt als de aanvraag
compleet is, dat een dubbel registratiekenmerk wordt geweigerd, dat RD-invoer op dezelfde plek
uitkomt als GPS-invoer, dat de afstands- en termijnmeldingen verschijnen, en dat de aanvraag die het
venster oplevert door de validatiestap wordt geaccepteerd. Daarnaast dat de kaart geen lokale
bestanden kan lezen, geen externe scripts laadt en links naar de systeembrowser stuurt, en dat
`tmp/` bij het opstarten wordt geleegd.

### 19.3 Stijl- en beveiligingscontrole

`pyproject.toml` legt vast waar ruff, bandit, mypy en pytest op letten, zodat een controle op elke
machine hetzelfde oordeel geeft. De ruff-regelset staat op E, F, W, B, SIM, UP, C4, RET, ARG, PTH en N,
aangevuld met S (beveiligingsregels), BLE (geen blinde `except Exception`) en T20 (geen `print()`
buiten het installatiescript en de regressietest), met een regellengte van 100 tekens; de
uitzonderingen staan met reden in het bestand of op de regel zelf. De subprocess-aanroepen naar
`git` en `python` gaan met een argumentenlijst en zonder shell; bandit meldt ze als laag risico.
`ruff format` wordt bewust niet toegepast: de uitgelijnde toekenningsblokken zouden verdwijnen.

mypy controleert alle modules en tests; de geo-bibliotheken, reportlab en odfpy leveren geen
typeinformatie en worden als ongetypeerd behandeld.

`.pre-commit-config.yaml` draait ruff en gitleaks vóór elke commit (`.venv/bin/pre-commit install`).

### 19.4 Codereview

De codebase is twee keer doorgelicht aan de hand van een gelaagd reviewprotocol op basis van de
SOLID-principes, de code smells uit Fowler's *Refactoring*, *Clean Code* en PEP 8. De eerste ronde
leverde elf bevindingen op; de belangrijkste ingreep was het opsplitsen van een module van bijna
1.400 regels in zes gespecialiseerde bronmodules. De tweede ronde leverde achttien bevindingen op.
Daarvan waren de zwaarste dat het classificatieoordeel op drie plaatsen afzonderlijk werd uitgerekend
en dat verzamelingen aan elkaar werden geknoopt via `id()` — geldig zolang elke lijst exact dezelfde
objecten bevat, en stil onjuist zodra dat niet meer zo is. Beide zijn verholpen; de uitwerking staat
in de interne projectdocumentatie.

### 19.5 Beveiligingsreview

De volledige codebase, de afhankelijkheden en de versiegeschiedenis zijn in september 2026
doorgelicht in een zelfstandige security-audit. Toetsingskader: OWASP Top 10:2025, OWASP ASVS 5.0 en
CWE, aangevuld met de specifieke eisen voor Python-datapijplijnen en desktopschillen; ernst in
CVSS v4.0, kwetsbare afhankelijkheden geprioriteerd op CISA KEV, bereikbaarheid en EPSS. Alle
bevindingen zijn met een reproductie bevestigd, daarna verholpen en opnieuw getest. De uitwerking,
de scanrapporten en de softwarestuklijst (SBOM) staan in de interne projectdocumentatie.

**Dreigingsmodel.** Een lokaal draaiende pipeline zonder netwerkinterface, accounts of
API-sleutels, waarvan alle uitgaande aanroepen naar Nederlandse overheidsbronnen gaan, plus een
ingebedde browsercomponent in de grafische schil. Het grootste risico vraagt geen aanvaller: het
gedrag wanneer een bron niet reageert.

| Bevinding | Ernst | Maatregel |
|---|---|---|
| Een uitgevallen bron leverde in het rapport dezelfde tekst op als "niets gevonden", en de run eindigde als geslaagd | Hoog (8.2) | Bronregister; kernbronnen breken de toetsing af, overige bronnen geven een rode melding zonder conclusie en exitcode 2 ([§16](#16-gegevensbronnen-en-caching)) |
| De beeldbibliotheek pillow 12.2.0 had dertien bekende kwetsbaarheden, twee bereikbaar via kaarttegels | Middel (6.3) | pillow 12.3.0; kaarttegels alleen als PNG of JPEG geopend |
| Verwijzingen in bronantwoorden werden deels zonder controle gevolgd; `http://` werd niet geweigerd | Middel (6.3) | Eén toegangsweg (`tug_http.py`) met `https` en een vaste hostlijst, ook na redirects |
| Geen schemacontrole van de aanvraag; `straal_override` zonder grenzen | Middel (4.8) | Schema bij het inlezen ([§12](#12-aanvraag-json)); `straal_override` vervallen |
| De kaart in de schil kon naar een externe site navigeren die de koppeling met Python erfde, en mocht lokale bestanden lezen | Laag (2.1) | Navigatie geblokkeerd, links naar de systeembrowser; Leaflet meegeleverd; geen uitzonderingen voor lokale inhoud |
| State en tijdelijke invoer bleven staan na een onderbreking; ruime bestandsrechten | Laag (2.0) | Opruimen bij elke afloop en bij sluiten en opstarten van de schil; rechten alleen voor de eigenaar |

Verbeterpunten zonder aantoonbaar aanvalspad, eveneens doorgevoerd: een omvanggrens per bron;
lockfiles met hashes voor alle, ook indirecte, afhankelijkheden; een geautomatiseerde
beveiligingscontrole (vóór elke commit, maandelijks en vóór een productierun); de projectomgeving als
enige omgeving; het ongebruikte `lxml` verwijderd en `defusedxml` expliciet vastgelegd; volledige
escaping van JSON in de HTML-kaart; en een invulhint die persoonsnamen buiten de bestandsnamen houdt.
Aan de codekwaliteit: mypy zonder fouten, ruff met beveiligingsregels, specifieke uitzonderingen in
plaats van `except Exception`, en voortgangstellers die alleen in een terminal schrijven.

**Restrisico.** Wat na de hertest bewust openstaat:

- Het stoppen van een lopende run en de herstart in `.venv` zijn alleen onder Linux beproefd, niet
  onder Windows.
- De HTML-kaart haalt Leaflet bij het openen van `unpkg.com`, vastgepind met Subresource Integrity
  ([§5.3](#53-waar-gegevens-naartoe-gaan)).
- De ingebedde browser krijgt beveiligingsupdates alleen via een nieuwe PySide6-versie, en `odfpy`
  wordt niet meer onderhouden ([§20](#20-onderhoud-en-houdbaarheid), [§9.3](#93-dependencies)).

**Eerder verholpen** (codereview september 2026):

| Bevinding | Maatregel |
|---|---|
| JSON in een scripttag in de HTML-uitvoer kon de tag voortijdig sluiten | Escaping toegepast op alle JSON die in HTML wordt ingebed |
| De downloadlink van het ILT-register werd van een webpagina gelezen zonder domeincontrole | Alleen adressen op `*.ilent.nl` worden geaccepteerd |
| Polygoonverwijzingen uit de PDOK Location API werden zonder controle gevolgd | Toegestane domeinen vastgelegd: `api.pdok.nl` en `geodata.nationaalgeoregister.nl` |
| Het ODS-archief werd uitgepakt zonder groottelimiet | Limiet van 50 MB ongecomprimeerd |
| Meldingen buiten het logboeksysteem om | Meldingen lopen via het logboek; alleen voortgangstellers schrijven rechtstreeks naar de terminal |

**Herhalen.** Kwetsbaarheden in afhankelijkheden verschijnen ook zonder codewijziging. Eén commando
doet de hele controle en legt de uitkomst vast in `.beveiligingscontrole.json`:

```bash
.venv/bin/python beveiligingscontrole.py
```

Het draait pip-audit op de geïnstalleerde pakketten (en schrijft een SBOM), vergelijkt de omgeving
met de lockfiles, en draait ruff, bandit, mypy en gitleaks over de hele geschiedenis. Een onderdeel
dat niet kan draaien telt als bevinding. `tug_run.py` meldt bij de start als de uitkomst ontbreekt,
ouder is dan 31 dagen of bevindingen bevat.

| Wanneer | Hoe |
|---|---|
| Vóór elke commit | pre-commit: ruff en gitleaks |
| Maandelijks | systemd-timer (Linux): `cp beheer/tug-beveiligingscontrole.* ~/.config/systemd/user/`, daarna `systemctl --user enable --now tug-beveiligingscontrole.timer`. Onder Windows: een taak in Taakplanner die `.venv\Scripts\python.exe beveiligingscontrole.py` in de projectmap start |
| Vóór een productierun | Handmatig, of de melding van `tug_run.py` volgen |

gitleaks is geen Python-pakket. Installeer de release-binary van
[github.com/gitleaks/gitleaks](https://github.com/gitleaks/gitleaks/releases) na controle tegen het
bijbehorende checksumbestand, en zet hem op het PATH.

### 19.6 Bronnenaudit

Alle eenentwintig externe aanroepen en datasets zijn in één ronde nagelopen tegen de live bronnen.
Dat leverde twee stille storingen op — een register dat de standaard opvraging weigerde, en een
begraafplaatsdetectie die na een herstructurering de verkeerde collectie bevroeg en sindsdien altijd
nul resultaten gaf. Beide zijn verholpen en gevalideerd met een volledige batch.

Dat is de reden dat het proceslogboek per bron rapporteert wat zij heeft opgeleverd: een detectielaag
die niets vindt is niet te onderscheiden van een detectielaag die stuk is, tenzij het rapport
vermeldt dat zij is geraadpleegd. Het bronregister gaat een stap verder: het rapport trekt geen
conclusie uit een bron die niet volledig is geraadpleegd ([§16](#16-gegevensbronnen-en-caching)).

## 20. Onderhoud en houdbaarheid

| Onderdeel | Wat het onderhoud vraagt |
|---|---|
| **Python** | Versie 3.12 heeft ondersteuning tot oktober 2028. CPython kent geen langetermijnversies: elke minor versie krijgt ongeveer twee jaar bugfixes en daarna drie jaar beveiligingsupdates. De opvolging is dus een geplande handeling, en precies waar de regressietest voor is gebouwd |
| **Bibliotheken** | Exacte pins met hashes, ook voor indirecte afhankelijkheden; bijwerken is een bewuste handeling via de `.in`-bestanden, met de regressietest als controle ([§9.3](#93-dependencies)). Let vooral op de HTTP- en beeldbibliotheek, waar beveiligingslekken het vaakst voorkomen. Een pin veroudert ook zonder codewijziging; de maandelijkse beveiligingscontrole meldt dat ([§19.5](#195-beveiligingsreview)) |
| **Ingebedde browser en Leaflet** | QtWebEngine krijgt zijn Chromium-beveiligingsupdates via de PySide6-versie; pip-audit ziet die niet, dus PySide6 ook zonder melding periodiek bijwerken. De meegeleverde Leaflet in `gui/vendor/leaflet` moet dezelfde zijn als de vastgepinde versie in `gui/kaart_export.html`; een test bewaakt dat |
| **NLR-indelingslijst** | Staat in de code en wordt met de hand bijgewerkt wanneer NLR of ILT de lijst wijzigt |
| **Afstandsnormen en marges** | Alle drempelwaarden staan in `tug_config.py`; een beleidswijziging is daar één aanpassing, met de regressietest als controle op de gevolgen |
| **Externe bronnen** | Endpoints en bestandsformaten veranderen zonder aankondiging. Uitval wordt gemeld (afgebroken toetsing of exitcode 2), een formaatwijziging die nul resultaten oplevert niet altijd; een periodieke controle van alle endpoints tegen de live bronnen blijft de manier om stille uitval op te sporen. Wijzigt een host, dan ook `BRON_HOSTS` in `tug_config.py` bijwerken |
| **Bewaartermijnen** | Verlopen caches worden automatisch vernieuwd; handmatig vernieuwen kan door het betreffende bestand in `geo/` te verwijderen |

## 21. Licentie en eigenaarschap

Intern gebruik Provincie Overijssel. Niet bestemd voor publieke distributie.

**Versionering.** Git is de enige versiegeschiedenis; er zijn geen handmatige versienummers. Elk
voortbrengsel — runlogboek, PDF, HTML en de tussentijdse state — draagt de workflowversie: de korte
commit-hash, met de toevoeging *(niet-gecommitte wijzigingen)* wanneer er lokaal gewijzigde
bestanden zijn.
