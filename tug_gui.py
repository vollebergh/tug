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

import sys

# Eerst naar de projectomgeving, vóór PySide6 en de rest: een andere interpreter
# heeft die pakketten mogelijk niet (tug_omgeving gebruikt alleen de standaardbibliotheek).
if __name__ == "__main__":
    from tug_omgeving import zorg_voor_projectomgeving
    zorg_voor_projectomgeving()

import html
import json
import os
import platform
import re
import shutil
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pyproj import Transformer

from PySide6.QtCore import (
    QDate, QObject, QSettings, QStandardPaths, Qt, QThread, QThreadPool, QTime, QUrl, Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QIcon, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDateEdit, QDoubleSpinBox,
    QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QDialog, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QToolTip,
    QSpinBox, QSplitter, QTimeEdit, QVBoxLayout, QWidget,
)

from tug_config import (
    GUI_DIR, KAART_ACHTERGRONDEN, MIN_INDIENTERMIJN_DAGEN,
    NL_BBOX, OUTPUT_DIR, REGISTRATIE_PATROON, TOETSING_TOESLAG_M, VERSION,
)
from tug_aanvraag import AFSTAND_BEREIK, TOESLAG_BEREIK
from tug_bronnen_geocode import zoek_locatie
from tug_geo import in_nederland, json_voor_script, naam_slug, puntafstand_melding
from tug_http import BronFout
from tug_opslag import schrijf_state, wis_state

# ──────────────────────────────────────────────
# Constanten
# ──────────────────────────────────────────────

SCRIPT_DIR   = Path(__file__).parent
TMP_DIR      = SCRIPT_DIR / "tmp"
STATE_PAD    = SCRIPT_DIR / "tug_state.json"   # tug_run.STATE_PAD
LEAFLET_DIR  = GUI_DIR / "vendor" / "leaflet"

# Herkomst van de kaartpagina. Een niet-lokale oorsprong (.invalid bestaat nooit)
# zodat de pagina geen lokale inhoud is: zij krijgt geen toegang tot bestanden en
# laat de tegels gewoon laden, zonder de LocalContentCanAccess…-uitzonderingen.
KAART_BASIS_URL = "https://tug-kaart.invalid/"

# Bugmelding (B22): gaat via de eigen e-mailclient van de gebruiker, nooit automatisch.
BUG_ADRES = "vollebergh@fdle.eu"

# Hoe lang de schil wacht tot een gestopte pipeline zelf heeft opgeruimd.
STOP_WACHTTIJD_S = 15

VENSTER_B, VENSTER_H = 1400, 1000
LINKER_FRACTIE       = 0.33

# Startbeeld van de kaart: midden Overijssel, zoomniveau ~halve provincie
KAART_START_LAT  = 52.46126
KAART_START_LON  = 6.496964
KAART_START_ZOOM = 11

# De datumvelden staan op "vanaf heden" (conform specificatie). Zet op False om
# ook een ondertekening in het verleden te kunnen invoeren.
ONDERTEKENING_VANAF_HEDEN = True

# Velden die de toetsing niet sturen zijn verborgen (B15): soort ontheffing, aantal
# vluchten, tijdstip ondertekening, UDP/vluchttijden en het type luchtvaartuig.
# Ze bestaan wel en hun standaardwaarden gaan nog steeds de aanvraag-JSON in, zodat
# schema en rapport ongewijzigd blijven. Zet op True om ze weer te tonen.
TOON_AANVULLENDE_VELDEN = False

STANDAARD_DAGEN_VOORUIT   = 42   # voorgestelde vluchtdatum: ruim buiten de indientermijn
STANDAARD_AANTAL_VLUCHTEN = 50
AANTAL_STAPPEN            = 4    # tug_run.STAPPEN
EXIT_ONVOLLEDIG           = 2    # tug_run.EXIT_ONVOLLEDIG: rapport gemaakt, toetsing onvolledig

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

