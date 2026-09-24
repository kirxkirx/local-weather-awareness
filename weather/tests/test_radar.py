"""Tests for weather.radar: palette, MRMS frame discovery/loading, basemap caching and
the renderer on a synthetic 7000x3500 palette frame. No network: tiles and frames come
from ``fake_http``."""
from __future__ import annotations

import io
import math
import os
from datetime import datetime, timezone

import pytest
from PIL import Image, ImageDraw, ImageFont

from weather import geo, http, radar

UTC = timezone.utc
FIXED_NOW = datetime(2026, 9, 23, 15, 21, 30, tzinfo=UTC)   # search starts at 15:16Z
BASE = (40, 44, 52)                                          # colour of every fake tile


def _png(mode="RGB", size=(256, 256), color=BASE) -> bytes:
    im = Image.new(mode, size, color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _box(lon, lat, half):
    return [[lon - half, lat - half], [lon + half, lat - half], [lon + half, lat + half],
            [lon - half, lat + half], [lon - half, lat - half]]


def _alert(aid, rings, color="#ff0000", rank=0, event="Test Warning"):
    return {"id": aid, "event": event, "rank": rank, "color": color, "end_dt": None,
            "geometry": {"type": "Polygon", "coordinates": rings}}


def _radar(img, stale=False):
    return {"ok": True, "stale": stale, "error": "IEM slow" if stale else None,
            "age_s": 900.0 if stale else 0.0, "ts": datetime(2026, 9, 23, 15, 12, tzinfo=UTC),
            "url": "https://example/lcref.png", "image": img}


NO_RADAR = {"ok": False, "stale": True, "error": "IEM down", "age_s": None, "ts": None,
            "url": None, "image": None}


@pytest.fixture
def site(cfg):
    return cfg.sites[3]                      # Socorro, NM


@pytest.fixture
def frame_img(site):
    """No-data everywhere except a 21x21-cell block of index 124 (30 dBZ) at the site."""
    img = Image.new("P", (geo.MRMS_W, geo.MRMS_H), geo.MRMS_NODATA)
    c, r = geo.mrms_cell(site["lat"], site["lon"])
    img.paste(124, (c - 10, r - 10, c + 11, r + 11))
    return img


@pytest.fixture
def tiles(fake_http):
    fake_http.add("openstreetmap", _png())
    return fake_http


def _centre(sm):
    cx, cy = sm.frame.lonlat_to_px(sm.lon, sm.lat)
    return int(cx), int(cy)


# ---- palette --------------------------------------------------------------------------
def test_palette_and_dbz_color():
    assert radar.deps_available()
    thresholds = [t for t, _ in radar.DBZ_PALETTE]
    assert thresholds == sorted(thresholds, reverse=True) and thresholds[-1] == 5
    assert radar.dbz_color(None) is None and radar.dbz_color(4.9) is None
    assert radar.dbz_color(5) == radar.DBZ_PALETTE[-1][1]
    assert radar.dbz_color(30) == (0, 142, 0) and radar.dbz_color(34.5) == (0, 142, 0)
    assert radar.dbz_color(200) == radar.DBZ_PALETTE[0][1]
    lut = radar._index_lut(5.0, 200)
    assert lut[geo.MRMS_NODATA] is None and lut[0] is None          # no data / -32 dBZ
    assert lut[124] == (0, 142, 0, 200)
    assert radar._index_lut(35.0, 200)[124] is None                 # below cfg floor


# ---- frame discovery / loading ---------------------------------------------------------
def test_find_latest_frame_picks_newest_head_ok(cfg, fake_http):
    fake_http.add("lcref_202609231510.png", b"x")
    fake_http.add("lcref_202609231512.png", b"x")
    found = radar.find_latest_frame(cfg, now=FIXED_NOW)
    assert found["ts"] == datetime(2026, 9, 23, 15, 12, tzinfo=UTC)
    assert found["url"] == ("https://mesonet.agron.iastate.edu/archive/data/2026/09/23/"
                            "GIS/mrms/lcref_202609231512.png")
    # started at now floored to an even minute minus 4 min, stepping back 2 min
    assert fake_http.calls[0].endswith("lcref_202609231516.png")
    assert fake_http.calls[1].endswith("lcref_202609231514.png")
    assert all(c.startswith("HEAD ") for c in fake_http.calls) and len(fake_http.calls) == 3


def test_find_latest_frame_none_within_max_back(cfg, fake_http):
    cfg.radar_max_back = 3
    assert radar.find_latest_frame(cfg, now=FIXED_NOW) is None
    assert len(fake_http.calls) == 4                    # start + 3 steps back
    assert fake_http.calls[-1].endswith("lcref_202609231510.png")


class _Probe:
    """``http.head_ok`` stand-in answering from a script: True / False / None per call."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, cfg, timeout=15.0):
        self.calls.append((url, timeout))
        return self.answers.pop(0) if self.answers else False


@pytest.mark.parametrize("answers,n_calls", [([None], 1), ([False, False, None], 3)])
def test_find_latest_frame_stops_when_archive_does_not_answer(
        cfg, fake_http, monkeypatch, answers, n_calls, caplog):
    """A probe with no answer (timeout/refused/budget spent) ends the walk-back at once;
    only a definite "not there" (False) moves on to the previous frame."""
    probe = _Probe(answers + [True])
    monkeypatch.setattr(http, "head_ok", probe)
    assert radar.find_latest_frame(cfg, now=FIXED_NOW) is None
    assert len(probe.calls) == n_calls
    assert all(t == radar.HEAD_TIMEOUT == 8.0 for _, t in probe.calls)
    assert "not answering" in caplog.text
    # ... whereas False, False, True walks back to the third candidate
    probe = _Probe([False, False, True])
    monkeypatch.setattr(http, "head_ok", probe)
    found = radar.find_latest_frame(cfg, now=FIXED_NOW)
    assert found["url"].endswith("lcref_202609231512.png") and len(probe.calls) == 3


def test_load_frame_unreachable_archive_uses_cached_frame(cfg, cache, fake_http, monkeypatch,
                                                          frame_img):
    monkeypatch.setattr(radar.util, "utcnow", lambda: FIXED_NOW)
    buf = io.BytesIO()
    frame_img.save(buf, format="PNG")
    fake_http.add("lcref_202609231516.png", buf.getvalue())
    assert radar.load_frame(cfg, cache)["ok"]
    fake_http.calls.clear()
    probe = _Probe([None])
    monkeypatch.setattr(http, "head_ok", probe)
    r = radar.load_frame(cfg, cache)
    assert len(probe.calls) == 1 and fake_http.calls == []          # no GET either
    assert r["ok"] and r["stale"] and "unreachable" in r["error"]
    # without a cached frame the reason reaches the page as the radar error
    os.remove(cache.path("mrms_last", ".png"))
    probe = _Probe([None])
    monkeypatch.setattr(http, "head_ok", probe)
    r = radar.load_frame(cfg, cache)
    assert not r["ok"] and r["error"].startswith("MRMS archive unreachable")


def test_load_frame_fresh_reuse_and_stale_fallback(cfg, cache, fake_http, monkeypatch,
                                                   frame_img):
    monkeypatch.setattr(radar.util, "utcnow", lambda: FIXED_NOW)
    buf = io.BytesIO()
    frame_img.save(buf, format="PNG")
    png = buf.getvalue()
    fake_http.add("lcref_202609231512.png", png)

    r = radar.load_frame(cfg, cache)
    assert r["ok"] and not r["stale"] and r["age_s"] == 0.0 and r["error"] is None
    assert r["image"].mode == "P" and r["image"].size == (geo.MRMS_W, geo.MRMS_H)
    assert r["ts"] == datetime(2026, 9, 23, 15, 12, tzinfo=UTC)
    assert r["url"].endswith("lcref_202609231512.png")
    assert cache.get_bytes("mrms_last", ".png") == png
    meta, age = cache.get_any("mrms_last_meta")
    assert meta["url"] == r["url"] and meta["ts"] == r["ts"].isoformat() and age >= 0

    def gets():
        return [c for c in fake_http.calls if not c.startswith("HEAD ")]
    assert len(gets()) == 1
    # the same frame is still the newest -> cached bytes reused, nothing re-downloaded
    r2 = radar.load_frame(cfg, cache)
    assert r2["ok"] and not r2["stale"] and r2["ts"] == r["ts"] and len(gets()) == 1

    # IEM unreachable -> last good frame, flagged stale, error kept
    fake_http.routes.clear()
    r3 = radar.load_frame(cfg, cache)
    assert r3["ok"] and r3["stale"] and r3["error"] and r3["image"] is not None
    assert r3["ts"] == r["ts"] and r3["url"] == r["url"] and r3["age_s"] >= 0
    # ... but not beyond radar_max_stale
    cfg.radar_max_stale = -1
    r4 = radar.load_frame(cfg, cache)
    assert not r4["ok"] and r4["image"] is None and r4["ts"] is None and r4["error"]


def test_load_frame_rejects_unusable_images(cfg, cache, fake_http, monkeypatch):
    monkeypatch.setattr(radar.util, "utcnow", lambda: FIXED_NOW)
    fake_http.add("lcref_202609231516.png", b"not a png")
    r = radar.load_frame(cfg, cache)
    assert not r["ok"] and "bad MRMS image" in r["error"]
    fake_http.routes.clear()
    fake_http.add("lcref_202609231516.png", _png("RGB", (10, 10)))
    r = radar.load_frame(cfg, cache)
    assert not r["ok"] and "mode" in r["error"]
    fake_http.routes.clear()
    fake_http.add("lcref_202609231516.png", _png("P", (10, 10), 0))
    r = radar.load_frame(cfg, cache)
    assert not r["ok"] and "size" in r["error"]
    assert cache.get_bytes("mrms_last", ".png") is None     # nothing bad was cached


# ---- basemap -------------------------------------------------------------------------
def test_basemap_from_tiles_then_cache(cfg, cache, tiles, site):
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    tx0, ty0, tx1, ty1 = sm.frame.tile_range()
    bm = sm.basemap()
    assert bm.mode == "RGB" and bm.size == sm.frame.size
    assert bm.getpixel((3, 3)) == BASE and bm.getpixel((bm.width - 3, bm.height - 3)) == BASE
    assert len(tiles.calls) == (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    assert tiles.calls[0] == cfg.tile_url_dark.format(z=cfg.tile_zoom, x=tx0, y=ty0)
    key = sm.basemap_key
    assert key.startswith("basemap_%s_dark_%s_" % (site["slug"], sm.frame.cache_key()))
    assert cache.get_bytes(key, ".png") is not None and sm.basemap_note is None
    assert sm.basemap() is bm                               # memoised per SiteMap
    # a new SiteMap loads the cached composite: no tile request at all
    n = len(tiles.calls)
    sm2 = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    assert sm2.basemap().size == sm.frame.size and len(tiles.calls) == n
    # the light theme has its own cache entry but, sharing the tile URL, refetches nothing:
    # the raw tiles were kept on disk
    sm3 = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light)
    sm3.basemap()
    assert sm3.basemap_key != key and sm3.basemap_key.startswith("basemap_%s_light_" % site["slug"])
    assert cache.get_bytes(sm3.basemap_key, ".png") is not None
    assert len(tiles.calls) == n


def test_bad_tile_bytes_are_not_cached(cfg, cache, fake_http, site):
    fake_http.add("openstreetmap", b"<html>not a tile</html>")
    sm = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light)
    sm.basemap()
    assert "0/" in sm.basemap_note                              # every tile failed
    assert not [p for p in os.listdir(cfg.cache_dir) if p.startswith("tile_")]
    # a later run with a healthy server gets real tiles (nothing bogus was remembered)
    fake_http.routes.clear()
    fake_http.add("openstreetmap", _png())
    sm2 = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light)
    assert sm2.basemap().getpixel((3, 3)) == BASE and sm2.basemap_note is None


def test_basemap_key_tracks_tile_url_and_inversion(cfg, cache, site):
    """A changed tile source or inversion setting must never reuse the old composite."""
    a = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark).basemap_key
    b = radar.SiteMap(cfg, cache, site, "dark", "https://tiles.example/{z}/{x}/{y}.png").basemap_key
    cfg.tile_dark_invert = False
    c = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark).basemap_key
    assert len({a, b, c}) == 3
    # the light theme ignores the inversion flag entirely
    d = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light).basemap_key
    cfg.tile_dark_invert = True
    assert radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light).basemap_key == d


def test_invert_lightness_keeps_hue():
    im = Image.new("RGB", (4, 1))
    im.putdata([(255, 255, 255), (0, 0, 0), (170, 211, 223), (255, 128, 0)])
    out = list(radar.invert_lightness(im).getdata())
    assert out[0] == (0, 0, 0) and out[1] == (255, 255, 255)    # lightness flipped
    assert out[2] == (32, 73, 85)                               # light blue -> dark blue
    assert out[3] == (255, 128, 0)                              # max+min == 255: unchanged
    assert radar.invert_lightness(radar.invert_lightness(im)).tobytes() == im.tobytes()


def test_dark_theme_inverts_light_tiles_only(cfg, cache, fake_http, site):
    white = (250, 250, 245)
    fake_http.add("openstreetmap", _png(color=white))
    dark = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark).basemap()
    light = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light).basemap()
    assert light.getpixel((5, 5)) == white                      # light theme: as served
    assert dark.getpixel((5, 5)) == (10, 10, 5)                 # dark theme: c + 255 - (max+min)
    assert radar.mean_luminance(dark) < radar.LIGHT_TILE_LUMINANCE < radar.mean_luminance(light)
    # genuinely dark tiles (the BASE colour) are left alone -- see test_basemap_from_tiles
    cfg.tile_dark_invert = False
    off = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark).basemap()
    assert off.getpixel((5, 5)) == white


def _tile_spots(fr):
    """{(tx, ty): (frame px at the centre of the tile's visible part, its shorter side)}."""
    left, top, right, bottom = fr.canvas_crop()
    sx, sy = fr.width / float(right - left), fr.height / float(bottom - top)
    tx0, ty0, tx1, ty1 = fr.tile_range()
    spots = {}
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            ox, oy = fr.tile_paste_origin(tx, ty)
            x0, y0 = max(ox, left), max(oy, top)
            x1, y1 = min(ox + geo.TILE_SIZE, right), min(oy + geo.TILE_SIZE, bottom)
            if x1 > x0 and y1 > y0:
                spots[(tx, ty)] = ((int(((x0 + x1) / 2.0 - left) * sx),
                                    int(((y0 + y1) / 2.0 - top) * sy)),
                                   min((x1 - x0) * sx, (y1 - y0) * sy))
    return spots


@pytest.mark.parametrize("keep", ["all_but_one", "only_one"])
def test_partial_dark_basemap_inverts_per_tile(cfg, cache, fake_http, site, keep):
    """Missing tiles leave the theme background; every fetched light tile is inverted,
    however many of its neighbours are missing (the decision is per tile, not taken on a
    composite whose mean the placeholder can drag either way)."""
    white = (250, 250, 245)
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    spots = _tile_spots(sm.frame)
    big = max(spots, key=lambda k: spots[k][1])         # tile with the largest visible part
    others = [k for k in spots if k != big]
    assert spots[big][1] > 30 and others
    failing = [big] if keep == "all_but_one" else others
    for tx, ty in failing:
        fake_http.fail("/%d/%d/%d.png" % (cfg.tile_zoom, tx, ty))   # first route wins
    fake_http.add("openstreetmap", _png(color=white))
    bm = sm.basemap()
    assert "incomplete" in sm.basemap_note
    bg, inverted = radar.THEMES["dark"]["bg"], (10, 10, 5)
    (bx, by), _ = spots[big]
    if keep == "all_but_one":
        assert bm.getpixel((bx, by)) == bg              # placeholder not turned bright
        fetched = [spots[k][0] for k in others if spots[k][1] > 12]
        assert fetched and all(bm.getpixel(xy) == inverted for xy in fetched)
    else:
        assert bm.getpixel((bx, by)) == inverted        # the lone tile is still inverted
        missing = [spots[k][0] for k in others if spots[k][1] > 12]
        assert all(bm.getpixel(xy) == bg for xy in missing)


def test_basemap_partial_is_used_but_not_cached(cfg, cache, fake_http, site):
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    tx0, ty0, _, _ = sm.frame.tile_range()
    fake_http.fail("/%d/%d/%d.png" % (cfg.tile_zoom, tx0, ty0))   # first route wins
    fake_http.add("openstreetmap", _png())
    bm = sm.basemap()
    assert bm.size == sm.frame.size and "incomplete" in sm.basemap_note
    assert cache.get_bytes(sm.basemap_key, ".png") is None
    out = os.path.join(cfg.out_dir, "partial.png")
    res = sm.render(NO_RADAR, [], out)
    assert res["ok"] and os.path.exists(out) and "basemap incomplete" in res["error"]


def test_basemap_without_any_tiles_still_renders(cfg, cache, fake_http, site):
    fake_http.fail("openstreetmap")
    sm = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light)
    out = os.path.join(cfg.out_dir, "notiles.png")
    res = sm.render(NO_RADAR, [], out)
    assert res["ok"] and os.path.exists(out) and not res["radar_drawn"]
    assert "0/" in sm.basemap_note


# ---- render: radar -------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["P", "L"])
def test_render_draws_radar_near_centre(cfg, cache, tiles, site, frame_img, mode):
    img = frame_img if mode == "P" else Image.new("L", frame_img.size, geo.MRMS_NODATA)
    if mode == "L":
        c, r = geo.mrms_cell(site["lat"], site["lon"])
        img.paste(124, (c - 10, r - 10, c + 11, r + 11))
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "maps", cfg.map_basename(site["slug"], "dark"))
    res = sm.render(_radar(img), [], out, tz="America/Denver")
    assert res == {"ok": True, "error": None, "path": out, "drawn_ids": [], "radar_drawn": True,
                   "roads_drawn_ids": []}
    assert os.path.exists(out) and not os.path.exists(out + ".tmp.%d" % os.getpid())
    im = Image.open(out)
    assert im.format == "PNG" and im.size == sm.frame.size
    im = im.convert("RGB")
    cx, cy = _centre(sm)
    r, g, b = im.getpixel((cx - 8, cy + 8))                # inside the 30 dBZ block
    assert g > 90 and g > r + 40 and g > b + 40
    assert im.getpixel((10, cy)) == BASE                   # far from the echo: basemap only


def test_render_respects_dbz_min_and_stale_label(cfg, cache, tiles, site, frame_img):
    cfg.radar_dbz_min = 35.0                               # the 30 dBZ block must vanish
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "faint.png")
    res = sm.render(_radar(frame_img, stale=True), [], out)
    assert res["ok"] and res["radar_drawn"] and res["error"] is None
    cx, cy = _centre(sm)
    assert Image.open(out).convert("RGB").getpixel((cx - 8, cy + 8)) == BASE


