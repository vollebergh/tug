"""
tug_gui.py -- Grafische schil rond de TUG-ontheffingen workflow
Versie: zie git (workflowversie = korte commit-hash, zie tug_config.VERSION)

Gebruik:
    python tug_gui.py

Het venster bestaat uit vier delen met elk een eigen verantwoordelijkheid:
    Aanvraagpaneel        dossier, soort ontheffing, data en tijden
    Luchtvaartuigenpaneel registratiekenmerken
    Puntlocatiespaneel    coördinaten, kaartklikken, onderlinge afstand
    Exportafhandeling     pipeline starten, exportmap, bestanden afleveren
Hoofdvenster zet ze naast elkaar en verbindt ze; het houdt zelf geen
aanvraaggegevens bij.

De invoerregels (indientermijn, maximale onderlinge afstand, de omhullende van
Nederland, het registratiepatroon) staan niet hier maar in tug_config: het
venster moet dezelfde grenzen hanteren als de validatiestap, anders kan het
groen geven waar het rapport straks een gebrek meldt.

Het venster verzamelt de gegevens van één aanvraag, schrijft die naar een
aanvraag-JSON in `tmp/` en start daarmee tug_run.py. Zodra op "Genereer" wordt
gedrukt loopt de pipeline; ondertussen kiest de gebruiker de map waar de HTML-
en PDF-export naartoe moeten. Na afloop worden de exports verplaatst en geopend,
en wordt `tmp/` geleegd: de procesbeschrijving in het PDF-rapport legt de
gebruikte invoer al vast.

De kaart is dezelfde Leaflet-kaart met dezelfde PDOK-tegels als de HTML- en
PDF-export (tug_config.KAART_ACHTERGRONDEN); zie gui/kaart.html.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from pyproj import Transformer

from PySide6.QtCore import (
    QDate, QObject, QSettings, QStandardPaths, Qt, QThread, QTime, QUrl, Signal, Slot,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QIcon, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QTimeEdit, QVBoxLayout, QWidget,
)

from tug_config import (
    GUI_DIR, KAART_ACHTERGRONDEN, MAX_PUNT_AFSTAND_M, MIN_INDIENTERMIJN_DAGEN,
    NL_BBOX, OUTPUT_DIR, REGISTRATIE_PATROON, VERSION,
)
from tug_geo import in_nederland, naam_slug

# ──────────────────────────────────────────────
# Constanten
# ──────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent
TMP_DIR      = SCRIPT_DIR / "tmp"

VENSTER_B, VENSTER_H = 1400, 1000
LINKER_FRACTIE       = 0.33

# Startbeeld van de kaart: midden Overijssel, zoomniveau ~halve provincie
KAART_START_LAT  = 52.46126
KAART_START_LON  = 6.496964
KAART_START_ZOOM = 11

# De datumvelden staan op "vanaf heden" (conform specificatie). Zet op False om
# ook een ondertekening in het verleden te kunnen invoeren.
ONDERTEKENING_VANAF_HEDEN = True

STANDAARD_DAGEN_VOORUIT   = 42   # voorgestelde vluchtdatum: ruim buiten de indientermijn
STANDAARD_AANTAL_VLUCHTEN = 50
AANTAL_STAPPEN            = 4    # tug_run.STAPPEN

# Bereik van de RD-invoervelden (EPSG:28992), ruim om Nederland heen.
RD_X_BEREIK = (0.0, 300000.0)
RD_Y_BEREIK = (300000.0, 640000.0)

SOORTEN_ONTHEFFING = ["locatiegebonden", "generiek"]
TYPEN_LUCHTVAARTUIG = ["heli", "vliegtuig", "MLA", "heteluchtballon"]

_REGISTRATIE_PATROON = re.compile(REGISTRATIE_PATROON)

_NAAR_RD  = Transformer.from_crs("EPSG:4326", "EPSG:28992", always_xy=True)
_NAAR_WGS = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)


def naar_rd(lat: float, lon: float) -> tuple[float, float]:
    return _NAAR_RD.transform(lon, lat)


def naar_wgs(x: float, y: float) -> tuple[float, float]:
    lon, lat = _NAAR_WGS.transform(x, y)
    return lat, lon


def toon_melding(label: QLabel, tekst: str) -> None:
    """Zet een meldingtekst en maak het label zichtbaar."""
    label.setText(tekst)
    label.show()


def leeg_layout(layout) -> None:
    """Verwijder alle widgets uit een layout, zodat hij opnieuw gevuld kan worden."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


# ──────────────────────────────────────────────
# Kleine bouwstenen
# ──────────────────────────────────────────────

class Info(QLabel):
    """Klein i-tje met uitleg in een tooltip."""

    def __init__(self, uitleg: str):
        super().__init__("i")
        self.setObjectName("info")
        self.setAlignment(Qt.AlignCenter)
        self.setToolTip(uitleg)
        self.setCursor(Qt.WhatsThisCursor)


def sectiekop(tekst: str, uitleg: str | None = None) -> QWidget:
    rij = QWidget()
    lay = QHBoxLayout(rij)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(7)
    label = QLabel(tekst)
    label.setObjectName("sectie")
    lay.addWidget(label)
    if uitleg:
        lay.addWidget(Info(uitleg))
    lay.addStretch(1)
    return rij


def veldlabel(tekst: str, uitleg: str | None = None) -> QWidget:
    rij = QWidget()
    lay = QHBoxLayout(rij)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    label = QLabel(tekst)
    label.setObjectName("label")
    lay.addWidget(label)
    if uitleg:
        lay.addWidget(Info(uitleg))
    lay.addStretch(1)
    return rij


def kaartje() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("kaartje")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(16, 14, 16, 16)
    lay.setSpacing(10)
    return frame, lay


def scheiding() -> QFrame:
    lijn = QFrame()
    lijn.setObjectName("scheiding")
    lijn.setFixedHeight(1)
    return lijn


def _teken_streep(pad: Path, punten: list[tuple[float, float]], kleur: str,
                  formaat: int = 16, dikte: float = 1.8) -> None:
    """Tekent een chevron of vinkje als PNG; QSS kan geen vormen tekenen."""
    pm = QPixmap(formaat, formaat)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(kleur), dikte)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    lijn = QPainterPath()
    lijn.moveTo(*punten[0])
    for punt in punten[1:]:
        lijn.lineTo(*punt)
    p.drawPath(lijn)
    p.end()
    pm.save(str(pad))