def uitlegtekst(tekst: str) -> str:
    """Tooltiptekst als opgemaakte tekst: Qt breekt die af over meerdere regels,
    platte tekst niet (die liep als één regel van het scherm)."""
    return f"<p>{html.escape(tekst)}</p>"


class Info(QLabel):
    """Klein i-tje met uitleg. De uitleg verschijnt meteen bij hover en bij klik,
    ook als het venster niet actief is — Qt's eigen tooltip doet dat niet."""

    def __init__(self, uitleg: str):
        super().__init__("i")
        self.setObjectName("info")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setToolTip(uitlegtekst(uitleg))
        self.setCursor(Qt.CursorShape.WhatsThisCursor)

    def _toon(self) -> None:
        QToolTip.showText(self.mapToGlobal(self.rect().bottomLeft()), self.toolTip(), self)

    def enterEvent(self, event):  # noqa: N802 — Qt-override
        self._toon()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 — Qt-override
        QToolTip.hideText()
        super().leaveEvent(event)

    def mousePressEvent(self, _event):  # noqa: N802 — Qt-override
        self._toon()


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
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(kleur), dikte)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
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
    cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    map_ = Path(cache) / "iconen"
    map_.mkdir(parents=True, exist_ok=True)
    neer: list[tuple[float, float]] = [(4, 6.5), (8, 10.5), (12, 6.5)]
    op:   list[tuple[float, float]] = [(4, 10), (8, 6), (12, 10)]
    vink: list[tuple[float, float]] = [(4, 8.4), (6.8, 11.2), (12, 5.4)]
    specificaties: dict[str, tuple[list[tuple[float, float]], str]] = {
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
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#4b93ff"))
    p.setPen(Qt.PenStyle.NoPen)
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
    afstand_gewijzigd = Signal(str, int)   # id, meters (0 = uit het register)

    def __init__(self, nummer: int, lv: dict):
        super().__init__()
        self.setObjectName("rij")
        self._id = lv["id"]

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 8, 8)
        lay.setSpacing(10)

        nr = QLabel(str(nummer))
        nr.setObjectName("rijNr")
        nr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(nr)

        naam = QLabel(lv["registratie"])
        naam.setObjectName("rijTitel")
        lay.addWidget(naam)

        if TOON_AANVULLENDE_VELDEN:
            badge = QLabel(lv["type"])
            badge.setObjectName("badge")
            lay.addWidget(badge)

        lay.addStretch(1)

        # Handmatige geluidsafstand (B12), bijvoorbeeld voor een buitenlandse
        # registratie die het ILT-register niet kent. 0 = uit het register.
        self.veld_afstand = QSpinBox()
        self.veld_afstand.setRange(0, AFSTAND_BEREIK[1])
        self.veld_afstand.setSingleStep(10)
        self.veld_afstand.setSuffix(" m")
        self.veld_afstand.setSpecialValueText("uit register")
        self.veld_afstand.setValue(lv.get("afstand_m") or 0)
        self.veld_afstand.setFixedWidth(116)
        self.veld_afstand.setToolTip(uitlegtekst(
            "Geluidsafstand handmatig opgeven, bijvoorbeeld voor een buitenlands "
            "luchtvaartuig dat niet in het ILT-register staat. Laat op 'uit register' "
            "om de afstand uit het register en de NLR-tabel te halen. Een handmatige "
            "waarde gaat vóór het register en staat als zodanig in het rapport."
        ))
        self.veld_afstand.valueChanged.connect(
            lambda waarde: self.afstand_gewijzigd.emit(self._id, waarde))
        lay.addWidget(self.veld_afstand)

        weg = QPushButton("✕")
        weg.setObjectName("verwijder")
        weg.setToolTip("Luchtvaartuig verwijderen")
        weg.setCursor(Qt.CursorShape.PointingHandCursor)
        weg.clicked.connect(lambda: self.verwijderd.emit(self._id))
        lay.addWidget(weg)


