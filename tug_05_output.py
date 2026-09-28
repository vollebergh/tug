"""
tug_05_output.py -- Stap 6 TUG-ontheffingen workflow
Versie: zie git (workflowversie = korte commit-hash, zie tug_config.VERSION)

Leest tug_state.json (sectie ruimtelijk) en genereert:
  - PDF-rapport (proceslog + adressenlijst + vier kaartpagina's: situatie en
    omgeving, elk als luchtfoto en topografisch)
  - Interactieve HTML-kaart

Of een conclusie getrokken mag worden, leest dit rapport uit de bronstatus in de
state (tug_bronstatus), niet uit het aantal treffers. Is een toetsingsrelevante
bron niet volledig geraadpleegd, dan staat dat in rood bovenaan het rapport, in
de betreffende paragraaf (zonder conclusie), boven de adressenlijst en als
banner op de HTML-kaart.

Gebruik: python tug_05_output.py tug_state.json
"""

import json
import logging
import sys
from datetime import datetime
from html import escape as html_escape
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as rl_canvas

from tug_config import (
    VERSION, MODEL_LABEL, OUTPUT_DIR, GUI_DIR,
    MARGE_M, TOETSING_TOESLAG_M, meters, toetsing_label,
    MANEGE_SIGNAAL_MARGE,
    N2000_SIGNAAL_MARGE, NNN_SIGNAAL_MARGE, NNN_TTL_DAGEN,
    LUCHTHAVEN_GRENS_M, LUCHTHAVEN_KOPPEL_M, LUCHTHAVEN_SIGNAAL_M,
    LRK_CACHE_DAYS,
    MANEGE_ZOEKTERMEN,
    _PDF_MARGIN_MM, _PDF_PAGE_W_MM, _PDF_PAGE_H_MM,
)
from tug_bronstatus import (
    BRONNEN, CACHE, MISLUKT, beschrijf, is_volledig, kort, onvolledige_toetsing,
    uitkomsten_uit_state,
)
from tug_geo import json_voor_script, naam_slug, puntafstand_melding
from tug_logging import LogAccumulator, setup_logging
from tug_opslag import schrijf_state

_logger = logging.getLogger("tug.05_output")

# ──────────────────────────────────────────────
# Paginanummering — NumberedCanvas
# ──────────────────────────────────────────────

class _NumberedCanvas(rl_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        rl_canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        totaal = len(self._saved_page_states)
        for i, state in enumerate(self._saved_page_states):
            self.__dict__.update(state)
            self._teken_paginanummer(i + 1, totaal)
            rl_canvas.Canvas.showPage(self)
        rl_canvas.Canvas.save(self)

    def _teken_paginanummer(self, huidige, totaal):
        w    = self._pagesize[0]
        marg = _PDF_MARGIN_MM * mm
        self.saveState()
        self.setFont("Helvetica", 7)
        self.setFillColorRGB(0.55, 0.55, 0.55)
        self.drawRightString(w - marg, marg * 0.45, f"Pagina {huidige} van {totaal}")
        self.restoreState()


# ──────────────────────────────────────────────
# PDF — paginakop
# ──────────────────────────────────────────────

def _locatie_tekst(punten, lat, lon):
    """Korte weergave van de puntlocatie(s) voor paginakoppen."""
    if punten and len(punten) > 1:
        return f"{len(punten)} puntlocaties"
    return f"lat={lat:.6f}, lon={lon:.6f}"


def _binnen_tekst(item, n_punten):
    """'Puntlocatie binnen gebied', bij meerdere locaties met de volgnummers erbij."""
    nrs = item.get("punten_binnen") or []
    if n_punten > 1 and nrs:
        return f"Puntlocatie {', '.join(map(str, nrs))} (van {n_punten}) binnen gebied"
    return "Puntlocatie binnen gebied"


def _pdf_pagina_kop(c, y, lat, lon, straal, datum_leesbaar, pagina_titel, page_w=None, punten=None):
    PAGE_W = page_w if page_w is not None else A4[0]
    MARGIN = _PDF_MARGIN_MM * mm
    x = MARGIN

    c.setFont("Helvetica-Bold", 11)
    c.setFillColorRGB(0.10, 0.32, 0.46)
    c.drawString(x, y, f"TUG-ontheffingen — Stap 4  |  {pagina_titel}")
    y -= 15

    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.42, 0.42, 0.42)
    meta = (f"Gegenereerd: {datum_leesbaar}   •   "
            f"{_locatie_tekst(punten, lat, lon)}, straal={straal:.0f} m   •   "
            f"workflowversie {VERSION}")
    c.drawString(x, y, meta)
    y -= 8

    c.setStrokeColorRGB(0.72, 0.72, 0.72)
    c.setLineWidth(0.5)
    c.line(x, y, PAGE_W - MARGIN, y)
    y -= 10
    return y


# ──────────────────────────────────────────────
# PDF — gestructureerd proceslogboek
# ──────────────────────────────────────────────

_MANEGE_ZOEKTERMEN = MANEGE_ZOEKTERMEN  # alias voor leesbaarheid in deze module


# ──────────────────────────────────────────────
# PDF-proceslogbouwer
# Eén klasse met één methode per paragraaf, zodat een wijziging aan paragraaf H7
# niet de andere paragrafen raakt. Tekenprimitieven (kop/regel/schrijf/logregels)
# zijn instance-methoden i.p.v. nested closures.
# ──────────────────────────────────────────────