def test_render_without_radar(cfg, cache, tiles, site, frame_img):
    sm = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light)
    out = os.path.join(cfg.out_dir, "noradar.png")
    res = sm.render(NO_RADAR, [], out)
    assert res["ok"] and not res["radar_drawn"] and "IEM down" in res["error"]
    assert Image.open(out).size == sm.frame.size
    # ok but no/unusable image -> still written, radar flagged unavailable
    res = sm.render(dict(_radar(None)), [], out)
    assert res["ok"] and not res["radar_drawn"] and "radar unavailable" in res["error"]
    res = sm.render(_radar(Image.new("RGB", (8, 8))), [], out)
    assert res["ok"] and not res["radar_drawn"] and "mode" in res["error"]
    res = sm.render(None, None, out)
    assert res["ok"] and not res["radar_drawn"]


# ---- render: alerts ------------------------------------------------------------------
def test_render_alert_polygons_drawn_ids_and_holes(cfg, cache, tiles, site):
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    cx, cy = _centre(sm)
    out = os.path.join(cfg.out_dir, "alerts.png")

    covering = _alert("cover", [_box(lon, lat, 0.6)])
    far = _alert("far", [_box(-90.0, 30.0, 0.6)], color="#00ff00")
    nogeom = dict(_alert("nogeom", [_box(lon, lat, 0.6)]), geometry=None)
    multi = {"id": "multi", "event": "Flood Watch", "rank": 12, "color": "#2e8b57",
             "end_dt": None, "bbox": None,
             "geometry": {"type": "MultiPolygon",
                          "coordinates": [[_box(-90.0, 30.0, 0.5)],
                                          [_box(lon + 0.5, lat + 0.5, 0.1)]]}}
    res = sm.render(NO_RADAR, [far, covering, nogeom, multi], out)
    assert res["ok"] and set(res["drawn_ids"]) == {"cover", "multi"}
    px = Image.open(out).convert("RGB").getpixel((cx, cy))
    assert px != BASE and px[0] > BASE[0] + 40 and px[0] > px[1]    # red-tinted centre

    # a hole around the site: the alert is drawn (ring) but the centre stays untinted
    holed = _alert("holed", [_box(lon, lat, 0.6), _box(lon, lat, 0.15)])
    res = sm.render(NO_RADAR, [holed], out)
    assert res["drawn_ids"] == ["holed"]
    im = Image.open(out).convert("RGB")
    assert im.getpixel((cx, cy)) == BASE
    px_ring = im.getpixel((cx + 40, cy + 40))                  # inside the outer ring
    assert px_ring != BASE and px_ring[0] > BASE[0] + 40

    # a polygon whose bbox misses the frame produces no mask at all
    assert sm._alert_mask(far) == (None, [])
    assert sm._alert_mask(covering)[0].getbbox() is not None


