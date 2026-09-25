"""
tug_03_kaart.py -- Kaartgeneratie (TUG-ontheffingen workflow)
Versie: zie git (workflowversie = korte commit-hash, zie tug_config.VERSION)

Bevat: tile-helpers, pixel-coördinatenconversie, teken-primitieven,
legenda-rendering en de hoofd-renderfunctie `_render_kaart`.

Deze module tekent; hij beoordeelt niet. Welke verblijfsobjecten wettelijk
relevant zijn, in de margeband vallen of alleen ter informatie meegaan, is al
bepaald in tug_03_ruimtelijk en komt binnen als `oordelen`. Hier wordt alleen
nog een kleur en een tekenvolgorde aan dat oordeel gehangen.
"""

import io as _io
import math

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from tug_bronstatus import Bronregister
from tug_config import (
    KAART_ACHTERGRONDEN,
    MAX_BYTES_TEGEL,
    MARGE_M,
    MANEGE_SIGNAAL_MARGE,
    LUCHTHAVEN_GRENS_M,
    LUCHTHAVEN_SIGNAAL_M,
    TOETSING_TOESLAG_M,
    meters,
    toetsing_label,
    _TILE_URL,
    _TILE_SIZE,
    _LOC_CX_FRAC,
    _LOC_CY_FRAC,
)
from tug_geo import extract_lon_lat
from tug_http import BronFout, haal

# Een kaarttegel is PNG (BRT) of JPEG (luchtfoto). Andere formaten worden niet
# geopend, zodat een gemanipuleerde tegel geen van de overige beeldparsers van
# Pillow bereikt (EPS, JPEG 2000 e.a.).
_TEGEL_FORMATEN = ["PNG", "JPEG"]

# Toeslagcirkel rond de puntlocatie (B20): lichtblauw, los van het rood van het
# kruis en het donkerblauw van de toetsingsafstand; zelfde tint als de HTML-kaart.
KLEUR_TOESLAG = (0, 170, 225)


# ──────────────────────────────────────────────
# Kaartgeneratie — hulpfuncties
# ──────────────────────────────────────────────

def _world_px(lon, lat, zoom):
    x = (lon + 180) / 360 * _TILE_SIZE * (2 ** zoom)
    lat_r = math.radians(lat)
    y = ((1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2
         * _TILE_SIZE * (2 ** zoom))
    return x, y


def _world_px_inv(px, py, zoom):
    n   = _TILE_SIZE * (2 ** zoom)
    lon = px / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * py / n))))
    return lon, lat


def _ll_to_img(lon, lat, clon, clat, zoom, img_w, img_h):
    cx, cy = _world_px(clon, clat, zoom)
    px, py = _world_px(lon, lat, zoom)
    return img_w / 2 + (px - cx), img_h / 2 + (py - cy)


def _m_to_px(meters, lat, zoom):
    mpp = 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)
    return meters / mpp


def _bereken_zoom(lat, straal_m, diameter_frac, map_h_px):
    radius_px = (diameter_frac / 2) * map_h_px
    mpp = straal_m / radius_px
    zoom_f = math.log2(156543.03392 * math.cos(math.radians(lat)) / mpp)
    return max(1, min(19, int(zoom_f)))