class _ProcesLogBuilder:
    """Bouwt het gestructureerde proceslogboek-PDF in 12 paragrafen."""

    # Layout-constanten
    LH_B   = 11    # body regelafstand
    LH_K   = 14    # paragraafkop regelafstand
    FS_B   = 8     # body fontgrootte
    FS_K   = 9.5   # paragraafkop fontgrootte
    FS_SK  = 8.5   # subkop fontgrootte
    FONT   = "Helvetica"
    FONT_B = "Helvetica-Bold"

    KLEUR_KOP   = (0.10, 0.32, 0.46)
    KLEUR_ROOD  = (0.80, 0.05, 0.05)
    KLEUR_BODY  = (0.10, 0.10, 0.10)
    KLEUR_SUB   = (0.25, 0.25, 0.25)
    KLEUR_MUT   = (0.30, 0.30, 0.30)

    def __init__(self, c, state):
        self.c          = c
        self.state      = state
        self.PAGE_W, self.PAGE_H = A4
        self.MARGIN     = _PDF_MARGIN_MM * mm
        self.MAX_W      = self.PAGE_W - 2 * self.MARGIN
        self.x          = self.MARGIN

        self.ruimtelijk = state.get("ruimtelijk", {})
        self.aanvraag   = state.get("aanvraag", {})
        self.validatie  = state.get("validatie", {})
        self.classif    = state.get("classificatie", {})
        self.stat       = self.ruimtelijk.get("statistieken", {})
        self.lat        = self.ruimtelijk.get("lat", 0)
        self.lon        = self.ruimtelijk.get("lon", 0)
        self.punten     = self.ruimtelijk.get("punten") or [[self.lat, self.lon]]
        self.toeslag    = self.ruimtelijk.get("toeslag_m", TOETSING_TOESLAG_M)
        self.straal     = self.ruimtelijk.get("straal", 0)
        self.datum_l    = self.ruimtelijk.get("datum_leesbaar", "")
        self.rlog       = self.ruimtelijk.get("log_regels", [])
        self.bronstatus = {u["sleutel"]: u for u in uitkomsten_uit_state(state)}
        self.onvolledig = onvolledige_toetsing(state)

        self.signaal_straal = self.straal + MANEGE_SIGNAAL_MARGE
        self._y             = 0.0

    # ── Tekenprimitieven ──────────────────────

    def _kop_teken(self):
        cy = self.PAGE_H - self.MARGIN
        self.c.setFont(self.FONT_B, 11)
        self.c.setFillColorRGB(*self.KLEUR_KOP)
        self.c.drawString(self.x, cy, "Inventarisatie kwetsbare gebouwen en functies TUG")
        cy -= 8
        self.c.setStrokeColorRGB(0.72, 0.72, 0.72)
        self.c.setLineWidth(0.5)
        self.c.line(self.x, cy, self.PAGE_W - self.MARGIN, cy)
        self._y = cy - 10

    def check(self, reserve=None):
        if reserve is None:
            reserve = self.LH_B * 4
        if self._y < self.MARGIN + reserve:
            self.c.showPage()
            self._kop_teken()

    def _wrap(self, tekst, font, fs, ind=0):
        mw = self.MAX_W - ind
        woorden = tekst.split()
        regels, huidig = [], ""
        for w in woorden:
            k = (huidig + " " + w).strip()
            if self.c.stringWidth(k, font, fs) <= mw:
                huidig = k
            else:
                if huidig:
                    regels.append(huidig)
                huidig = w
        if huidig:
            regels.append(huidig)
        return regels or [""]

    def schrijf(self, tekst, font=None, fs=None, lh=None, kleur=None, ind=0):
        font  = font  or self.FONT
        fs    = fs    or self.FS_B
        lh    = lh    or self.LH_B
        kleur = kleur or self.KLEUR_BODY
        self.c.setFont(font, fs)
        self.c.setFillColorRGB(*kleur)
        for r in self._wrap(tekst, font, fs, ind):
            self.check(lh * 3)
            self.c.drawString(self.x + ind, self._y, r)
            self._y -= lh

    def kop(self, tekst, rood=False):
        self._y -= 5
        self.check(self.LH_K * 5)
        kleur = self.KLEUR_ROOD if rood else self.KLEUR_KOP
        self.schrijf(tekst, self.FONT_B, self.FS_K, self.LH_K, kleur)
        self._y -= 2

    def subkop(self, tekst):
        self._y -= 3
        self.check(self.LH_B * 4)
        self.schrijf(tekst, self.FONT_B, self.FS_SK, self.LH_B + 1, self.KLEUR_SUB)

    def regel(self, tekst, rood=False, ind=0):
        kleur = self.KLEUR_ROOD if rood else self.KLEUR_BODY
        self.schrijf(tekst, self.FONT, self.FS_B, self.LH_B, kleur, ind)

    def lege(self):
        self._y -= self.LH_B * 0.4

    def logregels(self, markers, stop_markers=None, *, skip_stap_header=True):
        """Extraheer en toon log-regels die vallen binnen een stap-marker."""
        actief = False
        for r in self.rlog:
            rs = r.strip()
            if not rs:
                continue
            if any(m in rs for m in markers):
                actief = True
            if actief and stop_markers and any(m in rs for m in stop_markers):
                break
            if actief:
                if skip_stap_header and rs.startswith("Stap") and not any(m in rs for m in markers):
                    break
                if rs.startswith("Stap") and any(m in rs for m in markers):
                    continue  # sla stap-header zelf over
                rood = "TREFFER" in rs or "CONFLICT" in rs or "✗" in rs
                self.regel(rs, rood=rood, ind=8)

    # ── Bronstatus ───────────────────────────

    def niet_getoetst(self, sleutel):
        """True als de bron niet is geraadpleegd — of als dat niet is vastgelegd."""
        uitkomst = self.bronstatus.get(sleutel)
        return uitkomst is None or uitkomst.get("status") == MISLUKT

    def bronregels(self, *sleutels):
        """Per bron: grijs bij een lokale kopie, rood als zij niet volledig is geraadpleegd."""
        for sleutel in sleutels:
            uitkomst = self.bronstatus.get(sleutel)
            if uitkomst is None:
                self.regel(f"⚠ {BRONNEN[sleutel].label}: geen bronstatus vastgelegd — "
                           f"niet aantoonbaar geraadpleegd.", rood=True)
            elif not is_volledig(uitkomst):
                self.regel(f"⚠ {beschrijf(uitkomst)}", rood=True)
            elif uitkomst.get("status") == CACHE:
                self.regel(f"Bronstatus: {beschrijf(uitkomst)}")

    def geen_conclusie(self, onderwerp):
        self.regel(
            f"Geen conclusie: {onderwerp} is niet (volledig) getoetst omdat de bron niet "
            f"volledig is geraadpleegd. Dat hier niets wordt gemeld, betekent niet dat er "
            f"niets is.",
            rood=True,
        )

    # ── Paragrafen ───────────────────────────

    def _h0_opening(self):
        model_zin = (
            f"Taalmodel: {MODEL_LABEL}."
            if MODEL_LABEL
            else "Bij de verwerking is geen taalmodel ingezet."
        )
        self.schrijf(
            "Proceslogboek geautomatiseerde inventarisatie van kwetsbare gebouwen en functies "
            "ten behoeve van de beoordeling van ontheffingaanvragen TUG "
            "(artikel 8a.51 Wet luchtvaart). "
            f"Gegenereerd: {self.datum_l}. Workflowversie (git): {VERSION}. "
            f"{model_zin}",
            kleur=self.KLEUR_MUT,
        )
        self.lege()
        if self.onvolledig:
            self.kop("⚠ Onvolledige toetsing", rood=True)
            self.regel(
                "De volgende bronnen zijn niet (volledig) geraadpleegd. Voor wat zij hadden "
                "moeten opleveren trekt dit rapport geen conclusie; de vergunningverlener "
                "beoordeelt of de aanvraag opnieuw moet worden doorgerekend.",
                rood=True, ind=8,
            )
            for uitkomst in self.onvolledig:
                self.regel(f"• {beschrijf(uitkomst)}", rood=True, ind=12)
        elif self.bronstatus:
            self.regel("Alle bronnen die de toetsing bepalen zijn geraadpleegd; per paragraaf "
                       "staat of een lokale kopie is gebruikt.")
        else:
            self.kop("⚠ Bronstatus ontbreekt", rood=True)
            self.regel("Deze state bevat geen bronstatus; niet vast te stellen of alle "
                       "bronnen zijn geraadpleegd.", rood=True, ind=8)
        weergave = [u for u in uitkomsten_uit_state(self.state)
                    if not u.get("toetsingsrelevant", True) and not is_volledig(u)]
        for uitkomst in weergave:
            self.schrijf(f"Weergave (geen invloed op de toetsing): {beschrijf(uitkomst)}",
                         kleur=self.KLEUR_MUT)

    def _h1_input(self):
        self.kop("1. Inputparameters aanvraag")
        naam = self.aanvraag.get("naam", "")
        if naam:
            self.regel(f"Naam aanvraag: {naam}")
        vluchtdata = self.aanvraag.get("datum_vlucht") or []
        if isinstance(vluchtdata, str):
            vluchtdata = [vluchtdata]
        lv_lijst = self.aanvraag.get("luchtvaartuigen", [])
        lv_str = ", ".join(
            f"{lv.get('registratie','?')} (type: {lv.get('type','?')}"
            + (f", geluidsafstand {lv['afstand_m']} m handmatig" if lv.get("afstand_m") else "")
            + ")"
            for lv in lv_lijst
        )
        self.regel(f"Soort ontheffing: {self.aanvraag.get('soort_ontheffing', '—')}")
        vluchtdata_str = ', '.join(vluchtdata) if vluchtdata else "— ONBEKEND —"
        self.regel(f"Vluchtdatum/data: {vluchtdata_str}", rood=not bool(vluchtdata))
        self.regel(
            f"Aantal vluchten: {self.aanvraag.get('aantal_vluchten', '—')}  |  "
            f"UDP: {'ja' if self.aanvraag.get('vlucht_udp') else 'nee'}"
        )
        self.regel(f"Luchtvaartuigen: {lv_str}")
        for i, (p_lat, p_lon) in enumerate(self.punten, 1):
            nr = f" {i}" if len(self.punten) > 1 else ""
            self.regel(f"Puntlocatie{nr} (WGS84): lat={p_lat:.6f}, lon={p_lon:.6f}")
        self.regel(
            f"Ondertekend: {self.aanvraag.get('datum_ondertekening','—')} "
            f"om {self.aanvraag.get('tijdstip_ondertekening','—')}"
        )
        self.lege()
        val_ok = self.validatie.get("geslaagd", True)
        self.regel(
            f"Volledigheidscheck: {'geslaagd' if val_ok else 'MISLUKT'}", rood=not val_ok
        )
        for f in self.validatie.get("fouten", []):
            self.regel(f"Fout: {f}", rood=True, ind=12)
        ontbrekende = self.validatie.get("ontbrekende_velden", [])
        if ontbrekende:
            self.kop("⚠ Ontbrekende verplichte velden", rood=True)
            for v in ontbrekende:
                self.regel(
                    f"'{v}' ontbreekt in de aanvraag. Dient te worden aangevuld door de aanvrager.",
                    rood=True, ind=8,
                )
        if self.validatie.get("datum_vlucht_ontbreekt"):
            self.kop("⚠ Vluchtdatum onbekend", rood=True)
            self.regel(
                "datum_vlucht is niet ingevuld. Vergunningverlener dient de vluchtdatum "
                "handmatig aan te vullen voordat de ontheffing kan worden verleend.",
                rood=True, ind=8,
            )
        termijn_melding = self.validatie.get("indientermijn_melding")
        termijn_ok      = self.validatie.get("indientermijn_ok")
        if termijn_melding:
            self.regel(f"Indientermijn: {termijn_melding}", rood=(termijn_ok is False))

    def _h2_classificatie(self):
        self.kop("2. Classificatie luchtvaartuigen en toetsingsafstand")
        self.schrijf(
            "De toetsingsafstand volgt uit de NLR-indelingslijst CR-96650L (Suppl. 1, okt. 2022). "
            "Per luchtvaartuig wordt via het ILT Luchtvaartregister de ICAO-code opgezocht, "
            "waarna de bijbehorende Appendix-categorie en afstandsnorm worden bepaald. "
            "Bij meerdere luchtvaartuigen geldt het maximum van de individuele normen. "
            "Is een luchtvaartuig niet in het register te vinden, of leidt de ICAO-code niet "
            "tot een vastgestelde afstandsnorm, dan wordt geen norm aangenomen: het "
            "luchtvaartuig wordt hieronder in rood gesignaleerd en blijft buiten de toetsing. "
            "Het register is te downloaden via: "
            "https://www.ilent.nl/documenten/lijsten/luchtvaart/databestanden/luchtvaartregister-data"
        )
        self.lege()
        lv_resultaten = self.classif.get("luchtvaartuigen", [])
        bron_labels   = {
            "nlr_tabel":              "NLR-tabel",
            "pm_geen_norm":           "PM-categorie, geen norm vastgesteld in NLR-tabel",
            "fallback_150m":          "niet in NLR-tabel, beleidsregel 150 m toegepast",
            "niet_in_register":       "niet gevonden in ILT luchtvaartregister",
            "geen_registratie":       "registratiekenmerk ontbreekt in de aanvraag",
            "handmatig":              "handmatig opgegeven in de aanvraag",
        }
        zonder_norm = []
        for lv in lv_resultaten:
            kenmerk = lv.get("registratie", "?")
            icao = lv.get("icao_code") or "niet gevonden in register"
            cat  = lv.get("appendix_categorie") or "—"
            norm = lv.get("norm_m")
            bron = lv.get("norm_bron", "")
            bron_s = bron_labels.get(bron, bron)
            if norm is None:
                zonder_norm.append((kenmerk, bron, icao, cat))
                self.regel(
                    f"{kenmerk}: ICAO {icao} → Appendix {cat} → geen afstandsnorm "
                    f"bepaald ({bron_s}) — buiten de toetsing gelaten",
                    rood=True,
                )
                continue  # toelichting volgt in het rode blok hieronder
            if bron == "handmatig":
                self.regel(f"{kenmerk}: geluidsafstand {norm} m ({bron_s})")
            else:
                self.regel(
                    f"{kenmerk}: ICAO {icao} "
                    f"→ Appendix {cat} → norm {norm} m ({bron_s})"
                )
            for sig in lv.get("signalen", []):
                self.regel(f"Signalering: {sig}", rood=True, ind=12)
        self.lege()

        if zonder_norm:
            self.kop("⚠ Luchtvaartuig zonder herleidbare afstandsnorm", rood=True)
            for kenmerk, bron, icao, cat in zonder_norm:
                if bron == "geen_registratie":
                    reden = (
                        "Bij een van de opgegeven luchtvaartuigen ontbreekt het "
                        "registratiekenmerk; er is geen register te raadplegen."
                    )
                elif bron == "niet_in_register":
                    reden = (
                        f"Het luchtvaartuig met kenmerk {kenmerk} is niet gevonden in het "
                        f"ILT luchtvaartuigregister (registratie mogelijk in een ander land "
                        f"of onbekend)."
                    )
                else:
                    reden = (
                        f"Voor het luchtvaartuig met kenmerk {kenmerk} is de ICAO-code "
                        f"{icao} niet herleidbaar naar een afstandsnorm: Appendix-categorie "
                        f"{cat} heeft in de NLR-indelingslijst geen vastgestelde norm (PM)."
                    )
                self.regel(reden, rood=True, ind=8)
            self.regel(
                "Voor deze luchtvaartuigen is géén afstandsnorm aangenomen; zij zijn buiten "
                "de toetsingsafstand gelaten. De vergunningverlener "
                "bepaalt of deze luchtvaartuigen in de ontheffing worden opgenomen en, zo ja, "
                "welke norm daarvoor geldt.",
                rood=True, ind=8,
            )
            self.lege()

        norm_toep  = self.classif.get("norm_toepassing")
        if self.classif.get("norm_herleidbaar", True):
            maatgevend = [lv["registratie"] for lv in lv_resultaten
                          if lv.get("norm_m") == norm_toep]
            self.regel(
                f"Maatgevende toetsingsstraal: {norm_toep} m "
                f"(maatgevend: {', '.join(maatgevend)})."
                + (
                    f" Niet meegewogen: {', '.join(k for k, *_ in zonder_norm)}."
                    if zonder_norm else ""
                )
            )
        else:
            self.regel(
                f"Geen maatgevende toetsingsstraal: voor geen enkel luchtvaartuig is een "
                f"afstandsnorm herleidbaar. De omgeving is geïnventariseerd tot de ruimste "
                f"norm uit de NLR-tabel ({norm_toep} m), zodat niets buiten beeld blijft. "
                f"Dit is géén toetsing: de adressenlijst en de kaarten tonen alles binnen "
                f"{norm_toep} m, ook wat bij de werkelijke norm buiten de toetsingsafstand "
                f"valt. De vergunningverlener bepaalt de norm en beoordeelt de lijst daarop.",
                rood=True,
            )
        reg_datum = self.classif.get("register_datum", "")
        if reg_datum:
            try:
                reg_d = datetime.fromisoformat(reg_datum)
                publicatie = self.classif.get("register_publicatie", "")
                gepubliceerd = (f"gepubliceerd door de ILT op "
                                f"{datetime.fromisoformat(publicatie).strftime('%d-%m-%Y')}, "
                                if publicatie else "")
                self.regel(
                    f"ILT-register: {self.classif.get('register_bestand','—')}, "
                    f"{gepubliceerd}gedownload op {reg_d.strftime('%d-%m-%Y')}."
                )
            except (ValueError, TypeError):
                self.regel(f"ILT-register: {self.classif.get('register_bestand', '—')}, "
                           f"downloaddatum onleesbaar ({reg_datum!r}).")
        self.bronregels("ilt_register")

    def _h3_gps_rd(self):
        self.kop("3. GPS/RD-omzetting en toetsingsafstanden")
        self.schrijf(
            "De opgegeven GPS-coördinaten (WGS84, EPSG:4326) worden omgezet naar "
            "Rijksdriehoekscoördinaten (RD New, EPSG:28992), zodat afstandsberekeningen in meters "
            "nauwkeurig uitvoerbaar zijn. Alle cirkelbuffers worden in RD aangemaakt."
        )
        self.lege()
        rd_regels = [r.strip() for r in self.rlog if "RD: x=" in r]
        for i, (p_lat, p_lon) in enumerate(self.punten, 1):
            rd_r = rd_regels[i - 1].split("RD: ", 1)[-1] if i <= len(rd_regels) else "—"
            nr = f"Puntlocatie {i}:  " if len(self.punten) > 1 else ""
            self.regel(f"{nr}GPS (WGS84) lat={p_lat:.6f}, lon={p_lon:.6f}  →  "
                       f"RD (EPSG:28992) {rd_r}")
        if len(self.punten) > 1:
            self.regel(puntafstand_melding(self.ruimtelijk.get("max_onderlinge_afstand_m", 0)))
            self.regel(
                "Toetsings-, marge- en aandachtsgebied omvatten de cirkels rond alle "
                "puntlocaties samen; afstanden gelden tot de dichtstbijzijnde puntlocatie."
            )
        self.regel(
            f"Toetsingsafstand:  {self.straal:.0f} m  "
            f"(Lden-afstand {self.straal - self.toeslag:.0f} m + "
            f"marge {meters(self.toeslag)} m rond de puntlocatie"
            + (", afwijkend van de standaard"
               if self.toeslag != TOETSING_TOESLAG_M else "") + ")"
        )
        self.regel(
            f"Margeband:  {self.straal + MARGE_M:.0f} m  "
            f"(toetsingsafstand + {MARGE_M} m)"
        )
        self.regel(
            f"Aandachtsgebied maneges:  {self.signaal_straal:.0f} m  "
            f"(toetsingsafstand + {MANEGE_SIGNAAL_MARGE} m)"
        )

    def _h4_n2000(self):
        n2000_in    = self.ruimtelijk.get("n2000_in_straal", [])
        n2000_nabij = self.ruimtelijk.get("n2000_in_signaal", [])
        self.kop("4. Natura 2000", rood=bool(n2000_in))
        self.schrijf(
            f"De puntlocatie wordt getoetst aan Natura 2000-gebiedsgrenzen via de PDOK WFS-dienst "
            f"(service.pdok.nl, laag inspire:PS.ProtectedSite). De geometrieën worden on-the-fly "
            f"opgehaald binnen een bounding box van ±{N2000_SIGNAAL_MARGE} m rondom "
            f"de puntlocatie. "
            f"Er wordt onderscheid gemaakt tussen gebieden waarbinnen de puntlocatie "
            f"valt (treffer) "
            f"en gebieden die op minder dan {N2000_SIGNAAL_MARGE} m van de "
            f"puntlocatie liggen (nabij)."
        )
        self.lege()
        self.bronregels("natura2000")
        if self.niet_getoetst("natura2000"):
            self.geen_conclusie("Natura 2000")
            return
        if n2000_in:
            self.regel(
                f"TREFFER — puntlocatie ligt binnen {len(n2000_in)} Natura 2000-gebied(en):",
                rood=True,
            )
            for item in n2000_in:
                self.regel(f"• {item['naam']} — "
                           f"{_binnen_tekst(item, len(self.punten))}", rood=True, ind=12)
        else:
            self.regel("Puntlocatie ligt niet binnen een Natura 2000-gebied.")
            if n2000_nabij:
                self.lege()
                self.regel(
                    f"{len(n2000_nabij)} Natura 2000-gebied(en) nabij de puntlocatie "
                    f"(< {N2000_SIGNAAL_MARGE} m):"
                )
                for item in n2000_nabij:
                    self.regel(f"• {item['naam']}  (ca. {item['afstand_m']:.0f} m)", ind=12)
            else:
                self.regel(f"Geen Natura 2000-gebieden binnen {N2000_SIGNAAL_MARGE} m.")

    def _h5_nnn(self):
        nnn_in    = self.ruimtelijk.get("nnn_in_straal", [])
        nnn_nabij = self.ruimtelijk.get("nnn_in_signaal", [])
        self.kop("5. Natuurnetwerk Nederland (NNN)", rood=bool(nnn_in))
        self.schrijf(
            "NNN-gebiedsgrenzen worden niet on-the-fly opgehaald maar geladen uit een lokaal "
            "GeoPackage-cachebestand, aangemaakt vanuit de ATOM-feed van de provincie "
            "(https://service.pdok.nl/provincies/natuurnetwerk-nederland/atom/downloads/"
            "inspire-pv-ps.nlps-nnn.gml). "
            f"De cache heeft een geldigheidsduur van {NNN_TTL_DAGEN} dagen. "
            "Bij elke run wordt de aanmaakdatum gecontroleerd; bij een verlopen cache wordt "
            "het bestand opnieuw samengesteld uit de ATOM-brondata. "
            "Alle N2000-gebieden zijn tevens NNN; als de puntlocatie binnen N2000 valt maar "
            "de NNN-cache dit niet signaleert, wordt de NNN-treffer automatisch aangenomen."
        )
        self.lege()
        self.bronregels("nnn")
        if self.niet_getoetst("nnn"):
            self.geen_conclusie("het Natuurnetwerk Nederland")
            return
        if nnn_in:
            self.regel(
                f"TREFFER — puntlocatie ligt binnen {len(nnn_in)} NNN-gebied(en):", rood=True
            )
            for item in nnn_in:
                self.regel(f"• {item['naam']} — "
                           f"{_binnen_tekst(item, len(self.punten))}", rood=True, ind=12)
        else:
            self.regel("Puntlocatie ligt niet binnen een NNN-gebied.")
            if nnn_nabij:
                self.lege()
                self.regel(
                    f"{len(nnn_nabij)} NNN-gebied(en) overlappen de toetsingsafstand "
                    f"(< {NNN_SIGNAAL_MARGE} m van de puntlocatie)."
                )
            else:
                self.regel("Geen NNN-gebieden die de toetsingsafstand overlappen.")

    def _h6_bag(self):
        self.kop("6. Inventarisatie verblijfsobjecten (BAG WFS v2.0)")
        self.bronregels("bag_verblijfsobjecten", "bag_gevelcheck", "adresaanvulling",
                        "gevelcontouren")

        self.subkop("Stap 1 — Bounding box en BAG-query")
        self.schrijf(
            f"Verblijfsobjecten worden opgehaald via de BAG WFS v2.0 (service.pdok.nl). "
            f"De bounding box is gebaseerd op de buitenrand van de margeband "
            f"({self.straal + MARGE_M:.0f} m). Er worden uitsluitend de 9 benodigde eigenschappen "
            f"opgevraagd: identificatie (deduplicatie), gebruiksdoel (geluidsgevoeligheidsfilter), "
            f"openbare_ruimte / huisnummer / huisletter / toevoeging / postcode / woonplaats "
            f"(adressamenstelling) en pandidentificatie (gevel-check)."
        )
        self.logregels(["bbox", "verblijfsobjecten opgehaald"], stop_markers=["Stap 4:"])

        self.subkop("Stap 2 — Filtering op punten binnen de toetsingsafstand")
        self.schrijf(
            f"De opgehaalde verblijfsobjecten worden gepuntcontroleerd: alleen objecten waarvan "
            f"de puntgeometrie binnen of op de rand van de toetsingsafstand ({self.straal:.0f} m) "
            f"valt, worden meegenomen in de verdere analyse."
        )
        self.logregels(
            ["vallen binnen de straalcirkel", "Totaal features binnen straal"],
            stop_markers=["Stap 4b"],
        )

        self.subkop("Stap 3 — Deduplicatie en reverse geocode")
        self.schrijf(
            "Verblijfsobjecten met dezelfde BAG-identificatie worden samengevoegd. "
            "Voor objecten zonder woonplaats of straatnaam wordt via de PDOK Locatieserver "
            "een reverse geocode uitgevoerd om het adres aan te vullen."
        )
        self.logregels(["Stap 4b"], stop_markers=["Stap 4d"])

        self.subkop("Stap 4 — Marge-adressen (margeband)")
        self.schrijf(
            f"Verblijfsobjecten in de margeband "
            f"({self.straal:.0f}–{self.straal + MARGE_M:.0f} m) worden "
            f"apart geïnventariseerd. Deze objecten worden op de kaarten weergegeven, maar "
            f"niet in de adressenlijst opgenomen. Geluidsgevoelige objecten in deze band "
            f"doorlopen de gevel-check (stap 6)."
        )
        self.logregels(["Stap 4d"], stop_markers=["Stap 5"])

        self.subkop("Stap 5 — Filtering op geluidsgevoelige gebruiksdoelen")
        self.schrijf(
            "Alleen objecten met een geluidsgevoelig gebruiksdoel worden als wettelijk relevant "
            "beschouwd: woonfunctie, gezondheidszorgfunctie, onderwijsfunctie. "
            "Logiesfunctie wordt niet als geluidsgevoelig aangemerkt."
        )
        self.logregels(["Stap 5"], stop_markers=["Stap 6"])

        self.subkop("Stap 6 — Gevel-check (pandgeometrie BAG)")
        self.schrijf(
            "Voor elk geluidsgevoelig verblijfsobject in de margeband wordt de pandgeometrie "
            "opgehaald via de BAG WFS. Als de gevel de toetsingsafstand snijdt of er volledig "
            "binnen valt, wordt het VBO gepromoveerd van margeband naar wettelijk relevant — "
            "ook al ligt het BAG-adres buiten de toetsingsafstand."
        )
        self.logregels(["Stap 6"], stop_markers=["Stap 7"])

    def _h7_begraafplaatsen(self):
        self.kop("7. Begraafplaatsen")
        self.schrijf(
            f"Begraafplaatsen worden opgespoord via de PDOK Locatieserver (BRT-dataset) op "
            f"zoektermen 'begraafplaats' en 'erebegraafplaats', binnen een zoekafstand van "
            f"straal + 1.500 m. De polygoongeometrie van gevonden kandidaten wordt getoetst: "
            f"snijdt of overlapt de polygoon de toetsingsafstand ({self.straal:.0f} m), "
            f"dan is de begraafplaats wettelijk relevant."
        )
        self.bronregels("begraafplaatsen")
        if self.niet_getoetst("begraafplaatsen"):
            self.geen_conclusie("de aanwezigheid van begraafplaatsen")
        self.logregels(["Stap 7"], stop_markers=["Stap 8"])

    def _h8_kdv(self):
        self.kop("8. Kinderopvanglocaties (KDV — LRK/BAG-matching)")
        self.schrijf(
            "Kinderdagverblijven (KDV) worden geïdentificeerd via koppeling van het Landelijk "
            "Register Kinderopvang (LRK, CSV van LRK/RvIG) aan BAG-verblijfsobjecten op basis "
            f"van BAG-identificatiecode. Het LRK-bestand wordt lokaal gecachet "
            f"(TTL: {LRK_CACHE_DAYS} dagen); bij een verlopen cache wordt het opnieuw gedownload. "
            "De koppeling maakt gebruik van de BAG-eigenschappen identificatie "
            "en pandidentificatie."
        )
        self.bronregels("lrk")
        if self.niet_getoetst("lrk"):
            self.geen_conclusie("de aanwezigheid van kinderopvanglocaties")
        self.logregels(["Stap 8", "LRK CSV", "KDV"], stop_markers=["Stap 9"])

    def _h9_scholen(self):
        self.kop("9. Scholen (DUO Open Onderwijsdata)")
        self.schrijf(
            "Schoollocaties worden opgehaald via DUO Open Onderwijsdata (GeoJSON). Er worden "
            "twee groepen geraadpleegd. PO (primair onderwijs): "
            "https://onderwijsdata.duo.nl/datastore/dump/"
            "dcc9c9a5-6d01-410b-967f-810557588ba4?format=json. "
            "Overig (SO/VO/MBO/HO): "
            "https://onderwijsdata.duo.nl/datastore/dump/"
            "8f0f1639-712d-4adb-bb59-cabd43730dc8?format=json (SO), "
            "https://onderwijsdata.duo.nl/datastore/dump/"
            "5187f8d5-ff9c-4284-8e06-4311f0354956?format=json (VO), "
            "https://onderwijsdata.duo.nl/datastore/dump/"
            "1a946297-a7ca-48d5-9ae8-19ad73bf8176?format=json (MBO), "
            "https://onderwijsdata.duo.nl/datastore/dump/"
            "bf1da9c6-c688-4873-91b1-b12c9ac2c132?format=json (HO). "
            "De GeoJSON-bestanden worden lokaal gecachet en gehasht (SHA-256 van de inhoud); "
            "bij een gewijzigde hash wordt het bestand opnieuw gedownload en verwerkt."
        )
        self.bronregels("duo")
        if self.niet_getoetst("duo"):
            self.geen_conclusie("de aanwezigheid van scholen")
        scholen_in    = self.ruimtelijk.get("scholen_in_straal", [])
        scholen_marge = self.ruimtelijk.get("scholen_in_marge", [])
        if scholen_in or scholen_marge:
            self.lege()
            self.regel(f"Scholen binnen toetsingsafstand:  {len(scholen_in)}")
            self.regel(f"Scholen in margeband:             {len(scholen_marge)}")
        self.logregels(["Stap 9", "DUO", "scholen"], stop_markers=["Stap 10"])

    def _h10_maneges(self):
        self.kop("10. Maneges")
        self.schrijf(
            f"Maneges worden opgespoord via de PDOK Locatieserver (BRT) binnen een zoekafstand "
            f"van toetsingsafstand + {MANEGE_SIGNAAL_MARGE} m = {self.signaal_straal:.0f} m "
            f"(aandachtsgebied). Gevonden locaties worden getoetst op de toetsingsafstand "
            f"({self.straal:.0f} m) en het aandachtsgebied ({self.signaal_straal:.0f} m)."
        )
        self.bronregels("maneges")
        if self.niet_getoetst("maneges"):
            self.geen_conclusie("de aanwezigheid van maneges")
        self.logregels(["Stap 10:"], stop_markers=["Stap 10b"])

    def _h11_luchthavens(self):
        self.kop("11. Luchthavens")
        self.schrijf(
            f"Luchthaventerreinen (vliegvelden, zweefvliegvelden en "
            f"helikopterlandingsterreinen) worden on-the-fly als vlak opgehaald uit BRT "
            f"Top10NL (PDOK OGC API, functioneel_gebied_vlak), ook over de provinciegrens. "
            f"Aanvullend komen de provinciale luchthavenregelingen als punt uit de WFS-dienst "
            f"van GeoPortaal Overijssel (laag B64_nutsvoorzieningen:B6_Luchthaven_puntlocaties); "
            f"een regeling binnen {LUCHTHAVEN_KOPPEL_M} m van een terrein geeft dat terrein "
            f"zijn naam. De afstand wordt gemeten van de puntlocatie tot de rand van het "
            f"terrein (0 m als de puntlocatie erbinnen ligt). Top10NL is een topografische "
            f"registratie en niet de juridische grens uit een luchthavenbesluit. "
            f"Luchthavens binnen {LUCHTHAVEN_GRENS_M} m van de aanvraaglocatie zijn "
            f"NIET TOEGESTAAN. Luchthavens op {LUCHTHAVEN_GRENS_M}–{LUCHTHAVEN_SIGNAAL_M} m "
            f"worden als signalering opgenomen."
        )
        self.bronregels("luchthaventerreinen")
        self.bronregels("luchthavens")
        if self.niet_getoetst("luchthaventerreinen") or self.niet_getoetst("luchthavens"):
            self.geen_conclusie("de afstand tot luchthavens")
        self.logregels(["Stap 10b", "Luchthaven"], stop_markers=["Stap 11"])

    def _h12_synthese(self):
        self.kop("12. Synthese en output")
        wettelijk_n = len(self.ruimtelijk.get("adressen_wettelijk", []))
        marge_n     = len(self.ruimtelijk.get("adressen_marge", []))
        aandacht_n  = len(self.ruimtelijk.get("adressen_aandacht", []))
        overig_n    = len(self.ruimtelijk.get("adressen_overig", []))
        timestamp   = self.ruimtelijk.get("timestamp", "")
        naam        = self.aanvraag.get("naam", "")
        slug        = naam_slug(naam)
        naam_infix  = f"_{slug}" if slug else ""
        pdf_naam    = (f"tug_rapport{naam_infix}_{timestamp}.pdf" if timestamp
                       else "tug_rapport_<naam>_<timestamp>.pdf")
        html_naam   = (f"tug_kaart{naam_infix}_{timestamp}.html" if timestamp
                       else "tug_kaart_<naam>_<timestamp>.html")
        self.schrijf(
            "Op basis van de bovenstaande inventarisatie zijn de adressen gecategoriseerd "
            "en opgenomen in de adressenlijst. De resultaten zijn verwerkt in een PDF-rapport "
            "(adressenlijst + situatie- en omgevingskaart, elk als luchtfoto en als "
            "topografische kaart) en een interactieve HTML-kaart op luchtfoto, "
            "omschakelbaar naar de topografische kaart. "
            "De tijdelijke procesdata (tug_state.json) wordt na voltooiing gewist "
            "in het kader van dataveiligheid."
        )
        self.lege()
        self.regel(f"PDF-rapport:   {pdf_naam}")
        self.regel(f"HTML-kaart:    {html_naam}")
        self.lege()
        self.regel(f"Wettelijk relevant (instemmingsverklaring vereist):  {wettelijk_n}")
        self.regel(f"Margeband (alleen op kaart, niet in adressenlijst):  {marge_n}")
        self.regel(f"Aandachtslocaties (maneges, luchthavens):           {aandacht_n}")
        self.regel(f"Overig (weergave op kaart):                         {overig_n}")
        if self.onvolledig:
            self.lege()
            self.regel(
                "Toetsing onvolledig — "
                + "; ".join(kort(u) for u in self.onvolledig)
                + ". Zie het rode blok bovenaan dit rapport.",
                rood=True,
            )

    def render(self):
        self._kop_teken()
        self._h0_opening()
        self._h1_input()
        self._h2_classificatie()
        self._h3_gps_rd()
        self._h4_n2000()
        self._h5_nnn()
        self._h6_bag()
        self._h7_begraafplaatsen()
        self._h8_kdv()
        self._h9_scholen()
        self._h10_maneges()
        self._h11_luchthavens()
        self._h12_synthese()
        self.c.showPage()