def test_render_alert_draw_order_warning_on_top(cfg, cache, tiles, site):
    cfg.alert_fill_alpha = 255
    cfg.alert_outline_width = 0
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    cx, cy = _centre(sm)
    out = os.path.join(cfg.out_dir, "order.png")
    watch = _alert("watch", [_box(lon, lat, 0.5)], color="#00ff00", rank=12, event="Flood Watch")
    warning = _alert("warn", [_box(lon, lat, 0.3)], color="#ff0000", rank=0,
                     event="Flash Flood Warning")
    for order in ([watch, warning], [warning, watch]):
        res = sm.render(NO_RADAR, order, out)
        assert res["drawn_ids"] == ["watch", "warn"]           # statements first, warnings last
        assert Image.open(out).convert("RGB").getpixel((cx, cy)) == (255, 0, 0)
    assert [a["id"] for a in radar._draw_order(order)] == ["watch", "warn"]


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_alert_outline_has_casing_over_same_hue_radar(cfg, cache, tiles, site, theme):
    """A red flood outline over red (50 dBZ) echoes: the coloured line runs between two
    stroke-coloured edges (black on dark, white on light), so the boundary stays visible."""
    cfg.alert_outline_width = 3
    img = Image.new("P", (geo.MRMS_W, geo.MRMS_H), geo.MRMS_NODATA)
    c, r = geo.mrms_cell(site["lat"], site["lon"])
    img.paste(164, (c - 120, r - 120, c + 121, r + 121))      # 50 dBZ all over the frame
    assert radar.dbz_color(geo.mrms_dbz(164)) == (253, 0, 0)
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, theme, getattr(cfg, "tile_url_" + theme))
    out = os.path.join(cfg.out_dir, "casing_%s.png" % theme)
    res = sm.render(_radar(img), [_alert("fw", [_box(lon, lat, 0.3)], color="#E53935",
                                         event="Flood Watch")], out)
    assert res["ok"] and res["drawn_ids"] == ["fw"] and res["radar_drawn"]
    im = Image.open(out).convert("RGB")
    cx, cy = _centre(sm)
    _, top = sm.frame.lonlat_to_px(lon, lat + 0.3)         # the box's northern edge
    x = cx - 20                                            # clear of crosshair and label
    column = [im.getpixel((x, int(top) + dy)) for dy in range(-6, 7)]
    stroke, line = radar.THEMES[theme]["stroke"], (0xE5, 0x39, 0x35)
    assert line in column
    first, last = column.index(line), len(column) - 1 - column[::-1].index(line)
    assert stroke in column[:first] and stroke in column[last + 1:]   # casing on both sides
    # away from the edge: radar (outside) and radar + fill (inside), no casing colour
    assert stroke not in (im.getpixel((x, int(top) - 12)), im.getpixel((x, int(top) + 12)))


def test_render_survives_garbage_alerts(cfg, cache, tiles, site):
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "garbage.png")
    junk = [None, "nope", {"id": "x"}, {"id": "badgeom", "geometry": {"type": "Polygon"}},
            {"id": "weird", "geometry": {"type": "Polygon", "coordinates": [[[1]]]}, "rank": "?"},
            dict(_alert("nocolor", [_box(lon, lat, 0.4)]), color=None, rank=None, end_dt="x")]
    res = sm.render(NO_RADAR, junk, out)
    assert res["ok"] and res["drawn_ids"] == ["nocolor"]
    assert radar._hex_rgb(None) == (128, 128, 128) and radar._hex_rgb("#abc") == (170, 187, 204)


def test_render_write_failure_reports_error(cfg, cache, tiles, site, tmp_path):
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    blocker = tmp_path / "file"
    blocker.write_text("x")
    res = sm.render(NO_RADAR, [], str(blocker / "map.png"))   # parent is a file
    assert not res["ok"] and res["error"].startswith("write failed")


def test_render_fsyncs_png_before_rename(cfg, cache, tiles, site, monkeypatch):
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    sm.basemap()                               # tile/basemap cache writes happen before
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        st = os.fstat(fd)
        events.append(("fsync", st.st_ino, st.st_size))
        return real_fsync(fd)

    def replace(src, dst):
        events.append(("replace", os.stat(src).st_ino, dst))
        return real_replace(src, dst)
    monkeypatch.setattr(radar.os, "fsync", fsync)
    monkeypatch.setattr(radar.os, "replace", replace)
    out = os.path.join(cfg.out_dir, "synced.png")
    assert sm.render(NO_RADAR, [], out)["ok"]
    assert [e[0] for e in events] == ["fsync", "replace"]
    (_, ino_synced, size_synced), (_, ino_renamed, dst) = events
    assert ino_synced == ino_renamed and dst == out            # the temp file was synced
    assert size_synced == os.path.getsize(out) > 0             # ... with all of its data


# ---- fonts ------------------------------------------------------------------------------
@pytest.fixture
def fresh_fonts(monkeypatch):
    """Empty font cache for the test; the module's cache is restored afterwards."""
    monkeypatch.setattr(radar, "_fonts", {})
    monkeypatch.setattr(radar, "_font_state", {"path": None, "searched": False})


class _NoBBoxDraw:
    def textbbox(self, xy, text, font=None):
        raise ValueError("Only supported for TrueType fonts")