class PuntRij(QFrame):
    verwijderd = Signal(str)
    gekozen    = Signal(str)

    def __init__(self, nummer: int, punt: dict):
        super().__init__()
        self.setObjectName("rij")
        self._id = punt["id"]
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Klik om deze puntlocatie op de kaart te tonen")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 8, 8)
        lay.setSpacing(10)

        nr = QLabel(str(nummer))
        nr.setObjectName("rijNr")
        nr.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        weg.setCursor(Qt.CursorShape.PointingHandCursor)
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
    pinsGezet     = Signal(str)
    focusGezet    = Signal(float, float)
    zoekResultaat = Signal(int, str)    # volgnummer, JSON {"treffers": [...]} of {"fout": ...}

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

    @Slot(int, str)
    def zoekAdres(self, volgnummer: int, tekst: str) -> None:
        """Zoek op adres buiten de GUI-thread; het antwoord komt via zoekResultaat.

        De kaartpagina doet zelf geen netwerkverzoeken buiten de tegels: de
        bevraging loopt hier, via tug_http, naar de PDOK Locatieserver. Het
        volgnummer laat de pagina verouderde antwoorden negeren.
        """
        def zoek() -> None:
            antwoord: dict[str, Any]
            try:
                antwoord = {"treffers": zoek_locatie(tekst)}
            except BronFout as fout:
                antwoord = {"fout": str(fout)}
            except Exception as fout:  # noqa: BLE001 — anders blijft de pagina op 'zoeken…' staan
                antwoord = {"fout": f"onverwachte fout ({fout.__class__.__name__})"}
            # Signalen naar een object in de GUI-thread worden in de wachtrij gezet.
            self.zoekResultaat.emit(volgnummer, json.dumps(antwoord))

        QThreadPool.globalInstance().start(zoek)


class KaartPagina(QWebEnginePage):
    """De ingebedde kaart mag nergens anders heen.

    Na het laden van de eigen pagina wordt elke navigatie geweigerd. Een klik op
    een link (zoals de bronvermelding van Leaflet) opent in de systeembrowser: een
    externe pagina in dit venster zou anders de brug naar Python erven en
    puntlocaties kunnen toevoegen of verwijderen.
    """

    def __init__(self, ouder: QObject | None = None):
        super().__init__(ouder)
        self.geladen = False
        self.loadFinished.connect(self._na_laden)

    def _na_laden(self, _gelukt: bool) -> None:
        self.geladen = True

    def acceptNavigationRequest(self, url, soort, _hoofdframe):  # noqa: N802 — Qt-override
        if not self.geladen:
            return True
        if soort == QWebEnginePage.NavigationType.NavigationTypeLinkClicked and \
                url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)
        return False


def kaart_html(config: dict) -> str:
    """De kaartpagina met Leaflet ingevoegd uit gui/vendor en de configuratie als JSON."""
    sjabloon = (GUI_DIR / "kaart.html").read_text(encoding="utf-8")
    # Eerst de configuratie, dan pas Leaflet: zo kan niets in de bibliotheek per
    # ongeluk als plaatshouder worden gelezen.
    html = sjabloon.replace("__CONFIG__", json_voor_script(config))
    css = (LEAFLET_DIR / "leaflet.css").read_text(encoding="utf-8")
    js  = (LEAFLET_DIR / "leaflet.js").read_text(encoding="utf-8")
    return html.replace("__LEAFLET_CSS__", css).replace("__LEAFLET_JS__", js)