def _pdf_proceslog(c, state):
    """Genereert het gestructureerde proceslogboek als PDF-pagina('s)."""
    _ProcesLogBuilder(c, state).render()


# ──────────────────────────────────────────────
# PDF — adressenlijst (vier secties, landscape A4)
# ──────────────────────────────────────────────

def _pdf_adressen_pagina(c, datum_leesbaar, lat, lon, straal,
                          wettelijk, aandacht, overig, n2000=None, punten=None,
                          onvolledig=None):
    from reportlab.lib.pagesizes import landscape as _landscape

    LS             = _landscape(A4)
    PAGE_W, PAGE_H = LS
    MARGIN         = _PDF_MARGIN_MM * mm
    CONTENT_W      = PAGE_W - 2 * MARGIN
    x              = MARGIN
    LINE_H         = 11
    FS             = 8
    FONT           = "Helvetica"
    FONT_BOLD      = "Helvetica-Bold"

    CX = [0,   220, 370, 640]
    CW = [220, 150, 270, 117]

    c.setPageSize(LS)

    def afkap(tekst, max_pt, font=FONT, size=FS):
        if stringWidth(tekst, font, size) <= max_pt:
            return tekst
        while tekst and stringWidth(tekst + "…", font, size) > max_pt:
            tekst = tekst[:-1]
        return tekst + "…"

    def wrap_tekst(tekst, max_pt, font=FONT, size=FS):
        if stringWidth(tekst, font, size) <= max_pt:
            return [tekst]
        regels = []
        huidig = ""
        for deel in tekst.split(", "):
            kandidaat = huidig + (", " if huidig else "") + deel
            if stringWidth(kandidaat, font, size) <= max_pt:
                huidig = kandidaat
            else:
                if huidig:
                    regels.append(huidig)
                huidig = deel
        if huidig:
            regels.append(huidig)
        return regels or [tekst]

    y_ref = [PAGE_H - MARGIN]

    def y():
        return y_ref[0]

    def set_y(val):
        y_ref[0] = val

    def check_pagina(reserve=LINE_H * 3):
        if y() < MARGIN + reserve:
            c.showPage()
            c.setPageSize(LS)
            set_y(PAGE_H - MARGIN)
            set_y(_pdf_pagina_kop(c, y(), lat, lon, straal,
                                  datum_leesbaar, "Adressenlijst (vervolg)", page_w=PAGE_W,
                                  punten=punten))

    set_y(_pdf_pagina_kop(c, y(), lat, lon, straal, datum_leesbaar, "Adressenlijst",
                          page_w=PAGE_W, punten=punten))

    def teken_sectie_kop(titel, n, extra_header, titel_kleur=(0.10, 0.32, 0.46)):
        check_pagina(LINE_H * 5)
        c.setFont(FONT_BOLD, 9)
        c.setFillColorRGB(*titel_kleur)
        c.drawString(x, y(), f"{titel}  ({n})")
        set_y(y() - LINE_H * 0.9)
        koppen = ["Adres", "Postcode / Woonplaats", "Gebruiksdoel", extra_header]
        c.setFont(FONT_BOLD, 7.5)
        c.setFillColorRGB(0.38, 0.38, 0.38)
        for i, kop in enumerate(koppen):
            c.drawString(x + CX[i], y(), kop)
        set_y(y() - 5)
        c.setStrokeColorRGB(0.65, 0.65, 0.65)
        c.setLineWidth(0.4)
        c.line(x, y(), x + CONTENT_W, y())
        set_y(y() - LINE_H * 0.8)

    def teken_rij(rij, tekst_kleur=(0.10, 0.10, 0.10)):
        gebruiksdoel_regels = wrap_tekst(rij["gebruiksdoel"], CW[2] - 4)
        n = len(gebruiksdoel_regels)
        check_pagina(reserve=LINE_H * (n + 2))
        c.setFont(FONT, FS)
        c.setFillColorRGB(*tekst_kleur)
        y0 = y()
        for i in [0, 1, 3]:
            velden_i = [rij["adres"], rij["pc_wpl"], rij["gebruiksdoel"], rij["extra"]]
            c.drawString(x + CX[i], y0, afkap(velden_i[i], CW[i] - 4))
        for j, regel in enumerate(gebruiksdoel_regels):
            c.drawString(x + CX[2], y0 - j * LINE_H, regel)
        set_y(y0 - n * LINE_H)

    def teken_leeg(tekst):
        check_pagina()
        c.setFont(FONT, FS)
        c.setFillColorRGB(0.55, 0.55, 0.55)
        c.drawString(x, y(), tekst)
        set_y(y() - LINE_H)

    _ROOD = (0.80, 0.05, 0.05)

    # Onvolledige toetsing — bovenaan, zodat de lijst niet als compleet wordt gelezen
    if onvolledig:
        check_pagina(LINE_H * 4)
        c.setFont(FONT_BOLD, 9)
        c.setFillColorRGB(*_ROOD)
        kop = ("⚠ Onvolledige toetsing — deze lijst is mogelijk niet compleet"
               if any(u.get("sleutel") != "afstandsnorm" for u in onvolledig)
               else "⚠ Geen afstandsnorm — deze lijst is een inventarisatie, geen toetsing")
        c.drawString(x, y(), kop)
        set_y(y() - LINE_H)
        c.setFont(FONT, FS)
        for uitkomst in onvolledig:
            check_pagina()
            c.drawString(x + 8, y(), afkap(f"• {beschrijf(uitkomst)}", CONTENT_W - 8))
            set_y(y() - LINE_H)
        set_y(y() - LINE_H)

    # N2000 — bovenaan, alleen als puntlocatie binnen gebied ligt
    n2000_in_straal = (n2000 or {}).get("n2000_in_straal", [])
    if n2000_in_straal:
        teken_sectie_kop(
            "Natura 2000 — puntlocatie binnen gebied", len(n2000_in_straal), "Status",
            titel_kleur=_ROOD,
        )
        for item in n2000_in_straal:
            teken_rij({
                "adres":        item.get("naam", "Onbekend"),
                "pc_wpl":       "",
                "gebruiksdoel": "Natura 2000-gebied",
                "extra":        _binnen_tekst(item, len(punten or [None])),
            }, tekst_kleur=_ROOD)
        set_y(y() - LINE_H * 2)

    # NNN — bovenaan, alleen als puntlocatie binnen gebied ligt
    nnn_in_straal = (n2000 or {}).get("nnn_in_straal", [])
    if nnn_in_straal:
        teken_sectie_kop(
            "Natuurnetwerk Nederland — puntlocatie binnen gebied", len(nnn_in_straal), "Status",
            titel_kleur=_ROOD,
        )
        for item in nnn_in_straal:
            teken_rij({
                "adres":        item.get("naam", "Onbekend"),
                "pc_wpl":       "",
                "gebruiksdoel": "Natuurnetwerk Nederland",
                "extra":        _binnen_tekst(item, len(punten or [None])),
            }, tekst_kleur=_ROOD)
        set_y(y() - LINE_H * 2)

    teken_sectie_kop("Wettelijk relevante adressen", len(wettelijk), "Afstand")
    if wettelijk:
        for rij in wettelijk:
            teken_rij(rij)
    else:
        teken_leeg("Geen wettelijk relevante adressen gevonden.")
    set_y(y() - LINE_H * 2)

    heeft_maneges    = any("manege"    in (r.get("gebruiksdoel") or "").lower() for r in aandacht)
    heeft_luchthaven = any("luchthaven" in (r.get("gebruiksdoel") or "").lower() for r in aandacht)
    teken_sectie_kop(
        "Aandachtslocaties — maneges en luchthavens",
        len(aandacht), "Afstand",
    )
    if aandacht:
        for rij in aandacht:
            teken_rij(rij)
    else:
        teken_leeg("Geen maneges of luchthavens aangetroffen binnen het aandachtsgebied.")
    if aandacht and not heeft_maneges:
        teken_leeg("Geen maneges aangetroffen binnen het aandachtsgebied.")
    if aandacht and not heeft_luchthaven:
        teken_leeg("Geen luchthavens aangetroffen binnen het aandachtsgebied.")
    set_y(y() - LINE_H * 2)

    teken_sectie_kop("Overige adressen", len(overig), "Afstand")
    if overig:
        for rij in overig:
            teken_rij(rij)
    else:
        teken_leeg("Geen overige adressen.")

    c.showPage()