def _haal_tiles(clon, clat, zoom, map_w, map_h, log, tile_url=_TILE_URL, *,
               bronnen: Bronregister):
    cx, cy = _world_px(clon, clat, zoom)
    tl_x = cx - map_w / 2
    tl_y = cy - map_h / 2
    tx_min = int(tl_x / _TILE_SIZE)
    tx_max = int((tl_x + map_w - 1) / _TILE_SIZE)
    ty_min = int(tl_y / _TILE_SIZE)
    ty_max = int((tl_y + map_h - 1) / _TILE_SIZE)

    canvas = Image.new("RGB", (map_w, map_h), (230, 230, 230))
    n_tiles = 2 ** zoom
    headers = {"User-Agent": "TUG-ontheffingen (Provincie Overijssel)"}
    mislukt = 0

    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            tx_w = tx % n_tiles
            if tx_w < 0:
                tx_w += n_tiles
            url = tile_url.format(z=zoom, x=tx_w, y=ty)
            try:
                inhoud = haal(url, headers=headers, timeout=15, max_bytes=MAX_BYTES_TEGEL)
                # RGBA + masker: transparante delen (bv. BRT buiten Nederland) worden
                # lichtgrijs i.p.v. zwart
                tile = Image.open(_io.BytesIO(inhoud), formats=_TEGEL_FORMATEN).convert("RGBA")
                canvas.paste(tile, (int(tx * _TILE_SIZE - tl_x), int(ty * _TILE_SIZE - tl_y)), tile)
            except (BronFout, OSError, ValueError, Image.DecompressionBombError) as e:
                mislukt += 1
                log(f"  WAARSCHUWING: tile ({tx_w},{ty},z{zoom}) mislukt: {e}")
    if mislukt:
        bronnen.mislukt("kaarttegels", f"{mislukt} tegel(s) niet geladen (zoom {zoom})")
    else:
        bronnen.geraadpleegd("kaarttegels")
    return canvas


def _teken_kruis(draw, cx, cy, size, color, breedte=3):
    draw.line([cx - size, cy, cx + size, cy], fill=color, width=breedte)
    draw.line([cx, cy - size, cx, cy + size], fill=color, width=breedte)