class Kaartpaneel(QWidget):
    def __init__(self):
        super().__init__()
        self.brug = Brug()

        self.web = QWebEngineView()
        self.pagina = KaartPagina(self.web)
        self.web.setPage(self.pagina)
        instellingen = self.web.settings()
        instellingen.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
        instellingen.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, False)
        instellingen.setAttribute(QWebEngineSettings.ShowScrollBars, False)

        kanaal = QWebChannel(self.pagina)
        kanaal.registerObject("brug", self.brug)
        self.pagina.setWebChannel(kanaal)
        self.pagina.setBackgroundColor(QColor("#0f131a"))

        config = {
            "start_lat":  KAART_START_LAT,
            "start_lon":  KAART_START_LON,
            "start_zoom": KAART_START_ZOOM,
            "standaard_achtergrond": "topografisch",
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

        self.web.setHtml(kaart_html(config), QUrl(KAART_BASIS_URL))

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
        self.proces: subprocess.Popen | None = None

    def run(self) -> None:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
        # Een eigen procesgroep, zodat stop() de run netjes kan beëindigen: onder
        # Windows is CTRL_BREAK alleen zo aan één groep te sturen.
        groep: dict[str, Any]
        if sys.platform == "win32":
            groep = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            groep = {"start_new_session": True}
        try:
            proces = subprocess.Popen(  # noqa: S603 — eigen interpreter, vast script
                [sys.executable, str(SCRIPT_DIR / "tug_run.py"), str(self.json_pad)],
                cwd=str(SCRIPT_DIR), env=env, text=True, bufsize=1,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                encoding="utf-8", errors="replace", **groep,
            )
        except OSError as fout:
            self.regel.emit(f"FOUT: pipeline kon niet worden gestart — {fout}")
            self.klaar.emit(1)
            return
        self.proces = proces
        if proces.stdout is None:   # kan niet: stdout=PIPE; voor de typecontrole
            self.klaar.emit(proces.wait())
            return

        for regel in proces.stdout:
            # Voortgangstellers schrijven met \r over dezelfde regel heen. Zonder
            # \n arriveert zo'n hele reeks hier als één lange regel; alleen de
            # laatste stand is nog interessant.
            self.regel.emit(regel.rstrip().rsplit("\r", 1)[-1])
        self.klaar.emit(proces.wait())

    def stop(self) -> bool:
        """Vraag de pipeline te stoppen; True als hij binnen de wachttijd zelf eindigde.

        tug_run.py vangt het signaal op, stopt de lopende stap en wist de state. Pas
        als dat niet binnen STOP_WACHTTIJD_S lukt, wordt het proces hard beëindigd.
        """
        proces = self.proces
        if proces is None or proces.poll() is not None:
            return True
        if sys.platform == "win32":
            proces.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proces.terminate()
        try:
            proces.wait(timeout=STOP_WACHTTIJD_S)
            return True
        except subprocess.TimeoutExpired:
            proces.kill()
            proces.wait()
            return False


# ──────────────────────────────────────────────
# Voortgangssluier
# ──────────────────────────────────────────────

class Sluier(QWidget):
    """Halftransparante laag over het venster met live pipeline-uitvoer."""

    def __init__(self, ouder: QWidget):
        super().__init__(ouder)
        self.setObjectName("sluier")
        # Zonder WA_StyledBackground tekent een kaal QWidget zijn QSS-achtergrond niet.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
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
        self.sluit.setCursor(Qt.CursorShape.PointingHandCursor)
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
        ouder = self.parentWidget()
        if ouder is not None:
            self.resize(ouder.size())
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
            "Een zaaknummer of een omschrijving van plaats en datum. Komt terug in de "
            "bestandsnaam van de HTML- en PDF-export en op het voorblad van het "
            "PDF-rapport — vul daarom geen naam van aanvrager of omwonende in."
        ), rij, 0, 1, 2)
        rij += 1
        self.veld_naam = QLineEdit()
        self.veld_naam.setPlaceholderText("zaaknummer of plaats en datum — geen persoonsnamen")
        self.veld_naam.textChanged.connect(self.gewijzigd)
        rooster.addWidget(self.veld_naam, rij, 0, 1, 2)
        rij += 1

        # Soort ontheffing + aantal vluchten (verborgen tenzij TOON_AANVULLENDE_VELDEN)
        self.veld_soort = QComboBox(self)
        self.veld_soort.addItems(SOORTEN_ONTHEFFING)
        self.veld_aantal = QSpinBox(self)
        self.veld_aantal.setRange(1, 999)
        self.veld_aantal.setValue(STANDAARD_AANTAL_VLUCHTEN)
        if TOON_AANVULLENDE_VELDEN:
            rooster.addWidget(veldlabel("Soort ontheffing"), rij, 0)
            rooster.addWidget(veldlabel("Aantal vluchten"), rij, 1)
            rij += 1
            rooster.addWidget(self.veld_soort, rij, 0)
            rooster.addWidget(self.veld_aantal, rij, 1)
            rij += 1
        else:
            self.veld_soort.hide()
            self.veld_aantal.hide()

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
        ), rij, 0, 1, 1 if TOON_AANVULLENDE_VELDEN else 2)
        if TOON_AANVULLENDE_VELDEN:
            rooster.addWidget(veldlabel("Tijdstip"), rij, 1)
        rij += 1
        self.veld_onder = QDateEdit()
        self.veld_onder.setCalendarPopup(True)
        self.veld_onder.setDisplayFormat("dd-MM-yyyy")
        if ONDERTEKENING_VANAF_HEDEN:
            self.veld_onder.setMinimumDate(QDate.currentDate())
        self.veld_onder.setDate(QDate.currentDate())
        self.veld_onder.dateChanged.connect(self.gewijzigd)
        self.veld_tijd = QTimeEdit(self)
        self.veld_tijd.setDisplayFormat("HH:mm")
        self.veld_tijd.setTime(QTime.currentTime())
        if TOON_AANVULLENDE_VELDEN:
            rooster.addWidget(self.veld_onder, rij, 0)
            rooster.addWidget(self.veld_tijd, rij, 1)
        else:
            rooster.addWidget(self.veld_onder, rij, 0, 1, 2)
            self.veld_tijd.hide()
        rij += 1

        vak.addLayout(rooster)

        # Vluchttijden (verborgen tenzij TOON_AANVULLENDE_VELDEN)
        tijdenvak = QWidget()
        tijden = QHBoxLayout(tijdenvak)
        tijden.setContentsMargins(0, 0, 0, 0)
        tijden.setSpacing(10)
        self.veld_udp = QCheckBox("Vluchten binnen UDP")
        self.veld_udp.setChecked(True)
        self.veld_udp.setToolTip(uitlegtekst(
            "Uniform Daglicht Periode: vluchten vinden plaats tussen zonsopgang en "
            "zonsondergang. Zet uit om een start- en eindtijd op te geven."
        ))
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
        lijn = scheiding()
        vak.addWidget(lijn)
        vak.addWidget(tijdenvak)
        lijn.setVisible(TOON_AANVULLENDE_VELDEN)
        tijdenvak.setVisible(TOON_AANVULLENDE_VELDEN)

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
            "daaruit volgt de maatgevende geluidnorm en de toetsingsafstand. Staat een "
            "luchtvaartuig er niet in (bijvoorbeeld een buitenlandse registratie), vul "
            "dan in het overzicht zelf een geluidsafstand in."
        ))

        invoer = QHBoxLayout()
        invoer.setSpacing(8)
        self.veld_registratie = QLineEdit()
        self.veld_registratie.setPlaceholderText("registratiekenmerk, bijv. PH-ECE")
        self.veld_registratie.returnPressed.connect(self.toevoegen)
        invoer.addWidget(self.veld_registratie, 1)

        self.veld_type = QComboBox(self)
        self.veld_type.addItems(TYPEN_LUCHTVAARTUIG)
        self.veld_type.setFixedWidth(120)
        if TOON_AANVULLENDE_VELDEN:
            invoer.addWidget(self.veld_type)
        else:
            self.veld_type.hide()

        plus = QPushButton("+")
        plus.setObjectName("plus")
        plus.setToolTip("Luchtvaartuig toevoegen (of druk op Enter)")
        plus.setCursor(Qt.CursorShape.PointingHandCursor)
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
                rij.afstand_gewijzigd.connect(self._afstand_gewijzigd)
                self.lijst.addWidget(rij)
        self.gewijzigd.emit()

    def _afstand_gewijzigd(self, lv_id: str, meters: int) -> None:
        for lv in self.luchtvaartuigen:
            if lv["id"] == lv_id:
                lv["afstand_m"] = meters or None
        self.gewijzigd.emit()

    def velden(self) -> dict:
        return {"luchtvaartuigen": [
            {"registratie": lv["registratie"], "type": lv["type"]}
            | ({"afstand_m": lv["afstand_m"]} if lv.get("afstand_m") else {})
            for lv in self.luchtvaartuigen
        ]}


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
            "Klik op de kaart om een puntlocatie te plaatsen. Bij meerdere puntlocaties "
            "staat hieronder de grootste onderlinge afstand; de toetsing wordt als geheel "
            "uitgevoerd op alle puntlocaties."
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
            knop.setCursor(Qt.CursorShape.PointingHandCursor)
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
        knop.setCursor(Qt.CursorShape.PointingHandCursor)
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
        self.melding_afstand.setObjectName("hulp")
        self.melding_afstand.setWordWrap(True)
        self.melding_afstand.hide()
        vak.addWidget(self.melding_afstand)

        # Toeslag rond de puntlocatie (B21): opgeteld bij de Lden-afstand van 150/250/500 m.
        vak.addWidget(scheiding())
        toeslag = QHBoxLayout()
        toeslag.setSpacing(10)
        toeslag.addWidget(veldlabel(
            "Marge rond puntlocatie",
            f"Wordt opgeteld bij de geluidsafstand van het luchtvaartuig (150, 250 of "
            f"500 m) en vormt samen de toetsingsafstand. Standaard {TOETSING_TOESLAG_M} m; "
            f"afwijken kan, het rapport vermeldt de gebruikte waarde."
        ), 1)
        self.veld_toeslag = QSpinBox()
        self.veld_toeslag.setRange(*TOESLAG_BEREIK)
        self.veld_toeslag.setSuffix(" m")
        self.veld_toeslag.setValue(TOETSING_TOESLAG_M)
        self.veld_toeslag.setFixedWidth(96)
        self.veld_toeslag.valueChanged.connect(self.gewijzigd)
        toeslag.addWidget(self.veld_toeslag)
        vak.addLayout(toeslag)

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

        # B14: bij meerdere puntlocaties altijd de grootste onderlinge afstand, zonder
        # grens. De pipeline breekt er niet op af.
        if len(self.punten) > 1:
            toon_melding(self.melding_afstand, puntafstand_melding(self.max_onderlinge_afstand()))
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
            "toeslag_m": self.veld_toeslag.value(),
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

    def leeg_tmp(self) -> None:
        """Wist de tijdelijke aanvraag-JSON's.

        De procesbeschrijving in het PDF-rapport legt de gebruikte invoer al
        vast, dus het bestand hoeft niet te blijven staan. Gebeurt na elke run
        (ook na een fout), bij het sluiten van de schil en bij het opstarten —
        zodat ook een eerder vastgelopen of hard afgesloten schil niets achterlaat.
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

    def stop(self) -> None:
        """Stop een lopende pipeline en ruim op: voor het sluiten van de schil."""
        if self.pijplijn is not None:
            if not self.pijplijn.stop():
                # tug_run kon zelf niet meer opruimen; dan doet de schil het.
                wis_state(STATE_PAD)
            self.pijplijn.wait()
        self.leeg_tmp()

    def _afronden(self) -> None:
        if self._afgerond:
            return
        self._afgerond = True

        code = self._resultaat or 0
        if code not in (0, EXIT_ONVOLLEDIG):
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
                if code == EXIT_ONVOLLEDIG:
                    self.voltooi.emit(
                        "Voltooid — toetsing ONVOLLEDIG",
                        f"Niet alle bronnen zijn volledig geraadpleegd, of voor geen enkel "
                        f"luchtvaartuig is een afstandsnorm herleidbaar; het rapport meldt "
                        f"wat. {len(verplaatst)} bestand(en) in {map_tekst} — geopend.",
                        False,
                    )
                    self.status.emit(
                        f"Onvolledige toetsing — zie de rode melding bovenaan het rapport "
                        f"({map_tekst}).", "statusFout"
                    )
                else:
                    self.voltooi.emit(
                        "Pipeline voltooid",
                        f"{len(verplaatst)} bestand(en) in {map_tekst} — geopend.",
                        True,
                    )
                    self.status.emit(
                        f"Klaar: {len(verplaatst)} bestand(en) in {map_tekst}.", "statusOk"
                    )

        self.leeg_tmp()
        self.pijplijn = None


# ──────────────────────────────────────────────
# Bug rapporteren (B22)
# ──────────────────────────────────────────────

def bug_icoon(kleur: str = "#93a1b3", formaat: int = 18) -> QIcon:
    """Tekent een insect: lijf, kop, voelsprieten en drie paar pootjes."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(kleur), 4.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    poten = QPainterPath()
    for y, dy in ((30, -6), (40, 0), (50, 6)):
        poten.moveTo(20, y)
        poten.lineTo(8, y + dy)
        poten.moveTo(44, y)
        poten.lineTo(56, y + dy)
    poten.moveTo(27, 14)
    poten.lineTo(20, 4)
    poten.moveTo(37, 14)
    poten.lineTo(44, 4)
    p.drawPath(poten)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(kleur))
    p.drawEllipse(22, 10, 20, 16)       # kop
    p.drawEllipse(18, 22, 28, 38)       # lijf
    p.setPen(QPen(QColor("#151b24"), 3))
    p.drawLine(32, 26, 32, 58)          # scheiding van de dekschilden
    p.end()
    return QIcon(pm.scaled(formaat * 2, formaat * 2, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation))