# ──────────────────────────────────────────────
# PDF — kaart-titel overlay
# ──────────────────────────────────────────────

def _pdf_kaart_titel(c, titel, w_pt, h_pt, x0_pt, y0_pt):
    FS  = 20
    PAD = 6
    tekst_b = stringWidth(titel, "Helvetica-Bold", FS)
    box_w   = tekst_b + PAD * 2
    box_h   = FS + PAD * 2
    bx = x0_pt + (w_pt - box_w) / 2
    by = y0_pt + h_pt - box_h - 6
    c.setFillColorRGB(1, 1, 1)
    c.roundRect(bx, by, box_w, box_h, 4, stroke=0, fill=1)
    c.setFillColorRGB(0.08, 0.26, 0.42)
    c.setFont("Helvetica-Bold", FS)
    c.drawString(bx + PAD, by + PAD + 2, titel)


# ──────────────────────────────────────────────
# PDF-export — synthetiseert rapport uit state
# ──────────────────────────────────────────────

def genereer_pdf(state, log):
    ruimtelijk     = state["ruimtelijk"]
    lat            = ruimtelijk["lat"]
    lon            = ruimtelijk["lon"]
    straal         = ruimtelijk["straal"]
    datum_leesbaar = ruimtelijk["datum_leesbaar"]
    timestamp      = ruimtelijk["timestamp"]
    wettelijk      = ruimtelijk["adressen_wettelijk"]

    aandacht       = ruimtelijk["adressen_aandacht"]
    overig         = ruimtelijk["adressen_overig"]
    kaarten_png    = ruimtelijk.get("kaarten_png", [])

    slug = naam_slug(state.get("aanvraag", {}).get("naam", ""))
    naam_infix = f"_{slug}" if slug else ""
    bestand = OUTPUT_DIR / f"tug_rapport{naam_infix}_{timestamp}.pdf"
    c = _NumberedCanvas(str(bestand), pagesize=A4)
    c.setSubject(f"workflow_versie: {VERSION}")

    _pdf_proceslog(c, state)
    signaleringen = {
        "n2000_in_straal":  ruimtelijk.get("n2000_in_straal", []),
        "n2000_in_signaal": ruimtelijk.get("n2000_in_signaal", []),
        "nnn_in_straal":    ruimtelijk.get("nnn_in_straal", []),
        "nnn_in_signaal":   ruimtelijk.get("nnn_in_signaal", []),
    }
    _pdf_adressen_pagina(c, datum_leesbaar, lat, lon, straal, wettelijk, aandacht, overig,
                         n2000=signaleringen, punten=ruimtelijk.get("punten"),
                         onvolledig=onvolledige_toetsing(state))

    c.setPageSize(A4)
    x0_pt = _PDF_MARGIN_MM * mm
    y0_pt = _PDF_MARGIN_MM * mm
    w_pt  = (_PDF_PAGE_W_MM - 2 * _PDF_MARGIN_MM) * mm
    h_pt  = (_PDF_PAGE_H_MM - 2 * _PDF_MARGIN_MM) * mm

    for kaart in kaarten_png:
        c.drawImage(ImageReader(kaart["pad"]), x0_pt, y0_pt, width=w_pt, height=h_pt)
        _pdf_kaart_titel(c, kaart["titel"], w_pt, h_pt, x0_pt, y0_pt)
        c.showPage()

    c.save()
    log(f"  PDF opgeslagen: {bestand}")
    return bestand