def test_text_size_falls_back_through_font_apis():
    class NewBitmap:                                   # Pillow >= 9.2 bitmap font
        def getbbox(self, text):
            return (0, 1, 6 * len(text), 12)

    class OldBitmap:                                   # Pillow < 9.2: getsize only
        def getsize(self, text):
            return (7 * len(text), 11)

    class MaskOnly:
        def getmask(self, text):
            return Image.new("L", (5 * len(text), 9))

    d = _NoBBoxDraw()
    assert radar._text_size(d, "abc", NewBitmap()) == (18, 11)
    assert radar._text_size(d, "abc", OldBitmap()) == (21, 11)
    assert radar._text_size(d, "abc", MaskOnly()) == (15, 9)
    assert radar._text_size(d, "abc", object()) == (18, 11)          # rough guess, no raise


def test_safe_text_for_bitmap_font():
    bitmap = _bitmap_font()
    assert radar._safe_text("Socorro · 50 dBZ ©", bitmap) == "Socorro · 50 dBZ ©"   # Latin-1
    assert radar._safe_text("a…b — c “d” Łódź", bitmap) == 'a...b - c "d" ?ód?'
    img = Image.new("RGB", (60, 20))
    radar._draw_text(ImageDraw.Draw(img), (0, 0), "Łódź…", bitmap, fill=(255, 255, 255))
    assert radar._fit_text(ImageDraw.Draw(img), "x" * 80, bitmap, 40).endswith("...")
    ttf = next((p for p in radar.FONT_PATHS if os.path.isabs(p) and os.path.exists(p)), None)
    if ttf:                                    # TrueType fonts draw any text unchanged
        assert radar._safe_text("a…b", ImageFont.truetype(ttf, 10)) == "a…b"


def test_font_search_order_and_caching(fresh_fonts, monkeypatch):
    tried = []
    sentinel = object()

    def truetype(path, size):
        tried.append(path)
        if path != "good.ttf":
            raise OSError("cannot open resource")
        return sentinel
    monkeypatch.setattr(radar.ImageFont, "truetype", truetype)
    monkeypatch.setattr(radar, "FONT_PATHS", ("/nope/DejaVuSans.ttf", "good.ttf", "later.ttf"))
    assert radar._font(10) is sentinel and tried == ["/nope/DejaVuSans.ttf", "good.ttf"]
    assert radar._font(12) is sentinel and tried[2:] == ["good.ttf"]    # the found one only
    assert radar._font(10) is sentinel and len(tried) == 3              # cached per size


def test_font_fallback_uses_sized_default_when_available(fresh_fonts, monkeypatch, caplog):
    monkeypatch.setattr(radar, "FONT_PATHS", ())
    calls = []
    real = ImageFont.load_default

    def old_pillow(*args):                  # Pillow < 10.1: load_default() takes no size
        calls.append(args)
        if args:
            raise TypeError("load_default() takes 0 positional arguments but 1 was given")
        return real()
    monkeypatch.setattr(radar.ImageFont, "load_default", old_pillow)
    assert radar._font(13) is not None and calls == [(13,), ()]
    assert "no TrueType font found" in caplog.text

    def new_pillow(size=None):              # Pillow >= 10.1: sized default font
        calls.append(("sized", size))
        return real()
    monkeypatch.setattr(radar.ImageFont, "load_default", new_pillow)
    radar._fonts.clear()
    radar._font(11)
    assert calls[-1] == ("sized", 11)


@pytest.fixture
def old_pillow_no_ttf(fresh_fonts, monkeypatch):
    """No TrueType font installed and Pillow < 9.2: ``textbbox`` refuses the bitmap font and
    the bitmap font has no ``getbbox``."""
    monkeypatch.setattr(radar, "FONT_PATHS", ())
    bitmap = _bitmap_font()
    monkeypatch.setattr(radar.ImageFont, "load_default", lambda *a: bitmap)
    real_textbbox = ImageDraw.ImageDraw.textbbox

    def textbbox(self, xy, text, font=None, *a, **k):
        if not isinstance(font, ImageFont.FreeTypeFont):
            raise ValueError("Only supported for TrueType fonts")
        return real_textbbox(self, xy, text, font, *a, **k)
    monkeypatch.setattr(ImageDraw.ImageDraw, "textbbox", textbbox)
    monkeypatch.delattr(ImageFont.ImageFont, "getbbox", raising=False)   # < 9.2 had none
    return bitmap


def test_render_decorates_without_truetype_on_old_pillow(cfg, cache, tiles, site, frame_img,
                                                         old_pillow_no_ttf, caplog):
    """No TrueType font installed and Pillow < 9.2 (``textbbox`` refuses the bitmap font):
    the map still gets its caption, marker and bars, and render() reports no error."""
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "oldpillow.png")
    res = sm.render(_radar(frame_img), [], out, tz="America/Denver",
                    notes=("zone outline missing — Flood Watch",))
    assert res["ok"] and res["error"] is None, res
    assert "decorations failed" not in caplog.text
    im = Image.open(out).convert("RGB")
    caption = [im.getpixel((x, y)) for x in range(8, 90) for y in range(6, 18)]
    assert any(min(px) > 150 for px in caption)            # light caption text on the map


def _bitmap_font():
    """Pillow's fixed bitmap font, which is what ``load_default()`` returned before 10.1."""
    load = getattr(ImageFont, "load_default_imagefont", None)       # Pillow >= 10.1
    return load() if load else ImageFont.load_default()


# ---- render: road closures and storm reports ---------------------------------------------
# Real items, trimmed from the live feeds of 2026-09-23 (coordinates rounded to 5 decimals):
# NMDOT nmroads.json + rss.xml -- NM 41 at McIntosh, a "Closure" whose RSS narrative is a
# crash, and NM 333 near Tijeras, "Difficult Driving Conditions" with "Standing water on
# roadway"; IEM's LSR GeoJSON -- the NWS report of NM 304 closed at Las Nutrias.
NM41_CLOSURE = {
    "id": "dd4b80e9-93bb-4549-b38d-e545867c920c", "kind": "closure", "category": "Closure",
    "title": "Closure, NM 41 northbound and southbound from mile marker 17, 1 mile south of "
             "McIntosh to mile marker 19, 1 mile north McIntosh.",
    "route": "NM 41", "mm_from": 17.0, "mm_to": 19.0, "direction": "both",
    "cause": "Emergency services are on the scene and working to clear the crash scene as "
             "soon as possible.", "cause_known": True, "source": "NMDOT",
    "link": "https://nmroads.com/", "bbox": (-106.05339, 34.84876, -106.05286, 34.87409),
    "geometry": {"type": "LineString", "coordinates": [
        [-106.05339, 34.84876], [-106.05336, 34.85012], [-106.05328, 34.85376],
        [-106.05324, 34.85627], [-106.0532, 34.85799], [-106.05316, 34.85995],
        [-106.05311, 34.86244], [-106.05301, 34.86772], [-106.05296, 34.87006],
        [-106.05295, 34.87044], [-106.05293, 34.87081], [-106.05291, 34.8717],
        [-106.0529, 34.87246], [-106.05286, 34.87409]]}}
NM333_WATER = {
    "id": "1140ecaf-4d65-4036-a4b7-7a09bbc64154", "kind": "water",
    "category": "Difficult Driving Conditions",
    "title": "Difficult Driving Conditions, NM 333 eastbound and westbound from mile marker 21 "
             "to mile marker 20.",
    "route": "NM 333", "mm_from": 21.0, "mm_to": 20.0, "direction": "both",
    "cause": "Standing water on roadway. Use extreme caution. Expect delays and use caution "
             "while travelling through the area.", "cause_known": True, "source": "NMDOT",
    "link": "https://nmroads.com/", "bbox": (-106.18503, 35.05312, -106.17104, 35.05879),
    "geometry": {"type": "LineString", "coordinates": [
        [-106.18503, 35.05879], [-106.18367, 35.05824], [-106.18199, 35.05756],
        [-106.18059, 35.05699], [-106.17956, 35.05658], [-106.17812, 35.05599],
        [-106.17673, 35.05544], [-106.17301, 35.05393], [-106.17104, 35.05312]]}}
NM304_REPORT = {
    "id": "202609222319-KABQ-las-nutrias", "kind": "report", "type": "FLASH FLOOD",
    "time": datetime(2026, 9, 22, 23, 19, tzinfo=UTC), "place": "Las Nutrias",
    "county": "Socorro", "lat": 34.48, "lon": -106.77, "source": "NWS ABQ",
    "remark": "NMDOT reports NM State Highway 304 closed at Mile Marker 11 (Las Nutrias) due "
              "to standing water.", "bbox": (-106.77, 34.48, -106.77, 34.48),
    "geometry": {"type": "Point", "coordinates": [-106.77, 34.48]}}


def _road(aid, kind, coords):
    """A road item with a LineString ([[lon, lat], ...]) or Point ([lon, lat]) geometry."""
    point = bool(coords) and not isinstance(coords[0], (list, tuple))
    return {"id": aid, "kind": kind,
            "geometry": {"type": "Point" if point else "LineString", "coordinates": coords}}


def _roads(events=(), reports=()):
    return {"covers_nm": True, "events": list(events), "reports": list(reports)}


