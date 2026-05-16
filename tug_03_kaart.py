"""
tug_03_kaart.py -- Kaartgeneratie (TUG-ontheffingen workflow)
Versie: 4.4.0  |  2026-05-04

Bevat: tile-helpers, pixel-coördinatenconversie, teken-primitieven,
legenda-rendering en de hoofd-renderfunctie `_render_kaart`.

Afhankelijkheden: tug_03_bronnen (constanten + extract_lon_lat).
"""

import io as _io
import math

import requests
from PIL import Image, ImageDraw, ImageFont

from tug_03_bronnen import (
    MARGE_M,
    MANEGE_SIGNAAL_MARGE,
    N2000_SIGNAAL_MARGE,
    NNN_SIGNAAL_MARGE,
    LUCHTHAVEN_GRENS_M,
    LUCHTHAVEN_SIGNAAL_M,
    _TILE_URL,
    _TILE_SIZE,
    _LOC_CX_FRAC,
    _LOC_CY_FRAC,
    _PDF_MARGIN_MM,
    _PDF_DPI,
    _PDF_PAGE_W_MM,
    _PDF_PAGE_H_MM,
    extract_lon_lat,
)


# ──────────────────────────────────────────────
# Kaartgeneratie — hulpfuncties
# ──────────────────────────────────────────────