# ──────────────────────────────────────────────
# HTML-export — interactieve Leaflet-kaart
# ──────────────────────────────────────────────

def genereer_html(state, log):
    ruimtelijk     = state["ruimtelijk"]
    lat            = ruimtelijk["lat"]
    lon            = ruimtelijk["lon"]
    straal         = ruimtelijk["straal"]
    datum_leesbaar = ruimtelijk["datum_leesbaar"]
    timestamp      = ruimtelijk["timestamp"]
    markers        = ruimtelijk.get("html_markers", [])
    polygonen      = ruimtelijk.get("html_polygonen", [])
    signaal_straal = straal + MANEGE_SIGNAAL_MARGE
    puntafstand    = ruimtelijk.get("max_onderlinge_afstand_m", 0)
    toeslag        = ruimtelijk.get("toeslag_m", TOETSING_TOESLAG_M)

    naam           = state.get("aanvraag", {}).get("naam", "")
    slug           = naam_slug(naam)
    naam_infix     = f"_{slug}" if slug else ""
    # De dossieromschrijving is vrije invoer en gaat hier een HTML-document in;
    # escapen, anders kan een naam de opmaak van het document breken.
    html_titel     = (f"TUG-ontheffingen — Kaart ({html_escape(naam)})" if naam
                      else "TUG-ontheffingen — Kaart")

    # Alles wat in het scriptblok komt, gaat door json_voor_script: ook eigen
    # waarden, zodat er één regel geldt en geen uitzondering onopgemerkt blijft.
    waarden = {
        "CENTER_LAT":           lat,
        "CENTER_LON":           lon,
        "STRAAL":               straal,
        "TOETSING_LABEL":       toetsing_label(straal, toeslag),
        "SIGNAAL_STRAAL":       signaal_straal,
        "TOESLAG_M":            toeslag,
        "TOESLAG_TEKST":        meters(toeslag),
        "PUNTEN":               ruimtelijk.get("punten") or [[lat, lon]],
        "TOETSING_RINGS":       ruimtelijk.get("toetsing_rings", []),
        "SIGNAAL_RINGS":        ruimtelijk.get("signaal_rings", []),
        "WORKFLOW_VERSIE":      VERSION,
        "MANEGE_SIGNAAL_MARGE": MANEGE_SIGNAAL_MARGE,
        "LUCHTHAVEN_SIGNAAL_M": LUCHTHAVEN_SIGNAAL_M,
        "DATUM":                datum_leesbaar,
        "MARKERS":              markers,
        "POLYGONEN":            polygonen,
        "ONVOLLEDIG":           [kort(u) for u in onvolledige_toetsing(state)],
        "PUNTAFSTAND_M":        puntafstand,
        "PUNTAFSTAND":          puntafstand_melding(puntafstand),
    }
    data_js = "".join(f"var {naam}={json_voor_script(waarde)};\n"
                      for naam, waarde in waarden.items())

    sjabloon = (GUI_DIR / "kaart_export.html").read_text(encoding="utf-8")
    html = (sjabloon
            .replace("__WORKFLOW_VERSIE__", html_escape(VERSION))
            .replace("__TITEL__", html_titel)
            .replace("__DATA__", data_js))

    bestand = OUTPUT_DIR / f"tug_kaart{naam_infix}_{timestamp}.html"
    bestand.write_text(html, encoding="utf-8")
    log(f"  HTML-kaart opgeslagen: {bestand}")
    return bestand