def _classifier(theme):
    """Pixel -> "I" (theme ink), "S" (theme stroke, i.e. halo), "b" (basemap), "?" (other)."""
    ink, stroke = radar.THEMES[theme]["ink"], radar.THEMES[theme]["stroke"]
    return lambda px: "I" if px == ink else "S" if px == stroke else "b" if px == BASE else "?"


def _runs(s):
    """"IIISSb" -> [("I", 3), ("S", 2), ("b", 1)]"""
    out = []
    for ch in s:
        if out and out[-1][0] == ch:
            out[-1][1] += 1
        else:
            out.append([ch, 1])
    return [tuple(r) for r in out]


def _png_bytes(sm, out, **kw):
    """Render with no radar and no alerts; the PNG's bytes."""
    res = sm.render(NO_RADAR, [], out, **kw)
    assert res["ok"], res
    with open(out, "rb") as f:
        return f.read()


def _render_roads(cfg, cache, site, theme, roads, name="roads.png"):
    sm = radar.SiteMap(cfg, cache, site, theme, getattr(cfg, "tile_url_" + theme))
    out = os.path.join(cfg.out_dir, "%s_%s" % (theme, name))
    res = sm.render(NO_RADAR, [], out, roads=roads)
    assert res["ok"] and "road" not in (res["error"] or ""), res
    return sm, res, Image.open(out).convert("RGB")


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_render_without_road_items_is_byte_identical(cfg, cache, tiles, site, frame_img, theme):
    """No road items on the map (none given, empty lists, the Lubbock case, or items that all
    miss the frame) = exactly the PNG written without the argument: no layer, no key."""
    lon, lat = site["lon"], site["lat"]
    far = _roads([_road("far", "closure", [[-90.0, 30.0], [-89.0, 31.0]]),
                  _road("farw", "water", [-100.0, 40.0])],
                 [dict(NM304_REPORT, id="farr", lat=45.0, lon=-120.0, geometry=None)])
    variants = [{}, {"roads": None}, {"roads": {}}, {"roads": _roads()},
                {"roads": {"covers_nm": False, "events": [], "reports": []}}, {"roads": far}]
    blobs = []
    for i, kw in enumerate(variants):
        sm = radar.SiteMap(cfg, cache, site, theme, getattr(cfg, "tile_url_" + theme))
        out = os.path.join(cfg.out_dir, "same_%d.png" % i)
        res = sm.render(_radar(frame_img), [_alert("a", [_box(lon, lat, 0.3)])], out,
                        notes=("not shaded (no outline available): Flood Watch",),
                        tz="America/Denver", **kw)
        assert res["ok"] and res["radar_drawn"] and res["drawn_ids"] == ["a"]
        assert res["roads_drawn_ids"] == [] and sm._road_key is None
        with open(out, "rb") as f:
            blobs.append(f.read())
    assert all(b == blobs[0] for b in blobs[1:])


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_closure_line_is_ink_on_a_halo_with_no_entry_symbol(cfg, cache, tiles, site, theme):
    """A closure through the centre: a 4 px ink core inside a 9 px halo in the stroke colour
    (white on black on the dark map, near-black on white on the light one), and the "no
    entry" disc halfway along it: 1 px stroke ring, ink disc, stroke bar."""
    lon, lat = site["lon"], site["lat"]
    closure = _road("c1", "closure", [[lon - 0.6, lat], [lon + 0.2, lat]])
    sm, res, im = _render_roads(cfg, cache, site, theme, _roads([closure]))
    assert res["roads_drawn_ids"] == ["c1"]
    cls = _classifier(theme)
    cx, cy = sm.frame.lonlat_to_px(lon, lat)
    # across the line, clear of the symbol and the site marker
    col = _runs("".join(cls(im.getpixel((int(cx) - 55, int(cy) + dy))) for dy in range(-9, 10)))
    assert [c for c, _ in col] == ["b", "S", "I", "S", "b"], col
    assert col[2][1] == radar.ROAD_LINE_W == 4
    assert col[1][1] + col[2][1] + col[3][1] == radar.ROAD_HALO_W == 9
    # the symbol sits halfway along the line (lon - 0.2)
    sx, sy = sm.frame.lonlat_to_px(lon - 0.2, lat)
    sx, sy = int(sx), int(sy)
    col = _runs("".join(cls(im.getpixel((sx, sy + dy))) for dy in range(-12, 13)))
    assert [c for c, _ in col] == ["b", "S", "I", "S", "I", "S", "b"], col
    assert col[1][1] == col[5][1] == 1 and 3 <= col[3][1] <= 4          # ring, bar
    assert col[2][1] >= 5 and col[4][1] >= 5                             # ink disc
    row = _runs("".join(cls(im.getpixel((sx + dx, sy))) for dx in range(-12, 13)))
    assert [c for c, _ in row] == ["I", "S", "I", "S", "I", "S", "I"], row   # line runs into it


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_water_line_is_dashed_over_a_continuous_halo(cfg, cache, tiles, site, theme):
    lon, lat = site["lon"], site["lat"]
    water = _road("w1", "water", [[lon - 0.6, lat + 0.3], [lon + 0.6, lat + 0.3]])
    sm, res, im = _render_roads(cfg, cache, site, theme, _roads([water]))
    assert res["roads_drawn_ids"] == ["w1"]
    cls = _classifier(theme)
    x0, y = sm.frame.lonlat_to_px(lon - 0.6, lat + 0.3)
    sx, sy = sm.frame.lonlat_to_px(lon, lat + 0.3)          # the symbol, halfway along
    xs = range(int(x0) + 1, int(sx) - radar.ROAD_SYMBOL_R - 2)
    row = "".join(cls(im.getpixel((x, int(y)))) for x in xs)
    assert set(row) == {"I", "S"}, row                     # halo in every gap: no basemap
    inner = _runs(row)[1:-1]                               # whole dashes and gaps only
    dashes = [n for c, n in inner if c == "I"]
    gaps = [n for c, n in inner if c == "S"]
    assert len(dashes) >= 3 and len(gaps) >= 3, row
    assert all(7 <= n <= 9 for n in dashes) and all(5 <= n <= 7 for n in gaps), row
    assert abs(sum(dashes) / len(dashes) - radar.ROAD_DASH[0]) <= 0.5
    assert abs(sum(gaps) / len(gaps) - radar.ROAD_DASH[1]) <= 0.5
    # the halo is continuous and 9 px wide: stroke above and below every dash and gap
    for dy in (-4, 4):
        assert set(cls(im.getpixel((x, int(y) + dy))) for x in xs) == {"S"}
    for dy in (-6, 6):
        assert set(cls(im.getpixel((x, int(y) + dy))) for x in xs) == {"b"}
    # symbol: stroke-filled disc with an ink ring and two ink waves (4 ink runs down its
    # middle), inside a 1 px stroke ring
    col = _runs("".join(cls(im.getpixel((int(sx), int(sy) + dy))) for dy in range(-12, 13)))
    assert col[0][0] == col[-1][0] == "b" and col[1] == ("S", 1) and col[-2] == ("S", 1), col
    assert sum(1 for c, _ in col if c == "I") == 4, col


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_storm_report_is_an_upward_triangle_with_halo(cfg, cache, tiles, site, theme):
    lon, lat = site["lon"], site["lat"]
    rep = dict(NM304_REPORT, id="r1", lat=lat + 0.25, lon=lon + 0.4,
               geometry={"type": "Point", "coordinates": [lon + 0.4, lat + 0.25]})
    bare = {"id": "r2", "lat": lat + 0.25, "lon": lon - 0.4}     # no kind, no geometry
    sm, res, im = _render_roads(cfg, cache, site, theme, _roads(reports=[rep, bare]))
    assert res["roads_drawn_ids"] == ["r1", "r2"]
    assert sm._road_shape("report", rep)[0] == []                  # a report has no line
    cls = _classifier(theme)
    for lo in (lon + 0.4, lon - 0.4):
        tx, ty = sm.frame.lonlat_to_px(lo, lat + 0.25)
        tx, ty = int(tx), int(ty)

        def ink_width(dy):
            return sum(1 for dx in range(-12, 13)
                       if cls(im.getpixel((tx + dx, ty + dy))) == "I")
        assert cls(im.getpixel((tx, ty))) == "I"
        assert 0 < ink_width(-5) < ink_width(-1) < ink_width(2) <= radar.ROAD_REPORT_SIDE + 1
        col = _runs("".join(cls(im.getpixel((tx, ty + dy))) for dy in range(-14, 10)))
        assert [c for c, _ in col] == ["b", "S", "I", "S", "b"], col
        assert 10 <= col[2][1] <= 12                              # height of a 12 px triangle


