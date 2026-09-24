"""Tests for weather.page: HTML rendering, escaping, units, atomic writes, status.json.

The ``run`` dict is built by hand (no network): two sites, one with a polygon Flash Flood
Warning AT it and a zone-based Flood Watch NEAR it (both drawn on its map), one with nothing
and every source down. The fixtures use the current run-dict keys (sun ``phase`` /
``upcoming``, alert ``threat`` / ``nws_headline``, site ``alerts_drawn_ids`` and ``tz``);
tests further down drop them again to prove older run dicts still render.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest

from weather import page, util

UTC = timezone.utc
GEN = datetime(2026, 9, 23, 16, 12, 0, tzinfo=UTC)          # 10:12 MDT / 11:12 CDT

# A headline with the two characters a page must escape correctly.
FFW_HEADLINE = ("Flash Flood Warning issued <script>alert(1)</script> for Socorro & Catron "
                "Counties until 6:00PM MDT")
FFW_ID = "urn:oid:2.49.0.1.840.0.ffw-1"
FA_ID = "urn:oid:2.49.0.1.840.0.fa-2"


def _status(ok=True, stale=False, error=None, age_s=0.0):
    return {"ok": ok, "stale": stale, "error": error, "age_s": age_s}


def _alert(**over):
    a = {
        "id": FFW_ID, "event": "Flash Flood Warning", "severity": "Severe",
        "urgency": "Immediate", "certainty": "Likely", "kind": "warning", "rank": 1,
        "color": "#8B0000", "headline": FFW_HEADLINE,
        "description": ("At 345 PM MDT, Doppler radar indicated thunderstorms producing heavy rain.\n\n"
                        "HAZARD...Flash flooding caused by thunderstorms."),
        "instruction": "Turn around, don't drown when encountering flooded roads.",
        "area_desc": "Socorro, NM; Catron, NM", "sender": "NWS Albuquerque NM",
        "web": "https://alerts.weather.gov/search?id=ffw-1", "message_type": "Alert",
        "status": "Actual", "sent": "2026-09-23T15:45:00+00:00", "effective": "2026-09-23T15:45:00+00:00",
        "onset": "2026-09-23T15:45:00+00:00", "ends": "2026-09-24T00:00:00+00:00",
        "expires": "2026-09-24T00:00:00+00:00", "end_dt": datetime(2026, 9, 24, 0, 0, tzinfo=UTC),
        "threat": "CONSIDERABLE FLASH FLOODING", "nws_headline": None,
        "affected_zone_urls": ["https://api.weather.gov/zones/county/NMC053"],
        "affected_zone_ids": ["NMC053", "NMC003"],
        "geometry": {"type": "Polygon", "coordinates": [[[-107.2, 33.8], [-106.5, 33.8], [-106.5, 34.3],
                                                         [-107.2, 34.3], [-107.2, 33.8]]]},
        "geometry_source": "polygon", "bbox": (-107.2, 33.8, -106.5, 34.3),
    }
    a.update(over)
    return a


FFW = _alert()
# Issued ahead of time: onset 18:00Z = 12:00 MDT, after GEN (10:12 MDT).
FLOOD_WATCH = _alert(
    id=FA_ID, event="Flood Watch", severity="Moderate", urgency="Future",
    kind="watch", rank=12, color="#E53935",
    headline="Flood Watch issued September 23 at 9:40AM MDT until September 23 at 10:00PM MDT",
    nws_headline="FLOOD WATCH IN EFFECT FROM NOON TODAY THROUGH THIS EVENING",
    description="Excessive rainfall may lead to flooding.", instruction=None,
    area_desc="Sierra County; Northern Sierra Foothills", sender="NWS Albuquerque NM",
    web="http://insecure.example/not-https",             # must NOT be emitted
    onset="2026-09-23T18:00:00+00:00", threat=None,
    affected_zone_ids=["NMZ226"], geometry_source="zones", bbox=(-108.0, 32.8, -106.9, 33.9),
    end_dt=datetime(2026, 9, 24, 4, 0, tzinfo=UTC),
)


def _meta(tz, grid_id, office, gx, gy, ok=True, zones=(None, None, None)):
    d = _status(ok=ok, error=None if ok else "HTTP 503")
    d.update({"grid_id": grid_id if ok else None, "grid_x": gx, "grid_y": gy, "office": office,
              "forecast_url": None, "hourly_url": None, "stations_url": None, "tz": tz,
              "city": None, "state": None, "radar_station": None, "forecast_zone": zones[0],
              "county_zone": zones[1], "fire_zone": zones[2], "zone_urls": []})
    return d


def _obs_ok():
    d = _status()
    d.update({
        "station_id": "KONM", "station_name": "Socorro Municipal Airport",
        "timestamp": "2026-09-23T15:55:00+00:00", "age_min": 17.0, "text": "Mostly Cloudy",
        "icon": "https://api.weather.gov/icons/land/day/bkn?size=medium",
        "temp_f": 72.0, "temp_c": 22.2, "dewpoint_f": 55.4, "dewpoint_c": 13.0, "rh": 55.0,
        "wind_mph": 12.4, "wind_kmh": 20.0, "gust_mph": 21.1, "gust_kmh": 34.0,
        "wind_dir_deg": 140.0, "wind_dir": "SE", "pressure_inhg": 29.92, "pressure_hpa": 1013.2,
        "visibility_mi": 10.0, "visibility_km": 16.1, "heat_index_f": None, "wind_chill_f": None,
        "cloud_layers": ["SCT 4500 ft", "BKN 9000 ft"],
    })
    return d


def _forecast_ok():
    d = _status()
    d.update({"updated": "2026-09-23T14:30:00+00:00", "periods": [
        {"number": 1, "name": "Today", "start": "2026-09-23T06:00:00-06:00", "end": "2026-09-23T18:00:00-06:00",
         "is_day": True, "temp_f": 81, "temp_c": 27.2, "temp_trend": None, "pop": 50,
         "wind": "10 to 15 mph", "wind_dir": "SE", "short": "Chance Showers And Thunderstorms",
         "detailed": "A chance of showers and thunderstorms after noon. Mostly cloudy, with a high near 81.",
         "icon": "https://api.weather.gov/icons/land/day/tsra_hi,50?size=medium"},
        {"number": 2, "name": "Tonight", "start": "2026-09-23T18:00:00-06:00", "end": "2026-09-24T06:00:00-06:00",
         "is_day": False, "temp_f": 58, "temp_c": 14.4, "temp_trend": None, "pop": None,
         "wind": "5 mph", "wind_dir": "S", "short": "Partly Cloudy",
         "detailed": "Partly cloudy, with a low around 58.",
         "icon": "http://api.weather.gov/icons/land/night/sct?size=medium"},     # not https
    ]})
    return d


def _hourly_ok():
    d = _status()
    hours = []
    for i, (pop, is_day, short) in enumerate([(10, True, "Mostly Cloudy"), (60, True, "Showers And Thunderstorms"),
                                              (None, False, "Partly Cloudy")]):
        t = GEN.replace(minute=0) + timedelta(hours=i)
        loc = util.to_local(t, "America/Denver")
        hours.append({"start": t.isoformat(), "local": loc.strftime("%a %H:%M"), "day": loc.strftime("%a"),
                      "hour": loc.strftime("%H:%M"), "is_day": is_day, "temp_f": 70 + i, "temp_c": 21.1 + i,
                      "pop": pop, "rh": 50 + i, "dewpoint_f": 54.0, "dewpoint_c": 12.2,
                      "wind_mph": 10.0 + i, "wind_kmh": 16.1 + i, "wind_dir": "SE", "short": short,
                      "icon": None})
    d.update({"updated": "2026-09-23T14:30:00+00:00", "hours": hours})
    return d


LABELS = {"astro_dawn": "Astro dawn", "nautical_dawn": "Nautical dawn", "civil_dawn": "Civil dawn",
          "sunrise": "Sunrise", "solar_noon": "Solar noon", "sunset": "Sunset",
          "civil_dusk": "Civil dusk", "nautical_dusk": "Nautical dusk", "astro_dusk": "Astro dusk"}


def _upcoming(now, *days):
    """sun_summary's ``upcoming`` rule: events in [now - 30 min, now + 24 h], in order."""
    items = []
    for d in days:
        for key, label in LABELS.items():
            dt = d[key]
            if now - timedelta(minutes=30) <= dt <= now + timedelta(hours=24):
                items.append({"label": label, "dt": dt, "passed": dt < now})
    return sorted(items, key=lambda x: x["dt"])


def _sun(now=GEN, alt=34.2, phase="day"):
    tz = "America/Denver"
    z = util.tzinfo_for(tz)

    def lt(day, h, m, s=0):
        return datetime(2026, 9, day, h, m, s, tzinfo=z)

    # sunset 19:05:44 must DISPLAY as 19:06 (rounded, not truncated)
    today = {"sunrise": lt(23, 6, 55), "solar_noon": lt(23, 13, 1), "sunset": lt(23, 19, 5, 44),
             "civil_dusk": lt(23, 19, 31), "civil_dawn": lt(23, 6, 30), "nautical_dawn": lt(23, 6, 1),
             "nautical_dusk": lt(23, 20, 0), "astro_dawn": lt(23, 5, 32), "astro_dusk": lt(23, 20, 29)}
    tomorrow = {k: v + timedelta(days=1, minutes=-1) for k, v in today.items()}
    yesterday = {k: v - timedelta(days=1, minutes=-1) for k, v in today.items()}
    return {"tz": tz, "now_local": util.to_local(now, tz), "sun_alt_deg": alt,
            "is_night": alt < -6, "phase": phase, "today": today, "tomorrow": tomorrow,
            "upcoming": _upcoming(now, yesterday, today, tomorrow),
            "evening": [("Sunset", today["sunset"])], "morning": [("Sunrise", tomorrow["sunrise"])]}