def bug_onderwerp() -> str:
    return f"bugreport Workflow TUG-ontheffingen Overijssel - {VERSION}"


def bug_tekst(moment: datetime) -> str:
    """Invulinstructies voor een bugmelding, met wat de schil zelf al weet."""
    return f"""Beschrijf hieronder de bug. Hoe vollediger, hoe sneller hij na te bootsen
en op te lossen is.

1. Wat deed je? Beschrijf de stappen in volgorde.
-

2. Wat verwachtte je dat er zou gebeuren?
-

3. Wat gebeurde er in plaats daarvan?
-

4. Foutmelding of schermafbeelding
Plak de foutmelding (bijvoorbeeld uit het voortgangsvenster) hieronder,
of voeg een schermafbeelding toe als bijlage.
-

5. Bijlagen, als dat kan
- de aanvraag-JSON uit de map tmp/ (die staat er alleen tijdens een run;
  de schil leegt tmp/ daarna)
- het PDF-rapport en/of de HTML-kaart uit output/ of uit de gekozen exportmap

Let op: een aanvraag of export kan persoonsgegevens bevatten, zoals
adressen van omwonenden. Weeg af of meesturen nodig is, en laat weg wat
voor het oplossen van de bug niet nodig is.

--- Door de schil ingevuld ---
Datum en tijdstip: {moment:%Y-%m-%d %H:%M}
Besturingssysteem: {platform.system()} {platform.release()} ({platform.machine()})
Workflowversie: {VERSION}
"""