def test_roads_are_drawn_over_alerts_and_under_the_decorations(cfg, cache, tiles, site):
    cfg.alert_fill_alpha = 255
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "order.png")
    closure = _road("c", "closure", [[lon - 0.6, lat], [lon + 0.6, lat]])
    res = sm.render(NO_RADAR, [_alert("a", [_box(lon, lat, 0.6)], color="#E53935")], out,
                    roads=_roads([closure]))
    assert res["drawn_ids"] == ["a"] and res["roads_drawn_ids"] == ["c"]
    im = Image.open(out).convert("RGB")
    cx, cy = _centre(sm)
    assert im.getpixel((cx - 55, cy)) == radar.THEMES["dark"]["ink"]      # road over alert
    assert im.getpixel((cx - 55, cy - 12)) == (0xE5, 0x39, 0x35)          # opaque alert fill
    # the site name is drawn after the roads: marker-coloured text over the road's core
    marker = radar.THEMES["dark"]["marker"]
    assert any(im.getpixel((x, y)) == marker
               for x in range(cx + 12, min(cx + 90, im.width)) for y in range(cy - 1, cy + 2))


def test_roads_outside_the_frame_are_skipped(cfg, cache, tiles, site):
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    w, h = sm.frame.size
    west, south, east, north = sm.frame.bbox
    corner = [[west - 0.5, north - 0.1], [west + 0.1, north + 0.5]]
    outside = [
        _road("far-line", "closure", [[-90.0, 30.0], [-89.0, 31.0]]),
        _road("far-point", "water", [-100.0, 40.0]),
        _road("corner-miss", "closure", corner),     # its box overlaps the frame, it does not
        _road("just-east", "water", [[east + 0.01, lat - 0.2], [east + 0.3, lat + 0.2]]),
        _road("just-north", "closure", [lon, north + 0.001]),
    ]
    far_report = dict(NM304_REPORT, id="far-report", lat=north + 0.05, lon=lon,
                      geometry={"type": "Point", "coordinates": [lon, north + 0.05]})
    assert geo.bbox_intersects(geo.geometry_bbox({"type": "Polygon", "coordinates": [
        corner + corner[:1]]}), sm.frame.bbox)
    assert sm._road_shape("closure", outside[2]) == ([], None)
    out = os.path.join(cfg.out_dir, "outside.png")
    plain = _png_bytes(sm, out)
    res = sm.render(NO_RADAR, [], out, roads=_roads(outside, [far_report]))
    assert res["ok"] and res["roads_drawn_ids"] == [] and sm._road_key is None
    with open(out, "rb") as f:
        assert f.read() == plain
    # a line crossing the western edge is clipped there; its symbol goes halfway along the
    # part that is on the map
    crossing = _road("crossing", "closure", [[lon - 5.0, lat + 0.3], [lon - 0.3, lat + 0.3]])
    res = sm.render(NO_RADAR, [], out, roads=_roads(outside + [crossing], [far_report]))
    assert res["roads_drawn_ids"] == ["crossing"]
    pieces, (ax, ay) = sm._road_shape("closure", crossing)
    x_end, y = sm.frame.lonlat_to_px(lon - 0.3, lat + 0.3)
    assert len(pieces) == 1 and pieces[0][0][0] == pytest.approx(0.0, abs=1e-9)
    assert ax == pytest.approx(x_end / 2.0, abs=0.5) and ay == pytest.approx(y, abs=0.5)
    im = Image.open(out).convert("RGB")
    assert im.getpixel((0, int(y))) == radar.THEMES["dark"]["ink"]      # runs to the edge
    # a symbol right at the edge is pulled in so all of it is on the map
    edge = _road("edge", "water", [east - 1e-4, lat + 0.3])
    _, (ex, _) = sm._road_shape("water", edge)
    assert ex == pytest.approx(w - 1 - radar._symbol_extent("water"))


def test_road_key_only_when_something_is_drawn(cfg, cache, tiles, site):
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light)
    w, h = sm.frame.size
    out = os.path.join(cfg.out_dir, "key.png")
    _png_bytes(sm, out, roads=None)
    assert sm._road_key is None
    im0 = Image.open(out).convert("RGB")

    closure = _road("c", "closure", [[lon - 0.5, lat + 0.3], [lon + 0.5, lat + 0.3]])
    sm.render(NO_RADAR, [], out, roads=_roads([closure]))
    key = dict(sm._road_key)
    assert key["labels"] == ["Road closed"]
    x0, y0, x1, y1 = key["box"]
    assert 0 < x0 < 20 and x1 < w and h / 2 < y0 < y1 < h - 25       # bottom-left
    im1 = Image.open(out).convert("RGB")
    assert im1.crop((x0, y0, x1, y1)).tobytes() != im0.crop((x0, y0, x1, y1)).tobytes()
    # it sits above the scale bars and the attribution: nothing below it changed
    assert im1.crop((0, y1 + 1, w, h)).tobytes() == im0.crop((0, y1 + 1, w, h)).tobytes()

    def sample_ink(img, box):                   # ink left of the key's symbols = line sample
        bx0, by0, _, by1 = box
        ink = radar.THEMES["light"]["ink"]
        return sum(1 for x in range(bx0 + 2, bx0 + 30) for y in range(by0 + 1, by1)
                   if img.getpixel((x, y)) == ink)
    assert sample_ink(im1, key["box"]) > 0
    # a closure drawn only as a point: its key row has the symbol but no line sample
    sm.render(NO_RADAR, [], out, roads=_roads([_road("p", "closure", [lon, lat + 0.3])]))
    assert sm._road_key["labels"] == ["Road closed"] and sm._road_key["box"] == key["box"]
    assert sample_ink(Image.open(out).convert("RGB"), key["box"]) == 0

    # items that all miss the map: no key
    sm.render(NO_RADAR, [], out, roads=_roads([_road("far", "water", [-90.0, 30.0])]))
    assert sm._road_key is None

    # every kind: one row each, in a fixed order whatever the input order (on the default
    # 600 px map; on this 240 px one three rows would reach the site marker)
    cfg.map_px = 600
    big = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light)
    big.render(NO_RADAR, [], out, roads=_roads([closure]))
    one = big._road_key["box"]
    assert big._road_key["where"] == "bottom-left"
    water = _road("w", "water", [[lon - 0.5, lat + 0.5], [lon + 0.5, lat + 0.5]])
    rep = {"id": "r", "kind": "report", "lat": lat + 0.4, "lon": lon + 0.6}
    big.render(NO_RADAR, [], out, roads=_roads([water, closure], [rep]))
    assert big._road_key["labels"] == ["Road closed", "Water on road", "Storm report (NWS)"]
    bx0, by0, bx1, by1 = big._road_key["box"]
    assert by1 == one[3] and by0 < one[1] and bx1 > one[2]          # taller, same bottom


SOCORRO_NM52 = (-107.65421, 33.52915)       # a real NM 52 vertex, bottom-left of Socorro's map


def test_road_key_never_hides_a_road_symbol(cfg, cache, tiles, site):
    """A symbol where the key would go (bottom-left, above the scale bars) moves the key to
    another corner, so the symbol stays visible and its id stays in roads_drawn_ids (the
    page's "on map"); live 2026-09-23 a point on NM 52 landed exactly there on Socorro's
    600 px map."""
    cfg.map_px = 600
    sm = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light)
    x, y = sm.frame.lonlat_to_px(*SOCORRO_NM52)
    assert x < 60 and y > sm.frame.size[1] - 110                     # the bottom-left corner
    out = os.path.join(cfg.out_dir, "keymove.png")
    plain = _png_bytes(sm, out)
    point = _road("nm52", "closure", list(SOCORRO_NM52))
    res = sm.render(NO_RADAR, [], out, roads=_roads([point]))
    assert res["roads_drawn_ids"] == ["nm52"]
    key = sm._road_key
    assert key["where"] != "bottom-left"
    x0, y0, x1, y1 = key["box"]
    assert not (x0 <= x <= x1 and y0 <= y <= y1)
    cls = _classifier("light")
    im = Image.open(out).convert("RGB")
    xi, yi = int(round(x)), int(round(y))
    assert [cls(im.getpixel((xi, yi + dy))) for dy in (-5, 0, 5)] == ["I", "S", "I"]
    assert plain != open(out, "rb").read()
    # a line under the bottom-left spot, but with its symbol elsewhere, only costs pixels:
    # the key then takes the corner with the fewest road pixels under it
    lon, lat = site["lon"], site["lat"]
    long = _road("long", "water", [[lon - 0.95, lat - 0.62], [lon + 0.95, lat - 0.62]])
    res = sm.render(NO_RADAR, [], out, roads=_roads([long]))
    assert res["roads_drawn_ids"] == ["long"] and sm._road_key is not None