def iconen() -> dict[str, str]:
    """Genereert de pictogrammen die het stijlblad nodig heeft en geeft hun paden."""
    map_ = Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation)) / "iconen"
    map_.mkdir(parents=True, exist_ok=True)
    neer = [(4, 6.5), (8, 10.5), (12, 6.5)]
    op   = [(4, 10), (8, 6), (12, 10)]
    vink = [(4, 8.4), (6.8, 11.2), (12, 5.4)]
    specificaties = {
        "__PIJL_NEER__":      (neer, "#93a1b3"),
        "__PIJL_NEER_ZWAK__": (neer, "#4a5566"),
        "__PIJL_OP__":        (op,   "#93a1b3"),
        "__PIJL_OP_ZWAK__":   (op,   "#4a5566"),
        "__VINK__":           (vink, "#ffffff"),
    }
    paden = {}
    for sleutel, (punten, kleur) in specificaties.items():
        pad = map_ / (sleutel.strip("_").lower() + ".png")
        _teken_streep(pad, punten, kleur, dikte=2.0 if sleutel == "__VINK__" else 1.8)
        paden[sleutel] = pad.as_posix()
    return paden


def app_icoon() -> QIcon:
    """Tekent het pin-icoon van de kaart als vensterpictogram."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#4b93ff"))
    p.setPen(Qt.NoPen)
    pad = QPainterPath()
    pad.moveTo(32, 58)
    pad.cubicTo(52, 36, 54, 28, 54, 24)
    pad.arcTo(10, 2, 44, 44, 0, 180)
    pad.cubicTo(10, 28, 12, 36, 32, 58)
    p.drawPath(pad)
    p.setBrush(QColor("#0f131a"))
    p.drawEllipse(24, 16, 16, 16)
    p.end()
    return QIcon(pm)


# ──────────────────────────────────────────────
# Rijen in de overzichtsvelden
# ──────────────────────────────────────────────

class LuchtvaartuigRij(QFrame):
    verwijderd = Signal(str)

    def __init__(self, nummer: int, lv: dict):
        super().__init__()
        self.setObjectName("rij")
        self._id = lv["id"]

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 8, 8)
        lay.setSpacing(10)

        nr = QLabel(str(nummer))
        nr.setObjectName("rijNr")
        nr.setAlignment(Qt.AlignCenter)
        lay.addWidget(nr)

        naam = QLabel(lv["registratie"])
        naam.setObjectName("rijTitel")
        lay.addWidget(naam)

        badge = QLabel(lv["type"])
        badge.setObjectName("badge")
        lay.addWidget(badge)

        lay.addStretch(1)

        weg = QPushButton("✕")
        weg.setObjectName("verwijder")
        weg.setToolTip("Luchtvaartuig verwijderen")
        weg.setCursor(Qt.PointingHandCursor)
        weg.clicked.connect(lambda: self.verwijderd.emit(self._id))
        lay.addWidget(weg)


class PuntRij(QFrame):
    verwijderd = Signal(str)
    gekozen    = Signal(str)

    def __init__(self, nummer: int, punt: dict):
        super().__init__()
        self.setObjectName("rij")
        self._id = punt["id"]
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Klik om deze puntlocatie op de kaart te tonen")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 8, 8)
        lay.setSpacing(10)

        nr = QLabel(str(nummer))
        nr.setObjectName("rijNr")
        nr.setAlignment(Qt.AlignCenter)
        lay.addWidget(nr)

        kolom = QVBoxLayout()
        kolom.setContentsMargins(0, 0, 0, 0)
        kolom.setSpacing(1)
        wgs = QLabel(f"{punt['lat']:.6f}, {punt['lon']:.6f}")
        wgs.setObjectName("rijTitel")
        rd = QLabel(f"RD  {punt['x']:.0f} / {punt['y']:.0f}")
        rd.setObjectName("rijSub")
        kolom.addWidget(wgs)
        kolom.addWidget(rd)
        lay.addLayout(kolom)

        lay.addStretch(1)

        weg = QPushButton("✕")
        weg.setObjectName("verwijder")
        weg.setToolTip("Puntlocatie verwijderen")
        weg.setCursor(Qt.PointingHandCursor)
        weg.clicked.connect(lambda: self.verwijderd.emit(self._id))
        lay.addWidget(weg)

    def mousePressEvent(self, event):
        self.gekozen.emit(self._id)
        super().mousePressEvent(event)


# ──────────────────────────────────────────────
# Kaart (Leaflet in QtWebEngine)
# ──────────────────────────────────────────────

class Brug(QObject):
    """Tweerichtingsverbinding tussen de Leaflet-kaart en Python."""

    # Python → JavaScript
    pinsGezet  = Signal(str)
    focusGezet = Signal(float, float)

    # JavaScript → Python
    puntGevraagd    = Signal(float, float)
    puntWegGevraagd = Signal(str)
    kaartGereed     = Signal()

    @Slot(float, float)
    def voegPuntToe(self, lat: float, lon: float) -> None:
        self.puntGevraagd.emit(lat, lon)

    @Slot(str)
    def verwijderPunt(self, punt_id: str) -> None:
        self.puntWegGevraagd.emit(punt_id)

    @Slot()
    def gereed(self) -> None:
        self.kaartGereed.emit()


class Kaartpaneel(QWidget):
    def __init__(self):
        super().__init__()
        self.brug = Brug()

        self.web = QWebEngineView()
        instellingen = self.web.settings()
        instellingen.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        instellingen.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)
        instellingen.setAttribute(QWebEngineSettings.ShowScrollBars, False)

        kanaal = QWebChannel(self.web.page())
        kanaal.registerObject("brug", self.brug)
        self.web.page().setWebChannel(kanaal)
        self.web.page().setBackgroundColor(QColor("#0f131a"))

        config = {
            "start_lat":  KAART_START_LAT,
            "start_lon":  KAART_START_LON,
            "start_zoom": KAART_START_ZOOM,
            "standaard_achtergrond": "topografisch",
            "max_afstand_m": MAX_PUNT_AFSTAND_M,
            "achtergronden": {
                sleutel: {"titel": waarde["titel"].capitalize(),
                          "url": waarde["url"],
                          "bron": waarde["bron"]}
                for sleutel, waarde in KAART_ACHTERGRONDEN.items()
            },
        }
        # Volgorde: topografisch eerst (standaard), luchtfoto tweede
        config["achtergronden"] = {
            k: config["achtergronden"][k]
            for k in ("topografisch", "satelliet") if k in config["achtergronden"]
        }

        html = (GUI_DIR / "kaart.html").read_text(encoding="utf-8")
        html = html.replace("__CONFIG__", json.dumps(config, ensure_ascii=False))
        self.web.setHtml(html, QUrl.fromLocalFile(str(GUI_DIR) + "/"))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.web)


# ──────────────────────────────────────────────
# Pipeline in een achtergrondthread
# ──────────────────────────────────────────────

class Pijplijn(QThread):
    regel = Signal(str)
    klaar = Signal(int)

    def __init__(self, json_pad: Path):
        super().__init__()
        self.json_pad = json_pad

    def run(self) -> None:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
        try:
            proces = subprocess.Popen(
                [sys.executable, str(SCRIPT_DIR / "tug_run.py"), str(self.json_pad)],
                cwd=str(SCRIPT_DIR), env=env, text=True, bufsize=1,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace",
            )
        except OSError as fout:
            self.regel.emit(f"FOUT: pipeline kon niet worden gestart — {fout}")
            self.klaar.emit(1)
            return

        for regel in proces.stdout:
            # Voortgangstellers schrijven met \r over dezelfde regel heen. Zonder
            # \n arriveert zo'n hele reeks hier als één lange regel; alleen de
            # laatste stand is nog interessant.
            self.regel.emit(regel.rstrip().rsplit("\r", 1)[-1])
        self.klaar.emit(proces.wait())


# ──────────────────────────────────────────────
# Voortgangssluier
# ──────────────────────────────────────────────

class Sluier(QWidget):
    """Halftransparante laag over het venster met live pipeline-uitvoer."""

    def __init__(self, ouder: QWidget):
        super().__init__(ouder)
        self.setObjectName("sluier")
        # Zonder WA_StyledBackground tekent een kaal QWidget zijn QSS-achtergrond niet.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.hide()

        buiten = QVBoxLayout(self)
        buiten.setContentsMargins(0, 0, 0, 0)
        buiten.addStretch(1)

        midden = QHBoxLayout()
        midden.addStretch(1)

        self.paneel = QFrame()
        self.paneel.setObjectName("voortgang")
        self.paneel.setFixedWidth(760)
        lay = QVBoxLayout(self.paneel)
        lay.setContentsMargins(26, 22, 26, 22)
        lay.setSpacing(12)

        self.titel = QLabel("Pipeline draait …")
        self.titel.setObjectName("voortgangTitel")
        lay.addWidget(self.titel)

        self.stap = QLabel("Aanvraag wordt voorbereid")
        self.stap.setObjectName("voortgangStap")
        lay.addWidget(self.stap)

        self.balk = QProgressBar()
        self.balk.setRange(0, AANTAL_STAPPEN)
        self.balk.setValue(0)
        self.balk.setTextVisible(False)
        lay.addWidget(self.balk)

        self.logboek = QPlainTextEdit()
        self.logboek.setObjectName("logboek")
        self.logboek.setReadOnly(True)
        self.logboek.setMinimumHeight(320)
        lay.addWidget(self.logboek)

        knoppen = QHBoxLayout()
        knoppen.addStretch(1)
        self.sluit = QPushButton("Sluiten")
        self.sluit.setCursor(Qt.PointingHandCursor)
        self.sluit.clicked.connect(self.hide)
        self.sluit.setEnabled(False)
        knoppen.addWidget(self.sluit)
        lay.addLayout(knoppen)

        midden.addWidget(self.paneel)
        midden.addStretch(1)
        buiten.addLayout(midden)
        buiten.addStretch(1)

    def start(self, dossier: str) -> None:
        self.titel.setText(f"Pipeline draait — {dossier}")
        self.stap.setText("Aanvraag wordt voorbereid …")
        self.logboek.clear()
        self.balk.setValue(0)
        self.sluit.setEnabled(False)
        self.resize(self.parentWidget().size())
        self.show()
        self.raise_()

    def voeg_regel_toe(self, regel: str) -> None:
        self.logboek.appendPlainText(regel)
        if "Start: " in regel:
            self.balk.setValue(min(self.balk.value() + 1, AANTAL_STAPPEN))
            self.stap.setText(regel.split("Start: ", 1)[1])

    def afgerond(self, titel: str, stap: str, geslaagd: bool) -> None:
        self.titel.setText(titel)
        self.stap.setText(stap)
        self.balk.setValue(AANTAL_STAPPEN if geslaagd else self.balk.value())
        self.sluit.setEnabled(True)


# ──────────────────────────────────────────────
# Hoofdvenster
# ──────────────────────────────────────────────

# ──────────────────────────────────────────────
# Aanvraaggegevens
# ──────────────────────────────────────────────

class Aanvraagpaneel(QWidget):
    """De vaste gegevens van de aanvraag: dossier, soort, data en tijden.

    Kent alleen zijn eigen velden. De termijnbewaking komt uit tug_config, zodat
    het venster niet groen kan geven waar de validatiestap een gebrek meldt.
    """

    gewijzigd = Signal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        frame, vak = kaartje()
        lay.addWidget(frame)
        vak.addWidget(sectiekop("Aanvraaggegevens"))

        rooster = QGridLayout()
        rooster.setContentsMargins(0, 0, 0, 0)
        rooster.setHorizontalSpacing(10)
        rooster.setVerticalSpacing(8)
        rij = 0

        # Omschrijving dossier
        rooster.addWidget(veldlabel(
            "Omschrijving dossier",
            "Vrij in te vullen. Komt terug in de bestandsnaam van de HTML- en "
            "PDF-export en op het voorblad van het PDF-rapport."
        ), rij, 0, 1, 2)
        rij += 1
        self.veld_naam = QLineEdit()
        self.veld_naam.setPlaceholderText("bijv. Ambt Delden 4 oktober")
        self.veld_naam.textChanged.connect(self.gewijzigd)
        rooster.addWidget(self.veld_naam, rij, 0, 1, 2)
        rij += 1

        # Soort ontheffing + aantal vluchten
        rooster.addWidget(veldlabel("Soort ontheffing"), rij, 0)
        rooster.addWidget(veldlabel("Aantal vluchten"), rij, 1)
        rij += 1
        self.veld_soort = QComboBox()
        self.veld_soort.addItems(SOORTEN_ONTHEFFING)
        rooster.addWidget(self.veld_soort, rij, 0)
        self.veld_aantal = QSpinBox()
        self.veld_aantal.setRange(1, 999)
        self.veld_aantal.setValue(STANDAARD_AANTAL_VLUCHTEN)
        rooster.addWidget(self.veld_aantal, rij, 1)
        rij += 1

        # Datum vlucht
        rooster.addWidget(veldlabel(
            "Datum vlucht",
            f"Hiermee bepalen we of de aanvraag niet te laat is ingediend: de "
            f"ondertekening moet minimaal {MIN_INDIENTERMIJN_DAGEN} dagen vóór de "
            f"vroegste vluchtdatum liggen."
        ), rij, 0, 1, 2)
        rij += 1
        self.veld_vlucht = QDateEdit()
        self.veld_vlucht.setCalendarPopup(True)
        self.veld_vlucht.setDisplayFormat("dd-MM-yyyy")
        self.veld_vlucht.setMinimumDate(QDate.currentDate())
        self.veld_vlucht.setDate(QDate.currentDate().addDays(STANDAARD_DAGEN_VOORUIT))
        self.veld_vlucht.dateChanged.connect(self.gewijzigd)
        rooster.addWidget(self.veld_vlucht, rij, 0, 1, 2)
        rij += 1

        # Datum + tijdstip ondertekening
        rooster.addWidget(veldlabel(
            "Datum ondertekening",
            f"Hiermee bepalen we of de aanvraag niet te laat is ingediend: bij minder "
            f"dan {MIN_INDIENTERMIJN_DAGEN} dagen vóór de vroegste vluchtdatum volgt "
            f"een waarschuwing in het rapport."
        ), rij, 0)
        rooster.addWidget(veldlabel("Tijdstip"), rij, 1)
        rij += 1
        self.veld_onder = QDateEdit()
        self.veld_onder.setCalendarPopup(True)
        self.veld_onder.setDisplayFormat("dd-MM-yyyy")
        if ONDERTEKENING_VANAF_HEDEN:
            self.veld_onder.setMinimumDate(QDate.currentDate())
        self.veld_onder.setDate(QDate.currentDate())
        self.veld_onder.dateChanged.connect(self.gewijzigd)
        rooster.addWidget(self.veld_onder, rij, 0)
        self.veld_tijd = QTimeEdit()
        self.veld_tijd.setDisplayFormat("HH:mm")
        self.veld_tijd.setTime(QTime.currentTime())
        rooster.addWidget(self.veld_tijd, rij, 1)
        rij += 1

        vak.addLayout(rooster)
        vak.addWidget(scheiding())

        # Vluchttijden
        tijden = QHBoxLayout()
        tijden.setSpacing(10)
        self.veld_udp = QCheckBox("Vluchten binnen UDP")
        self.veld_udp.setChecked(True)
        self.veld_udp.setToolTip(
            "Uniform Daglicht Periode: vluchten vinden plaats tussen zonsopgang en "
            "zonsondergang. Zet uit om een start- en eindtijd op te geven."
        )
        self.veld_udp.toggled.connect(self._udp_gewijzigd)
        tijden.addWidget(self.veld_udp)
        tijden.addStretch(1)
        self.veld_start = QTimeEdit()
        self.veld_start.setDisplayFormat("HH:mm")
        self.veld_start.setTime(QTime(9, 0))
        self.veld_start.setEnabled(False)
        self.veld_start.setFixedWidth(84)
        tijden.addWidget(self.veld_start)
        pijl = QLabel("–")
        pijl.setObjectName("hulp")
        tijden.addWidget(pijl)
        self.veld_einde = QTimeEdit()
        self.veld_einde.setDisplayFormat("HH:mm")
        self.veld_einde.setTime(QTime(17, 0))
        self.veld_einde.setEnabled(False)
        self.veld_einde.setFixedWidth(84)
        tijden.addWidget(self.veld_einde)
        vak.addLayout(tijden)

        self.melding_datum = QLabel("")
        self.melding_datum.setObjectName("waarschuwing")
        self.melding_datum.setWordWrap(True)
        self.melding_datum.hide()
        vak.addWidget(self.melding_datum)

        self.gewijzigd.connect(self._toets_termijn)

    def _udp_gewijzigd(self, aan: bool) -> None:
        self.veld_start.setEnabled(not aan)
        self.veld_einde.setEnabled(not aan)

    def _toets_termijn(self) -> None:
        """Toon dezelfde uitkomst als de validatiestap straks in het rapport zet."""
        dagen = self.veld_onder.date().daysTo(self.veld_vlucht.date())
        if dagen < 0:
            toon_melding(
                self.melding_datum,
                "De ondertekening ligt ná de vluchtdatum; het rapport meldt dit als gebrek."
            )
        elif dagen < MIN_INDIENTERMIJN_DAGEN:
            toon_melding(
                self.melding_datum,
                f"Slechts {dagen} dag(en) tussen ondertekening en vlucht; de aanvraag is "
                f"te laat ingediend (eis: minimaal {MIN_INDIENTERMIJN_DAGEN} dagen). "
                f"Het rapport meldt dit."
            )
        else:
            self.melding_datum.hide()

    def dossiernaam(self) -> str:
        return self.veld_naam.text().strip()

    def velden(self) -> dict:
        """De aanvraagvelden die dit paneel beheert."""
        udp = self.veld_udp.isChecked()
        return {
            "naam":                   self.dossiernaam(),
            "soort_ontheffing":       self.veld_soort.currentText(),
            "datum_vlucht":           [self.veld_vlucht.date().toString("yyyy-MM-dd")],
            "vlucht_udp":             udp,
            "vlucht_start":           None if udp else self.veld_start.time().toString("HH:mm"),
            "vlucht_einde":           None if udp else self.veld_einde.time().toString("HH:mm"),
            "aantal_vluchten":        self.veld_aantal.value(),
            "datum_ondertekening":    self.veld_onder.date().toString("yyyy-MM-dd"),
            "tijdstip_ondertekening": self.veld_tijd.time().toString("HH:mm"),
        }


# ──────────────────────────────────────────────
# Luchtvaartuigen
# ──────────────────────────────────────────────

class Luchtvaartuigenpaneel(QWidget):
    """Invoer en overzicht van de luchtvaartuigen bij deze aanvraag."""

    gewijzigd = Signal()

    def __init__(self):
        super().__init__()
        self.luchtvaartuigen: list[dict] = []
        self._teller = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        frame, vak = kaartje()
        lay.addWidget(frame)
        vak.addWidget(sectiekop(
            "Luchtvaartuigen",
            "Registratiekenmerken worden opgezocht in het ILT-luchtvaartuigregister; "
            "daaruit volgt de maatgevende geluidnorm en de toetsingsafstand."
        ))

        invoer = QHBoxLayout()
        invoer.setSpacing(8)
        self.veld_registratie = QLineEdit()
        self.veld_registratie.setPlaceholderText("registratiekenmerk, bijv. PH-ECE")
        self.veld_registratie.returnPressed.connect(self.toevoegen)
        invoer.addWidget(self.veld_registratie, 1)

        self.veld_type = QComboBox()
        self.veld_type.addItems(TYPEN_LUCHTVAARTUIG)
        self.veld_type.setFixedWidth(120)
        invoer.addWidget(self.veld_type)

        plus = QPushButton("+")
        plus.setObjectName("plus")
        plus.setToolTip("Luchtvaartuig toevoegen (of druk op Enter)")
        plus.setCursor(Qt.PointingHandCursor)
        plus.clicked.connect(self.toevoegen)
        invoer.addWidget(plus)
        vak.addLayout(invoer)

        self.melding = QLabel("")
        self.melding.setObjectName("fout")
        self.melding.setWordWrap(True)
        self.melding.hide()
        vak.addWidget(self.melding)

        vak.addWidget(scheiding())

        self.lijst = QVBoxLayout()
        self.lijst.setContentsMargins(0, 0, 0, 0)
        self.lijst.setSpacing(6)
        vak.addLayout(self.lijst)

        self._ververs()

    def toevoegen(self) -> None:
        registratie = self.veld_registratie.text().strip().upper()
        self.melding.hide()

        if not registratie:
            return
        if any(lv["registratie"] == registratie for lv in self.luchtvaartuigen):
            toon_melding(self.melding, f"{registratie} staat al in de lijst.")
            return
        if not _REGISTRATIE_PATROON.match(registratie):
            toon_melding(
                self.melding,
                f"'{registratie}' lijkt geen registratiekenmerk (verwacht bijv. PH-ECE). "
                f"Het kenmerk is toch toegevoegd."
            )

        self._teller += 1
        self.luchtvaartuigen.append({
            "id": f"lv{self._teller}",
            "registratie": registratie,
            "type": self.veld_type.currentText(),
        })
        self.veld_registratie.clear()
        self.veld_registratie.setFocus()
        self._ververs()

    def _verwijderen(self, lv_id: str) -> None:
        self.luchtvaartuigen = [lv for lv in self.luchtvaartuigen if lv["id"] != lv_id]
        self._ververs()

    def _ververs(self) -> None:
        leeg_layout(self.lijst)
        if not self.luchtvaartuigen:
            leeg = QLabel("Nog geen luchtvaartuigen toegevoegd.")
            leeg.setObjectName("leeg")
            self.lijst.addWidget(leeg)
        else:
            for nummer, lv in enumerate(self.luchtvaartuigen, 1):
                rij = LuchtvaartuigRij(nummer, lv)
                rij.verwijderd.connect(self._verwijderen)
                self.lijst.addWidget(rij)
        self.gewijzigd.emit()

    def velden(self) -> dict:
        return {"luchtvaartuigen": [{"registratie": lv["registratie"], "type": lv["type"]}
                                    for lv in self.luchtvaartuigen]}


# ──────────────────────────────────────────────
# Puntlocaties
# ──────────────────────────────────────────────

class Puntlocatiespaneel(QWidget):
    """Invoer en overzicht van de puntlocaties, in WGS84 of RD.

    Beheert ook de onderlinge-afstandsregel (B03); de grens komt uit tug_config,
    net als de omhullende van Nederland waaraan handmatige invoer wordt getoetst.
    """

    gewijzigd      = Signal()
    focus_gevraagd = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.punten: list[dict] = []
        self._teller = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        frame, vak = kaartje()
        lay.addWidget(frame)
        vak.addWidget(sectiekop(
            "Puntlocaties",
            f"Klik op de kaart om een puntlocatie te plaatsen. Meerdere puntlocaties "
            f"mogen onderling hooguit {MAX_PUNT_AFSTAND_M} m uit elkaar liggen."
        ))

        # Handmatige invoer: WGS84 of RD
        balk = QFrame()
        balk.setObjectName("segmentbalk")
        balk_lay = QHBoxLayout(balk)
        balk_lay.setContentsMargins(3, 3, 3, 3)
        balk_lay.setSpacing(3)
        self.knop_wgs = QPushButton("GPS (WGS84)")
        self.knop_rd  = QPushButton("RD (EPSG:28992)")
        groep = QButtonGroup(self)
        for knop in (self.knop_wgs, self.knop_rd):
            knop.setObjectName("segment")
            knop.setCheckable(True)
            knop.setCursor(Qt.PointingHandCursor)
            groep.addButton(knop)
            balk_lay.addWidget(knop)
        balk_lay.addStretch(1)
        self.knop_wgs.setChecked(True)
        self.knop_wgs.toggled.connect(self._stelsel_gewijzigd)
        vak.addWidget(balk)

        invoer = QHBoxLayout()
        invoer.setSpacing(8)
        self.veld_a = QDoubleSpinBox()
        self.veld_b = QDoubleSpinBox()
        for veld in (self.veld_a, self.veld_b):
            veld.setButtonSymbols(QDoubleSpinBox.NoButtons)
            invoer.addWidget(veld, 1)
        knop = QPushButton("+")
        knop.setObjectName("plus")
        knop.setToolTip("Puntlocatie toevoegen")
        knop.setCursor(Qt.PointingHandCursor)
        knop.clicked.connect(self._handmatig_toevoegen)
        invoer.addWidget(knop)
        vak.addLayout(invoer)
        self._stelsel_gewijzigd(True)

        self.melding = QLabel("")
        self.melding.setObjectName("fout")
        self.melding.setWordWrap(True)
        self.melding.hide()
        vak.addWidget(self.melding)

        vak.addWidget(scheiding())

        self.lijst = QVBoxLayout()
        self.lijst.setContentsMargins(0, 0, 0, 0)
        self.lijst.setSpacing(6)
        vak.addLayout(self.lijst)

        self.melding_afstand = QLabel("")
        self.melding_afstand.setObjectName("waarschuwing")
        self.melding_afstand.setWordWrap(True)
        self.melding_afstand.hide()
        vak.addWidget(self.melding_afstand)

        self._ververs()

    # ── Invoer ────────────────────────────────

    def _stelsel_gewijzigd(self, _=None) -> None:
        lat_min, lat_max, lon_min, lon_max = NL_BBOX
        if self.knop_wgs.isChecked():
            self.veld_a.setRange(lat_min, lat_max)
            self.veld_a.setDecimals(6)
            self.veld_a.setSingleStep(0.0001)
            self.veld_a.setPrefix("lat  ")
            self.veld_a.setValue(KAART_START_LAT)
            self.veld_b.setRange(lon_min, lon_max)
            self.veld_b.setDecimals(6)
            self.veld_b.setSingleStep(0.0001)
            self.veld_b.setPrefix("lon  ")
            self.veld_b.setValue(KAART_START_LON)
        else:
            x, y = naar_rd(KAART_START_LAT, KAART_START_LON)
            self.veld_a.setRange(*RD_X_BEREIK)
            self.veld_a.setDecimals(1)
            self.veld_a.setSingleStep(1.0)
            self.veld_a.setPrefix("X  ")
            self.veld_a.setValue(x)
            self.veld_b.setRange(*RD_Y_BEREIK)
            self.veld_b.setDecimals(1)
            self.veld_b.setSingleStep(1.0)
            self.veld_b.setPrefix("Y  ")
            self.veld_b.setValue(y)

    def _handmatig_toevoegen(self) -> None:
        self.melding.hide()
        if self.knop_wgs.isChecked():
            lat, lon = self.veld_a.value(), self.veld_b.value()
        else:
            lat, lon = naar_wgs(self.veld_a.value(), self.veld_b.value())
        if not in_nederland(lat, lon):
            toon_melding(
                self.melding,
                f"({lat:.6f}, {lon:.6f}) ligt niet in Nederland — controleer de invoer."
            )
            return
        self.toevoegen(lat, lon, focus=True)

    def toevoegen(self, lat: float, lon: float, focus: bool = False) -> None:
        self._teller += 1
        x, y = naar_rd(lat, lon)
        self.punten.append({
            "id": f"p{self._teller}", "lat": float(lat), "lon": float(lon),
            "x": float(x), "y": float(y),
        })
        self._ververs()
        if focus:
            self.focus_gevraagd.emit(lat, lon)

    def verwijderen(self, punt_id: str) -> None:
        self.punten = [p for p in self.punten if p["id"] != punt_id]
        self._ververs()

    def _tonen(self, punt_id: str) -> None:
        for punt in self.punten:
            if punt["id"] == punt_id:
                self.focus_gevraagd.emit(punt["lat"], punt["lon"])
                return

    # ── Overzicht ─────────────────────────────

    def _ververs(self) -> None:
        leeg_layout(self.lijst)
        if not self.punten:
            leeg = QLabel("Nog geen puntlocaties. Klik op de kaart of vul coördinaten in.")
            leeg.setObjectName("leeg")
            leeg.setWordWrap(True)
            self.lijst.addWidget(leeg)
        else:
            for nummer, punt in enumerate(self.punten, 1):
                rij = PuntRij(nummer, punt)
                rij.verwijderd.connect(self.verwijderen)
                rij.gekozen.connect(self._tonen)
                self.lijst.addWidget(rij)

        afstand = self.max_onderlinge_afstand()
        if afstand > MAX_PUNT_AFSTAND_M:
            toon_melding(
                self.melding_afstand,
                f"De puntlocaties liggen tot {afstand:.0f} m uit elkaar; maximaal "
                f"{MAX_PUNT_AFSTAND_M} m is toegestaan. De validatiestap breekt hierop af."
            )
        else:
            self.melding_afstand.hide()

        self.gewijzigd.emit()

    def max_onderlinge_afstand(self) -> float:
        """Grootste onderlinge afstand in meters; 0 bij minder dan twee punten."""
        grootste = 0.0
        for i, a in enumerate(self.punten):
            for b in self.punten[i + 1:]:
                grootste = max(
                    grootste, ((a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2) ** 0.5
                )
        return grootste

    def velden(self) -> dict:
        return {
            "coord_lat": [p["lat"] for p in self.punten],
            "coord_lon": [p["lon"] for p in self.punten],
        }


# ──────────────────────────────────────────────
# Exportafhandeling
# ──────────────────────────────────────────────

class Exportafhandeling(QObject):
    """Start de pipeline, vraagt de exportmap en levert de bestanden af.

    De volgorde is met opzet zo: eerst starten, dan pas de map vragen. De
    pipeline hoeft niet te wachten op de gebruiker, en de gebruiker niet op de
    pipeline. Wie van de twee als eerste klaar is, wacht op de ander;
    `_afronden` draait daarna precies één keer.
    """

    regel   = Signal(str)
    voltooi = Signal(str, str, bool)   # titel, toelichting, geslaagd
    status  = Signal(str, str)         # bericht, stijlnaam

    def __init__(self, ouder: QWidget, instellingen: QSettings):
        super().__init__(ouder)
        self.ouder        = ouder
        self.instellingen = instellingen
        self.pijplijn: Pijplijn | None = None
        self._doelmap: Path | None     = None
        self._doel_gekozen             = False
        self._resultaat: int | None    = None
        self._afgerond                 = False
        self._bestanden_voor: set[str] = set()

    def loopt(self) -> bool:
        return self.pijplijn is not None and self.pijplijn.isRunning()

    def start(self, json_pad: Path) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._bestanden_voor = {p.name for p in OUTPUT_DIR.iterdir() if p.is_file()}
        self._doelmap        = None
        self._doel_gekozen   = False
        self._resultaat      = None
        self._afgerond       = False

        self.pijplijn = Pijplijn(json_pad)
        self.pijplijn.regel.connect(self.regel)
        self.pijplijn.klaar.connect(self._pijplijn_klaar)
        self.pijplijn.start()
        self.regel.emit(f"Aanvraag geschreven naar {json_pad}")
        QApplication.processEvents()

        self._vraag_exportmap()
        if self._resultaat is not None:
            self._afronden()

    def _vraag_exportmap(self) -> None:
        vorige  = self.instellingen.value("exportmap", str(Path.home()))
        gekozen = QFileDialog.getExistingDirectory(
            self.ouder, "Kies de map voor de HTML- en PDF-export", str(vorige)
        )
        if gekozen:
            self._doelmap = Path(gekozen)
            self.instellingen.setValue("exportmap", gekozen)
            self.regel.emit(f"Exportmap: {gekozen}")
        else:
            self.regel.emit(
                f"Geen exportmap gekozen — de export blijft staan in {OUTPUT_DIR}"
            )
        self._doel_gekozen = True

    def _pijplijn_klaar(self, code: int) -> None:
        self._resultaat = code
        if self._doel_gekozen:
            self._afronden()

    # ── Afronden ──────────────────────────────

    def _nieuwe_exports(self) -> list[Path]:
        return sorted(
            p for p in OUTPUT_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in (".pdf", ".html")
            and p.name not in self._bestanden_voor
        )

    def _verplaats_en_open(self, nieuw: list[Path]) -> list[Path]:
        verplaatst = []
        for bestand in nieuw:
            doel = bestand
            if self._doelmap:
                try:
                    self._doelmap.mkdir(parents=True, exist_ok=True)
                    doel = Path(shutil.move(str(bestand), str(self._doelmap / bestand.name)))
                    self.regel.emit(f"Verplaatst: {doel}")
                except OSError as fout:
                    self.regel.emit(
                        f"Verplaatsen mislukt ({fout}); export blijft in {OUTPUT_DIR}"
                    )
            verplaatst.append(doel)

        for doel in verplaatst:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(doel)))
            self.regel.emit(f"Geopend: {doel.name}")
        return verplaatst

    def _leeg_tmp(self) -> None:
        """Wist de tijdelijke aanvraag-JSON's na afloop van de pipeline.

        De procesbeschrijving in het PDF-rapport legt de gebruikte invoer al
        vast, dus het bestand hoeft niet te blijven staan. Gelijk aan het wissen
        van tug_state.json door tug_run.py: ook na een fout.
        """
        if not TMP_DIR.exists():
            return
        for bestand in TMP_DIR.iterdir():
            if not bestand.is_file():
                continue
            try:
                bestand.unlink()
            except OSError as fout:
                self.regel.emit(f"Kon {bestand.name} niet wissen ({fout})")
        self.regel.emit(f"Tijdelijke invoer gewist ({TMP_DIR.name}/) — dataveiligheid.")

    def _afronden(self) -> None:
        if self._afgerond:
            return
        self._afgerond = True

        code = self._resultaat or 0
        if code != 0:
            self.voltooi.emit(
                "Pipeline afgebroken",
                f"De pipeline stopte met foutcode {code}. Zie het logboek hierboven.",
                False,
            )
            self.status.emit(f"Pipeline afgebroken (exit {code}).", "statusFout")
        else:
            nieuw = self._nieuwe_exports()
            if not nieuw:
                self.voltooi.emit(
                    "Pipeline voltooid",
                    "Er zijn geen nieuwe exportbestanden aangetroffen in de outputmap.",
                    True,
                )
                self.status.emit("Pipeline voltooid, maar geen export gevonden.", "statusFout")
            else:
                verplaatst = self._verplaats_en_open(nieuw)
                map_tekst  = str(self._doelmap) if self._doelmap else str(OUTPUT_DIR)
                self.voltooi.emit(
                    "Pipeline voltooid",
                    f"{len(verplaatst)} bestand(en) in {map_tekst} — geopend.",
                    True,
                )
                self.status.emit(
                    f"Klaar: {len(verplaatst)} bestand(en) in {map_tekst}.", "statusOk"
                )

        self._leeg_tmp()
        self.pijplijn = None


# ──────────────────────────────────────────────
# Hoofdvenster
# ──────────────────────────────────────────────

class Hoofdvenster(QMainWindow):
    """Zet de panelen naast elkaar en verbindt ze; zelf houdt het venster geen
    aanvraaggegevens bij."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Workflow TUG-ontheffingen Overijssel - {VERSION}")
        self.setWindowIcon(app_icoon())
        self.resize(VENSTER_B, VENSTER_H)
        self.setMinimumSize(1100, 760)

        self.instellingen = QSettings("ProvincieOverijssel", "TUG-ontheffingen")
        self.gegevens     = Aanvraagpaneel()
        self.toestellen   = Luchtvaartuigenpaneel()
        self.locaties     = Puntlocatiespaneel()
        self.export       = Exportafhandeling(self, self.instellingen)

        wortel = QWidget()
        wortel.setObjectName("wortel")
        self.setCentralWidget(wortel)
        buiten = QVBoxLayout(wortel)
        buiten.setContentsMargins(0, 0, 0, 0)
        buiten.setSpacing(0)
        buiten.addWidget(self._bouw_kop())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._bouw_linkerkolom())

        self.kaart = Kaartpaneel()
        splitter.addWidget(self.kaart)

        links = int(VENSTER_B * LINKER_FRACTIE)
        splitter.setSizes([links, VENSTER_B - links])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        buiten.addWidget(splitter, 1)

        self.sluier = Sluier(self)
        self._verbind()
        self._ververs_status()

    # ── Opbouw ────────────────────────────────

    def _bouw_kop(self) -> QWidget:
        kop = QWidget()
        kop.setObjectName("kop")
        kop.setFixedHeight(56)
        lay = QHBoxLayout(kop)
        lay.setContentsMargins(20, 0, 20, 0)
        lay.setSpacing(12)

        merk = QLabel()
        merk.setPixmap(app_icoon().pixmap(24, 24))
        lay.addWidget(merk)

        titel = QLabel("Workflow TUG-ontheffingen Overijssel")
        titel.setObjectName("kopTitel")
        lay.addWidget(titel)

        versie = QLabel(f"workflowversie {VERSION}")
        versie.setObjectName("kopVersie")
        versie.setToolTip("Git-commit van de workflowcode; komt terug in elke export.")
        lay.addWidget(versie)

        lay.addStretch(1)
        return kop

    def _bouw_linkerkolom(self) -> QWidget:
        kolom = QWidget()
        kolom.setMinimumWidth(430)
        lay = QVBoxLayout(kolom)
        lay.setContentsMargins(16, 16, 12, 16)
        lay.setSpacing(12)

        lay.addWidget(self.gegevens)                  # vast
        lay.addWidget(self._bouw_overzichten(), 1)    # uitbreidbaar, scrollt
        lay.addWidget(self._bouw_actie())             # vast
        return kolom

    def _bouw_overzichten(self) -> QWidget:
        gebied = QScrollArea()
        gebied.setWidgetResizable(True)
        gebied.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        binnen = QWidget()
        lay = QVBoxLayout(binnen)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(12)
        lay.addWidget(self.toestellen)
        lay.addWidget(self.locaties)
        lay.addStretch(1)

        gebied.setWidget(binnen)
        return gebied

    def _bouw_actie(self) -> QWidget:
        houder = QWidget()
        lay = QVBoxLayout(houder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)

        self.knop_genereer = QPushButton("Genereer")
        self.knop_genereer.setObjectName("genereer")
        self.knop_genereer.setCursor(Qt.PointingHandCursor)
        self.knop_genereer.setToolTip(
            "Schrijft de aanvraag-JSON, start de pipeline en vraagt daarna waar de "
            "HTML- en PDF-export moeten worden opgeslagen."
        )
        self.knop_genereer.clicked.connect(self._genereer)
        lay.addWidget(self.knop_genereer)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        return houder

    def _verbind(self) -> None:
        self.kaart.brug.puntGevraagd.connect(self.locaties.toevoegen)
        self.kaart.brug.puntWegGevraagd.connect(self.locaties.verwijderen)
        self.kaart.brug.kaartGereed.connect(self._stuur_punten_naar_kaart)
        self.locaties.gewijzigd.connect(self._stuur_punten_naar_kaart)
        self.locaties.focus_gevraagd.connect(self.kaart.brug.focusGezet)

        for paneel in (self.gegevens, self.toestellen, self.locaties):
            paneel.gewijzigd.connect(self._ververs_status)

        self.export.regel.connect(self.sluier.voeg_regel_toe)
        self.export.voltooi.connect(self.sluier.afgerond)
        self.export.status.connect(self._toon_eindstatus)

    # ── Status ────────────────────────────────

    def _stuur_punten_naar_kaart(self) -> None:
        self.kaart.brug.pinsGezet.emit(json.dumps(self.locaties.punten))

    def _ontbrekend(self) -> list[str]:
        ontbreekt = []
        if not self.gegevens.dossiernaam():
            ontbreekt.append("omschrijving dossier")
        if not self.toestellen.luchtvaartuigen:
            ontbreekt.append("luchtvaartuig")
        if not self.locaties.punten:
            ontbreekt.append("puntlocatie")
        return ontbreekt

    def _ververs_status(self) -> None:
        ontbreekt = self._ontbrekend()
        loopt     = self.export.loopt()
        self.knop_genereer.setEnabled(not ontbreekt and not loopt)
        if loopt:
            self.status.setText("Pipeline draait …")
        elif ontbreekt:
            self._zet_status("Nog invullen: " + ", ".join(ontbreekt) + ".", "status")
        else:
            self._zet_status(
                f"Gereed: {len(self.toestellen.luchtvaartuigen)} luchtvaartuig(en), "
                f"{len(self.locaties.punten)} puntlocatie(s).",
                "statusOk",
            )

    def _zet_status(self, bericht: str, stijl: str) -> None:
        self.status.setObjectName(stijl)
        self.status.setText(bericht)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _toon_eindstatus(self, bericht: str, stijl: str) -> None:
        # Eerst de knop weer vrijgeven, daarna pas het eindbericht tonen: anders
        # overschrijft _ververs_status() de uitkomst met de invulhint.
        self._ververs_status()
        self._zet_status(bericht, stijl)

    # ── Genereren ─────────────────────────────

    def aanvraag(self) -> dict:
        """De volledige aanvraag zoals hij naar de pipeline gaat."""
        return {**self.gegevens.velden(), **self.toestellen.velden(), **self.locaties.velden()}

    def _genereer(self) -> None:
        afstand = self.locaties.max_onderlinge_afstand()
        if afstand > MAX_PUNT_AFSTAND_M:
            QMessageBox.warning(
                self, "Puntlocaties te ver uit elkaar",
                f"De puntlocaties liggen tot {afstand:.0f} m uit elkaar. De validatiestap "
                f"staat maximaal {MAX_PUNT_AFSTAND_M} m toe en breekt hierop af.\n\n"
                f"Verwijder of verplaats een puntlocatie."
            )
            return

        aanvraag = self.aanvraag()
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        stempel  = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_pad = TMP_DIR / f"{naam_slug(aanvraag['naam']) or 'aanvraag'}_{stempel}.json"
        json_pad.write_text(json.dumps(aanvraag, ensure_ascii=False, indent=2), encoding="utf-8")

        self.knop_genereer.setEnabled(False)
        self.status.setText("Pipeline draait …")
        self.sluier.start(aanvraag["naam"])
        self.export.start(json_pad)

    # ── Vensterafhandeling ────────────────────

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.sluier.isVisible():
            self.sluier.resize(self.size())

    def closeEvent(self, event):
        if self.export.loopt():
            antwoord = QMessageBox.question(
                self, "Pipeline draait",
                "De pipeline draait nog. Venster toch sluiten?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if antwoord != QMessageBox.Yes:
                event.ignore()
                return
        super().closeEvent(event)


# ──────────────────────────────────────────────
# Start
# ──────────────────────────────────────────────

def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("TUG-ontheffingen")
    stijl = (GUI_DIR / "stijl.qss").read_text(encoding="utf-8")
    for sleutel, pad in iconen().items():
        stijl = stijl.replace(sleutel, pad)
    app.setStyleSheet(stijl)

    venster = Hoofdvenster()
    venster.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