def _teken_legenda(img, straal, toeslag=TOETSING_TOESLAG_M):
    map_w, map_h = img.size
    try:
        font      = ImageFont.truetype("arial.ttf", 17)
        font_bold = ImageFont.truetype("arialbd.ttf", 17)
    except OSError:
        try:
            font = font_bold = ImageFont.truetype("DejaVuSans.ttf", 17)
        except OSError:
            font = font_bold = ImageFont.load_default()

    LINE_H = 25
    PAD    = 13
    DOT_R  = 6
    TEXT_X = DOT_R * 2 + 8

    regels = [
        ("titel",     None,            "Legenda"),
        ("vlak_sterk", (220, 50, 50),  "Geluidgevoelig gebouw (gevelcontour)"),
        ("vlak_sterk", (240, 200, 60), f"Geluidgevoelig gebouw (marge +{MARGE_M} m)"),
        ("vlak",      (150, 150, 150), "Gebouw (niet beschermd)"),
        ("vlak_dim",  (150, 150, 150), f"Gebouw (niet beschermd, marge +{MARGE_M} m)"),
        ("vlak",      (184, 134, 11),  "Begraafplaats (beschermd)"),
        ("vlak_dim",  (255, 215, 0),   "Begraafplaats (niet beschermd)"),
        ("dot",       (128, 0, 128),   "Manege (attentie!)"),
        ("dot_dim",   (128, 0, 128),   "Manege (niet beschermd)"),
        ("ruit",      (210, 90, 0),    f"Luchthaven (signalering ≤ {LUCHTHAVEN_SIGNAAL_M} m)"),
        ("ruit_conf", (210, 40, 0),    f"Luchthaven (CONFLICT < {LUCHTHAVEN_GRENS_M} m)"),
        ("kruis",     (200, 0, 0),     "Stijg- en landingsplaats"),
        ("cirkel",    KLEUR_TOESLAG,   f"Marge rond puntlocatie ({meters(toeslag)} m)"),
        ("vlak",      (0, 100, 0),    "Natura 2000-gebied"),
        ("vlak",      (60, 179, 60),  "Natuurnetwerk Nederland"),
        ("lijn_vol",  (26, 82, 118),   toetsing_label(straal, toeslag)),
        ("lijn_vol",  (202, 111, 30),  f"Aandachtsgebied (+{MANEGE_SIGNAAL_MARGE} m)"),
    ]

    if not toeslag:
        regels = [r for r in regels if r[0] != "cirkel"]  # geen toeslag, geen cirkel

    # Breedte volgt de langste regel, zodat lange labels niet buiten het kader lopen.
    tekst_w = max(font.getlength(t) for _, _, t in regels)
    BOX_W = max(390, int(tekst_w) + TEXT_X + 2 * PAD)
    BOX_H = len(regels) * LINE_H + 2 * PAD
    x0 = map_w - BOX_W - 20
    y0 = map_h - BOX_H - 20
    x1, y1 = x0 + BOX_W, y0 + BOX_H

    overlay = Image.new("RGBA", (map_w, map_h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle([x0, y0, x1, y1], fill=(255, 255, 255, 215))
    od.rectangle([x0, y0, x1, y1], outline=(160, 160, 160, 255), width=1)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    for i, (soort, kleur, tekst) in enumerate(regels):
        rx  = x0 + PAD
        ry  = y0 + PAD + i * LINE_H
        mid_y = ry + DOT_R
        if soort == "titel":
            draw.text((rx, ry), tekst, fill=(20, 20, 20), font=font_bold)
        elif soort in ("dot", "dot_dim"):
            if soort == "dot_dim":
                kleur = tuple(int(c * 0.35 + 255 * 0.65) for c in kleur)
            draw.ellipse([rx, mid_y - DOT_R, rx + DOT_R * 2, mid_y + DOT_R],
                         fill=kleur, outline=(80, 80, 80) if soort == "dot" else (180, 180, 180))
            draw.text((rx + TEXT_X, ry), tekst, font=font,
                      fill=(20, 20, 20) if soort == "dot" else (140, 140, 140))
        elif soort in ("vlak", "vlak_dim", "vlak_sterk"):
            vul  = (kleur if soort == "vlak_sterk"
                    else tuple(int(c * 0.25 + 255 * 0.75) for c in kleur))
            rand = (120, 120, 120) if soort == "vlak_dim" else kleur
            draw.rectangle([rx, mid_y - DOT_R, rx + DOT_R * 2, mid_y + DOT_R],
                           fill=vul, outline=rand, width=1)
            draw.text((rx + TEXT_X, ry), tekst, font=font,
                      fill=(140, 140, 140) if soort == "vlak_dim" else (20, 20, 20))
        elif soort in ("ruit", "ruit_conf"):
            alpha = 200 if soort == "ruit_conf" else 130
            cx_r  = rx + DOT_R
            pts   = [(cx_r, mid_y - DOT_R), (cx_r + DOT_R, mid_y),
                     (cx_r, mid_y + DOT_R), (cx_r - DOT_R, mid_y)]
            draw.polygon(pts, fill=kleur + (alpha,), outline=kleur + (255,))
            draw.text((rx + TEXT_X, ry), tekst, fill=(20, 20, 20), font=font)
        elif soort == "kruis":
            draw.line([rx, mid_y, rx + DOT_R * 2, mid_y], fill=kleur, width=2)
            draw.line([rx + DOT_R, mid_y - DOT_R, rx + DOT_R, mid_y + DOT_R], fill=kleur, width=2)
            draw.text((rx + TEXT_X, ry), tekst, fill=(20, 20, 20), font=font)
        elif soort == "cirkel":
            draw.ellipse([rx, mid_y - DOT_R, rx + DOT_R * 2, mid_y + DOT_R],
                         fill=tuple(int(c * 0.25 + 255 * 0.75) for c in kleur),
                         outline=kleur, width=2)
            draw.text((rx + TEXT_X, ry), tekst, fill=(20, 20, 20), font=font)
        elif soort == "lijn_vol":
            draw.line([rx, mid_y, rx + DOT_R * 2, mid_y], fill=kleur, width=3)
            draw.text((rx + TEXT_X, ry), tekst, fill=(20, 20, 20), font=font)
    return img


def _teken_cirkel_label(img, cx, cy, r, tekst, kleur_rgb):
    map_w, map_h = img.size
    if cx + r + 8 >= map_w - 20:
        return img
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 18)
        except OSError:
            font = ImageFont.load_default()

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox  = dummy.textbbox((0, 0), tekst, font=font, stroke_width=3)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    lbl  = Image.new("RGBA", (tw + 10, th + 8), (0, 0, 0, 0))
    ld   = ImageDraw.Draw(lbl)
    # Witte halo: leesbaar op zowel luchtfoto als topografische kaart
    ld.text((5, 4), tekst, fill=kleur_rgb + (255,), font=font,
            stroke_width=3, stroke_fill=(255, 255, 255, 230))
    lbl  = lbl.rotate(-90, expand=True)
    paste_x = max(0, min(int(cx + r + 8), map_w - lbl.width))
    paste_y = max(0, min(int(cy - lbl.height / 2), map_h - lbl.height))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(lbl, (paste_x, paste_y), lbl)
    return img_rgba.convert("RGB")


def _teken_bronvermelding(img, tekst):
    """Kleine bronvermelding van de achtergrondkaart, linksonder (leesrichting na rotatie)."""
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bb = dummy.textbbox((0, 0), tekst, font=font)
    lbl = Image.new("RGBA", (bb[2] - bb[0] + 10, bb[3] - bb[1] + 8), (255, 255, 255, 190))
    ImageDraw.Draw(lbl).text((5, 3), tekst, fill=(40, 40, 40, 255), font=font)
    lbl = lbl.rotate(-90, expand=True)
    img_rgba = img.convert("RGBA")
    img_rgba.paste(lbl, (0, 0), lbl)
    return img_rgba.convert("RGB")


def _render_kaart(clon, clat, zoom, map_w, map_h, straal,
                  bev, oordelen, toon_legenda, log,
                  achtergrond="satelliet",
                  punten=None, toetsing_rings=None, signaal_rings=None, *,
                  bronnen: Bronregister, toeslag: float = TOETSING_TOESLAG_M):
    """Render één kaartuitsnede.

    `bev` is het Bevindingen-record van de bronstap, `oordelen` het oordeel per
    verblijfsobject uit de classificatiestap — dezelfde twee die ook de PDF-tabel
    en de HTML-markers voeden.
    """
    # Verschuif canvas-centrum zodat locatie op (_LOC_CX_FRAC, _LOC_CY_FRAC) valt
    px_loc, py_loc = _world_px(clon, clat, zoom)
    px_canvas = px_loc + (0.5 - _LOC_CX_FRAC) * map_w
    py_canvas = py_loc + (0.5 - _LOC_CY_FRAC) * map_h
    clon_c, clat_c = _world_px_inv(px_canvas, py_canvas, zoom)

    log(f"    Achtergrondtiles ophalen (zoom {zoom}, {map_w}×{map_h} px) ...")
    bron = KAART_ACHTERGRONDEN[achtergrond]
    img = _haal_tiles(clon_c, clat_c, zoom, map_w, map_h, log, tile_url=bron["url"],
                      bronnen=bronnen)
    if bron["contrast"] != 1.0:
        img = ImageEnhance.Contrast(img).enhance(bron["contrast"])
    img = img.convert("RGBA")
    overlay = Image.new("RGBA", (map_w, map_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    def ll2px(lon, lat):
        return _ll_to_img(lon, lat, clon_c, clat_c, zoom, map_w, map_h)

    def m2px(m):
        return _m_to_px(m, clat, zoom)


    # BAG-adressen: gevelcontour van het pand (B02), met puntweergave als terugval.
    # Per pand wint de zwaarste categorie; vlakken worden na de natuurgebieden getekend.
    ROOD   = ((220, 50, 50, 170),   (150, 20, 20, 255),   (220, 50, 50, 220),   7)
    GEEL   = ((240, 200, 60, 170),  (160, 130, 0, 255),   (240, 200, 60, 210),  7)
    GRIJS  = ((150, 150, 150, 110), (90, 90, 90, 200),    (150, 150, 150, 180), 5)
    LICHT  = ((150, 150, 150, 55),  (120, 120, 120, 150), (150, 150, 150, 100), 5)
    if achtergrond == "satelliet":
        # Lichte randen: grijze panden vallen anders weg tegen de luchtfoto
        GRIJS = ((200, 200, 200, 110), (255, 255, 255, 230), GRIJS[2], GRIJS[3])
        LICHT = ((200, 200, 200, 50),  (235, 235, 235, 170), LICHT[2], LICHT[3])
    pand_vlakken: dict[str, tuple] = {}

    def _adres(feat, prio, stijl):
        contour, pid = feat.get("_contour"), feat.get("_pand_id")
        if contour and pid:
            if pid not in pand_vlakken or prio > pand_vlakken[pid][0]:
                pand_vlakken[pid] = (prio, contour, stijl)
            return
        try:
            lat_f, lon_f = extract_lon_lat(feat)
        except (KeyError, IndexError, TypeError, ValueError):
            return
        px, py = ll2px(lon_f, lat_f)
        r = stijl[3]
        draw.ellipse([px - r, py - r, px + r, py + r],
                     fill=stijl[2], outline=(50, 50, 50, 130), width=1)

    # Kleur en tekenvolgorde per oordeel. De prioriteit bepaalt welke categorie
    # wint als meerdere objecten hetzelfde pand delen; het oordeel zelf komt uit
    # tug_03_ruimtelijk, zodat kaart en tabel hetzelfde laten zien.
    # De volgorde van de banden is de tekenvolgorde: wat later komt, komt bovenop.
    STIJL_PER_BAND = {
        # In de margeband is het overige alleen achtergrond: lichtgrijs, onderop.
        "marge_overig": {"marge": (3, GEEL), "overig": (0, LICHT)},
        "straal":       {"wettelijk": (4, ROOD), "overig": (1, GRIJS)},
        "marge":        {"marge": (3, GEEL), "overig": (1, GRIJS)},
    }
    for band, stijlen in STIJL_PER_BAND.items():
        for oordeel in oordelen.get(band, []):
            prio, stijl = stijlen[oordeel.categorie]
            _adres(oordeel.feature, prio, stijl)
    for feat in [*bev.kdv_in_marge, *bev.scholen_in_marge]:
        _adres(feat, 3, GEEL)
    for feat in [*bev.kdv_in_straal, *bev.scholen_in_straal]:
        _adres(feat, 4, ROOD)

    def _teken_poly_rings(draw, rings, fill_rgba, outline_rgba):
        for ring in rings:
            pixels = [ll2px(lon, lat) for lon, lat in ring]
            if len(pixels) >= 3:
                draw.polygon(pixels, fill=fill_rgba, outline=outline_rgba, width=1)

    for item in bev.begraafplaatsen_buiten_straal:
        _teken_poly_rings(draw, item.get("poly_rings", []), (255, 215, 0, 51), (200, 160, 0, 180))
    for item in bev.begraafplaatsen_in_straal:
        _teken_poly_rings(draw, item.get("poly_rings", []), (184, 134, 11, 51), (140, 90, 0, 200))

    # NNN — lichtgroen (onder N2000 renderen, zodat N2000 dominant blijft)
    for item in bev.nnn_in_signaal:
        _teken_poly_rings(draw, item.get("poly_rings", []),
                          (144, 238, 144, 35), (102, 205, 102, 110))
    for item in bev.nnn_in_straal:
        _teken_poly_rings(draw, item.get("poly_rings", []), (102, 205, 102, 55), (60, 179, 60, 160))

    # Natura 2000 — dominant donkergroen, over NNN heen (hogere alpha)
    for item in bev.n2000_in_signaal:
        _teken_poly_rings(draw, item.get("poly_rings", []), (34, 139, 34, 60), (0, 120, 0, 170))
    for item in bev.n2000_in_straal:
        _teken_poly_rings(draw, item.get("poly_rings", []), (0, 100, 0, 100), (0, 80, 0, 230))

    for _prio, contour, stijl in sorted(pand_vlakken.values(), key=lambda v: v[0]):
        _teken_poly_rings(draw, contour, stijl[0], stijl[1])

    for item in bev.maneges_buiten_straal:
        px, py = ll2px(item["lon"], item["lat"])
        draw.ellipse([px - 7, py - 7, px + 7, py + 7],
                     fill=(128, 0, 128, 80), outline=(160, 110, 160, 160), width=1)
    for item in bev.maneges_in_straal:
        px, py = ll2px(item["lon"], item["lat"])
        draw.ellipse([px - 8, py - 8, px + 8, py + 8],
                     fill=(128, 0, 128, 200), outline=(80, 0, 80, 255), width=2)

    def _teken_diamant(draw, px, py, r, fill, outline):
        """Teken een ◇ diamantsymbool (rotated square) rondom (px, py)."""
        pts = [(px, py - r), (px + r, py), (px, py + r), (px - r, py)]
        draw.polygon(pts, fill=fill, outline=outline)

    # Luchthaventerreinen als vlak (B23), onder het ruitsymbool.
    for item in bev.luchthavens_in_signaal:
        _teken_poly_rings(draw, item.get("poly_rings", []), (210, 90, 0, 40), (160, 60, 0, 200))
    for item in bev.luchthavens_in_straal:
        _teken_poly_rings(draw, item.get("poly_rings", []), (210, 40, 0, 70), (140, 0, 0, 255))
    for item in bev.luchthavens_in_signaal:
        px, py = ll2px(item["lon"], item["lat"])
        _teken_diamant(draw, px, py, 9, (210, 90, 0, 120), (160, 60, 0, 200))
    for item in bev.luchthavens_in_straal:
        px, py = ll2px(item["lon"], item["lat"])
        _teken_diamant(draw, px, py, 10, (210, 40, 0, 220), (140, 0, 0, 255))

    # Toetsings- en aandachtsgebied: omtrek van de vereniging van cirkels rond alle
    # puntlocaties (B03); bij één locatie is dat gewoon de cirkel.
    # Witte onderrand houdt lijnen en kruisen leesbaar op de luchtfoto.
    label_anker: dict[str, tuple[float, float]] = {}
    for sleutel, rings, kleur in (("signaal", signaal_rings or [], (202, 111, 30, 240)),
                                  ("toetsing", toetsing_rings or [], (26, 82, 118, 255))):
        for ring in rings:
            pix = [ll2px(lon_r, lat_r) for lon_r, lat_r in ring]
            draw.line(pix + pix[:1], fill=(255, 255, 255, 200), width=7, joint="curve")
            draw.line(pix + pix[:1], fill=kleur, width=3, joint="curve")
            rechts = max(pix, key=lambda p: p[0])
            if sleutel not in label_anker or rechts[0] > label_anker[sleutel][0]:
                label_anker[sleutel] = rechts
    # Toeslag rond elke puntlocatie (B20): de meters die bij de Lden-afstand worden
    # opgeteld. Vlak onder het kruis, rand erboven: zo blijft de ring zichtbaar ook
    # waar hij op kaartschaal kleiner is dan het kruissymbool (minimaal 6 px).
    punt_px = [ll2px(p_lon, p_lat) for p_lat, p_lon in (punten or [(clat, clon)])]
    r_toeslag = max(6, m2px(toeslag)) if toeslag else 0

    def _vak(px, py):
        return [px - r_toeslag, py - r_toeslag, px + r_toeslag, py + r_toeslag]

    for px, py in punt_px:
        if r_toeslag:
            draw.ellipse(_vak(px, py), fill=(*KLEUR_TOESLAG, 60))
    for px, py in punt_px:
        _teken_kruis(draw, int(px), int(py), 15, (255, 255, 255, 230), breedte=7)
        _teken_kruis(draw, int(px), int(py), 14, (200, 0, 0, 255), breedte=3)
    for px, py in (punt_px if r_toeslag else []):
        draw.ellipse(_vak(px, py), outline=(255, 255, 255, 220), width=4)
        draw.ellipse(_vak(px, py), outline=(*KLEUR_TOESLAG, 255), width=2)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    if toon_legenda:
        img = _teken_legenda(img, straal, toeslag)
    if "toetsing" in label_anker:
        ax, ay = label_anker["toetsing"]
        img = _teken_cirkel_label(img, int(ax), int(ay), 0, toetsing_label(straal, toeslag),
                                   (26, 82, 118))
    if "signaal" in label_anker:
        ax, ay = label_anker["signaal"]
        img = _teken_cirkel_label(img, int(ax), int(ay), 0,
                                   f"Aandachtsgebied (+{MANEGE_SIGNAAL_MARGE} m)", (202, 111, 30))
    return _teken_bronvermelding(img, bron["bron"])