def make_run(cfg, write_dark_map=True):
    """Two sites: socorro (everything ok, FFW at, Flood Watch near, both drawn, dark map ok,
    light map failing) and lubbock (nothing at all, every source down, metadata fallback
    tz UTC, both maps 'ok' but absent)."""
    socorro = {"slug": "socorro", "name": "Socorro, NM", "lat": 34.0584, "lon": -106.8914,
               "tz": "America/Denver"}
    lubbock = {"slug": "lubbock", "name": "Lubbock, TX", "lat": 33.5779, "lon": -101.8552,
               "tz": "America/Chicago"}
    if write_dark_map:
        os.makedirs(cfg.out_dir, exist_ok=True)
        with open(os.path.join(cfg.out_dir, cfg.map_basename("socorro", "dark")), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
    down = _status(ok=False, stale=True, error="HTTP 503 for https://api.weather.gov/x", age_s=9000.0)
    return {
        "generated": GEN, "generated_ts": time.time(), "night_default": False,
        "alerts": dict(_status(), fetched_ts=time.time(), alerts=[FFW, FLOOD_WATCH], count_raw=7),
        "radar": dict(_status(age_s=240.0), ts=GEN - timedelta(minutes=4)),
        "errors": [],
        "sites": [
            {"site": socorro,
             "meta": _meta("America/Denver", "ABQ", "ABQ", 86, 76, zones=("NMZ220", "NMC053", "NMZ106")),
             "obs": _obs_ok(), "forecast": _forecast_ok(), "hourly": _hourly_ok(), "sun": _sun(),
             "alerts_at": [FFW], "alerts_near": [FLOOD_WATCH], "alerts_drawn_ids": sorted([FFW_ID, FA_ID]),
             "maps": {"dark": {"basename": cfg.map_basename("socorro", "dark"), "ok": True, "error": None},
                      "light": {"basename": cfg.map_basename("socorro", "light"), "ok": False,
                                "error": "tiles unavailable: HTTP 503"}},
             "map_notes": ["basemap partial (2 tiles missing)"]},
            {"site": lubbock, "meta": _meta("UTC", "LUB", "LUB", 49, 33, ok=False),
             "obs": dict(down), "forecast": dict(down), "hourly": dict(down), "sun": None,
             "alerts_at": [], "alerts_near": [], "alerts_drawn_ids": [],
             "maps": {"dark": {"basename": cfg.map_basename("lubbock", "dark"), "ok": True, "error": None},
                      "light": {"basename": cfg.map_basename("lubbock", "light"), "ok": True, "error": None}},
             "map_notes": []},
        ],
    }


@pytest.fixture
def run(cfg):
    return make_run(cfg)


class _Counter(HTMLParser):
    """Counts open/close tags so we can assert the page is well formed enough."""

    def __init__(self):
        super().__init__()
        self.opened = {}
        self.closed = {}

    def handle_starttag(self, tag, attrs):
        self.opened[tag] = self.opened.get(tag, 0) + 1

    def handle_endtag(self, tag):
        self.closed[tag] = self.closed.get(tag, 0) + 1


class _Imgs(HTMLParser):
    """Collects the attributes of every radar <img>."""

    def __init__(self):
        super().__init__()
        self.imgs = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "img" and "radar-img" in (d.get("class") or ""):
            self.imgs.append(d)


def _radar_imgs(out):
    p = _Imgs()
    p.feed(out)
    return {(d["class"].split("radar-")[-1], d["alt"].split(" around ")[1].split(" (")[0]): d for d in p.imgs}


def _card(out, alert_id_fragment):
    """The <article> whose details key mentions the alert id."""
    i = out.index(alert_id_fragment)
    start = out.rindex('<article class="acard"', 0, i)
    return out[start:out.index("</article>", start)]


def _banner(out):
    return out[out.index('class="banner"'):out.index('<section')]


# ---- tests ---------------------------------------------------------------------------
def test_structure_and_anchors(cfg, run):
    out = page.render_html(cfg, run)
    assert out.startswith("<!DOCTYPE html>")
    assert out.count("<html") == 1 and out.count("</html>") == 1
    assert out.count("<section") == out.count("</section>") == 2
    p = _Counter()
    p.feed(out)
    for tag in ("div", "section", "table", "details", "article", "figure", "nav", "h2", "a", "span",
                "noscript"):
        assert p.opened.get(tag, 0) == p.closed.get(tag, 0), tag
    assert 'name="viewport"' in out and "<title>" in out
    for slug, name in (("socorro", "Socorro, NM"), ("lubbock", "Lubbock, TX")):
        assert 'id="site-%s"' % slug in out
        assert 'href="#site-%s"' % slug in out
        assert name in out
    # masthead: UTC and the first site's local time
    assert "16:12 UTC" in out and "10:12 MDT" in out
    # sticky nav + day/night machinery from the template
    assert 'class="nav"' in out and 'id="modebtn"' in out and "sessionStorage" in out
    assert "NWS ABQ 86,76" in out and "NWS metadata unavailable" in out


def test_refresh_is_js_with_noscript_fallback(cfg, run):
    """Regression: a bare <meta refresh> replaced the document every 5 min, collapsing every
    open <details> and losing the scroll position. Now PAGE_JS reloads (restoring both) and
    the meta refresh only remains inside <noscript>."""
    out = page.render_html(cfg, run)
    meta = '<meta http-equiv="refresh" content="%d">' % cfg.refresh_seconds
    assert out.count('http-equiv="refresh"') == 1
    assert "<noscript>%s</noscript>" % meta in out
    assert 'data-refresh="%d"' % cfg.refresh_seconds in out
    assert "location.reload()" in page.PAGE_JS and "details[data-k]" in page.PAGE_JS
    # stable keys: "<slug>:<alert id>" and "<slug>:p<period number>"
    assert '<details data-k="socorro:%s">' % FFW_ID in out
    assert '<details data-k="socorro:p1">' in out and '<details data-k="socorro:p2">' in out


def test_headline_is_escaped_exactly(cfg, run):
    out = page.render_html(cfg, run)
    assert "<script>alert" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out
    assert "Socorro &amp; Catron Counties" in out
    assert "Flash Flood Warning" in out
    # description / instruction land in the collapsible details, escaped
    assert "Turn around, don&#x27;t drown" in out
    # https web link is emitted, the http one is not
    assert 'href="https://alerts.weather.gov/search?id=ffw-1"' in out
    assert "insecure.example" not in out


def test_legend_lists_drawn_and_at_alerts(cfg, run):
    out = page.render_html(cfg, run)
    legend = out[out.index('<div class="legend">'):]
    legend = legend[:legend.index("</div>")]
    assert legend.startswith('<div class="legend"><span>Shaded:</span>')
    assert 'style="background:#8B0000"' in legend and "Flash Flood Warning" in legend
    assert 'style="background:#E53935"' in legend and "Flood Watch" in legend
    assert "Listed, not shaded" not in out
    # the alert-free site says so
    assert "No alert areas drawn." in out
    # alert cards carry the colour on the border and the AT/NEAR chips
    assert 'class="acard" style="border-left-color:#8B0000"' in out
    assert 'class="acard" style="border-left-color:#E53935"' in out
    assert "AT THIS SITE" in out and "NEARBY" in out
    # "until" in the site's zone (00:00Z = 18:00 MDT)
    assert "until Wed Sep 23 18:00 MDT" in out


def test_legend_separates_undrawn_alerts(cfg, run):
    """Regression: every AT alert was listed under 'Shaded:' even when its outline could not
    be fetched (the caption said 'not shaded' one line above) or its mask missed the map."""
    soc = run["sites"][0]
    undrawn = _alert(id="urn:x-red-flag", event="Red Flag Warning", color="#FF1493", rank=3,
                     geometry=None, geometry_source=None, bbox=None, threat=None)
    soc["alerts_at"] = [FFW, undrawn]
    soc["alerts_drawn_ids"] = [FFW_ID, FA_ID]
    out = page.render_html(cfg, run)
    fig = out[out.index('<figure class="mapbox">'):out.index("</figure>")]
    shaded = fig[fig.index("<span>Shaded:</span>"):]
    shaded = shaded[:shaded.index("</div>")]
    assert "Flash Flood Warning" in shaded and "Flood Watch" in shaded and "Red Flag" not in shaded
    listed = fig[fig.index("<span>Listed, not shaded:</span>"):]
    listed = listed[:listed.index("</div>")]
    assert "Red Flag Warning" in listed and 'background:#FF1493' in listed and "Flood" not in listed

    # nothing drawn at all although the map rendered
    soc["alerts_drawn_ids"] = []
    out = page.render_html(cfg, run)
    fig = out[out.index('<figure class="mapbox">'):out.index("</figure>")]
    assert "Shaded:" not in fig and "No alert areas drawn." in fig and "Listed, not shaded:" in fig

    # no theme rendered: say so instead of claiming anything is shaded
    for m in soc["maps"].values():
        m["ok"] = False
    out = page.render_html(cfg, run)
    fig = out[out.index('<figure class="mapbox">'):out.index("</figure>")]
    assert "No map rendered" in fig and "Shaded:" not in fig

    # an old run dict without alerts_drawn_ids: NEAR and AT-with-geometry count as drawn
    soc["maps"]["dark"]["ok"] = True
    del soc["alerts_drawn_ids"]
    out = page.render_html(cfg, run)
    fig = out[out.index('<figure class="mapbox">'):out.index("</figure>")]
    assert "Red Flag Warning" in fig[fig.index("Listed, not shaded:"):]
    assert "Flash Flood Warning" in fig[fig.index("Shaded:"):fig.index("Listed, not shaded:")]


def test_units_switch(cfg, run):
    cfg.units = "us"
    out = page.render_html(cfg, run)
    assert '72°F <span class="alt">22°C</span>' in out
    assert 'SE 12 mph <span class="alt">20 km/h</span>' in out
    assert '29.92 inHg <span class="alt">1013 hPa</span>' in out
    assert "10 to 15 mph" in out
    cfg.units = "metric"
    out = page.render_html(cfg, run)
    assert '22°C <span class="alt">72°F</span>' in out
    assert 'SE 20 km/h <span class="alt">12 mph</span>' in out
    assert '1013 hPa <span class="alt">29.92 inHg</span>' in out
    assert '16 to 24 km/h <span class="alt">10 to 15 mph</span>' in out


def test_nav_badge_count_and_colour(cfg, run):
    out = page.render_html(cfg, run)
    nav = out[out.index('<nav'):out.index('</nav>')]
    soc = nav[nav.index('href="#site-socorro"'):nav.index('href="#site-lubbock"')]
    lub = nav[nav.index('href="#site-lubbock"'):]
    assert 'class="badge" style="background:#8B0000;color:#ffffff"' in soc and ">1</span>" in soc
    assert "badge" not in lub
    # banner: one box per distinct alert, linking to the site, counting both
    banner = _banner(out)
    assert "2 active" in banner
    assert 'border-left-color:#8B0000"><b>Flash Flood Warning</b>' in banner
    assert 'at <a href="#site-socorro">Socorro, NM</a>' in banner
    assert 'near <a href="#site-socorro">Socorro, NM</a>' in banner
    assert "No NWS alerts for these sites" not in banner


def test_banner_names_office_and_area(cfg, run):
    """Regression: same-named alerts from different offices / areas were indistinguishable
    in the banner. Each box now names the office (without 'NWS ') and the area, cut at a
    '; ' boundary to ~90 characters with the full text in title=."""
    long_area = "; ".join("Zone Number %02d Of The Watch Area" % i for i in range(1, 9))
    other = _alert(id="urn:x-fa-3", event="Flood Watch", color="#E53935", rank=12, threat=None,
                   sender="NWS El Paso Tx/Santa Teresa NM", area_desc=long_area)
    run["sites"][1]["alerts_near"] = [other]
    out = page.render_html(cfg, run)
    banner = _banner(out)
    boxes = [b for b in banner.split('<div class="abox"')[1:] if "<b>Flood Watch</b>" in b]
    assert len(boxes) == 2
    abq = [b for b in boxes if "Sierra County" in b][0]
    elp = [b for b in boxes if "El Paso" in b][0]
    assert '<span class="src">Albuquerque NM · <span>Sierra County; Northern Sierra Foothills</span></span>' in abq
    assert "NWS Albuquerque" not in abq
    assert 'El Paso Tx/Santa Teresa NM · <span title="%s">' % long_area in elp
    shown = elp[elp.index('title="'):]
    shown = shown[shown.index(">") + 1:shown.index("</span>")]
    assert shown.endswith("…") and len(shown) <= 91
    assert shown[:-1] == "; ".join(long_area.split("; ")[:len(shown[:-1].split("; "))])


def test_threat_chip_and_nws_headline(cfg, run):
    out = page.render_html(cfg, run)
    chip = '<span class="chip threat">CONSIDERABLE FLASH FLOODING</span>'
    assert chip in _card(out, 'data-k="socorro:%s"' % FFW_ID)
    assert chip in _banner(out)
    assert out.count('class="chip threat"') == 2               # the watch has no threat
    assert ".chip.threat" in page.PAGE_CSS
    # NWSheadline as a subtitle below the (kept) headline
    fa = _card(out, 'data-k="socorro:%s"' % FA_ID)
    assert "Flood Watch issued September 23" in fa
    assert '<div class="nh">FLOOD WATCH IN EFFECT FROM NOON TODAY THROUGH THIS EVENING</div>' in fa
    # a headline-less threat value from a garbage record is escaped
    run["sites"][0]["alerts_at"] = [_alert(threat="<b>PDS</b>")]
    out = page.render_html(cfg, run)
    assert "&lt;b&gt;PDS&lt;/b&gt;" in out and "<b>PDS</b>" not in out


def test_onset_in_future_is_shown(cfg, run):
    """Regression: an alert issued ahead of time read 'until …' only, as if in effect now."""
    out = page.render_html(cfg, run)
    fa = out[out.index('class="acard" style="border-left-color:#E53935"'):]
    fa = fa[:fa.index("</article>")]
    assert "from Wed Sep 23 12:00 MDT until Wed Sep 23 22:00 MDT" in fa
    assert "from Wed Sep 23 12:00 MDT until Wed Sep 23 22:00 MDT" in _banner(out)
    ffw = _card(out, 'data-k="socorro:%s"' % FFW_ID)
    assert "until Wed Sep 23 18:00 MDT" in ffw and "from " not in ffw.split('<div class="m">')[1][:6]
    # future onset, no end
    run["sites"][0]["alerts_near"] = [dict(FLOOD_WATCH, end_dt=None, ends=None, expires=None)]
    out = page.render_html(cfg, run)
    assert "from Wed Sep 23 12:00 MDT (no end time given)" in out


def test_alert_links_to_nws_hazard_page(cfg, run):
    """Regression: the only link was ``web``, which api.weather.gov fills with the bare
    http://www.weather.gov, so real alerts had no link at all. AT alerts link the site's
    zone triple; NEAR alerts substitute their own zone; no zones -> point forecast."""
    out = page.render_html(cfg, run)

    def links(card):
        return [x.split('"')[0].replace("&amp;", "&") for x in card.split('href="')[1:]]

    ffw = [u for u in links(_card(out, 'data-k="socorro:%s"' % FFW_ID)) if "showsigwx" in u]
    assert len(ffw) == 1
    u = urlsplit(ffw[0])
    assert (u.scheme, u.netloc, u.path) == ("https", "forecast.weather.gov", "/showsigwx.php")
    assert parse_qs(u.query) == {"warnzone": ["NMZ220"], "warncounty": ["NMC053"], "firewxzone": ["NMZ106"],
                                 "local_place1": ["Socorro, NM"], "product1": ["Flash Flood Warning"]}
    assert "product1=Flash+Flood+Warning" in ffw[0]
    fa_card = out[out.index('class="acard" style="border-left-color:#E53935"'):]
    fa_card = fa_card[:fa_card.index("</article>")]
    fa = [u for u in links(fa_card) if "showsigwx" in u]
    assert parse_qs(urlsplit(fa[0]).query)["warnzone"] == ["NMZ226"]      # the watch's own zone
    assert parse_qs(urlsplit(fa[0]).query)["warncounty"] == ["NMC053"]
    assert "Full text on weather.gov" in fa_card
    # the bare weather.gov root that the API sends in "web" is not worth a second link
    run["sites"][0]["alerts_at"] = [_alert(web="https://www.weather.gov")]
    out = page.render_html(cfg, run)
    assert "NWS alert page" not in _card(out, 'data-k="socorro:%s"' % FFW_ID)
    # without metadata zones (and no county id of the alert's own) -> point forecast page
    run["sites"][0]["meta"] = _meta("UTC", None, None, None, None, ok=False)
    run["sites"][0]["alerts_at"] = [_alert(affected_zone_ids=["NMZ220"])]
    out = page.render_html(cfg, run)
    card = _card(out, 'data-k="socorro:%s"' % FFW_ID)
    assert "showsigwx" not in card
    assert 'href="https://forecast.weather.gov/MapClick.php?lat=34.0584&amp;lon=-106.8914"' in card
    assert "NWS forecast for this site" in card
    # malformed zone ids never reach a URL: without a valid forecast zone -> point forecast
    run["sites"][0]["meta"] = _meta("America/Denver", "ABQ", "ABQ", 86, 76,
                                    zones=('NMZ220"><script>', "NMC053", "NMZ106"))
    run["sites"][0]["alerts_at"] = [_alert(affected_zone_ids=['NMZ1"y', "NMC053"])]
    out = page.render_html(cfg, run)
    card = _card(out, 'data-k="socorro:%s"' % FFW_ID)
    assert "showsigwx" not in card and "MapClick.php" in card and "NMZ1" not in card and "NMZ220" not in card


def test_site_heading_links_point_forecast_and_uses_static_tz(cfg, run):
    out = page.render_html(cfg, run)
    soc = out[out.index('<section class="site" id="site-socorro">'):]
    head = soc[:soc.index("</h2>")]
    assert ('<a class="sitelink" href="https://forecast.weather.gov/MapClick.php?lat=34.0584&amp;lon=-106.8914"'
            in head and "NWS point forecast" in head)
    # lubbock's metadata failed (fallback tz "UTC"): the site's static tz is used instead
    lub = out[out.index('<section class="site" id="site-lubbock">'):]
    assert "local time Wed Sep 23 11:12 CDT" in lub[:lub.index("</h2>")]


def test_no_alerts_and_feed_warning(cfg, run):
    for s in run["sites"]:
        s["alerts_at"], s["alerts_near"] = [], []
    out = page.render_html(cfg, run)
    assert "No NWS alerts for these sites" in out
    assert 'class="feedwarn"' not in out
    run["alerts"] = _status(ok=True, stale=True, error="HTTP 500 for /alerts/active", age_s=600.0)
    out = page.render_html(cfg, run)
    assert 'class="feedwarn"' in out and "HTTP 500 for /alerts/active" in out and "10 min ago" in out
    run["alerts"] = _status(ok=False, stale=True, error="timed out", age_s=None)
    out = page.render_html(cfg, run)
    assert "NWS alert feed unavailable: timed out" in out


def test_unavailable_sources_render_text(cfg, run):
    out = page.render_html(cfg, run)
    lub = out[out.index('id="site-lubbock"'):]
    assert "Current conditions unavailable" in lub
    assert "Hourly forecast unavailable" in lub
    assert "7-day forecast unavailable" in lub
    assert "HTTP 503 for https://api.weather.gov/x" in lub
    # the failing light map of socorro is a muted box with its error, in the theme class
    assert 'class="mapmiss radar-light">Radar map unavailable (light theme): tiles unavailable: HTTP 503' in out
    # footer per-source pills
    assert "Lubbock, TX: obs unavailable · forecast unavailable · hourly unavailable" in out
    assert "Socorro, NM: obs ok · forecast ok · hourly ok" in out
    assert "Radar: frame 16:08Z" in out


def test_missing_values_render_dash(cfg, run):
    obs = run["sites"][0]["obs"]
    obs["temp_f"] = obs["temp_c"] = None
    obs["pressure_inhg"] = obs["pressure_hpa"] = None
    obs["wind_mph"] = obs["wind_kmh"] = None
    out = page.render_html(cfg, run)
    now = out[out.index('<div class="now">'):]
    now = now[:now.index("</div>\n<h2>")]
    assert now.count("—") >= 3
    assert "None" not in now
    # PoP None in the hourly table and the 7-day card
    assert "Precip —" in out


def test_map_cache_busting(cfg, run):
    out = page.render_html(cfg, run)
    imgs = _radar_imgs(out)
    dark = os.path.join(cfg.out_dir, cfg.map_basename("socorro", "dark"))
    # day default: the dark map's URL waits in data-src (with the mtime cache-buster)
    assert imgs[("dark", "Socorro, NM")]["data-src"] == "radar_socorro_dark.png?t=%d" % int(os.path.getmtime(dark))
    # lubbock maps are "ok" but the files do not exist -> fall back to the run timestamp
    assert imgs[("light", "Lubbock, TX")]["src"] == "radar_lubbock_light.png?t=%d" % int(run["generated_ts"])
    assert imgs[("dark", "Lubbock, TX")]["data-src"] == "radar_lubbock_dark.png?t=%d" % int(run["generated_ts"])
    assert 'class="radar-img radar-dark"' in out and 'class="radar-img radar-light"' in out
    assert "MRMS frame 16:08Z · 10:08 MDT" in out
    assert "basemap partial (2 tiles missing)" in out


def test_only_default_theme_map_is_downloaded(cfg, run):
    """Regression: both theme PNGs had a real src; the display:none one is still fetched,
    doubling every page view's transfer."""
    out = page.render_html(cfg, run)
    imgs = _radar_imgs(out)
    for (theme, _), d in imgs.items():
        if theme == "light":
            assert d["src"].startswith("radar_") and "data-src" not in d
        else:
            assert d["src"].startswith("data:image/gif;base64,") and d["data-src"].startswith("radar_")
    run["night_default"] = True
    out = page.render_html(cfg, run)
    assert 'data-night-default="1"' in out and 'class="page night"' in out
    for (theme, _), d in _radar_imgs(out).items():
        if theme == "dark":
            assert d["src"].startswith("radar_") and "data-src" not in d
        else:
            assert d["src"].startswith("data:") and d["data-src"].startswith("radar_")
    # the placeholder is a real 1x1 GIF
    import base64
    raw = base64.b64decode(page._PLACEHOLDER_SRC.split(",", 1)[1])
    assert raw[:6] == b"GIF89a" and raw[6:10] == b"\x01\x00\x01\x00"


def test_phone_nav_and_scroll_margin_css(cfg, run):
    """Regression: on phones the sticky nav wrapped to four rows (165 px) while sections had
    a fixed 58 px scroll-margin, so anchor jumps hid the site heading under the nav."""
    css = page.PAGE_CSS
    phone = css[css.index("@media (max-width: 600px) {\n    .nav"):]
    phone = phone[:phone.index("\n  }\n")]
    assert "flex-wrap: nowrap" in phone and "overflow-x: auto" in phone and "flex: none" in phone
    assert "scroll-margin-top: calc(var(--navh, 58px) + 8px)" in css
    assert "scroll-margin-top: 58px" not in css
    assert "setProperty('--navh'" in page.PAGE_JS


def test_hourly_pop_highlight_and_night_rows(cfg, run):
    cfg.pop_highlight_pct = 30.0
    out = page.render_html(cfg, run)
    table = out[out.index('<table class="hr">'):out.index("</table>")]
    assert table.count('class="hi"') == 1 and '<td class="hi">60%</td>' in table
    assert '<tr class="n">' in table and '<tr class="d">' in table
    assert "Showers And Thunderstorms" in table
    cfg.hourly_hours = 2
    out = page.render_html(cfg, run)
    table = out[out.index('<table class="hr">'):out.index("</table>")]
    assert table.count("<tr") == 3            # header + 2 rows


def test_forecast_cards_icons_and_sun(cfg, run):
    out = page.render_html(cfg, run)
    assert 'src="https://api.weather.gov/icons/land/day/tsra_hi,50?size=medium"' in out
    assert 'loading="lazy"' in out
    assert "http://api.weather.gov/icons" not in out
    assert '<details data-k="socorro:p1"><summary>Details</summary>' in out
    assert "high near 81" in out
    assert 'class="day n"' in out and 'class="day d"' in out
    # sun / twilight row present for socorro only, times in local tz, zone named once
    assert "Sun &amp; twilight" in out and out.count("Sun &amp; twilight") == 1
    assert 'Sun &amp; twilight <span class="chip">times in MDT</span>' in out
    assert "<b>34.2°</b> (day)" in out
    # upcoming, in order; same-day events without weekday, tomorrow's with it
    sun = out[out.index("Sun &amp; twilight"):out.index("<h2>Next")]
    assert "Civil dusk <b>19:31</b>" in sun and "Sunrise <b>Thu 06:54</b>" in sun
    order = [sun.index(x) for x in ("Solar noon <b>13:01", "Sunset <b>19:06", "Astro dusk <b>20:29",
                                    "Astro dawn <b>Thu 05:31", "Sunrise <b>Thu 06:54")]
    assert order == sorted(order)
    assert "This evening" not in sun and "Next morning" not in sun


def test_sun_phase_rounding_and_passed_events(cfg, run):
    """Regressions: 'Sun now -2.5° (day)' just after sunset (label came from is_night, the
    -6° palette switch); times truncated instead of rounded (19:05:44 read 19:05); between
    midnight and sunrise the imminent dawn was missing (now the 'upcoming' list)."""
    soc = run["sites"][0]
    # 19:15 MDT: 9 min after sunset, alt -2.5, civil twilight
    now = datetime(2026, 9, 24, 1, 15, tzinfo=UTC)
    run["generated"] = now
    soc["sun"] = _sun(now=now, alt=-2.5, phase="civil twilight")
    out = page.render_html(cfg, run)
    sun = out[out.index("Sun &amp; twilight"):out.index("<h2>Next")]
    assert "<b>-2.5°</b> (civil twilight)" in sun and "(day)" not in sun
    assert "Sunset <b>19:06</b>" in sun                            # 19:05:44 rounds up
    assert '<span class="past" title="passed">Sunset <b>19:06</b></span>' in sun
    assert '<span>Civil dusk <b>19:31</b></span>' in sun             # not passed yet
    # an old run dict without "phase" derives it from the altitude, never from is_night
    del soc["sun"]["phase"]
    for alt, want in ((10.0, "day"), (-0.5, "day"), (-2.5, "civil twilight"), (-7.0, "nautical twilight"),
                      (-13.0, "astronomical twilight"), (-30.0, "night")):
        soc["sun"]["sun_alt_deg"] = alt
        soc["sun"]["is_night"] = alt < -6
        assert "(%s)</span>" % want in page._sun_html(soc, "America/Denver", now)
    # 03:00 MDT: the dawn sequence of *today* comes first, dusk later (today's date, no weekday)
    now = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
    soc["sun"] = _sun(now=now, alt=-40.0, phase="night")
    h = page._sun_html(soc, "America/Denver", now)
    assert "(night)" in h
    assert h.index("Astro dawn <b>05:32") < h.index("Sunrise <b>06:55") < h.index("Sunset <b>19:06")
    # rounding across midnight shows the new day's weekday
    z = util.tzinfo_for("America/Denver")
    soc["sun"]["upcoming"] = [{"label": "Astro dusk", "dt": datetime(2026, 9, 23, 23, 59, 40, tzinfo=z),
                               "passed": False},
                              {"label": "Civil dawn", "dt": datetime(2026, 9, 23, 6, 30, 29, tzinfo=z),
                               "passed": False}]
    h = page._sun_html(soc, "America/Denver", now)
    assert "Astro dusk <b>Thu 00:00</b>" in h and "Civil dawn <b>06:30</b>" in h


def test_sun_tolerates_old_and_partial_dicts(cfg, run):
    soc = run["sites"][0]
    for k in ("phase", "upcoming"):
        del soc["sun"][k]
    out = page.render_html(cfg, run)
    assert "<b>34.2°</b> (day)" in out and "Sunrise" in out and 'class="errbox"' not in out
    soc["sun"] = {"sun_alt_deg": None, "upcoming": [None, {"label": "x"}, {"dt": "bad"}]}
    out = page.render_html(cfg, run)
    assert "Sun now" in out and 'class="errbox"' not in out


def test_bad_colour_and_url_fall_back(cfg, run):
    bad = _alert(id="x-3", color="javascript:alert(1)", web="javascript:alert(1)",
                 icon="javascript:alert(1)")
    run["sites"][0]["alerts_at"] = [bad]
    out = page.render_html(cfg, run)
    assert "javascript:" not in out
    assert 'style="border-left-color:#808080"' in out
    assert 'style="background:#808080;color:#ffffff"' in out           # nav badge


def test_attribution_is_linked(cfg, run):
    """Regression: the credit line was escaped config text with no link to the OSM
    copyright page (the licence asks for one)."""
    out = page.render_html(cfg, run)
    foot = out[out.index('<p class="foot">'):]
    foot = foot[:foot.index("</p>")]
    assert '<a href="https://www.openstreetmap.org/copyright" rel="noopener">OpenStreetMap</a>' in foot
    assert 'href="https://mesonet.agron.iastate.edu/"' in foot and 'href="https://www.weather.gov/"' in foot
    cfg.attribution = "<b>custom</b>"
    assert "custom" not in page.render_html(cfg, run)


def test_site_render_failure_yields_error_box(cfg, run, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom in now tiles")
    monkeypatch.setattr(page, "_now_html", boom)
    out = page.render_html(cfg, run)
    assert 'class="errbox"' in out and "RuntimeError: boom in now tiles" in out
    # the rest of the site (map, hourly) and the other site still render
    assert 'id="site-socorro"' in out and 'id="site-lubbock"' in out and "MRMS frame" in out
    assert out.count("<section") == out.count("</section>") == 2

    # a whole-section failure keeps the anchor and shows the error
    monkeypatch.setattr(page, "_site_head_html", boom)
    monkeypatch.setattr(page, "_guard", lambda what, fn, *a: fn(*a))
    out = page.render_html(cfg, run)
    assert 'id="site-socorro"' in out and "This site could not be rendered: RuntimeError: boom" in out

    # even a garbage run dict produces a page
    out = page.render_html(cfg, {"sites": [None, "junk", {"site": None}], "generated": "not a date"})
    assert out.startswith("<!DOCTYPE html>") and "</html>" in out


def test_old_run_dict_renders(cfg, run):
    """A run dict from before the threat / nws_headline / alerts_drawn_ids / site tz keys."""
    for s in run["sites"]:
        s.pop("alerts_drawn_ids", None)
        s["site"].pop("tz", None)
        for a in s["alerts_at"] + s["alerts_near"]:
            a.pop("threat", None)
            a.pop("nws_headline", None)
    run["sites"] = [dict(s, alerts_at=[{k: v for k, v in a.items() if k not in ("threat", "nws_headline")}
                                       for a in s["alerts_at"]]) for s in run["sites"]]
    out = page.render_html(cfg, run)
    assert 'class="errbox"' not in out and "Shaded:" in out
    lub = out[out.index('<section class="site" id="site-lubbock">'):]
    assert "local time Wed Sep 23 16:12 UTC" in lub[:lub.index("</h2>")]


def test_write_page_atomic(cfg, run):
    path = page.write_page(cfg, run)
    assert path == os.path.join(cfg.out_dir, "index.html")
    assert os.path.exists(path)
    assert not glob.glob(os.path.join(cfg.out_dir, "*.tmp*"))
    with open(path, encoding="utf-8") as f:
        body = f.read()
    assert body == page.render_html(cfg, run)
    # rewriting replaces in place
    run["night_default"] = True
    page.write_page(cfg, run)
    with open(path, encoding="utf-8") as f:
        assert 'class="page night"' in f.read()
    assert not glob.glob(os.path.join(cfg.out_dir, "*.tmp*"))


def test_atomic_write_fsyncs_before_replace(cfg, run, monkeypatch):
    """Regression: the temp file was renamed into place without fsync, so a crash right
    after a run could leave a zero-length index.html on XFS."""
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append(("fsync", os.fstat(fd).st_size))
        return real_fsync(fd)

    def replace(a, b):
        events.append(("replace", os.path.basename(b)))
        return real_replace(a, b)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    page.write_page(cfg, run)
    page.write_status_json(cfg, run)
    assert [e[0] for e in events] == ["fsync", "replace", "fsync", "replace"]
    assert events[0][1] > 1000 and events[1][1] == "index.html" and events[3][1] == "status.json"


def test_status_json_shape(cfg, run):
    run["errors"] = ["radar: HTTP 503", "lubbock obs: HTTP 503"]
    spath = page.write_status_json(cfg, run)
    with open(spath, encoding="utf-8") as f:
        doc = json.load(f)
    assert set(doc) == {"generated", "page_written", "degraded", "ok", "problems", "alerts", "radar",
                        "network", "roads", "sites", "errors"}
    assert doc["network"] is None                   # an older run dict without the key
    assert doc["roads"] is None                     # ditto: no road data in this run
    assert doc["page_written"] is False and doc["ok"] is False      # index.html not written yet
    assert doc["generated"] == GEN.isoformat()
    assert doc["alerts"] == {"ok": True, "stale": False, "count": 2}
    assert doc["radar"] == {"ok": True, "stale": False, "ts": (GEN - timedelta(minutes=4)).isoformat()}
    assert doc["sites"]["socorro"] == {"meta_ok": True, "obs_ok": True, "forecast_ok": True, "hourly_ok": True,
                                       "alerts_at": 1, "alerts_near": 1, "maps_ok": False,
                                       "roads_events": 0, "roads_reports": 0}
    assert doc["sites"]["lubbock"] == {"meta_ok": False, "obs_ok": False, "forecast_ok": False,
                                       "hourly_ok": False, "alerts_at": 0, "alerts_near": 0, "maps_ok": True,
                                       "roads_events": 0, "roads_reports": 0}
    assert doc["errors"] == run["errors"]
    assert doc["degraded"] is True
    assert doc["problems"] == ["socorro light map: not written", "lubbock metadata: unavailable",
                               "lubbock observation: unavailable", "lubbock forecast: unavailable",
                               "lubbock hourly forecast: unavailable", "2 error(s) logged in this run"]
    # Regression: "ok" used to mean only "index.html is new", so a fully degraded run said ok
    page.write_page(cfg, run)
    with open(page.write_status_json(cfg, run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["page_written"] is True and doc["degraded"] is True and doc["ok"] is False
    assert not glob.glob(os.path.join(cfg.out_dir, "*.tmp*"))
    # a hopeless run dict still yields a valid document
    with open(page.write_status_json(cfg, None), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["sites"] == {} and doc["alerts"]["count"] == 0 and doc["ok"] is False
    assert "alerts feed: missing" in doc["problems"] and "no sites in this run" in doc["problems"]


def test_status_json_ok_for_a_clean_run(cfg, run):
    run["sites"] = run["sites"][:1]
    soc = run["sites"][0]
    soc["maps"]["light"]["ok"] = True
    page.write_page(cfg, run)
    with open(page.write_status_json(cfg, run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["problems"] == [] and doc["degraded"] is False and doc["ok"] is True
    # a stale (last-good) source is a degradation too
    soc["forecast"]["stale"] = True
    with open(page.write_status_json(cfg, run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["problems"] == ["socorro forecast: stale"] and doc["ok"] is False


def test_status_json_names_the_network_cause(cfg, run):
    """run["network"] (http.run_status() after the fetches) turns into problems that say
    WHY the sources are stale: the budget ran out, or a host stopped answering."""
    run["sites"] = run["sites"][:1]
    run["sites"][0]["maps"]["light"]["ok"] = True
    run["network"] = {"budget_s": 240.0, "left_s": 180.5, "exhausted": False, "down_hosts": []}
    page.write_page(cfg, run)
    with open(page.write_status_json(cfg, run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["ok"] is True and doc["problems"] == []
    assert doc["network"] == {"budget_s": 240.0, "left_s": 180.5, "exhausted": False, "down_hosts": []}
    run["network"] = {"budget_s": 240.0, "left_s": -3.0, "exhausted": True,
                      "down_hosts": ["api.weather.gov", "mesonet.agron.iastate.edu"]}
    with open(page.write_status_json(cfg, run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["ok"] is False and doc["degraded"] is True
    assert doc["problems"] == ["network: run budget of 240 s used up",
                               "network: api.weather.gov unreachable",
                               "network: mesonet.agron.iastate.edu unreachable"]
    assert doc["network"]["down_hosts"] == ["api.weather.gov", "mesonet.agron.iastate.edu"]


def test_masthead_title_and_aria_pressed(cfg):
    """Regression: the h1 once rendered "false" and aria-pressed got the title (args swapped)."""
    h = page._masthead_html(cfg, datetime(2026, 9, 23, 17, 0, tzinfo=timezone.utc), "America/Denver", True)
    assert "<h1>%s</h1>" % page._e(cfg.page_title) in h
    assert 'aria-pressed="true"' in h and "Day mode" in h and "11:00 MDT" in h
    h = page._masthead_html(cfg, datetime(2026, 9, 23, 17, 0, tzinfo=timezone.utc), "America/Denver", False)
    assert 'aria-pressed="false"' in h and "Night mode" in h


# ---- PAGE_JS in node with a stub DOM ---------------------------------------------------
_JS_HARNESS = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
function el(attrs, classes) {
  const a = Object.assign({}, attrs), cls = new Set(classes || []);
  const e = {
    attrs: a, listeners: {}, textContent: '', open: false,
    classList: {add: c => cls.add(c), remove: c => cls.delete(c), contains: c => cls.has(c)},
    getAttribute: k => (k in a ? a[k] : null),
    setAttribute: (k, v) => { a[k] = String(v); },
    removeAttribute: k => { delete a[k]; },
    addEventListener: (t, f) => { e.listeners[t] = f; },
  };
  Object.defineProperty(e, 'src', {get: () => a.src, set: v => { a.src = v; }});
  return e;
}
const night = input.base === '1';
const page = el({'data-night-default': input.base, 'data-refresh': String(input.refresh)},
                night ? ['page', 'night'] : ['page']);
const btn = el({});
const imgs = ['dark', 'light'].map(t => {
  const def = (t === 'dark') === night;
  const at = def ? {src: t + '.png'} : {src: 'data:placeholder', 'data-src': t + '.png'};
  const i = el(at);
  i.theme = t;
  return i;
});
const details = (input.keys || []).map(k => el({'data-k': k}));
const rootStyle = {};
const store = Object.assign({}, input.storage || {});
if (input.view) {
  store['weather-view'] = JSON.stringify({open: input.view.open, y: input.view.y,
                                         t: Date.now() - input.view.age_ms});
}
const timers = [], scrolls = [];
let reloaded = false;
const win = {
  pageYOffset: 0, listeners: {},
  addEventListener: (t, f) => { win.listeners[t] = f; },
  scrollTo: (x, y) => { win.pageYOffset = y; scrolls.push(y); },
  location: {reload: () => { reloaded = true; }},
};
const document = {
  getElementById: id => ({page: page, modebtn: btn}[id] || null),
  querySelector: s => (s === '.nav' ? {offsetHeight: 97} : null),
  querySelectorAll: s => {
    if (s === 'details[data-k]') { return details; }
    const m = /^img\.radar-(dark|light)\[data-src\]$/.exec(s);
    if (m) { return imgs.filter(i => i.theme === m[1] && 'data-src' in i.attrs); }
    throw new Error('unexpected selector ' + s);
  },
  documentElement: {style: {setProperty: (k, v) => { rootStyle[k] = v; }}},
};
const sessionStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
vm.runInNewContext(input.js, {window: win, document, sessionStorage,
                              setTimeout: (f, ms) => { timers.push({f, ms}); }});
if (input.click) { btn.listeners.click(); }
if (input.scrollBy) { win.pageYOffset += input.scrollBy; }
if (input.fireLoad && win.listeners.load) { win.listeners.load(); }
if (input.fireTimer) { win.pageYOffset = input.fireTimer.y; details.forEach(d => { d.open = input.fireTimer.open.includes(d.attrs['data-k']); }); timers[0].f(); }
console.log(JSON.stringify({
  night: page.classList.contains('night'), btn: btn.textContent, pressed: btn.attrs['aria-pressed'] || null,
  imgs: imgs.map(i => ({theme: i.theme, src: i.attrs.src, dataSrc: i.attrs['data-src'] || null})),
  store, open: details.filter(d => d.open).map(d => d.attrs['data-k']), scrolls,
  timers: timers.map(t => t.ms), reloaded, navh: rootStyle['--navh'] || null,
  listeners: Object.keys(win.listeners).sort(),
}));
"""


@pytest.fixture
def run_js(tmp_path):
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        pytest.skip("node not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(_JS_HARNESS, encoding="utf-8")

    def go(**inp):
        inp.setdefault("base", "0")
        inp.setdefault("refresh", 300)
        inp["js"] = page.PAGE_JS
        res = subprocess.run([node, str(harness)], input=json.dumps(inp), capture_output=True,
                             text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        return json.loads(res.stdout)
    return go


def _mode(mode, base):
    return {"weather-mode": json.dumps({"mode": mode, "base": base})}


def test_js_default_mode_timer_and_nav_height(run_js):
    r = run_js()
    assert r["night"] is False and r["pressed"] is None          # no stored choice: untouched
    assert {i["theme"]: i["dataSrc"] for i in r["imgs"]} == {"dark": "dark.png", "light": None}
    assert r["timers"] == [300000] and r["navh"] == "97px"
    assert r["listeners"] == ["pagehide", "resize"]


def test_js_toggle_stores_mode_with_base_and_loads_hidden_maps(run_js):
    r = run_js(click=True)
    assert r["night"] is True and r["pressed"] == "true" and "Day mode" in r["btn"]
    assert json.loads(r["store"]["weather-mode"]) == {"mode": "1", "base": "0"}
    dark = [i for i in r["imgs"] if i["theme"] == "dark"][0]
    assert dark == {"theme": "dark", "src": "dark.png", "dataSrc": None}


def test_js_stored_mode_applies_only_while_default_unchanged(run_js):
    """Regression: one click pinned the palette for the life of the tab; the sun-driven
    default never re-applied."""
    r = run_js(base="0", storage=_mode("1", "0"))                # same default: override holds
    assert r["night"] is True and [i["src"] for i in r["imgs"] if i["theme"] == "dark"] == ["dark.png"]
    r = run_js(base="1", storage=_mode("0", "0"))                # default flipped at dusk
    assert r["night"] is True and "weather-mode" not in r["store"]
    r = run_js(base="0", storage={"weather-night": "1", "weather-mode": "garbage"})
    assert r["night"] is False


def test_js_reload_keeps_open_details_and_scroll(run_js):
    keys = ["socorro:%s" % FFW_ID, "socorro:p1", "lubbock:p1"]
    # the timer saves the view and reloads
    r = run_js(keys=keys, fireTimer={"y": 1234, "open": ["socorro:p1"]})
    assert r["reloaded"] is True
    saved = json.loads(r["store"]["weather-view"])
    assert saved["open"] == ["socorro:p1"] and saved["y"] == 1234
    # the next load restores it once (and forgets it)
    view = {"open": ["socorro:%s" % FFW_ID, "lubbock:p1", "gone:p9"], "y": 800, "age_ms": 1000}
    r = run_js(keys=keys, view=view, fireLoad=True)
    assert r["open"] == ["socorro:%s" % FFW_ID, "lubbock:p1"] and r["scrolls"] == [800, 800]
    assert "weather-view" not in r["store"]
    # the viewer scrolled before images finished loading: leave them where they are
    r = run_js(keys=keys, view=view, scrollBy=300, fireLoad=True)
    assert r["scrolls"] == [800]
    # a saved view from long ago (tab reopened later) is ignored
    r = run_js(keys=keys, view=dict(view, age_ms=3600 * 1000))
    assert r["open"] == [] and r["scrolls"] == []


# ---- road closures (roads contract R4) ---------------------------------------------------
# RoadEvents / RoadReports as roads.fetch_nm_roads / fetch_storm_reports / site_roads hand
# them over, trimmed from the live feeds of 2026-09-23 (nmroads.json joined with rss.xml on
# id == guid; the IEM LSR GeoJSON): real ids, titles, narratives and geometry. Times that lie
# after GEN in the real feed are moved before it so the ages are deterministic; the others
# are the real RSS "Update Date" values (America/Denver).
MDT = util.tzinfo_for("America/Denver")
NMROADS = "https://nmroads.com/"
CRASH_ID = "dd4b80e9-93bb-4549-b38d-e545867c920c"
NM94_ID = "b3f0d585-8422-471b-b0e9-de0b36a0f489"
SOCORRO_WATER_ID = "829db998-2f7d-4517-becb-4130ebd67cbc"
NM252_ID = "dfbcf2f3-65a7-4b7b-a3cb-8a9df0997b7a"
LSR_ID = "lsr-202609222339-KABQ-NWUS55-LSRABQ-369"


def _geom_bbox(geom):
    pts = [geom["coordinates"]] if geom["type"] == "Point" else geom["coordinates"]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _event(**over):
    ev = {"id": CRASH_ID, "kind": "closure", "category": "Closure",
          "title": ("Closure, NM 41 northbound and southbound from mile marker 17, 1 mile south of "
                    "McIntosh to mile marker 19, 1 mile north McIntosh."),
          "route": "NM 41", "mm_from": 17.0, "mm_to": 19.0, "direction": "both",
          "cause": ("Emergency services are on the scene and working to clear the crash scene as "
                    "soon as possible."),
          "cause_known": True,
          "updated": GEN - timedelta(minutes=41), "posted": GEN - timedelta(minutes=41),
          "geometry": {"type": "LineString", "coordinates": [
              [-106.05338785571736, 34.84876116483343], [-106.05335713243632, 34.85011966265168],
              [-106.05286267095786, 34.87408966929531]]},
          "source": "NMDOT", "link": NMROADS}
    ev.update(over)
    ev["bbox"] = _geom_bbox(ev["geometry"])
    return ev


# 1. a full closure with a stated emergency cause (crash)
EV_CRASH = _event()
# 2. the NM 94 closure as it reads when only nmroads.json is available (degraded path: the
#    cause is only in the RSS narrative), so NMDOT's own "Roadway closed" with no cause
EV_NOCAUSE = _event(
    id=NM94_ID,
    title=("Closure, NM 94 northbound and southbound from mile marker 14, Ledoux to mile marker 16, "
           "2 miles north of Ledoux.  Roadway  closed."),
    route="NM 94", mm_from=14.0, mm_to=16.0, direction="northbound and southbound",
    cause="", cause_known=False,
    updated=datetime(2026, 9, 22, 16, 59, 16, tzinfo=MDT), posted=datetime(2026, 9, 22, 16, 59, 16, tzinfo=MDT),
    geometry={"type": "LineString", "coordinates": [[-105.35828218780073, 35.919385233773],
                                                    [-105.34979124346326, 35.94440588674385]]})
# 3. water on the road, area-wide ('null-null' road name, a Point, direction 'NA')
EV_WATER = _event(
    id=SOCORRO_WATER_ID, kind="water", category="Difficult Driving Conditions",
    title="Difficult Driving Conditions exist throughout the Socorro - 41-57 area.",
    route=None, mm_from=None, mm_to=None, direction="NA",
    cause="Standing water on roadway. Use extreme caution.",
    updated=GEN - timedelta(hours=2), posted=GEN - timedelta(hours=2),
    geometry={"type": "Point", "coordinates": [-106.66111491697058, 33.959059193616376]})
# 4. water on the road, not updated for days (the Update Date of the NM 107 item, whose
#    "Post Date" came as "Thu Sep 17 07:11:31 MDT 2026")
EV_OLD = _event(
    id=NM252_ID, kind="water", category="Difficult Driving Conditions",
    title="Difficult Driving Conditions, NM 252 northbound and southbound from mile marker 24 to mile marker 25.",
    route="NM 252", mm_from=24.0, mm_to=25.0, direction="both",
    cause="Standing water on roadway. Use extreme caution. Cloudy conditions exist. Roadway flooding.",
    updated=datetime(2026, 9, 17, 7, 11, 31, tzinfo=MDT), posted=datetime(2026, 9, 17, 7, 11, 31, tzinfo=MDT),
    geometry={"type": "LineString", "coordinates": [[-103.90066455818607, 34.675835659384774],
                                                    [-103.90071480185823, 34.689098594264536]]})
# the NWS storm report of the NM 304 closure that NMDOT never posted
REP_304 = {"id": LSR_ID, "kind": "report", "type": "FLASH FLOOD",
           "time": datetime(2026, 9, 22, 23, 19, tzinfo=UTC), "place": "Las Nutrias", "county": "Socorro",
           "remark": "NMDOT reports NM State Highway 304 closed at Mile Marker 11 (Las Nutrias) due to standing water.",
           "lat": 34.48, "lon": -106.77, "geometry": {"type": "Point", "coordinates": [-106.77, 34.48]},
           "bbox": (-106.77, 34.48, -106.77, 34.48), "source": "NWS ABQ"}
FEED_TIME = datetime(2026, 9, 23, 16, 4, tzinfo=UTC)        # nmroads.json Last-Modified, 10:04 MDT
EXCLUDED = {"Roadwork": 49, "Alert": 14, "Lane Closure": 10, "Difficult Driving Conditions": 9,
            "Construction Closure": 2, "Fair Driving Conditions": 1}


def add_roads(run, events=None, reports=None, drawn=None):
    """Road data as make_weather_page adds it: run["roads"] (source statuses, no item lists)
    and a "roads" dict per site. Socorro lists the items; Lubbock is outside New Mexico."""
    events = [EV_CRASH, EV_NOCAUSE, EV_WATER, EV_OLD] if events is None else events
    reports = [REP_304] if reports is None else reports
    drawn = [CRASH_ID, SOCORRO_WATER_ID, LSR_ID] if drawn is None else drawn
    run["roads"] = {
        "nmdot": dict(_status(), count_raw=94, count_region=93, feed_time=FEED_TIME, excluded=dict(EXCLUDED)),
        "lsr": _status(),
    }
    run["sites"][0]["roads"] = {"covers_nm": True, "events": list(events), "reports": list(reports),
                                "drawn_ids": list(drawn)}
    run["sites"][1]["roads"] = {"covers_nm": False, "events": [], "reports": [], "drawn_ids": []}
    return run


@pytest.fixture
def road_run(cfg):
    return add_roads(make_run(cfg))


def _section(out, slug):
    start = out.index('<section class="site" id="site-%s">' % slug)
    return out[start:out.index("</section>", start)]


def _roads_block(out, slug):
    start = out.index('<div class="roads" id="roads-%s">' % slug)
    end = out.index('<div class="rlinks">', start) if "rlinks" in out[start:] else len(out)
    close = out.index("</div>\n</div>\n", end) if end < len(out) else out.index("</div>\n", start)
    return out[start:close]


def _rows(block):
    return ['<li class="road' + x.split("</li>")[0] for x in block.split('<li class="road')[1:]]


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _text(fragment):
    p = _Text()
    p.feed(fragment)
    return " ".join("".join(p.parts).split())


def test_roads_block_lists_events_in_order(cfg, road_run):
    out = page.render_html(cfg, road_run)
    soc = _section(out, "socorro")
    block = _roads_block(out, "socorro")
    # after the alert cards, in the right-hand column, before the sun row
    assert soc.index('class="acard"') < soc.index('id="roads-socorro"') < soc.index("Sun &amp; twilight")
    assert "<h2>Road closures (New Mexico, emergencies only)" in block
    assert '<span class="chip">2 closed · 2 water on road</span>' in block
    rows = _rows(block)
    assert [r.split('"')[1] for r in rows] == ["road closure", "road closure", "road water", "road water", "road report"]
    crash, nocause, water, old, rep = rows
    # 1. cause known: route, mile markers and direction; cause; place names; age + local time
    assert '<span class="chip rclosed"><span class="rsym closed" aria-hidden="true"></span>ROAD CLOSED</span>' in crash
    assert "<b>NM 41</b> · mile 17–19 · both directions" in crash
    assert "clear the crash scene as soon as possible." in crash and "cause not stated" not in crash
    assert "1 mile south of McIntosh" in crash
    assert "NMDOT · updated 41 min ago (09:31 MDT)" in crash
    assert '<span class="chip onmap">on map</span>' in crash and "may have reopened" not in crash
    # 2. NMDOT gives no cause: said so; whitespace in the title collapsed; yesterday gets a date
    assert "ROAD CLOSED" in nocause and '<span class="ns">cause not stated</span>' in nocause
    assert "<b>NM 94</b> · mile 14–16 · both directions" in nocause
    assert "2 miles north of Ledoux. Roadway closed." in nocause
    assert "NMDOT · updated 17 h 12 min ago (Tue Sep 22 16:59 MDT)" in nocause
    assert "on map" not in nocause and "may have reopened" not in nocause          # 0.7 days < 3
    # 3. water on the road, 'null-null' route: the title is the heading, no mile/direction
    assert '<span class="chip rwater"><span class="rsym water" aria-hidden="true"></span>WATER ON ROAD</span>' in water
    assert "Difficult Driving Conditions exist throughout the Socorro - 41-57 area." in water
    assert "mile" not in water and "direction" not in water and "NA" not in water
    assert "Standing water on roadway. Use extreme caution." in water
    assert "NMDOT · updated 2 h ago (08:12 MDT)" in water and "on map" in water
    # 4. older than cfg.roads_old_days (3): flagged
    assert "<b>NM 252</b> · mile 24–25 · both directions" in old
    assert "NMDOT · updated 6 d ago (Thu Sep 17 07:11 MDT)" in old
    assert '<span class="chip warn">not updated for 6 days, may have reopened</span>' in old
    # one NMRoads link per block, and the feed time
    assert block.count('href="https://nmroads.com/"') == 1
    assert '<a href="https://nmroads.com/" rel="noopener">NMRoads map</a> · NMDOT feed as of 10:04 MDT' in out
    assert "No emergency road closures" not in block


def test_old_days_threshold_comes_from_config(cfg, road_run):
    cfg.roads_old_days = 0.5
    block = _roads_block(page.render_html(cfg, road_run), "socorro")
    rows = _rows(block)
    assert "may have reopened" not in rows[0]                               # 41 min
    assert "not updated for 0 days, may have reopened" in rows[1]           # 17 h > 12 h
    cfg.roads_old_days = 10.0
    block = _roads_block(page.render_html(cfg, road_run), "socorro")
    assert "not updated for" not in block
    cfg.roads_old_days = 0.02                                               # 29 min
    rows = _rows(_roads_block(page.render_html(cfg, road_run), "socorro"))
    assert "not updated for 0 days" in rows[0]
    road_run["sites"][0]["roads"]["events"] = [_event(updated=GEN - timedelta(days=1, hours=2))]
    cfg.roads_old_days = 1.0
    rows = _rows(_roads_block(page.render_html(cfg, road_run), "socorro"))
    assert "not updated for 1 day, may have reopened" in rows[0]


def test_storm_reports_listed_with_time_and_may_have_reopened(cfg, road_run):
    block = _roads_block(page.render_html(cfg, road_run), "socorro")
    assert "<h3>NWS storm reports (last 24 h)</h3>" in block
    rep = _rows(block)[-1]
    assert '<span class="rsym report" aria-hidden="true"></span><b>Tue Sep 22 17:19 MDT</b>' in rep
    assert "Las Nutrias, Socorro County · Flash flood" in rep
    assert "NM State Highway 304 closed at Mile Marker 11 (Las Nutrias) due to standing water." in rep
    assert "NWS ABQ storm report · 16 h 53 min ago · may have reopened" in rep
    assert '<span class="chip onmap">on map</span>' in rep
    cfg.lsr_hours = 48
    assert "<h3>NWS storm reports (last 48 h)</h3>" in page.render_html(cfg, road_run)
    # reports but no NMDOT events: the line says NMDOT lists nothing (not "no closures",
    # which the NM 304 report right below would contradict), and the report still shows
    road_run["sites"][0]["roads"]["events"] = []
    block = _roads_block(page.render_html(cfg, road_run), "socorro")
    assert "No emergency road closures" not in block
    assert ('<p class="unavail">NMDOT lists no emergency road closures within this map; see the '
            'NWS storm reports below.</p>') in block
    assert "Las Nutrias" in block and block.index("NMDOT lists no") < block.index("NWS storm reports")


def test_roads_empty_site_says_so_with_one_link(cfg, road_run):
    road_run["sites"][0]["roads"].update(events=[], reports=[], drawn_ids=[])
    out = page.render_html(cfg, road_run)
    block = _roads_block(out, "socorro")
    assert ('<p class="unavail">No emergency road closures reported on New Mexico state routes '
            'within this map.</p>') in block
    assert "<ul" not in block and "NWS storm reports" not in block and "<span class=\"chip\">" not in block
    assert block.count("NMRoads map") == 1
    # nothing listed anywhere: no banner line, but the footer still credits the sources
    assert "Emergency road closures" not in _banner(out)
    assert "Road information:" in out


def test_site_outside_new_mexico_gets_one_line(cfg, road_run):
    out = page.render_html(cfg, road_run)
    lub = _section(out, "lubbock")
    block = lub[lub.index('<div class="roads" id="roads-lubbock">'):]
    block = block[:block.index("</div>")]
    assert block == ('<div class="roads" id="roads-lubbock">\n<h2>Road closures (New Mexico, emergencies only)</h2>\n'
                     '<p class="unavail">Road closures cover New Mexico state routes only; this map is '
                     'outside New Mexico.</p>\n')
    assert "NMRoads" not in lub and "No emergency road closures" not in lub
    # even if a list slipped through, nothing is listed for a map outside New Mexico
    road_run["sites"][1]["roads"]["events"] = [EV_CRASH]
    out = page.render_html(cfg, road_run)
    assert "ROAD CLOSED" not in _section(out, "lubbock")


def test_run_without_roads_renders_as_before(cfg, run):
    """Older run dicts (and runs with roads disabled) carry no road keys: no block, banner
    line, pill, credit or alt-text change, and status.json has roads None."""
    out = page.render_html(cfg, run)
    body = out[out.index("</style>"):]
    for needle in ('class="roads"', "roadbox", "Road closures", "Roads (NMDOT)", "storm report",
                   "Road information", "NMRoads", "road closures and basemap", 'class="road'):
        assert needle not in body, needle
    # "roads": None on the sites and run is the same as no key at all
    run2 = make_run(cfg)
    run2["roads"] = None
    for s in run2["sites"]:
        s["roads"] = None
    assert page.render_html(cfg, run2) == out
    # the page skeleton is unchanged: same sections and tag balance
    p = _Counter()
    p.feed(out)
    assert out.count("<section") == 2 and p.opened.get("ul", 0) == 0


def test_roads_markup_is_well_formed(cfg, road_run):
    out = page.render_html(cfg, road_run)
    p = _Counter()
    p.feed(out)
    for tag in ("div", "section", "ul", "li", "h2", "h3", "p", "span", "a", "b"):
        assert p.opened.get(tag, 0) == p.closed.get(tag, 0), tag
    assert p.opened["li"] == 5
    assert 'class="errbox"' not in out


def test_banner_counts_distinct_closures_and_links_blocks(cfg, road_run):
    # a third NM site whose map shares the crash closure and the old water item
    abq = copy_site(road_run["sites"][0], "albuquerque", "Albuquerque, NM")
    abq["roads"] = {"covers_nm": True, "events": [EV_CRASH, EV_OLD], "reports": [], "drawn_ids": [CRASH_ID]}
    road_run["sites"].append(abq)
    out = page.render_html(cfg, road_run)
    banner = _banner(out)
    line = banner[banner.index('<div class="abox roadbox">'):]
    line = line[:line.index("</div>")]
    assert _text(line) == ("Emergency road closures (NM): 2 closed, 2 water on road — Socorro, NM · "
                           "Albuquerque, NM")
    assert '<a href="#roads-socorro">Socorro, NM</a> · <a href="#roads-albuquerque">Albuquerque, NM</a>' in line
    assert "lubbock" not in line
    assert 'id="roads-albuquerque"' in out
    # the anchors land below the sticky nav
    assert ".roads { margin: 0 0 18px; scroll-margin-top: calc(var(--navh, 58px) + 8px); }" in page.PAGE_CSS
    # the road line is inside the banner, after the alert boxes
    assert banner.index("Flash Flood Warning") < banner.index("roadbox")
    # only water items: counted as such
    road_run["sites"] = road_run["sites"][:2]
    road_run["sites"][0]["roads"]["events"] = [EV_WATER]
    assert "Emergency road closures (NM): 0 closed, 1 water on road" in _banner(page.render_html(cfg, road_run))
    # storm reports alone do not make a banner line
    road_run["sites"][0]["roads"]["events"] = []
    assert "roadbox" not in _banner(page.render_html(cfg, road_run))


def test_banner_road_line_without_alerts(cfg, road_run):
    for s in road_run["sites"]:
        s["alerts_at"], s["alerts_near"] = [], []
    banner = _banner(page.render_html(cfg, road_run))
    assert "No NWS alerts for these sites." in banner
    assert banner.index("No NWS alerts") < banner.index("Emergency road closures (NM): 2 closed, 2 water on road")
    assert banner.rstrip().endswith("</div>")


def copy_site(s, slug, name):
    site = dict(s["site"], slug=slug, name=name)
    return dict(s, site=site, maps={t: dict(m, basename="radar_%s_%s.png" % (slug, t)) for t, m in s["maps"].items()})


def test_footer_credits_disclaimer_and_source_pills(cfg, road_run):
    out = page.render_html(cfg, road_run)
    foot = out[out.index("<h2>Sources</h2>"):]
    text = _text(foot)
    assert ("Road information: NMDOT / NMRoads.com (public domain, CC0); storm reports: NWS via "
            "Iowa Environmental Mesonet.") in text
    assert "may not reflect all incidents or road conditions" in text and "not to be your only source" in text
    assert '<a href="https://nmroads.com/" rel="noopener">NMDOT / NMRoads.com</a>' in foot
    assert '<a href="https://creativecommons.org/publicdomain/zero/1.0/" rel="noopener">CC0</a>' in foot
    # source pills: NMDOT feed time (Mountain time) + freshness, storm reports
    assert '<span class="pill"><span class="dot"></span>Roads (NMDOT): feed 10:04 MDT · fresh · just now</span>' in foot
    assert '<span class="pill"><span class="dot"></span>NWS storm reports: fresh · just now</span>' in foot
    # what is listed and what is left out, with NMDOT's counts of the left-out categories
    assert "water on the road (NMDOT driving-condition reports of flooding or standing water)" in text
    assert "NWS storm reports from New Mexico of the last 24 h" in text
    assert "Not listed: roadwork, construction, lane, scheduled and seasonal closures" in text
    assert ("Left out right now: 49 Roadwork · 14 Alert · 10 Lane Closure · 9 Difficult Driving "
            "Conditions · 2 Construction Closure · 1 Fair Driving Conditions.") in text
    # the scoping switches change the text
    cfg.roads_water = False
    cfg.lsr_enabled = False
    foot = page.render_html(cfg, road_run)
    foot = foot[foot.index("<h2>Sources</h2>"):]
    assert "water on the road (NMDOT" not in foot and "NWS storm reports from New Mexico" not in foot
    assert "NWS storm reports:" not in foot                     # no pill for a disabled source
    assert "Road information:" in foot


def test_footer_pills_for_failing_road_sources(cfg, road_run):
    road_run["roads"]["nmdot"].update(ok=True, stale=True, error="HTTP 503", age_s=1500.0)
    road_run["roads"]["lsr"] = _status(ok=False, error="mesonet.agron.iastate.edu unreachable")
    out = page.render_html(cfg, road_run)
    foot = out[out.index("<h2>Sources</h2>"):]
    assert ('<span class="dot warnc"></span>Roads (NMDOT): feed 10:04 MDT · stale · last good 25 min ago · '
            'HTTP 503</span>') in foot
    assert '<span class="dot off"></span>NWS storm reports: unavailable · mesonet.agron.iastate.edu unreachable' in foot
    # run["roads"] without the nmdot entry
    del road_run["roads"]["nmdot"]
    out = page.render_html(cfg, road_run)
    assert '<span class="dot off"></span>Roads (NMDOT): unavailable</span>' in out


def test_feed_problems_are_visible_in_the_block(cfg, road_run):
    # stale NMDOT copy: items still listed, with a warning
    road_run["roads"]["nmdot"].update(ok=True, stale=True, error="HTTP 503", age_s=1500.0)
    block = _roads_block(page.render_html(cfg, road_run), "socorro")
    assert ('<p class="feedwarn">NMDOT road feed: latest fetch failed (HTTP 503); showing the last good '
            'copy from 25 min ago.</p>') in block
    assert "ROAD CLOSED" in block
    # NMDOT down and nothing to show: never "no closures reported"
    road_run["roads"]["nmdot"] = _status(ok=False, error="timed out")
    road_run["sites"][0]["roads"].update(events=[], reports=[])
    block = _roads_block(page.render_html(cfg, road_run), "socorro")
    assert ('<p class="feedwarn">NMDOT road feed unavailable (timed out); road closures cannot be listed '
            'right now.</p>') in block
    assert "No emergency road closures" not in block and "NMDOT feed as of" not in block
    # storm reports down: said so (only while enabled)
    road_run["roads"]["lsr"] = _status(ok=False, error="HTTP 500")
    block = _roads_block(page.render_html(cfg, road_run), "socorro")
    assert '<p class="unavail small">NWS storm reports unavailable (HTTP 500).</p>' in block
    cfg.lsr_enabled = False
    assert "storm reports unavailable" not in page.render_html(cfg, road_run)


def test_road_text_is_escaped(cfg, road_run):
    evil = "<script>alert('x')</script> & <img src=x onerror=alert(1)>"
    ev = _event(id="evil-1", cause=evil, title=evil, route="NM <b>41</b>", direction="<i>north</i>bound",
                source="NMDOT <script>")
    odd = _event(id="evil-2", kind="<svg onload=alert(1)>", category="Crash <script>", route=evil)
    rep = dict(REP_304, id="evil-3", remark=evil, place="<b>Las</b> Nutrias", county="So<corro",
               type="FLASH <FLOOD>", source='NWS "ABQ"')
    road_run["sites"][0]["roads"].update(events=[ev, odd], reports=[rep])
    road_run["roads"]["nmdot"]["excluded"] = {"Road<work>": 3}
    road_run["roads"]["nmdot"]["error"] = "<b>boom</b>"
    road_run["roads"]["nmdot"]["stale"] = True
    road_run["sites"][0]["site"]["name"] = "Socorro <NM>"
    out = page.render_html(cfg, road_run)
    body = out[out.index("</style>"):out.rindex("<script>")]         # page markup, not PAGE_JS
    for bad in ("<script>", "<img src=x", "<b>41</b>", "<i>north", "<svg", "<FLOOD>", "<b>Las</b>",
                "So<corro", "Road<work>", "<b>boom</b>", "Socorro <NM>"):
        assert bad not in body, bad
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; &lt;img src=x onerror=alert(1)&gt;" in body
    assert "<b>NM &lt;b&gt;41&lt;/b&gt;</b>" in body
    assert '<span class="chip">CRASH &lt;SCRIPT&gt;</span>' in body            # unknown kind: category chip
    assert "3 Road&lt;work&gt;" in body and "Socorro &lt;NM&gt;" in body
    assert 'class="errbox"' not in body


def test_road_chips_are_monochrome(cfg, road_run):
    """Owner's rule: hazards are never green, flood alert areas are already red and the
    radar uses cyan..magenta, so the road block uses the page's ink/background only."""
    out = page.render_html(cfg, road_run)
    block = _roads_block(out, "socorro")
    banner_line = _banner(out)[_banner(out).index("roadbox"):]
    for frag in (block, banner_line[:banner_line.index("</div>")]):
        assert "style=" not in frag
        assert 'class="dot' not in frag                        # the green "good" dot
    css = [ln for ln in page.PAGE_CSS.splitlines()
           if any(k in ln for k in (".road", ".roads", "rclosed", "rwater", "onmap", ".rsym", "roadbox", ".rlinks", "ul.rlist"))]
    assert len(css) >= 12
    for ln in css:
        rule = ln.split("/*")[0]
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b|rgba?\(|hsla?\(|\b(green|red|lime|cyan|yellow|orange|magenta)\b", rule), ln
        assert "--good" not in rule and "--warn" not in rule
    for cls in (".chip.rclosed", ".chip.rwater"):
        rule = [ln for ln in css if ln.strip().startswith(cls)][0]
        assert "var(--ink)" in rule


def test_map_alt_mentions_roads_when_drawn(cfg, road_run):
    out = page.render_html(cfg, road_run)
    imgs = _radar_imgs(out)
    assert imgs[("dark", "Socorro, NM")]["alt"].startswith("MRMS radar, NWS alert areas, road closures and basemap")
    assert imgs[("dark", "Lubbock, TX")]["alt"].startswith("MRMS radar, NWS alert areas and basemap")
    road_run["sites"][0]["roads"]["drawn_ids"] = []
    imgs = _radar_imgs(page.render_html(cfg, road_run))
    assert imgs[("dark", "Socorro, NM")]["alt"].startswith("MRMS radar, NWS alert areas and basemap")
    # without drawn ids nothing is marked "on map"
    assert "on map" not in _roads_block(page.render_html(cfg, road_run), "socorro")


def test_malformed_road_records_degrade_quietly(cfg, road_run):
    weird = [None, "junk", 42,
             {"id": ["not", "hashable"], "kind": "closure", "route": "null-null", "title": "",
              "category": "Closure", "cause": None, "cause_known": None, "updated": "not a date",
              "posted": None, "mm_from": "abc", "mm_to": float("nan"), "direction": 7},
             {"kind": "water", "updated": "2026-09-23T15:00:00+00:00", "route": "US 60", "mm_from": 331,
              "mm_to": None, "direction": "eastbound", "cause": "Water over the road."}]
    road_run["sites"][0]["roads"].update(events=weird, reports=[None, {"remark": None}], drawn_ids=None)
    road_run["roads"]["nmdot"].update(feed_time="garbage", excluded={"Roadwork": "many", "Alert": None})
    out = page.render_html(cfg, road_run)
    assert 'class="errbox"' not in out
    block = _roads_block(out, "socorro")
    rows = _rows(block)
    assert len(rows) == 3
    head = rows[0].split('<div class="rc">')[0]
    assert "<b>" not in head and "null" not in rows[0]                     # no junk route shown
    assert "Closure" in rows[0] and "cause not stated" in rows[0] and "update time not given" in rows[0]
    assert "<b>US 60</b> · mile 331 · eastbound" in rows[1] and "updated 1 h 12 min ago (09:00 MDT)" in rows[1]
    assert "time not given" in rows[2] and "may have reopened" in rows[2]
    assert "Left out right now" not in out and "NMDOT feed as of" not in out
    with open(page.write_status_json(cfg, road_run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["roads"]["nmdot"]["events"] == 2 and doc["roads"]["nmdot"]["feed_time"] is None
    assert doc["sites"]["socorro"]["roads_events"] == 2 and doc["sites"]["socorro"]["roads_reports"] == 1
    # a road dict that is not a dict at all: nothing rendered for it
    road_run["sites"][0]["roads"] = ["junk"]
    assert 'id="roads-socorro"' not in page.render_html(cfg, road_run)


def test_one_bad_row_does_not_hide_the_others(cfg, road_run, monkeypatch):
    real = page._road_event_html

    def flaky(cfg_, ev, *a):
        if ev.get("id") == NM94_ID:
            raise RuntimeError("bad row")
        return real(cfg_, ev, *a)

    monkeypatch.setattr(page, "_road_event_html", flaky)
    block = _roads_block(page.render_html(cfg, road_run), "socorro")
    assert "road event could not be rendered: RuntimeError: bad row" in block
    assert "NM 41" in block and "NM 252" in block and "Las Nutrias" in block


def test_mile_marker_and_direction_text():
    mm = page._mm_txt
    assert mm({"mm_from": 17, "mm_to": 19}) == "mile 17–19"
    assert mm({"mm_from": 41, "mm_to": 0, "title": "NM 107 from mile marker 41, Magdalena to mile marker 0"}) == "mile 0–41"
    # a spot event: NMDOT's RSS fills mileMarkerTo with 0
    assert mm({"mm_from": 43, "mm_to": 0, "title": "Alert, US 380 eastbound and westbound at mile marker 43"}) == "mile 43"
    assert mm({"mm_from": 11.5, "mm_to": 11.5}) == "mile 11.5"
    assert mm({"mm_from": None, "mm_to": 9}) == "mile 9"
    assert mm({"mm_from": None, "mm_to": None}) is None and mm({}) is None
    assert mm({"mm_from": True, "mm_to": "x"}) is None
    d = page._direction_txt
    assert d("both") == d("northbound and southbound") == d("Eastbound and Westbound") == "both directions"
    assert d("eastbound") == "eastbound" and d("NA") is None and d("") is None and d(None) is None
    r = page._route_txt
    assert r("null-null") is None and r("-") is None and r(" NM  252 ") == "NM 252" and r(None) is None


def test_status_json_roads(cfg, road_run):
    road_run["sites"] = road_run["sites"][:1] + road_run["sites"][1:]
    for s in road_run["sites"]:
        for m in s["maps"].values():
            m["ok"] = True
    road_run["sites"][1].update(meta=_meta("America/Chicago", "LUB", "LUB", 49, 33),
                                obs=_obs_ok(), forecast=_forecast_ok(), hourly=_hourly_ok())
    page.write_page(cfg, road_run)
    with open(page.write_status_json(cfg, road_run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["roads"] == {"nmdot": {"ok": True, "stale": False, "events": 4, "feed_time": FEED_TIME.isoformat()},
                            "lsr": {"ok": True, "stale": False, "reports": 1}}
    assert doc["sites"]["socorro"]["roads_events"] == 4 and doc["sites"]["socorro"]["roads_reports"] == 1
    assert doc["sites"]["lubbock"]["roads_events"] == 0 and doc["sites"]["lubbock"]["roads_reports"] == 0
    assert doc["problems"] == [] and doc["ok"] is True
    # an event on two maps counts once
    road_run["sites"].append(dict(copy_site(road_run["sites"][0], "albuquerque", "Albuquerque, NM"),
                                  roads={"covers_nm": True, "events": [EV_CRASH], "reports": [REP_304],
                                         "drawn_ids": []}))
    with open(page.write_status_json(cfg, road_run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["roads"]["nmdot"]["events"] == 4 and doc["roads"]["lsr"]["reports"] == 1
    assert doc["sites"]["albuquerque"]["roads_events"] == 1
    road_run["sites"].pop()
    # failing road sources degrade the run while roads are enabled
    road_run["roads"]["nmdot"] = _status(ok=False, error="HTTP 503")
    road_run["roads"]["lsr"] = _status(ok=True, stale=True, error="timed out", age_s=4000.0)
    with open(page.write_status_json(cfg, road_run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["problems"] == ["NMDOT road feed: unavailable", "NWS storm reports: stale"]
    assert doc["degraded"] is True and doc["ok"] is False
    assert doc["roads"]["nmdot"] == {"ok": False, "stale": False, "events": 4, "feed_time": None}
    assert doc["roads"]["lsr"] == {"ok": True, "stale": True, "reports": 1}
    # storm reports switched off: their status does not count
    cfg.lsr_enabled = False
    with open(page.write_status_json(cfg, road_run), encoding="utf-8") as f:
        assert json.load(f)["problems"] == ["NMDOT road feed: unavailable"]
    # roads switched off: no road problems at all
    cfg.roads_enabled = False
    with open(page.write_status_json(cfg, road_run), encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["problems"] == [] and doc["ok"] is True
    # enabled, but the run has no lsr entry: reported as missing
    cfg.roads_enabled, cfg.lsr_enabled = True, True
    del road_run["roads"]["lsr"]
    with open(page.write_status_json(cfg, road_run), encoding="utf-8") as f:
        doc = json.load(f)
    assert "NWS storm reports: missing" in doc["problems"]
    assert doc["roads"]["lsr"] == {"ok": False, "stale": False, "reports": 1}


def test_incomplete_nmdot_feed_is_visible(cfg, road_run):
    """One NMDOT file missing (fetch_nm_roads: ok, not stale, error set -- e.g. rss.xml down,
    so causes are unknown and water on the road cannot be detected): the block, the footer
    pill and status.json say so instead of looking fresh."""
    err = "rss.xml: HTTP 503; rss.xml unavailable: causes unknown, water on road not detected"
    road_run["roads"]["nmdot"].update(ok=True, stale=False, error=err, age_s=0.0)
    out = page.render_html(cfg, road_run)
    block = _roads_block(out, "socorro")
    assert '<p class="feedwarn">NMDOT road feed incomplete (%s).</p>' % err in block
    assert "ROAD CLOSED" in block                                  # items still listed
    foot = out[out.index("<h2>Sources</h2>"):]
    assert '<span class="dot warnc"></span>Roads (NMDOT): feed 10:04 MDT · incomplete · rss.xml' in foot
    for m in road_run["sites"][0]["maps"].values():
        m["ok"] = True
    with open(page.write_status_json(cfg, road_run), encoding="utf-8") as f:
        doc = json.load(f)
    assert "NMDOT road feed: incomplete" in doc["problems"] and doc["degraded"] is True
    # complete again: no warning anywhere
    road_run["roads"]["nmdot"]["error"] = None
    out = page.render_html(cfg, road_run)
    assert "road feed incomplete" not in out and "· incomplete" not in out


def test_area_wide_and_approximate_road_items(cfg, road_run):
    """An area-wide NMDOT condition ("... exist throughout the Socorro - 41-57 area.") is
    listed with an area chip and never marked "on map" (the renderer does not draw it); an
    event known only from nmroads.json (rss.xml down) carries an approximate post time, so
    a week-old scheduled closure still gets its "may have reopened" flag."""
    area = dict(EV_WATER, area_wide=True)
    approx = dict(EV_NOCAUSE, updated=None, posted=GEN - timedelta(days=5, hours=3),
                  time_approx=True)
    road_run["sites"][0]["roads"].update(events=[approx, area],
                                         drawn_ids=[SOCORRO_WATER_ID, NM94_ID])
    rows = _rows(_roads_block(page.render_html(cfg, road_run), "socorro"))
    assert "WATER ON ROADS IN AREA" in rows[1] and "WATER ON ROAD<" not in rows[1]
    assert "whole area, not drawn on the map" in rows[1]
    assert '<span class="chip onmap">on map</span>' not in rows[1]
    assert "NMDOT · posted about 5 d ago (time approximate)" in rows[0]
    assert "not updated for 5 days, may have reopened" in rows[0]
    assert '<span class="chip onmap">on map</span>' in rows[0]
    # an area-wide closure reads "ROADS CLOSED IN AREA"
    road_run["sites"][0]["roads"]["events"] = [dict(area, kind="closure")]
    rows = _rows(_roads_block(page.render_html(cfg, road_run), "socorro"))
    assert "ROADS CLOSED IN AREA" in rows[0]


# ---- the page's own disclaimer, the page URL, anchors, products NWS does not provide ------------
def test_disclaimer_is_on_every_page(cfg, run, road_run):
    """The page says it is not an official product and points to weather.gov: in the footer
    (the full text) and under the alerts banner (short), with road data, without it (roads
    off), with roads "not applicable", and with or without alerts. NMDOT's own disclaimer is
    only for the road data and is not a substitute."""
    no_alerts = make_run(cfg)
    no_alerts["alerts"]["alerts"] = []
    for s in no_alerts["sites"]:
        s["alerts_at"], s["alerts_near"] = [], []
    not_applicable = dict(make_run(cfg), roads_not_applicable="no site's map reaches New Mexico")
    for r in (run, road_run, no_alerts, not_applicable):
        out = page.render_html(cfg, r)
        foot = out[out.index("<h2>Sources</h2>"):]
        assert '<p class="foot disclaimer">%s</p>' % page.DISCLAIMER_HTML in foot
        assert "must not be the only source for safety decisions" in foot
        assert page.BANNER_DISCLAIMER_HTML in _banner(out)
        assert out.count('<a href="https://www.weather.gov/" rel="noopener">weather.gov</a>') == 2
    assert "NMDOT's disclaimer" not in page.render_html(cfg, run)
    assert "No NWS alerts for these sites." in page.render_html(cfg, no_alerts)


def test_page_url_is_shown_only_when_set(cfg, run):
    """No address by default (every deployment would otherwise link the example's host);
    an https address is linked, anything else is shown as text."""
    assert cfg.page_url == ""
    foot = page.render_html(cfg, run).split("<h2>Sources</h2>", 1)[1]
    last = foot[foot.rindex('<p class="foot">'):]
    assert "<br>generated 2026-09-23 16:12:00 UTC · refreshes every 300 s</p>" in last
    assert "tau.kirx.net" not in foot and "href=\"https://example" not in foot
    cfg.page_url = "https://wx.example.org/myweather/"
    foot = page.render_html(cfg, run).split("<h2>Sources</h2>", 1)[1]
    assert ('<a href="https://wx.example.org/myweather/">https://wx.example.org/myweather/</a>'
            ' · generated 2026-09-23 16:12:00 UTC') in foot
    cfg.page_url = "http://intranet/wx/"
    foot = page.render_html(cfg, run).split("<h2>Sources</h2>", 1)[1]
    assert "http://intranet/wx/ · generated" in foot and 'href="http://intranet' not in foot


class _Ids(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if d.get("id"):
            self.ids.append(d["id"])
        if tag == "a" and (d.get("href") or "").startswith("#"):
            self.hrefs.append(d["href"][1:])


def test_slugs_never_collide_with_page_ids(cfg, road_run):
    """Site anchors are prefixed (site-<slug>, roads-<slug>): slugs such as "page"
    (Page, AZ), "alerts" or "modebtn", or "x" next to "x-roads", give no duplicate id, and
    every in-page link lands on the element it means."""
    template = road_run["sites"][0]
    road_run["sites"] = []
    for slug in ("page", "alerts", "modebtn", "x", "x-roads"):
        s = json.loads(json.dumps(template, default=str))
        s["site"] = dict(template["site"], slug=slug, name="%s, ST" % slug.title())
        s["alerts_at"], s["alerts_near"] = template["alerts_at"], template["alerts_near"]
        s["sun"] = None
        s["roads"] = template["roads"]
        road_run["sites"].append(s)
    out = page.render_html(cfg, road_run)
    p = _Ids()
    p.feed(out)
    assert len(p.ids) == len(set(p.ids)), sorted(i for i in p.ids if p.ids.count(i) > 1)
    assert {"page", "alerts", "modebtn"} <= set(p.ids)
    for slug in ("page", "alerts", "modebtn", "x", "x-roads"):
        assert '<section class="site" id="site-%s">' % slug in out
        assert '<div class="roads" id="roads-%s">' % slug in out
        assert '<a class="pill" href="#site-%s">' % slug in out
    assert set(p.hrefs) <= set(p.ids), set(p.hrefs) - set(p.ids)
    assert page.site_anchor("x-roads") != page.roads_anchor("x")


def test_products_nws_does_not_provide_are_not_errors(cfg, run):
    """American Samoa: /points has zones and a time zone but no forecast grid, so there is
    no forecast, hourly forecast or station list. The page says "not provided" (no
    "unavailable" chip), the heading names the office, and status.json does not call the
    run degraded for it."""
    np_ = {"ok": False, "stale": False, "error": "not provided by NWS for this location (it "
           "has no forecast grid)", "age_s": None, "not_provided": True}
    s = run["sites"][0]
    s["meta"].update(grid_id=None, grid_x=None, grid_y=None, office="PPG", no_grid=True)
    s["obs"] = dict(np_)
    s["forecast"] = dict(np_, updated=None, periods=[])
    s["hourly"] = dict(np_, updated=None, hours=[])
    run["sites"] = [s]
    out = page.render_html(cfg, run)
    sec = _section(out, "socorro")
    assert "NWS PPG, no forecast grid" in sec
    for what in ("Current conditions", "Hourly forecast", "7-day forecast"):
        assert "%s: not provided by NWS for this location (it has no forecast grid)." % what in sec
    assert '<span class="chip off">unavailable</span>' not in sec
    assert not re.search(r"(Current conditions|Hourly forecast|7-day forecast) unavailable", sec)
    foot = out[out.index("<h2>Sources</h2>"):]
    assert "Socorro, NM: obs not provided · forecast not provided · hourly not provided" in foot
    for m in s["maps"].values():
        m["ok"] = True
    with open(page.write_status_json(cfg, run), encoding="utf-8") as f:
        doc = json.load(f)
    assert not any("socorro" in p for p in doc["problems"]), doc["problems"]
    assert doc["sites"]["socorro"]["forecast_ok"] is False