def test_road_key_left_out_rather_than_covering_the_site_marker(cfg, cache, tiles, site):
    """On a small map (WEATHER_MAP_PX may be 200) a three-row key would cover the site
    marker wherever it went: it is left out, and every item still counts as drawn."""
    cfg.map_px = 200
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "keysmall.png")
    closure = _road("c", "closure", [[lon - 0.5, lat + 0.3], [lon + 0.5, lat + 0.3]])
    water = _road("w", "water", [[lon - 0.5, lat + 0.5], [lon + 0.5, lat + 0.5]])
    rep = {"id": "r", "kind": "report", "lat": lat + 0.4, "lon": lon + 0.6}
    res = sm.render(NO_RADAR, [], out, roads=_roads([water, closure], [rep]))
    assert res["ok"] and res["roads_drawn_ids"] == ["w", "c", "r"]
    assert sm._road_key is None
    # one row fits below the marker
    res = sm.render(NO_RADAR, [], out, roads=_roads([closure]))
    assert res["roads_drawn_ids"] == ["c"] and sm._road_key is not None
    cx, cy = sm.frame.lonlat_to_px(lon, lat)
    x0, y0, x1, y1 = sm._road_key["box"]
    assert y0 > cy + 12 or x1 < cx - 12 or x0 > cx + 12 or y1 < cy - 12


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_closures_are_drawn_over_water_and_symbols_over_reports(cfg, cache, tiles, site, theme):
    lon, lat = site["lon"], site["lat"]
    water = _road("w", "water", [[lon - 0.7, lat + 0.3], [lon + 0.1, lat + 0.3]])
    closure = _road("c", "closure", [[lon - 0.5, lat + 0.7], [lon - 0.5, lat + 0.2]])
    spot = [lon + 0.45, lat + 0.3]
    rep = {"id": "rp", "kind": "report", "lat": spot[1], "lon": spot[0]}
    events = [water, _road("wp", "water", spot), closure, _road("cp", "closure", spot)]
    sm, res, im = _render_roads(cfg, cache, site, theme, _roads(events, [rep]))
    assert res["roads_drawn_ids"] == ["w", "wp", "c", "cp", "rp"]       # input order
    cls = _classifier(theme)
    # where the lines cross, the closure's core runs straight through and its halo cuts
    # the water line
    xc, yc = sm.frame.lonlat_to_px(lon - 0.5, lat + 0.3)
    xc, yc = int(xc), int(yc)
    assert {cls(im.getpixel((xc, yc + dy))) for dy in range(-7, 8)} == {"I"}
    row = _runs("".join(cls(im.getpixel((xc + dx, yc))) for dx in range(-12, 13)))
    at = next(i for i, (c, n) in enumerate(row)
              if c == "I" and sum(m for _, m in row[:i]) <= 12 < sum(m for _, m in row[:i + 1]))
    assert row[at][1] == 4 and row[at - 1][0] == row[at + 1][0] == "S"
    assert row[at - 1][1] >= 2 and row[at + 1][1] >= 2
    # three symbols on one spot: the closure disc is on top (ink above and below its bar)
    sx, sy = sm.frame.lonlat_to_px(*spot)
    sx, sy = int(sx), int(sy)
    assert [cls(im.getpixel((sx, sy + dy))) for dy in (-5, 0, 5)] == ["I", "S", "I"]
    # ... and a storm report on the same spot as a water disc is not hidden under it: the
    # disc stays where it is, the triangle moves just above it (live 2026-09-23: "water over
    # NM 252" sat exactly under NMDOT's NM 252 water disc on the Clovis map)
    sm2, res2, im2 = _render_roads(cfg, cache, site, theme,
                                   _roads([_road("wp", "water", spot)], [rep]), name="wr.png")
    assert res2["roads_drawn_ids"] == ["wp", "rp"]
    assert cls(im2.getpixel((sx, sy - 5))) == "S"                  # the water disc's inside
    grown = radar.ROAD_REPORT_SIDE + 2.0 * math.sqrt(3.0) * radar.ROAD_REPORT_HALO
    ty = sy - (radar.ROAD_SYMBOL_R + 1) - grown / (2.0 * math.sqrt(3.0)) - 1
    assert cls(im2.getpixel((sx, int(round(ty))))) == "I"         # the triangle's ink
    assert cls(im2.getpixel((sx, int(round(ty)) + 9))) == "S"      # a gap before the disc
    sm3, _, im3 = _render_roads(cfg, cache, site, theme, _roads(reports=[rep]), name="r.png")
    assert cls(im3.getpixel((sx, sy - 5))) == "I"                  # alone: at its own spot


def test_real_nmdot_and_storm_report_items_land_on_the_right_maps(cfg, cache, tiles):
    roads = _roads([NM41_CLOSURE, NM333_WATER], [NM304_REPORT])
    ids = [NM41_CLOSURE["id"], NM333_WATER["id"], NM304_REPORT["id"]]
    expect = {"albuquerque": ids, "socorro": [NM304_REPORT["id"]],
              "lubbock": [], "clovis": [], "fort_sumner": []}
    sites = {s["slug"]: s for s in cfg.sites}
    assert set(expect) == set(sites)
    for slug, want in expect.items():
        for theme, url in cfg.themes:
            sm = radar.SiteMap(cfg, cache, sites[slug], theme, url)
            out = os.path.join(cfg.out_dir, "real_%s_%s.png" % (slug, theme))
            plain = _png_bytes(sm, out)
            res = sm.render(NO_RADAR, [], out, roads=roads)
            assert res["ok"] and res["roads_drawn_ids"] == want, (slug, theme)
            with open(out, "rb") as f:
                assert (f.read() == plain) is (not want), (slug, theme)
    # NM 41 is 2 miles long: a few pixels at this scale, all under its symbol, which sits
    # halfway along it
    sm = radar.SiteMap(cfg, cache, sites["albuquerque"], "light", cfg.tile_url_light)
    pieces, (ax, ay) = sm._road_shape("closure", NM41_CLOSURE)
    x_a, y_a = sm.frame.lonlat_to_px(*NM41_CLOSURE["geometry"]["coordinates"][0])
    x_b, y_b = sm.frame.lonlat_to_px(*NM41_CLOSURE["geometry"]["coordinates"][-1])
    assert len(pieces) == 1 and radar._length(pieces[0]) < 2 * radar.ROAD_SYMBOL_R
    assert ax == pytest.approx((x_a + x_b) / 2, abs=0.5) and ay == pytest.approx((y_a + y_b) / 2, abs=0.5)


def test_render_survives_garbage_road_items(cfg, cache, tiles, site, caplog):
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "junk.png")
    good = _road("ok", "closure", [[lon - 0.3, lat + 0.3], [lon + 0.3, lat + 0.3]])
    deep = {"type": "Point", "coordinates": [lon, lat]}
    for _ in range(6):
        deep = {"type": "GeometryCollection", "geometries": [deep]}
    junk_events = [
        None, "nope", 42,
        {"id": "nokind", "geometry": good["geometry"]},
        {"id": "roadwork", "kind": "roadwork", "geometry": good["geometry"]},
        {"id": "nogeom", "kind": "closure"},
        {"id": "badcoords", "kind": "closure",
         "geometry": {"type": "LineString", "coordinates": [["a", "b"], [1]]}},
        {"id": "notalist", "kind": "water", "geometry": {"type": "LineString", "coordinates": 5}},
        {"id": "nan", "kind": "water", "geometry": {"type": "LineString", "coordinates": [
            [float("nan"), lat], [lon, float("inf")]]}},
        {"id": "polygon", "kind": "closure",
         "geometry": {"type": "Polygon", "coordinates": [_box(lon, lat, 0.2)]}},
        {"id": "empty", "kind": "closure", "geometry": {"type": "LineString", "coordinates": []}},
        {"id": "nullpoint", "kind": "water", "geometry": {"type": "Point", "coordinates": None}},
        {"id": "deep", "kind": "water", "geometry": deep},
        good]
    junk_reports = [None, {"id": "r-nolatlon"}, {"id": "r-strings", "lat": "34", "lon": "-106"}]
    roads = {"covers_nm": True, "events": junk_events, "reports": junk_reports}
    res = sm.render(NO_RADAR, [], out, roads=roads)
    assert res["ok"] and res["roads_drawn_ids"] == ["ok"] and "road" not in res["error"]
    assert "road item badcoords not drawn" in caplog.text
    assert "road item notalist not drawn" in caplog.text
    # containers of the wrong shape are simply no roads
    for bad in ("nope", 5, {"events": "x", "reports": None}, {"events": {"a": 1}}):
        res = sm.render(NO_RADAR, [], out, roads=bad)
        assert res["ok"] and res["roads_drawn_ids"] == [] and sm._road_key is None
    # a plain list of items is accepted too
    assert sm.render(NO_RADAR, [], out, roads=[good])["roads_drawn_ids"] == ["ok"]


def test_roads_drawn_ids_are_unique_and_real(cfg, cache, tiles, site):
    lon, lat = site["lon"], site["lat"]
    a = _road("dup", "closure", [[lon - 0.5, lat + 0.3], [lon - 0.1, lat + 0.3]])
    b = _road("dup", "water", [[lon - 0.5, lat + 0.5], [lon - 0.1, lat + 0.5]])
    anon = _road(None, "closure", [lon + 0.4, lat + 0.4])
    del anon["id"]
    sm, res, im = _render_roads(cfg, cache, site, "dark", _roads([a, b, anon]))
    assert res["roads_drawn_ids"] == ["dup"]
    sx, sy = sm.frame.lonlat_to_px(lon + 0.4, lat + 0.4)
    assert im.getpixel((int(sx), int(sy) - 5)) == radar.THEMES["dark"]["ink"]   # still drawn