# ──────────────────────────────────────────────
# Hoofdfunctie
# ──────────────────────────────────────────────

def run(state_pad: str | Path) -> None:
    state_pad = Path(state_pad)
    state     = json.loads(state_pad.read_text(encoding="utf-8"))

    setup_logging()

    if "ruimtelijk" not in state:
        _logger.error(
            "FOUT: state bevat geen 'ruimtelijk'-sectie. "
            "Voer eerst tug_03_ruimtelijk.py uit."
        )
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    log = LogAccumulator("tug.05_output")

    log("Stap 6: Output genereren ...")
    genereer_pdf(state, log)

    for kaart in state["ruimtelijk"].get("kaarten_png", []):
        pad = Path(kaart["pad"])
        if pad.exists():
            try:
                pad.unlink()
                log(f"  PNG verwijderd: {pad.name}")
            except OSError as fout:
                log(f"  WAARSCHUWING: PNG {pad.name} niet verwijderd ({fout}).")

    genereer_html(state, log)

    state.setdefault("logboek", []).append({
        "stap":     "05_output",
        "tijdstip": datetime.now().isoformat(),
        "niveau":   "info",
        "bericht":  "PDF en HTML-kaart gegenereerd.",
    })

    schrijf_state(state_pad, state)
    log("Verwerking voltooid.")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) != 2:
        setup_logging()
        _logger.error(
            "Gebruik: python tug_05_output.py tug_state.json"
        )
        sys.exit(1)
    run(sys.argv[1])
