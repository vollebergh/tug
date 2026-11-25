# Installatie onder Windows 11

Stap voor stap, zonder adminrechten. Alle opdrachten zijn voor **PowerShell** (Start → typ
`PowerShell` → Windows PowerShell); ze werken ook in de Opdrachtprompt. Reken op een kwartier,
waarvan het meeste wachten is. Benodigd: ongeveer 1,5 GB schijfruimte.

## 1. Python controleren

Vereist is **Python 3.12.10, 64-bit**, het installatieprogramma *Windows installer (64-bit)* van
python.org. Niet 3.12.11 of hoger (die bestaan niet als Windows-installatie), niet ARM64, niet de
Microsoft Store-versie.

```powershell
py -3.12 --version
```

Verwacht: `Python 3.12.10`. Zie [Problemen](#problemen) als `py` niet wordt gevonden.

## 2. Scripts downloaden als ZIP

1. Open <https://github.com/vollebergh/tug> → groene knop **Code** → **Download ZIP**.
   Rechtstreeks: <https://github.com/vollebergh/tug/archive/refs/heads/master.zip>
2. Rechtsklik op `tug-master.zip` → **Alles uitpakken…** → kies als doel je gebruikersmap,
   `C:\Users\<jouw naam>` → **Uitpakken**.
3. Hernoem de map `C:\Users\<jouw naam>\tug-master` naar **`TUG`**.

Zorg dat in `C:\Users\<jouw naam>\TUG` direct `install.py` en `tug_gui.py` staan, niet nog een
map `tug-master`. Gebruik bewust dit korte pad: niet `Downloads`, `Documenten` of een
OneDrive-map. Windows weigert bestandspaden langer dan 260 tekens en de grafische bibliotheek bevat
diepe paden; OneDrive zou bovendien 1,2 GB aan bibliotheken gaan synchroniseren.

## 3. Installeren

```powershell
cd $env:USERPROFILE\TUG
```

```powershell
py -3.12 install.py
```

Geen flags of parameters. Het script maakt `.venv` in deze map, installeert alle pakketten met
hashcontrole (PySide6 is een grote download; enkele minuten) en controleert daarna elke import.
Klaar is het bij:

```
Installatie voltooid.
```

met daarboven vijftien regels `OK`. Staat er `FOUT` of `MISLUKT`, zie [Problemen](#problemen).
Opnieuw draaien kan altijd; een bestaande `.venv` wordt hergebruikt.

Controleer daarna de workflowversie die in elk rapport komt:

```powershell
.\.venv\Scripts\python.exe -c "import tug_config; print(tug_config.VERSION)"
```

Verwacht: een korte commit-code met `(archief)`, bijvoorbeeld `a1b2c3d (archief)`. Staat er
`onbekend (geen git)`, dan is de map niet uit de GitHub-ZIP afkomstig.

## 4. De grafische interface starten

```powershell
cd $env:USERPROFILE\TUG
```

```powershell
.\.venv\Scripts\python.exe tug_gui.py
```

Geen flags, parameters of omgevingsvariabelen: met hardwareversnelling werkt de kaart zoals hij
is. Naast het venster blijft het PowerShell-venster open; sluit dat niet, want dan sluit de
interface ook. Gebruik `python.exe`, niet `pythonw.exe`.

**Snelkoppeling op het bureaublad (optioneel).** Rechtsklik op het bureaublad → Nieuw →
Snelkoppeling. Als locatie:

```
C:\Users\<jouw naam>\TUG\.venv\Scripts\python.exe tug_gui.py
```

Daarna rechtsklik op de snelkoppeling → Eigenschappen → **Beginnen in**:
`C:\Users\<jouw naam>\TUG`.

**Batch zonder interface**, met een of meer aanvraag-JSON's:

```powershell
.\.venv\Scripts\python.exe tug_run.py aanvraag.json
```

Exitcode 0 = volledig getoetst, 2 = rapport met een bron die niet volledig is geraadpleegd (rood in
de PDF), 1 = afgebroken.

## 5. Eerste run en controle

De eerste run duurt enkele minuten langer: de pipeline downloadt dan ongeveer 210 MB aan bronnen
(o.a. Natuurnetwerk Nederland, het kinderopvangregister en het luchtvaartuigregister) naar `geo\`.
Latere runs gebruiken die kopie.

Na die eerste run kun je controleren of deze machine dezelfde afstanden en classificaties berekent
als de ontwikkelomgeving:

```powershell
.\.venv\Scripts\python.exe test\test_regressie.py
```

Verwacht onderaan: `GEEN VERSCHILLEN`. Bovenaan staat dat Python anders is dan bij de referentie
(3.12.13 → 3.12.10); dat is juist en geen fout.

**Verwachte melding:** *"Er is nog geen beveiligingscontrole vastgelegd."* De maandelijkse
beveiligingscontrole draait op de beheermachine, niet hier. De melding heeft geen invloed op de
toetsing.

De uitvoer (PDF en HTML) staat in `output\`. Tijdelijke invoer in `tmp\` wordt na elke run gewist.

## 6. Bijwerken naar een nieuwe versie

Staat er in de kop van de interface **Nieuwe versie … beschikbaar**, sluit dan de interface en
draai:

```powershell
cd $env:USERPROFILE\TUG
```

```powershell
py -3.12 update.py
```

Gebruik `py -3.12`, niet de Python uit `.venv`. Het script laat zien wat er nieuw is en welke
bestanden veranderen, en vraagt `Bijwerken? [J/n]`: Enter is ja. Je aanvragen, exports (`output\`),
gedownloade bronnen (`geo\`) en de omgeving (`.venv\`) blijven staan. Oude versies van vervangen
bestanden komen in `backup\`. Zijn de pakketten gewijzigd, dan draait `install.py` daarna vanzelf
opnieuw (enkele minuten). Klaar is het bij `Workflow bijgewerkt naar …`; staat er `Je hebt de
nieuwste versie al`, dan was er niets te doen.

**Snelkoppeling "TUG bijwerken" (optioneel)**, voor bijwerken met een dubbelklik. Rechtsklik op het
bureaublad → Nieuw → Snelkoppeling. Als locatie:

```
py.exe -3.12 C:\Users\<jouw naam>\TUG\update.py
```

Naam: `TUG bijwerken`. Windows vult bij `py.exe` zelf het volledige pad in; meldt het dat het
bestand niet gevonden wordt, gebruik dan het pad dat `where.exe py` in PowerShell toont. Het venster
blijft na afloop open tot je op Enter drukt.

**Eerste keer:** een installatie van vóór december 2026 heeft `update.py` nog niet. Open dan
<https://raw.githubusercontent.com/vollebergh/tug/master/update.py>, kies in de browser **Opslaan
als** (Ctrl+S) en sla het bestand op in `C:\Users\<jouw naam>\TUG` als `update.py` (let op dat het
niet `update.py.txt` wordt). Draai het daarna zoals hierboven: het werkt de rest van de map bij.

## Problemen

| Melding | Oplossing |
|---|---|
| `py` wordt niet herkend | De Python-launcher is niet mee-geïnstalleerd. Gebruik het volledige pad: `& "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" install.py` |
| `py -3.12` geeft een andere versie dan 3.12.10 | `py -0p` toont alle geïnstalleerde versies met pad; gebruik het pad van 3.12.10 zoals hierboven |
| pip: `SSL: CERTIFICATE_VERIFY_FAILED` of een proxyfout | Bedrijfsnetwerk. Vraag de IT-afdeling om het proxy-adres en het CA-certificaatbestand; de foutmelding van `install.py` noemt de opdracht |
| pip: `hashes do not match` | Niet omzeilen: het gedownloade pakket is niet het vastgelegde. Opnieuw proberen; blijft het, melden |
| pip: `No such file or directory` met een lang pad | Map staat te diep. Verplaats naar `C:\Users\<jouw naam>\TUG`, verwijder `.venv` en installeer opnieuw |
| Rode melding in de PDF dat een bron niet is geraadpleegd, terwijl internet werkt | Waarschijnlijk certificaatinspectie op het netwerk: de pipeline vertrouwt alleen zijn eigen certificatenbundel. Zet het CA-bestand van IT in `REQUESTS_CA_BUNDLE`: `$env:REQUESTS_CA_BUNDLE="C:\pad\naar\bedrijfs-ca.pem"` en start opnieuw vanuit hetzelfde venster |
| `update.py`: *GitHub is niet bereikbaar* of `CERTIFICATE_VERIFY_FAILED` | Geen internet of een proxy. `update.py` gebruikt de certificaten van Windows; werkt de browser wel, vraag dan IT naar de proxy. Er is niets gewijzigd |
| `update.py`: *Toegang geweigerd … alle bestanden zijn teruggezet* | Er staat nog een bestand open, meestal de interface. Sluit die (ook het PowerShell-venster ernaast) en draai `update.py` opnieuw |
| Kaart in de interface blijft leeg of zwart | Grafische driver. Start eenmalig zonder hardwareversnelling: `$env:QTWEBENGINE_CHROMIUM_FLAGS="--disable-gpu"` en daarna `.\.venv\Scripts\python.exe tug_gui.py` in hetzelfde venster |

Een fout tijdens het gebruik meld je met de knop **Bug rapporteren** rechtsboven in de interface.