def test_road_layer_failure_leaves_the_map_without_roads(cfg, cache, tiles, site, monkeypatch,
                                                         caplog):
    """The layer is composited only when it was drawn completely: a failure half-way leaves
    no partial roads on the map and none in ``roads_drawn_ids``, and says so."""
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "fail.png")
    plain = _png_bytes(sm, out)
    water = _road("w", "water", [[lon - 0.5, lat + 0.5], [lon + 0.5, lat + 0.5]])
    closure = _road("c", "closure", [[lon - 0.5, lat + 0.3], [lon + 0.5, lat + 0.3]])

    def boom(*a, **k):
        raise RuntimeError("symbol exploded")
    monkeypatch.setitem(radar._SYMBOLS, "closure", boom)
    res = sm.render(NO_RADAR, [], out, roads=_roads([water, closure]))
    assert res["ok"] and res["roads_drawn_ids"] == [] and sm._road_key is None
    assert "road layer failed: symbol exploded" in res["error"]
    with open(out, "rb") as f:
        assert f.read() == plain                                 # the water line is gone too
    assert "road layer failed" in caplog.text


def test_road_key_failure_keeps_roads_and_other_decorations(cfg, cache, tiles, site, frame_img,
                                                            monkeypatch, caplog):
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "keyfail.png")
    good = sm.render(_radar(frame_img), [], out, tz="America/Denver")
    caption = Image.open(out).convert("RGB").crop((0, 0, sm.frame.width, 20)).tobytes()

    def boom(self, *a, **k):
        raise RuntimeError("key exploded")
    monkeypatch.setattr(radar.SiteMap, "_draw_road_key", boom)
    closure = _road("c", "closure", [[lon - 0.5, lat + 0.3], [lon + 0.5, lat + 0.3]])
    res = sm.render(_radar(frame_img), [], out, tz="America/Denver", roads=_roads([closure]))
    assert res["ok"] and res["roads_drawn_ids"] == ["c"] and res["error"] == good["error"]
    assert "road key failed" in caplog.text and "decorations failed" not in caplog.text
    im = Image.open(out).convert("RGB")
    assert im.crop((0, 0, sm.frame.width, 20)).tobytes() == caption     # caption still there


def test_road_key_with_bitmap_font_on_old_pillow(cfg, cache, tiles, site, old_pillow_no_ttf,
                                                 caplog):
    lon, lat = site["lon"], site["lat"]
    sm = radar.SiteMap(cfg, cache, site, "light", cfg.tile_url_light)
    out = os.path.join(cfg.out_dir, "keybitmap.png")
    closure = _road("c", "closure", [[lon - 0.5, lat + 0.3], [lon + 0.5, lat + 0.3]])
    rep = {"id": "r", "kind": "report", "lat": lat + 0.5, "lon": lon + 0.5}
    res = sm.render(NO_RADAR, [], out, roads=_roads([closure], [rep]))
    assert res["ok"] and res["roads_drawn_ids"] == ["c", "r"]
    assert sm._road_key["labels"] == ["Road closed", "Storm report (NWS)"]
    assert "road key failed" not in caplog.text and "decorations failed" not in caplog.text


# ---- road geometry helpers ----------------------------------------------------------------
def test_clip_polyline():
    w, h = 100, 80
    assert radar._clip_polyline([(10, 10), (50, 40)], w, h) == [[(10, 10), (50, 40)]]
    # in, out through the east edge, back in: two parts, cut exactly at the edges
    pieces = radar._clip_polyline([(-50, 40), (50, 40), (150, 40), (150, 60), (50, 60)], w, h)
    assert pieces == [[(0, 40), (50, 40), (100, 40)], [(100, 60), (50, 60)]]
    assert radar._clip_polyline([(-10, -10), (-5, 90)], w, h) == []           # all outside
    assert radar._clip_polyline([(-30, 20), (20, -30)], w, h) == []           # misses a corner
    # a diagonal enters through the west edge and leaves through the east edge
    (piece,) = radar._clip_polyline([(-20, 90), (120, -10)], w, h)
    assert piece == [(0, pytest.approx(90 - 100 * 20 / 140.0)),
                     (100, pytest.approx(90 - 100 * 120 / 140.0))]
    assert radar._clip_polyline([(5, 5)], w, h) == [[(5, 5), (5, 5)]]
    assert radar._clip_polyline([(500, 5)], w, h) == []
    assert radar._clip_segment(0, 0, 10, 0, w, h) == (0.0, 1.0)
    assert radar._clip_segment(0, -1, 10, -1, w, h) is None                  # parallel, outside


def test_dashes_follow_the_pattern_across_vertices():
    assert radar._dashes([(0, 0), (40, 0)], 8, 6) == [
        [(0, 0), (8, 0)], [(14, 0), (22, 0)], [(28, 0), (36, 0)]]
    d = radar._dashes([(0, 0), (5, 0), (5, 20)], 8, 6)          # the first dash bends
    assert d == [[(0, 0), (5, 0), (5, 3)], [(5, 9), (5, 17)]]
    assert radar._dashes([(0, 0)], 8, 6) == [] and radar._dashes([(0, 0), (0, 0)], 8, 6) == []


def test_midpoint_thin_and_length():
    assert radar._midpoint([[(0, 0), (10, 0)], [(20, 0), (20, 30)]]) == (20, 10)
    assert radar._midpoint([]) is None and radar._midpoint([[(3, 4), (3, 4)]]) == (3, 4)
    assert radar._thin([(0, 0), (0.2, 0), (0.4, 0), (1, 0), (1.1, 0)]) == [(0, 0), (1.1, 0)]
    assert radar._thin([(0, 0), (1, 0)]) == [(0, 0), (1, 0)]
    assert radar._length([(0, 0), (3, 4), (3, 10)]) == 11.0


def test_road_parts_and_items():
    lines, pts = radar._road_parts({"type": "GeometryCollection", "geometries": [
        {"type": "MultiLineString",
         "coordinates": [[[1, 2, 99], [3, 4]], [], [[5, 6], [float("nan"), 1]]]},
        {"type": "MultiPoint", "coordinates": [[7, 8]]}, {"type": "Point", "coordinates": [9, 10]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1], [0, 0]]]}]})
    assert lines == [[(1.0, 2.0), (3.0, 4.0)], [(5.0, 6.0)]]
    assert pts == [(7.0, 8.0), (9.0, 10.0)]
    assert radar._road_parts(None) == ([], []) and radar._road_parts({"type": "Point"}) == ([], [])
    assert radar._item_geometry({"lat": 34.5, "lon": -106.0}) == {
        "type": "Point", "coordinates": [-106.0, 34.5]}
    assert radar._item_geometry({"lat": "34.5", "lon": -106.0}) is None
    items = radar._road_items({"events": [{"kind": "water"}, {"kind": "closure"}, {"kind": "x"},
                                          {}], "reports": [{}, {"kind": "closure"}]})
    assert [k for k, _ in items] == ["water", "closure", "report", "closure"]
    assert radar._road_items([{"kind": "report"}, {}]) == [("report", {"kind": "report"})]
    assert radar._road_items(None) == [] and radar._road_items("x") == []


def test_report_clear_of_discs():
    """A storm-report triangle that a disc would hide moves just above it, or just below
    when the disc is at the top edge; a clear one stays put."""
    r_disc = radar.ROAD_SYMBOL_R + 1.0
    grown = radar.ROAD_REPORT_SIDE + 2.0 * math.sqrt(3.0) * radar.ROAD_REPORT_HALO
    circ, inr = grown / math.sqrt(3.0), grown / (2.0 * math.sqrt(3.0))
    assert radar._report_clear_of(100.0, 100.0, [(200.0, 200.0)], 240) == (100.0, 100.0)
    x, y = radar._report_clear_of(100.0, 100.0, [(100.0, 100.0)], 240)
    assert x == 100.0 and y == pytest.approx(100.0 - r_disc - inr - 1.0)
    assert 100.0 - y >= r_disc + inr               # the base clears the disc
    x, y = radar._report_clear_of(50.0, 12.0, [(50.0, 12.0)], 240)
    assert y == pytest.approx(12.0 + r_disc + circ + 1.0)
    assert y - circ >= 12.0 + r_disc               # the apex clears the disc


def test_area_wide_events_are_listed_not_drawn(cfg, cache, tiles, site):
    """NMDOT's "Difficult Driving Conditions exist throughout the Socorro - 41-57 area." is
    one point for a whole patrol area (live 2026-09-23 it sat in open desert 5 mi from
    US 380): no symbol, no key, not in roads_drawn_ids."""
    sm = radar.SiteMap(cfg, cache, site, "dark", cfg.tile_url_dark)
    out = os.path.join(cfg.out_dir, "area.png")
    plain = _png_bytes(sm, out)
    area = dict(_road("829db998", "water", [-106.661115, 33.959059]), area_wide=True)
    res = sm.render(NO_RADAR, [], out, roads=_roads([area]))
    assert res["ok"] and res["roads_drawn_ids"] == [] and sm._road_key is None
    with open(out, "rb") as f:
        assert f.read() == plain
    res = sm.render(NO_RADAR, [], out, roads=_roads([dict(area, area_wide=False)]))
    assert res["roads_drawn_ids"] == ["829db998"]