def _world_px(lon, lat, zoom):
    x = (lon + 180) / 360 * _TILE_SIZE * (2 ** zoom)
    lat_r = math.radians(lat)
    y = (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * _TILE_SIZE * (2 ** zoom)
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


def _haal_tiles(clon, clat, zoom, map_w, map_h, log):
    cx, cy = _world_px(clon, clat, zoom)
    tl_x = cx - map_w / 2
    tl_y = cy - map_h / 2
    tx_min = int(tl_x / _TILE_SIZE)
    tx_max = int((tl_x + map_w - 1) / _TILE_SIZE)
    ty_min = int(tl_y / _TILE_SIZE)
    ty_max = int((tl_y + map_h - 1) / _TILE_SIZE)

    canvas = Image.new("RGB", (map_w, map_h), (230, 230, 230))
    n_tiles = 2 ** zoom
    headers = {"User-Agent": "tug_adressen/4.0 TUG-workflow"}

    for tx in range(tx_min, tx_max + 1):
        for ty in range(ty_min, ty_max + 1):
            tx_w = tx % n_tiles
            if tx_w < 0:
                tx_w += n_tiles
            url = _TILE_URL.format(z=zoom, x=tx_w, y=ty)
            try:
                resp = requests.get(url, headers=headers, timeout=15)
                resp.raise_for_status()
                tile = Image.open(_io.BytesIO(resp.content)).convert("RGB")
                canvas.paste(tile, (int(tx * _TILE_SIZE - tl_x), int(ty * _TILE_SIZE - tl_y)))
            except Exception as e:
                log(f"  WAARSCHUWING: tile ({tx_w},{ty},z{zoom}) mislukt: {e}")
    return canvas


def _teken_kruis(draw, cx, cy, size, color, breedte=3):
    draw.line([cx - size, cy, cx + size, cy], fill=color, width=breedte)
    draw.line([cx, cy - size, cx, cy + size], fill=color, width=breedte)


def _teken_legenda(img, straal, signaal_straal):
    map_w, map_h = img.size
    try:
        font      = ImageFont.truetype("arial.ttf", 17)
        font_bold = ImageFont.truetype("arialbd.ttf", 17)
    except Exception:
        try:
            font = font_bold = ImageFont.truetype("DejaVuSans.ttf", 17)
        except Exception:
            font = font_bold = ImageFont.load_default()

    LINE_H = 25
    PAD    = 13
    DOT_R  = 6
    TEXT_X = DOT_R * 2 + 8

    regels = [
        ("titel",     None,            "Legenda"),
        ("dot",       (220, 50, 50),   "Geluidgevoelig gebouw"),
        ("dot",       (240, 200, 60),  f"Geluidgevoelig gebouw (marge +{MARGE_M} m)"),
        ("dot",       (150, 150, 150), "Verblijfsobject (niet beschermd)"),
        ("dot_dim",   (150, 150, 150), f"Verblijfsobject (niet beschermd, marge +{MARGE_M} m)"),
        ("vlak",      (184, 134, 11),  "Begraafplaats (beschermd)"),
        ("vlak_dim",  (255, 215, 0),   "Begraafplaats (niet beschermd)"),
        ("dot",       (128, 0, 128),   "Manege (attentie!)"),
        ("dot_dim",   (128, 0, 128),   "Manege (niet beschermd)"),
        ("ruit",      (210, 90, 0),    f"Luchthaven (signalering ≤ {LUCHTHAVEN_SIGNAAL_M} m)"),
        ("ruit_conf", (210, 40, 0),    f"Luchthaven (CONFLICT < {LUCHTHAVEN_GRENS_M} m)"),
        ("kruis",     (200, 0, 0),     "Stijg- en landingsplaats"),
        ("vlak",      (0, 100, 0),    "Natura 2000-gebied"),
        ("vlak",      (60, 179, 60),  "Natuurnetwerk Nederland"),
        ("lijn_vol",  (26, 82, 118),   f"Toetsingsafstand TUG ({straal:.0f} m)"),
        ("lijn_vol",  (202, 111, 30),  f"Aandachtsgebied (+{MANEGE_SIGNAAL_MARGE} m)"),
    ]

    BOX_W = 390
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
            draw.text((rx + TEXT_X, ry), tekst, fill=(20, 20, 20) if soort == "dot" else (140, 140, 140), font=font)
        elif soort in ("vlak", "vlak_dim"):
            vul  = tuple(int(c * 0.25 + 255 * 0.75) for c in kleur)
            rand = kleur if soort == "vlak" else (120, 120, 120)
            draw.rectangle([rx, mid_y - DOT_R, rx + DOT_R * 2, mid_y + DOT_R], fill=vul, outline=rand, width=1)
            draw.text((rx + TEXT_X, ry), tekst, fill=(20, 20, 20) if soort == "vlak" else (140, 140, 140), font=font)
        elif soort in ("ruit", "ruit_conf"):
            alpha = 200 if soort == "ruit_conf" else 130
            cx_r  = rx + DOT_R
            pts   = [(cx_r, mid_y - DOT_R), (cx_r + DOT_R, mid_y), (cx_r, mid_y + DOT_R), (cx_r - DOT_R, mid_y)]
            draw.polygon(pts, fill=kleur + (alpha,), outline=kleur + (255,))
            draw.text((rx + TEXT_X, ry), tekst, fill=(20, 20, 20), font=font)
        elif soort == "kruis":
            draw.line([rx, mid_y, rx + DOT_R * 2, mid_y], fill=kleur, width=2)
            draw.line([rx + DOT_R, mid_y - DOT_R, rx + DOT_R, mid_y + DOT_R], fill=kleur, width=2)
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
    except Exception:
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 18)
        except Exception:
            font = ImageFont.load_default()

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox  = dummy.textbbox((0, 0), tekst, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    lbl  = Image.new("RGBA", (tw + 6, th + 4), (0, 0, 0, 0))
    ld   = ImageDraw.Draw(lbl)
    ld.text((3, 2), tekst, fill=kleur_rgb + (220,), font=font)
    lbl  = lbl.rotate(-90, expand=True)
    paste_x = max(0, min(int(cx + r + 8), map_w - lbl.width))
    paste_y = max(0, min(int(cy - lbl.height / 2), map_h - lbl.height))
    img_rgba = img.convert("RGBA")
    img_rgba.paste(lbl, (paste_x, paste_y), lbl)
    return img_rgba.convert("RGB")


def _render_kaart(clon, clat, zoom, map_w, map_h,
                  straal, signaal_straal,
                  alle_vbo, geluidgevoelig_vbo,
                  begraafplaatsen_in_straal, begraafplaatsen_buiten_straal,
                  maneges_in_straal, maneges_buiten_straal,
                  toon_legenda, log, marge_vbo=None, marge_vbo_overig=None,
                  kdv_in_straal=None, kdv_in_marge=None,
                  scholen_in_straal=None, scholen_in_marge=None,
                  n2000_in_straal=None, n2000_in_signaal=None,
                  nnn_in_straal=None, nnn_in_signaal=None,
                  luchthavens_in_straal=None, luchthavens_in_signaal=None,
                  **_kw):
    # Verschuif canvas-centrum zodat locatie op (_LOC_CX_FRAC, _LOC_CY_FRAC) valt
    px_loc, py_loc = _world_px(clon, clat, zoom)
    px_canvas = px_loc + (0.5 - _LOC_CX_FRAC) * map_w
    py_canvas = py_loc + (0.5 - _LOC_CY_FRAC) * map_h
    clon_c, clat_c = _world_px_inv(px_canvas, py_canvas, zoom)

    log(f"    Achtergrondtiles ophalen (zoom {zoom}, {map_w}×{map_h} px) ...")
    img = _haal_tiles(clon_c, clat_c, zoom, map_w, map_h, log).convert("RGBA")
    overlay = Image.new("RGBA", (map_w, map_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cx, cy = _LOC_CX_FRAC * map_w, _LOC_CY_FRAC * map_h

    def ll2px(lon, lat):
        return _ll_to_img(lon, lat, clon_c, clat_c, zoom, map_w, map_h)

    def m2px(m):
        return _m_to_px(m, clat, zoom)

    r_sig = m2px(signaal_straal)
    r_str = m2px(straal)

    geluidgevoelig_ids = {id(f) for f in geluidgevoelig_vbo}
    for feat in alle_vbo:
        try:
            lat_f, lon_f = extract_lon_lat(feat)
        except Exception:
            continue
        px, py = ll2px(lon_f, lat_f)
        is_gev = id(feat) in geluidgevoelig_ids
        kleur  = (220, 50, 50, 220) if is_gev else (150, 150, 150, 180)
        r      = 7 if is_gev else 5
        draw.ellipse([px - r, py - r, px + r, py + r], fill=kleur, outline=(50, 50, 50, 130), width=1)

    for feat in (kdv_in_marge or []):
        try:
            lat_f, lon_f = extract_lon_lat(feat)
        except Exception:
            continue
        px, py = ll2px(lon_f, lat_f)
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=(240, 200, 60, 210), outline=(160, 130, 0, 200), width=1)

    for feat in (kdv_in_straal or []):
        try:
            lat_f, lon_f = extract_lon_lat(feat)
        except Exception:
            continue
        px, py = ll2px(lon_f, lat_f)
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=(220, 50, 50, 220), outline=(50, 50, 50, 130), width=1)

    for feat in (scholen_in_marge or []):
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            lon_f, lat_f = geom["coordinates"][0], geom["coordinates"][1]
        except Exception:
            continue
        px, py = ll2px(lon_f, lat_f)
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=(240, 200, 60, 210), outline=(160, 130, 0, 200), width=1)

    for feat in (scholen_in_straal or []):
        geom = feat.get("geometry")
        if not geom:
            continue
        try:
            lon_f, lat_f = geom["coordinates"][0], geom["coordinates"][1]
        except Exception:
            continue
        px, py = ll2px(lon_f, lat_f)
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=(220, 50, 50, 220), outline=(50, 50, 50, 130), width=1)

    def _teken_poly_rings(draw, rings, fill_rgba, outline_rgba):
        for ring in rings:
            pixels = [ll2px(lon, lat) for lon, lat in ring]
            if len(pixels) >= 3:
                draw.polygon(pixels, fill=fill_rgba, outline=outline_rgba, width=1)

    for item in begraafplaatsen_buiten_straal:
        _teken_poly_rings(draw, item.get("poly_rings", []), (255, 215, 0, 51), (200, 160, 0, 180))
    for item in begraafplaatsen_in_straal:
        _teken_poly_rings(draw, item.get("poly_rings", []), (184, 134, 11, 51), (140, 90, 0, 200))

    # NNN — lichtgroen (onder N2000 renderen, zodat N2000 dominant blijft)
    for item in (nnn_in_signaal or []):
        _teken_poly_rings(draw, item.get("poly_rings", []), (144, 238, 144, 35), (102, 205, 102, 110))
    for item in (nnn_in_straal or []):
        _teken_poly_rings(draw, item.get("poly_rings", []), (102, 205, 102, 55), (60, 179, 60, 160))

    # Natura 2000 — dominant donkergroen, over NNN heen (hogere alpha)
    for item in (n2000_in_signaal or []):
        _teken_poly_rings(draw, item.get("poly_rings", []), (34, 139, 34, 60), (0, 120, 0, 170))
    for item in (n2000_in_straal or []):
        _teken_poly_rings(draw, item.get("poly_rings", []), (0, 100, 0, 100), (0, 80, 0, 230))

    for feat in (marge_vbo_overig or []):
        try:
            lat_f, lon_f = extract_lon_lat(feat)
        except Exception:
            continue
        px, py = ll2px(lon_f, lat_f)
        draw.ellipse([px - 5, py - 5, px + 5, py + 5], fill=(150, 150, 150, 100), outline=(100, 100, 100, 160), width=1)

    for feat in (marge_vbo or []):
        try:
            lat_f, lon_f = extract_lon_lat(feat)
        except Exception:
            continue
        px, py = ll2px(lon_f, lat_f)
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=(240, 200, 60, 210), outline=(160, 130, 0, 200), width=1)

    for item in maneges_buiten_straal:
        px, py = ll2px(item["lon"], item["lat"])
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=(128, 0, 128, 80), outline=(160, 110, 160, 160), width=1)
    for item in maneges_in_straal:
        px, py = ll2px(item["lon"], item["lat"])
        draw.ellipse([px - 8, py - 8, px + 8, py + 8], fill=(128, 0, 128, 200), outline=(80, 0, 80, 255), width=2)

    def _teken_diamant(draw, px, py, r, fill, outline):
        """Teken een ◇ diamantsymbool (rotated square) rondom (px, py)."""
        pts = [(px, py - r), (px + r, py), (px, py + r), (px - r, py)]
        draw.polygon(pts, fill=fill, outline=outline)

    for item in (luchthavens_in_signaal or []):
        px, py = ll2px(item["lon"], item["lat"])
        _teken_diamant(draw, px, py, 9, (210, 90, 0, 120), (160, 60, 0, 200))
    for item in (luchthavens_in_straal or []):
        px, py = ll2px(item["lon"], item["lat"])
        _teken_diamant(draw, px, py, 10, (210, 40, 0, 220), (140, 0, 0, 255))

    draw.ellipse([cx - r_sig, cy - r_sig, cx + r_sig, cy + r_sig], outline=(202, 111, 30, 230), width=3)
    draw.ellipse([cx - r_str, cy - r_str, cx + r_str, cy + r_str], outline=(26, 82, 118, 240), width=3)
    _teken_kruis(draw, int(cx), int(cy), 14, (200, 0, 0, 255), breedte=3)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    if toon_legenda:
        img = _teken_legenda(img, straal, signaal_straal)
    img = _teken_cirkel_label(img, int(cx), int(cy), int(r_str),
                               f"Toetsingsafstand TUG ({straal:.0f} m)", (26, 82, 118))
    img = _teken_cirkel_label(img, int(cx), int(cy), int(r_sig),
                               f"Aandachtsgebied (+{MANEGE_SIGNAAL_MARGE} m)", (202, 111, 30))
    return img