def mailto_url(aan: str, onderwerp: str, tekst: str) -> QUrl:
    """mailto-URL met onderwerp en tekst volledig gecodeerd (RFC 6068).

    Regeleinden worden CRLF (%0D%0A); spaties, '#', '&' en '?' worden
    procentgecodeerd, zodat geen teken de URL voortijdig afbreekt.
    """
    tekst = tekst.replace("\r\n", "\n").replace("\n", "\r\n")
    ruw = (f"mailto:{quote(aan, safe='@')}?subject={quote(onderwerp, safe='')}"
           f"&body={quote(tekst, safe='')}")
    return QUrl.fromEncoded(ruw.encode("ascii"), QUrl.ParsingMode.StrictMode)


class BugVenster(QDialog):
    """Terugval zonder e-mailclient: de velden als kopieerbare tekst."""

    def __init__(self, ouder: QWidget | None, aan: str, onderwerp: str, tekst: str):
        super().__init__(ouder)
        self.setWindowTitle("Bug rapporteren")
        self.setWindowIcon(bug_icoon())
        self.resize(640, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)

        uitleg = QLabel(
            "Er is geen e-mailprogramma gevonden om de melding in te openen. Kopieer de velden "
            "hieronder naar je eigen e-mailprogramma of webmail en verstuur de melding daar."
        )
        uitleg.setObjectName("hulp")
        uitleg.setWordWrap(True)
        lay.addWidget(uitleg)

        self.velden: dict[str, QLineEdit | QPlainTextEdit] = {}
        self.knoppen: dict[str, QPushButton] = {}
        for naam, waarde in (("Aan", aan), ("Onderwerp", onderwerp), ("Tekst", tekst)):
            kop = QHBoxLayout()
            label = QLabel(naam)
            label.setObjectName("label")
            kop.addWidget(label)
            kop.addStretch(1)
            knop = QPushButton("Kopiëren")
            knop.setObjectName("kopieer")
            knop.setCursor(Qt.CursorShape.PointingHandCursor)
            kop.addWidget(knop)
            lay.addLayout(kop)

            veld: QLineEdit | QPlainTextEdit
            if naam == "Tekst":
                veld = QPlainTextEdit(waarde)
                lay.addWidget(veld, 1)
            else:
                veld = QLineEdit(waarde)
                lay.addWidget(veld)
            veld.setReadOnly(True)
            self.velden[naam] = veld
            self.knoppen[naam] = knop
            knop.clicked.connect(lambda _=False, w=waarde, k=knop: self._kopieer(w, k))

        sluit = QPushButton("Sluiten")
        sluit.clicked.connect(self.accept)
        onder = QHBoxLayout()
        onder.addStretch(1)
        onder.addWidget(sluit)
        lay.addLayout(onder)

    @staticmethod
    def _kopieer(waarde: str, knop: QPushButton) -> None:
        QApplication.clipboard().setText(waarde)
        knop.setText("Gekopieerd ✓")


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
        # Een eerder hard afgesloten schil kan invoer in tmp/ hebben laten staan.
        self.export.leeg_tmp()

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

        self.knop_bug = QPushButton(" Bug rapporteren")
        self.knop_bug.setObjectName("bugknop")
        self.knop_bug.setIcon(bug_icoon())
        self.knop_bug.setCursor(Qt.CursorShape.PointingHandCursor)
        self.knop_bug.setToolTip(uitlegtekst(
            "Opent een ingevulde e-mail in je e-mailprogramma, met de workflowversie in het "
            "onderwerp en invulinstructies in de tekst. Er wordt niets automatisch verstuurd."))
        self.knop_bug.clicked.connect(self.meld_bug)
        lay.addWidget(self.knop_bug)
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
        gebied.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

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
        self.knop_genereer.setCursor(Qt.CursorShape.PointingHandCursor)
        self.knop_genereer.setToolTip(uitlegtekst(
            "Schrijft de aanvraag-JSON, start de pipeline en vraagt daarna waar de "
            "HTML- en PDF-export moeten worden opgeslagen."
        ))
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
        aanvraag = self.aanvraag()
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        stempel  = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_pad = TMP_DIR / f"{naam_slug(aanvraag['naam']) or 'aanvraag'}_{stempel}.json"
        schrijf_state(json_pad, aanvraag)

        self.knop_genereer.setEnabled(False)
        self.status.setText("Pipeline draait …")
        self.sluier.start(aanvraag["naam"])
        self.export.start(json_pad)

    # ── Bug rapporteren (B22) ─────────────────

    def meld_bug(self) -> None:
        """Open een concept in de e-mailclient; lukt dat niet, dan het kopieervenster."""
        onderwerp, tekst = bug_onderwerp(), bug_tekst(datetime.now())
        if QDesktopServices.openUrl(mailto_url(BUG_ADRES, onderwerp, tekst)):
            return
        self.bug_venster = BugVenster(self, BUG_ADRES, onderwerp, tekst)
        self.bug_venster.open()

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
            self.status.setText("Pipeline wordt gestopt en opgeruimd …")
            QApplication.processEvents()
            self.export.stop()
        else:
            self.export.leeg_tmp()
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
