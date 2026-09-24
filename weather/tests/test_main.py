"""End-to-end tests for make_weather_page: a full run with every source mocked, a run with
every source dead, per-step degradation, the per-run network budget, time-zone fallback,
the lock, the CLI and its exit codes; New Mexico road closures (NMDOT + NWS storm reports)
end to end, their failure modes and their switches; plus the config fields main relies on
and the run_local.sh preview wrapper (process cleanup, exit status, BIND).

Fixtures are trimmed api.weather.gov shapes for Socorro, NM; times are built relative to
the real clock so the hourly window and the observation age need no patched ``utcnow``.
The radar frame is a synthetic 7000x3500 palette PNG (encoded once per module). The road
fixtures are cut from the live nmroads.json / rss.xml / IEM storm-report GeoJSON of
2026-09-23 (see ``NM_EVENTS``).
"""
from __future__ import annotations

import io
import json
import logging
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

import make_weather_page as mwp
from weather import alerts, config, geo, http, nws, page, radar, roads, sun, util
from weather.config import Config, parse_sites

try:
    from zoneinfo import ZoneInfo
except ImportError:             # pragma: no cover - Python < 3.9
    ZoneInfo = None

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SOCORRO = {"slug": "socorro", "name": "Socorro, NM", "lat": 34.0584, "lon": -106.8914}
LUBBOCK = {"slug": "lubbock", "name": "Lubbock, TX", "lat": 33.5779, "lon": -101.8552}
TILE_RGB = (40, 44, 52)
FUTURE = "2099-09-23T12:00:00-06:00"
PAST = "2020-01-01T00:00:00-06:00"

POINTS = {"properties": {
    "gridId": "ABQ", "gridX": 86, "gridY": 76, "cwa": "ABQ",
    "forecast": "https://api.weather.gov/gridpoints/ABQ/86,76/forecast",
    "forecastHourly": "https://api.weather.gov/gridpoints/ABQ/86,76/forecast/hourly",
    "observationStations": "https://api.weather.gov/gridpoints/ABQ/86,76/stations",
    "relativeLocation": {"properties": {"city": "Socorro", "state": "NM"}},
    "timeZone": "America/Denver", "radarStation": "KABX",
    "forecastZone": "https://api.weather.gov/zones/forecast/NMZ220",
    "county": "https://api.weather.gov/zones/county/NMC053",
    "fireWeatherZone": "https://api.weather.gov/zones/fire/NMZ106"}}

STATIONS = {"features": [{"id": "https://api.weather.gov/stations/KONM",
                          "properties": {"stationIdentifier": "KONM",
                                         "name": "Socorro Municipal Airport"}}],
            "observationStations": ["https://api.weather.gov/stations/KONM"]}


def _q(unit, value):
    return {"unitCode": "wmoUnit:" + unit, "value": value}


def _iso(dt):
    return dt.isoformat().replace("+00:00", "+00:00")


def forecast_doc():
    now = util.utcnow()
    return {"properties": {"updateTime": _iso(now - timedelta(hours=1)), "periods": [
        {"number": 1, "name": "Today", "startTime": _iso(now), "endTime": _iso(now + timedelta(hours=8)),
         "isDaytime": True, "temperature": 69, "temperatureUnit": "F", "temperatureTrend": None,
         "probabilityOfPrecipitation": _q("percent", 85), "windSpeed": "5 to 10 mph",
         "windDirection": "SE", "icon": "https://api.weather.gov/icons/land/day/tsra,80?size=medium",
         "shortForecast": "Showers And Thunderstorms", "detailedForecast": "Showers likely."},
        {"number": 2, "name": "Tonight", "startTime": _iso(now + timedelta(hours=8)),
         "endTime": _iso(now + timedelta(hours=20)), "isDaytime": False, "temperature": 56,
         "temperatureUnit": "F", "temperatureTrend": None,
         "probabilityOfPrecipitation": _q("percent", None), "windSpeed": "5 mph",
         "windDirection": "S", "icon": None, "shortForecast": "Chance Showers",
         "detailedForecast": "A chance of showers."}]}}


def hourly_doc():
    start = util.utcnow().replace(minute=0, second=0, microsecond=0)
    periods = []
    for i in range(-1, 5):          # one period in the past: nws.hourly must drop it
        t = start + timedelta(hours=i)
        periods.append({"number": i + 2, "startTime": _iso(t), "endTime": _iso(t + timedelta(hours=1)),
                        "isDaytime": True, "temperature": 64 + i, "temperatureUnit": "F",
                        "probabilityOfPrecipitation": _q("percent", 40 + 5 * i),
                        "dewpoint": _q("degC", 15.5), "relativeHumidity": _q("percent", 80),
                        "windSpeed": "10 mph", "windDirection": "E", "icon": None,
                        "shortForecast": "Showers"})
    return {"properties": {"updateTime": _iso(start), "periods": periods}}


def observation_doc():
    ts = util.utcnow() - timedelta(minutes=15)
    return {"properties": {
        "station": "https://api.weather.gov/stations/KONM", "stationId": "KONM",
        "stationName": "Socorro Municipal Airport", "timestamp": _iso(ts),
        "textDescription": "Rain", "icon": None, "temperature": _q("degC", 15.7),
        "dewpoint": _q("degC", 15.1), "windDirection": _q("degree_(angle)", 350),
        "windSpeed": _q("km_h-1", 16.56), "windGust": _q("km_h-1", None),
        "barometricPressure": _q("Pa", 102340), "seaLevelPressure": _q("Pa", None),
        "visibility": _q("m", 16090), "relativeHumidity": _q("percent", 96.2),
        "windChill": _q("degC", None), "heatIndex": _q("degC", None), "cloudLayers": []}}


def _box(lon0, lat0, lon1, lat1):
    return [[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat1], [lon0, lat0]]


# Polygons relative to Socorro's 100-mile frame (about +-0.72 deg lat, +-0.87 deg lon).
COVERS_SITE = _box(-107.2, 33.8, -106.5, 34.3)           # contains the town
NW_OF_SITE = _box(-107.6, 34.4, -107.4, 34.6)            # in the frame, not containing it
SE_OF_SITE = _box(-106.5, 33.6, -106.3, 33.8)            # in the frame, not containing it


def alert_feature(fid, event, severity, geometry=None, zones=(), ends=FUTURE, mtype="Alert"):
    return {"id": "https://api.weather.gov/alerts/" + fid, "type": "Feature",
            "geometry": None if geometry is None else {"type": "Polygon", "coordinates": [geometry]},
            "properties": {"id": fid, "areaDesc": "Socorro, NM", "affectedZones": list(zones),
                           "sent": "2026-09-23T09:08:00-06:00", "effective": "2026-09-23T09:08:00-06:00",
                           "onset": "2026-09-23T09:08:00-06:00", "expires": ends, "ends": ends,
                           "status": "Actual", "messageType": mtype, "severity": severity,
                           "certainty": "Likely", "urgency": "Expected", "event": event,
                           "senderName": "NWS Albuquerque NM",
                           "headline": "%s issued by NWS Albuquerque NM" % event,
                           "description": "Details of the %s." % event,
                           "instruction": "Take care.", "web": "https://www.weather.gov"}}


def alert_feed():
    return {"type": "FeatureCollection", "features": [
        alert_feature("ffw", "Flash Flood Warning", "Severe", geometry=COVERS_SITE,
                      zones=["https://api.weather.gov/zones/county/NMC053"]),
        alert_feature("watch", "Flood Watch", "Moderate",
                      zones=["https://api.weather.gov/zones/forecast/NMZ220",
                             "https://api.weather.gov/zones/county/NMC053"]),
        alert_feature("adv", "Wind Advisory", "Minor", geometry=SE_OF_SITE),
        alert_feature("old", "Special Weather Statement", "Minor", geometry=COVERS_SITE, ends=PAST),
    ]}


ZONE_NMZ220 = {"id": "https://api.weather.gov/zones/forecast/NMZ220", "type": "Feature",
               "geometry": {"type": "Polygon", "coordinates": [NW_OF_SITE]},
               "properties": {"id": "NMZ220", "type": "forecast", "name": "Socorro"}}


def _png(mode="RGB", size=(256, 256), color=TILE_RGB) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


NO_RADAR = {"ok": False, "stale": True, "error": "IEM down", "age_s": None, "ts": None,
            "url": None, "image": None}
FRAME_TS = datetime(2026, 9, 23, 15, 12, tzinfo=timezone.utc)


def _no_frame(cfg, cache):
    return dict(NO_RADAR)


@pytest.fixture(scope="module")
def frame_png():
    """A 7000x3500 palette frame: no data everywhere except a 21x21-cell block of index
    124 (30 dBZ) 0.4 deg north of Socorro (inside the frame, outside the FFW polygon).

    The image gets a full 256-entry palette like the real IEM files: with the empty
    default palette Pillow writes a 1-bit PNG and every index collapses to 0/1."""
    img = Image.new("P", (geo.MRMS_W, geo.MRMS_H), geo.MRMS_NODATA)
    img.putpalette([v for i in range(256) for v in (i, i, i)])
    c, r = geo.mrms_cell(SOCORRO["lat"] + 0.4, SOCORRO["lon"])
    img.paste(124, (c - 10, r - 10, c + 11, r + 11))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    reloaded = Image.open(io.BytesIO(buf.getvalue()))
    assert reloaded.mode == "P" and reloaded.getpixel((c, r)) == 124
    return buf.getvalue()


@pytest.fixture
def two_sites(cfg):
    cfg.sites = [dict(SOCORRO), dict(LUBBOCK)]
    return cfg


@pytest.fixture(autouse=True)
def _fresh_http_run_state(monkeypatch):
    """run_once arms weather.http's per-run budget and circuit breaker, which is module
    state. Every test here starts from a pristine state and the previous one is put back
    afterwards, so no deadline or down-host list leaks into later test modules."""
    if hasattr(http, "_RunState") and hasattr(http, "_RUN"):
        monkeypatch.setattr(http, "_RUN", http._RunState())


def add_nws_routes(fake_http):
    """Socorro's whole NWS chain. Order matters: the first matching fragment wins, and the
    hourly URL contains the forecast URL."""
    fake_http.add("/points/34.0584,-106.8914", POINTS)
    fake_http.add("gridpoints/ABQ/86,76/forecast/hourly", hourly_doc())
    fake_http.add("gridpoints/ABQ/86,76/forecast", forecast_doc())
    fake_http.add("gridpoints/ABQ/86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", observation_doc())
    fake_http.add("/alerts/active?area=", alert_feed())        # any area list
    fake_http.add("/zones/forecast/NMZ220", ZONE_NMZ220)
    fake_http.add("openstreetmap", _png())


def _is_road_url(call):
    """A fake_http call record (``url``, ``HEAD url``, ...) aimed at a road source."""
    return "nmroads" in call or "lsr.geojson" in call


def read_status(cfg):
    with open(os.path.join(cfg.out_dir, "status.json"), encoding="utf-8") as f:
        return json.load(f)


def read_page(cfg):
    with open(os.path.join(cfg.out_dir, "index.html"), encoding="utf-8") as f:
        return f.read()


def ids(recs):
    return [a["id"] for a in recs]


def site_status(st, slug):
    """The per-site health keys these tests pin down (status.json may carry more)."""
    keys = ("meta_ok", "obs_ok", "forecast_ok", "hourly_ok", "alerts_at", "alerts_near", "maps_ok")
    return {k: st["sites"][slug].get(k) for k in keys}


def assert_status_degraded(st):
    """status.json of a run that wrote its page but had problems (contract C11)."""
    assert st["page_written"] is True and st["degraded"] is True and st["ok"] is False
    assert st["problems"] and all(isinstance(p, str) for p in st["problems"])


def assert_outputs(cfg, slugs):
    """index.html, status.json and two real PNG maps per site are in the output dir."""
    assert os.path.exists(os.path.join(cfg.out_dir, "index.html"))
    assert os.path.exists(os.path.join(cfg.out_dir, "status.json"))
    for slug in slugs:
        for theme in ("dark", "light"):
            p = os.path.join(cfg.out_dir, cfg.map_basename(slug, theme))
            assert os.path.exists(p), p
            assert Image.open(p).format == "PNG"
    assert not [f for f in os.listdir(cfg.out_dir) if ".tmp." in f]


# ---- the full run ----------------------------------------------------------------------------
def test_run_once_full_pipeline(two_sites, fake_http, frame_png):
    """Everything but road closures (WEATHER_ROADS=0: the run dict keeps its pre-roads
    shape and no road source is contacted); the roads tests further down add them."""
    cfg = two_sites
    cfg.map_px = 120                            # keeps the per-pixel radar remap cheap
    cfg.roads_enabled = False
    add_nws_routes(fake_http)
    fake_http.add("/GIS/mrms/lcref_", frame_png)

    run = mwp.run_once(cfg)
    assert not any(_is_road_url(c) for c in fake_http.calls)

    # run dict shape
    assert set(run) == {"generated", "generated_ts", "night_default", "alerts", "radar", "network",
                        "errors", "sites"}
    assert isinstance(run["generated"], datetime) and run["generated"].tzinfo is not None
    assert abs(run["generated_ts"] - run["generated"].timestamp()) < 1e-6
    assert [s["site"]["slug"] for s in run["sites"]] == ["socorro", "lubbock"]
    for s in run["sites"]:
        assert set(s) == {"site", "meta", "obs", "forecast", "hourly", "sun", "alerts_at",
                          "alerts_near", "alerts_drawn_ids", "maps", "map_notes"}
        assert set(s["maps"]) == {"dark", "light"}
        for theme, m in s["maps"].items():
            assert m == {"basename": cfg.map_basename(s["site"]["slug"], theme), "ok": True,
                         "error": None}

    # alerts: the expired statement is dropped, the zone-based watch got its outline
    feed = run["alerts"]
    assert feed["ok"] and not feed["stale"] and feed["count_raw"] == 4
    assert ids(feed["alerts"]) == ["ffw", "watch", "adv"]
    watch = feed["alerts"][1]
    assert watch["geometry_source"] == "zones" and watch["geometry"]["type"] == "MultiPolygon"

    # radar: one frame, fetched once, shared by all four maps, image kept out of the run
    assert run["radar"]["ok"] and not run["radar"]["stale"] and "image" not in run["radar"]
    assert isinstance(run["radar"]["ts"], datetime) and run["radar"]["url"].endswith(".png")
    frame_gets = [c for c in fake_http.calls if "lcref_" in c and not c.startswith("HEAD ")]
    assert len(frame_gets) == 1
    assert sum(1 for c in fake_http.calls if "/alerts/active" in c) == 1
    # WEATHER_ALERT_AREAS=auto: the states whose box touches the Socorro or Lubbock map
    # (Oklahoma's box, panhandle to Red River, covers the Lubbock map's north-east corner)
    assert [c for c in fake_http.calls if "/alerts/active" in c] == [
        alerts.BASE + "/alerts/active?area=NM,OK,TX"]
    assert sum(1 for c in fake_http.calls if "/zones/forecast/NMZ220" in c) == 1
    assert sum(1 for c in fake_http.calls if "/zones/county/NMC053" in c) == 1   # tried, failed

    # Socorro: everything fresh; FFW AT via polygon, Flood Watch AT via its zone id, the
    # Wind Advisory NEAR because its polygon landed on the map
    soc = run["sites"][0]
    assert soc["meta"]["ok"] and soc["meta"]["tz"] == "America/Denver"
    assert soc["obs"]["ok"] and soc["obs"]["station_id"] == "KONM" and soc["obs"]["temp_f"] == 60.3
    assert soc["forecast"]["ok"] and len(soc["forecast"]["periods"]) == 2
    assert soc["hourly"]["ok"] and len(soc["hourly"]["hours"]) == 5
    assert soc["sun"]["tz"] == "America/Denver" and isinstance(soc["sun"]["is_night"], bool)
    assert ids(soc["alerts_at"]) == ["ffw", "watch"]
    assert ids(soc["alerts_near"]) == ["adv"]
    assert soc["alerts_drawn_ids"] == ["adv", "ffw", "watch"]      # sorted union of both themes
    assert soc["map_notes"] == []
    assert run["night_default"] == soc["sun"]["is_night"]

    # Lubbock: no /points route -> NWS products unavailable, maps still drawn, sun in UTC
    lub = run["sites"][1]
    assert not lub["meta"]["ok"] and lub["meta"]["tz"] == "UTC" and lub["meta"]["zone_urls"] == []
    for k in ("obs", "forecast", "hourly"):
        assert not lub[k]["ok"] and lub[k]["error"]
    assert lub["sun"]["tz"] == "UTC"                  # this LUBBOCK dict has no static tz
    assert lub["alerts_at"] == [] and lub["alerts_near"] == [] and lub["alerts_drawn_ids"] == []
    assert any(e.startswith("lubbock metadata unavailable") for e in run["errors"])
    assert not any(e.startswith("socorro") for e in run["errors"])

    # files
    assert_outputs(cfg, ["socorro", "lubbock"])
    st = read_status(cfg)
    assert st["errors"] == run["errors"] and st["errors"]
    assert_status_degraded(st)                        # Lubbock's NWS products are missing
    assert st["alerts"] == {"ok": True, "stale": False, "count": 3}
    assert st["radar"]["ok"] and not st["radar"]["stale"] and st["radar"]["ts"] == run["radar"]["ts"].isoformat()
    assert site_status(st, "socorro") == {"meta_ok": True, "obs_ok": True, "forecast_ok": True,
                                          "hourly_ok": True, "alerts_at": 2, "alerts_near": 1,
                                          "maps_ok": True}
    assert site_status(st, "lubbock") == {"meta_ok": False, "obs_ok": False, "forecast_ok": False,
                                          "hourly_ok": False, "alerts_at": 0, "alerts_near": 0,
                                          "maps_ok": True}
    html = read_page(cfg)
    assert "Socorro, NM" in html and "Lubbock, TX" in html
    assert "Flash Flood Warning" in html and "Flood Watch" in html and "Wind Advisory" in html
    assert "Special Weather Statement" not in html
    assert "MRMS frame" in html and 'id="site-socorro"' in html and 'id="site-lubbock"' in html

    # the dark map really carries the radar echo (green, north of the town) and the FFW
    # tint over the town itself; the plain basemap colour survives far from both
    frame = geo.MapFrame(SOCORRO["lat"], SOCORRO["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
    im = Image.open(os.path.join(cfg.out_dir, cfg.map_basename("socorro", "dark"))).convert("RGB")
    assert im.size == frame.size
    ex, ey = frame.lonlat_to_px(SOCORRO["lon"], SOCORRO["lat"] + 0.4)
    r, g, b = im.getpixel((int(ex), int(ey)))
    assert g > 90 and g > r + 40 and g > b + 40
    cx, cy = frame.lonlat_to_px(SOCORRO["lon"], SOCORRO["lat"])
    r, g, b = im.getpixel((int(cx) - 6, int(cy) + 6))
    assert r > TILE_RGB[0] + 20 and r > g                         # dark-red FFW fill
    assert im.getpixel((3, frame.height // 2)) == TILE_RGB


def test_run_once_every_source_dead(cfg, fake_http):
    """fake_http with no routes at all: NWS, IEM and the tile server are all 'down'. The page and
    both maps per site must still appear, with ok=true and the problems listed."""
    run = mwp.run_once(cfg)
    slugs = [s["slug"] for s in cfg.sites]
    assert len(slugs) == 5 and [s["site"]["slug"] for s in run["sites"]] == slugs
    assert not run["alerts"]["ok"] and run["alerts"]["alerts"] == [] and run["alerts"]["error"]
    assert not run["radar"]["ok"] and run["radar"]["ts"] is None and "no MRMS frame" in run["radar"]["error"]
    for s in run["sites"]:
        # no /points metadata: local times come from the static zone in DEFAULT_SITES
        assert not s["meta"]["ok"] and s["site"]["tz"] in ("America/Chicago", "America/Denver")
        assert s["meta"]["tz"] == s["site"]["tz"] and s["sun"]["tz"] == s["site"]["tz"]
        assert s["alerts_drawn_ids"] == []
        for k in ("obs", "forecast", "hourly"):
            assert s[k]["ok"] is False and s[k]["stale"] is True and s[k]["error"]
        assert s["obs"]["cloud_layers"] == [] and s["forecast"]["periods"] == [] and s["hourly"]["hours"] == []
        assert isinstance(s["sun"], dict) and s["alerts_at"] == [] and s["alerts_near"] == []
        assert all(m["ok"] for m in s["maps"].values())          # bare background, but written
        assert any("basemap incomplete" in n for n in s["map_notes"])
        # road sources down: nothing to list, but the site still knows whether its map
        # reaches New Mexico (Lubbock's does not)
        assert s["roads"] == {"covers_nm": s["site"]["slug"] != "lubbock", "events": [],
                              "reports": [], "drawn_ids": []}
    assert run["night_default"] == run["sites"][0]["sun"]["is_night"]
    assert run["roads"]["nmdot"]["ok"] is False and run["roads"]["nmdot"]["error"]
    assert run["roads"]["lsr"]["ok"] is False and run["roads"]["lsr"]["error"]

    assert_outputs(cfg, slugs)
    st = read_status(cfg)
    assert_status_degraded(st)
    assert st["errors"] and len(st["errors"]) == len(run["errors"])
    assert any(e.startswith("alerts feed unavailable") for e in st["errors"])
    assert any(e.startswith("radar frame unavailable") for e in st["errors"])
    assert any(e.startswith("socorro observation unavailable") for e in st["errors"])
    assert any(e.startswith("road closures (NMDOT) unavailable") for e in st["errors"])
    assert any(e.startswith("storm reports (NWS) unavailable") for e in st["errors"])
    assert st["alerts"] == {"ok": False, "stale": True, "count": 0}
    assert st["radar"]["ok"] is False and st["radar"]["ts"] is None
    for slug in slugs:
        assert site_status(st, slug) == {"meta_ok": False, "obs_ok": False, "forecast_ok": False,
                                         "hourly_ok": False, "alerts_at": 0, "alerts_near": 0,
                                         "maps_ok": True}
    html = read_page(cfg)
    assert "unavailable" in html and "No NWS alerts" in html
    assert "%d problem(s) during this run" % len(run["errors"]) in html


# ---- degradation of single steps -------------------------------------------------------------
def test_step_exceptions_become_status_dicts(two_sites, fake_http, monkeypatch):
    cfg = two_sites
    add_nws_routes(fake_http)

    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(alerts, "fetch_active", boom)
    monkeypatch.setattr(radar, "load_frame", boom)
    monkeypatch.setattr(nws, "observation", boom)
    monkeypatch.setattr(sun, "sun_summary", boom)
    monkeypatch.setattr(roads, "fetch_nm_roads", boom)
    monkeypatch.setattr(roads, "fetch_storm_reports", boom)
    run = mwp.run_once(cfg)

    assert run["alerts"]["ok"] is False and "RuntimeError: kaboom" in run["alerts"]["error"]
    assert run["alerts"]["alerts"] == [] and run["alerts"]["count_raw"] == 0
    assert run["radar"]["ok"] is False and "kaboom" in run["radar"]["error"] and run["radar"]["ts"] is None
    soc = run["sites"][0]
    assert soc["obs"]["ok"] is False and "kaboom" in soc["obs"]["error"]
    assert soc["obs"]["temp_f"] is None and soc["obs"]["cloud_layers"] == []
    assert soc["sun"] is None and run["night_default"] is False
    assert soc["forecast"]["ok"] and soc["hourly"]["ok"]           # the neighbours survived
    assert all(m["ok"] for m in soc["maps"].values())
    for what in ("alerts feed failed: RuntimeError: kaboom", "radar frame failed: RuntimeError: kaboom",
                 "socorro observation failed: RuntimeError: kaboom",
                 "socorro sun times failed: RuntimeError: kaboom",
                 "road closures (NMDOT) failed: RuntimeError: kaboom",
                 "storm reports (NWS) failed: RuntimeError: kaboom"):
        assert what in run["errors"]
    nm = run["roads"]["nmdot"]
    assert nm["ok"] is False and "kaboom" in nm["error"] and "events" not in nm
    assert nm["count_raw"] == 0 and nm["excluded"] == {} and nm["feed_time"] is None
    assert run["roads"]["lsr"]["ok"] is False and "reports" not in run["roads"]["lsr"]
    assert soc["roads"]["events"] == [] and soc["roads"]["reports"] == []
    assert_outputs(cfg, ["socorro", "lubbock"])
    assert_status_degraded(read_status(cfg))


def test_map_renderer_failure_and_whole_site_guard(two_sites, fake_http, monkeypatch):
    cfg = two_sites
    add_nws_routes(fake_http)
    monkeypatch.setattr(radar, "load_frame", _no_frame)

    def bad_render(self, *a, **k):
        raise ValueError("renderer bug")

    monkeypatch.setattr(radar.SiteMap, "render", bad_render)
    real_run_site = mwp._run_site

    def flaky_site(cfg_, cache, run, site, feed, frame_res, road_src=None):
        if site["slug"] == "lubbock":
            raise KeyError("lat")
        return real_run_site(cfg_, cache, run, site, feed, frame_res, road_src)

    monkeypatch.setattr(mwp, "_run_site", flaky_site)
    run = mwp.run_once(cfg)

    soc, lub = run["sites"]
    # no map rendered at all -> every candidate is listed NEAR
    assert ids(soc["alerts_at"]) == ["ffw", "watch"] and ids(soc["alerts_near"]) == ["adv"]
    assert soc["alerts_drawn_ids"] == []                   # nothing is shaded anywhere
    for m in soc["maps"].values():
        assert m["ok"] is False and "renderer bug" in m["error"]
    assert "socorro dark map failed: ValueError: renderer bug" in run["errors"]
    # the placeholder for the site whose code blew up
    assert lub["site"] == LUBBOCK and lub["sun"] is None and lub["alerts_at"] == []
    assert lub["alerts_drawn_ids"] == [] and "roads" not in lub
    assert lub["meta"]["tz"] == "UTC" and "KeyError" in lub["meta"]["error"]
    assert set(lub["maps"]) == {"dark", "light"} and not any(m["ok"] for m in lub["maps"].values())
    assert any(e.startswith("site lubbock failed: KeyError") for e in run["errors"])
    st = read_status(cfg)
    assert_status_degraded(st)
    assert st["sites"]["socorro"]["maps_ok"] is False
    assert site_status(st, "lubbock") == {"meta_ok": False, "obs_ok": False, "forecast_ok": False,
                                          "hourly_ok": False, "alerts_at": 0, "alerts_near": 0,
                                          "maps_ok": False}
    assert "renderer bug" in read_page(cfg)


def test_undrawn_alert_note_and_pillow_missing(two_sites, fake_http, monkeypatch):
    """A zone-based alert whose outline cannot be fetched is still AT the site (zone id)
    and the caption says it is not shaded; without Pillow no map is written but the page is."""
    cfg = two_sites
    cfg.sites = [dict(SOCORRO)]
    add_nws_routes(fake_http)
    fake_http.routes = [r for r in fake_http.routes if "NMZ220" not in r[0]]
    monkeypatch.setattr(radar, "deps_available", lambda: False)
    run = mwp.run_once(cfg)
    soc = run["sites"][0]
    assert ids(soc["alerts_at"]) == ["ffw", "watch"] and soc["alerts_at"][1]["geometry"] is None
    assert soc["map_notes"] == ["not shaded (no outline available): Flood Watch"]
    assert run["radar"] == {"ok": False, "stale": False, "error": "Pillow not installed",
                            "age_s": None, "ts": None, "url": None}
    for m in soc["maps"].values():
        assert m["ok"] is False and "Pillow" in m["error"]
    assert ids(soc["alerts_near"]) == ["adv"]              # no map: all candidates listed
    assert soc["alerts_drawn_ids"] == []
    assert not any("lcref_" in c or "openstreetmap" in c for c in fake_http.calls)
    assert os.path.exists(os.path.join(cfg.out_dir, "index.html"))
    assert not os.path.exists(os.path.join(cfg.out_dir, cfg.map_basename("socorro", "dark")))
    assert "not shaded" in read_page(cfg)


def test_night_default_follows_first_site(two_sites, fake_http, monkeypatch):
    cfg = two_sites
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    fake_http.add("openstreetmap", _png())
    real = sun.sun_summary

    def night_at_socorro(lat, lon, tzname, now_utc=None):
        d = real(lat, lon, tzname, now_utc)
        d["is_night"] = (lat == SOCORRO["lat"])
        return d

    monkeypatch.setattr(sun, "sun_summary", night_at_socorro)
    run = mwp.run_once(cfg)
    assert run["night_default"] is True and 'class="page night"' in read_page(cfg)
    cfg.sites.reverse()
    run = mwp.run_once(cfg)
    assert run["night_default"] is False and 'class="page night"' not in read_page(cfg)
    cfg.show_sun = False
    run = mwp.run_once(cfg)
    assert all(s["sun"] is None for s in run["sites"]) and run["night_default"] is False


# ---- lock, CLI and exit codes ------------------------------------------------------------------
def _argv(cfg, *extra):
    return ["--out", cfg.out_dir, "--cache", cfg.cache_dir] + list(extra)


def test_already_running_exits_zero(cfg, fake_http, caplog):
    caplog.set_level(logging.INFO, logger="weather")
    with http.run_lock(cfg.lock_file):
        with pytest.raises(http.AlreadyRunning):
            mwp.run_once(cfg)
        assert mwp.main(_argv(cfg)) == 0
    assert not os.path.exists(os.path.join(cfg.out_dir, "index.html"))
    assert any("already running" in r.getMessage() for r in caplog.records)
    assert not fake_http.calls


def test_main_skip_radar(cfg, fake_http, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="weather")
    monkeypatch.setattr(radar, "load_frame", lambda *a, **k: pytest.fail("load_frame must not run"))
    monkeypatch.setenv("WEATHER_SITES", "socorro|Socorro, NM|34.0584|-106.8914")
    fake_http.add("openstreetmap", _png())
    assert mwp.main(_argv(cfg, "--skip-radar", "--once")) == 0
    assert not any("lcref_" in c for c in fake_http.calls)
    assert_outputs(cfg, ["socorro"])
    st = read_status(cfg)
    assert st["page_written"] is True and st["radar"] == {"ok": False, "stale": False, "ts": None}
    assert st["sites"]["socorro"]["maps_ok"] is True
    assert not any("radar" in e for e in st["errors"])       # a deliberate skip is not an error
    assert "radar skipped (--skip-radar)" in read_page(cfg)
    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("done:")]
    assert len(summary) == 1 and "radar none" in summary[0] and "sites ok 0/1" in summary[0]


def test_main_radar_disabled_by_config(cfg, fake_http, monkeypatch):
    cfg.sites = [dict(SOCORRO)]
    monkeypatch.setattr(radar, "load_frame", lambda *a, **k: pytest.fail("load_frame must not run"))
    cfg.radar_enabled = False
    run = mwp.run_once(cfg)
    assert run["radar"]["error"] == "radar disabled (WEATHER_RADAR=0)" and run["radar"]["ok"] is False
    assert all(m["ok"] for m in run["sites"][0]["maps"].values())


def test_main_sites_verbose_and_summary(cfg, fake_http, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="weather")
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    fake_http.add("openstreetmap", _png())
    weather_log = logging.getLogger("weather")
    old_level = weather_log.level
    try:
        rc = mwp.main(_argv(cfg, "--verbose", "--sites", "x|X Town|34.5|-105.0; y|Y|33.0|-102.0"))
        assert rc == 0 and weather_log.level == logging.DEBUG
    finally:
        weather_log.setLevel(old_level)
    assert_outputs(cfg, ["x", "y"])
    st = read_status(cfg)
    assert set(st["sites"]) == {"x", "y"}
    assert "X Town" in read_page(cfg)
    summary = [r for r in caplog.records if r.getMessage().startswith("done:")]
    assert len(summary) == 1 and summary[0].name == "weather.main"
    msg = summary[0].getMessage()
    assert "sites ok 0/2" in msg and "alerts 0 (feed unavailable)" in msg and " s" in msg


def test_main_exit_codes(cfg, fake_http, tmp_path):
    assert mwp.main(_argv(cfg, "--sites", "not-a-site-spec")) == 2
    assert mwp.main(_argv(cfg, "--sites", "a|A|999|0")) == 2
    assert mwp.main(_argv(cfg, "--sites", "a|A|34|-106|Mars/Olympus_Mons")) == 2   # unknown tz
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    assert mwp.main(["--out", str(blocker / "out"), "--cache", cfg.cache_dir]) == 1
    assert not fake_http.calls


def test_parse_args_defaults():
    ns = mwp.parse_args([])
    assert ns.out is None and ns.cache is None and not ns.verbose and not ns.skip_radar
    assert ns.sites is None and not ns.once
    ns = mwp.parse_args(["--out", "o", "--cache", "c", "-v", "--skip-radar", "--sites", "s", "--once"])
    assert (ns.out, ns.cache, ns.verbose, ns.skip_radar, ns.sites, ns.once) == ("o", "c", True, True, "s", True)
    with pytest.raises(SystemExit):
        mwp.parse_args(["--bogus"])


# ---- what the legend may call "shaded" (alerts_drawn_ids) ---------------------------------------
def test_drawn_ids_leave_out_an_at_alert_without_outline(cfg, fake_http, monkeypatch):
    """The Flood Watch is AT Socorro through its zone id, but its zone outline cannot be
    fetched: it is listed on the page, yet it is not on either map, so it must not be in
    alerts_drawn_ids (the legend's "Shaded:" group) while the drawn FFW and advisory are."""
    cfg.sites = [dict(SOCORRO)]
    add_nws_routes(fake_http)
    fake_http.routes = [r for r in fake_http.routes if "NMZ220" not in r[0]]
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    run = mwp.run_once(cfg)
    soc = run["sites"][0]
    assert all(m["ok"] for m in soc["maps"].values())
    assert ids(soc["alerts_at"]) == ["ffw", "watch"] and ids(soc["alerts_near"]) == ["adv"]
    assert soc["alerts_drawn_ids"] == ["adv", "ffw"]
    assert soc["map_notes"] == ["not shaded (no outline available): Flood Watch"]
    assert "Listed, not shaded" in read_page(cfg)


def test_drawn_ids_count_only_maps_that_were_written(cfg, fake_http, monkeypatch):
    """A render that failed (no PNG on disk) shades nothing the viewer can see: its
    drawn_ids must neither make an alert NEAR nor appear in alerts_drawn_ids."""
    cfg.sites = [dict(SOCORRO)]
    add_nws_routes(fake_http)
    monkeypatch.setattr(radar, "load_frame", _no_frame)

    def half_render(self, radar_res, recs, out_path, notes=(), roads=None):
        if self.theme == "light":
            return {"ok": False, "error": "disk full", "path": out_path, "drawn_ids": ["adv"],
                    "radar_drawn": False}
        return {"ok": True, "error": None, "path": out_path, "drawn_ids": ["watch", "ffw"],
                "radar_drawn": False}

    monkeypatch.setattr(radar.SiteMap, "render", half_render)
    run = mwp.run_once(cfg)
    soc = run["sites"][0]
    assert soc["maps"]["dark"]["ok"] and not soc["maps"]["light"]["ok"]
    assert soc["alerts_drawn_ids"] == ["ffw", "watch"]
    assert soc["alerts_near"] == []                         # "adv" only hit the failed map
    assert "socorro light map not written: disk full" in run["errors"]


def test_site_placeholder_shape(cfg):
    entry = mwp._site_placeholder(cfg, dict(LUBBOCK, tz="America/Chicago"), "KeyError: 'lat'")
    assert entry["alerts_drawn_ids"] == [] and entry["alerts_at"] == [] and entry["sun"] is None
    assert entry["meta"]["tz"] == "America/Chicago" and entry["meta"]["ok"] is False
    assert mwp._site_placeholder(cfg, dict(LUBBOCK), "x")["meta"]["tz"] == "UTC"


# ---- time zones: NWS first, then the site's static zone, then UTC -------------------------------
def test_local_times_fall_back_to_the_site_time_zone(cfg, fake_http, monkeypatch):
    cfg.sites = [dict(SOCORRO, tz="America/Chicago"),      # wrong on purpose: NWS must win
                 dict(LUBBOCK, tz="America/Chicago"),      # no /points route: static zone
                 {"slug": "x", "name": "X", "lat": 34.5, "lon": -105.0}]   # no zone known
    add_nws_routes(fake_http)
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    seen = {}
    real_render = radar.SiteMap.render

    def spy(self, *a, **k):
        seen[self.slug] = self.site.get("tz")
        return real_render(self, *a, **k)

    monkeypatch.setattr(radar.SiteMap, "render", spy)
    run = mwp.run_once(cfg)
    soc, lub, x = run["sites"]
    assert soc["meta"]["ok"] and soc["meta"]["tz"] == "America/Denver"
    assert soc["sun"]["tz"] == "America/Denver"
    assert not lub["meta"]["ok"] and lub["meta"]["tz"] == "America/Chicago"
    assert lub["sun"]["tz"] == "America/Chicago"
    assert x["meta"]["tz"] == "UTC" and x["sun"]["tz"] == "UTC"
    assert seen == {"socorro": "America/Denver", "lubbock": "America/Chicago", "x": "UTC"}


# ---- the per-run network budget (weather.http.begin_run) -----------------------------------------
def test_each_run_arms_the_network_budget_before_any_fetch(cfg, fake_http, monkeypatch):
    cfg.sites = [dict(SOCORRO)]
    add_nws_routes(fake_http)
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    armed = []
    real_begin = http.begin_run

    def spy(c):
        armed.append((c, len(fake_http.calls)))
        return real_begin(c)

    monkeypatch.setattr(http, "begin_run", spy)
    mwp.run_once(cfg)
    after_first = len(fake_http.calls)
    mwp.run_once(cfg)
    assert after_first > 0
    assert armed == [(cfg, 0), (cfg, after_first)]          # once per run, before its fetches


def test_spent_budget_skips_the_network_and_still_writes_the_page(cfg, monkeypatch):
    """With the real http module (no fake_http) and a budget that is already spent when the
    first request is due, nothing may reach urlopen; every source degrades to "unavailable"
    and the page, status.json and both maps are still written."""
    attempts = []

    def no_network(*a, **k):
        attempts.append(a[:1])
        raise AssertionError("network access attempted")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    cfg.sites = [dict(SOCORRO, tz="America/Denver")]
    cfg.run_budget_s = 1e-6                     # set after validate(): spent at once
    run = mwp.run_once(cfg)
    assert attempts == []
    assert not any("network access attempted" in e for e in run["errors"])
    assert any("run budget exhausted" in e for e in run["errors"])
    assert not run["alerts"]["ok"] and not run["radar"]["ok"]
    soc = run["sites"][0]
    assert not soc["meta"]["ok"] and soc["meta"]["tz"] == "America/Denver"
    assert soc["sun"]["tz"] == "America/Denver"
    assert all(m["ok"] for m in soc["maps"].values())       # bare background, but written
    assert_outputs(cfg, ["socorro"])
    st = read_status(cfg)
    assert_status_degraded(st)
    # the cause is named, not just its symptoms
    assert run["network"]["exhausted"] is True and st["network"]["exhausted"] is True
    assert any(p.startswith("network: run budget of") for p in st["problems"])


# ---- New Mexico road closures -----------------------------------------------------------------
# Cut from the live feeds of 2026-09-23 (nmroads.json + rss.xml fetched 21:35Z, IEM storm
# reports for NM): the property values and RSS texts are verbatim, LineStrings are thinned
# to 4 vertices. One row = one NMDOT event: (id, JSON event_type, JSON name, road_names[0],
# JSON description, JSON creation/update date (NMDOT's JSON times run 6 h early for many
# events), JSON direction, JSON geometry, RSS title suffix, RSS pubDate, RSS geo lat/lon,
# osow route name/number, mile markers, osow direction, RSS description body).
ALBUQUERQUE = {"slug": "albuquerque", "name": "Albuquerque, NM", "lat": 35.0844, "lon": -106.6504,
               "tz": "America/Denver"}
NM41_ID = "dd4b80e9-93bb-4549-b38d-e545867c920c"            # crash closure, Albuquerque map
NM333_ID = "1140ecaf-4d65-4036-a4b7-7a09bbc64154"           # standing water, Albuquerque map
NM333_DUP_ID = "5f0c2e7a-1b3d-4c55-9e0e-2a7d1c4b9f11"       # constructed duplicate of NM 333
SOCORRO_AREA_ID = "829db998-2f7d-4517-becb-4130ebd67cbc"    # area-wide standing water, 'null-null'
NM94_ID = "b3f0d585-8422-471b-b0e9-de0b36a0f489"            # flood closure, on no test map
NM_EVENTS = [
    (NM41_ID, "Closure", "Closure", "NM-41",
     "Closure, NM 41 northbound and southbound from mile marker 17, 1 mile south of McIntosh "
     "to mile marker 19, 1 mile north McIntosh.",
     "2026-09-23T09:30:43-06:00", "northbound, southbound",
     [[-106.05339, 34.84876], [-106.05316, 34.85995], [-106.05293, 34.87081], [-106.05286, 34.87409]],
     "", "Wed, 23 Sep 2026 15:30:43 -0600", (34.86772, -106.05301), ("NM", "41"), (17, 19), "both",
     "Title: Closure, NM 41 northbound and southbound from mile marker 17, 1 mile south of "
     "McIntosh to mile marker 19, 1 mile north McIntosh.<br/>Description: Emergency services are "
     "on the scene and working to clear the crash scene as soon as possible.<br/>Post Date: "
     "2026-09-23 15:30:43.0<br/>Update Date: 2026-09-23 15:30:43.0<br/>Expiration Date: unknown<br/>"),
    (NM333_ID, "Difficult Driving Conditions", "Difficult Driving Conditions", "NM-333",
     "Difficult Driving Conditions, NM 333 eastbound and westbound from mile marker 21 to mile "
     "marker 20.", "2026-09-22T09:46:56-06:00", "eastbound, westbound",
     [[-106.18503, 35.05879], [-106.18059, 35.05699], [-106.17673, 35.05544], [-106.17104, 35.05312]],
     "", "Tue, 22 Sep 2026 15:46:56 -0600", (35.05658, -106.17956), ("NM", "333"), (21, 20), "both",
     "Title: Difficult Driving Conditions, NM 333 eastbound and westbound from mile marker 21 to "
     "mile marker 20.<br/>Description: Standing water on roadway. Use extreme caution."
     "<autodescriptiondelimiter>Expect delays and use caution while travelling through the area."
     "<br/>Post Date: 2026-09-22 15:46:56.0<br/>Update Date: 2026-09-22 15:46:56.0<br/>"
     "Expiration Date: unknown<br/>"),
    (SOCORRO_AREA_ID, "Difficult Driving Conditions", "Difficult Driving Conditions", "null-null",
     "Difficult Driving Conditions  exist throughout the Socorro - 41-57 area.",
     "2026-09-23T08:15:38-06:00", "", [-106.66111, 33.95906],
     "", "Wed, 23 Sep 2026 14:15:38 -0600", (33.95906, -106.66111), ("null", "null"), (0, 0), "NA",
     "Title: Difficult Driving Conditions  exist throughout the Socorro - 41-57 area.<br/>"
     "Description: Standing water on roadway. Use extreme caution.<autodescriptiondelimiter><br/>"
     "Post Date: 2026-09-23 14:15:38.0<br/>Update Date: 2026-09-23 14:15:38.0<br/>"
     "Expiration Date: unknown<br/>"),
    (NM94_ID, "Closure", "Closure", "NM-94",
     "Closure, NM 94 northbound and southbound from mile marker 14, Ledoux to mile marker 16, 2 "
     "miles north of Ledoux.  Roadway  closed.", "2026-09-22T10:59:16-06:00", "northbound, southbound",
     [[-105.35828, 35.91939], [-105.36045, 35.92446], [-105.35419, 35.93486], [-105.34979, 35.94441]],
     "", "Tue, 22 Sep 2026 16:59:16 -0600", (35.93029, -105.35666), ("NM", "94"), (14, 16), "both",
     "Title: Closure, NM 94 northbound and southbound from mile marker 14, Ledoux to mile marker "
     "16, 2 miles north of Ledoux.  Roadway  closed.<br/>Description: Standing water on roadway. "
     "Use extreme caution. Cloudy conditions exist.<autodescriptiondelimiter>Roadway is closed due "
     "to flooding.  <br/>Post Date: 2026-09-22 16:59:16.0<br/>Update Date: 2026-09-22 16:59:16.0"
     "<br/>Expiration Date: unknown<br/>"),
    ("3051fd7d-688d-49f9-80b6-5f16c759fc28", "work-zone", "Construction Closure", "NM-118",
     "Construction Closure, NM 118 eastbound and westbound from mile marker 30, Church Rock to "
     "mile marker 31, 1 miles east of Church Rock.", "2026-03-18T13:18:28-06:00", "eastbound, westbound",
     [[-108.60015, 35.52971], [-108.59587, 35.52864], [-108.58744, 35.52609], [-108.58363, 35.52503]],
     "Construction Closure, NM 118 eastbound and westbound from mile marker 30, Church Rock to "
     "mile marker 31, 1 miles east of Church Rock.", "Wed, 18 Mar 2026 19:18:28 -0600",
     (35.52743, -108.59194), ("NM", "118"), (30, 31), "both",
     "Title: Construction Closure, NM 118 eastbound and westbound from mile marker 30, Church Rock "
     "to mile marker 31, 1 miles east of Church Rock.<br/>Description: NM 118 is closed at mile "
     "marker 29.5-31 (NM 566 Intersection to Navajo Blvd) due to bridge replacement project. "
     "Please use I-40 as a detour. <br/>Post Date: 2026-03-18 19:18:28.0<br/>Update Date: "
     "2026-08-28 07:42:16.257<br/>Expiration Date: unknown<br/>"),
    ("11fc0557-e294-44c9-bdff-042f07ecc605", "work-zone", "Roadwork", "null-null",
     "Roadwork,  northbound and southbound.", "2026-06-16T09:30:10-06:00", "northbound, southbound",
     [-106.65698, 35.23675],
     "", "Tue, 16 Jun 2026 09:30:10 -0600", (35.23675, -106.65698), ("null", "null"), (0, 0), "both",
     "Title: Roadwork,  northbound and southbound.<br/>Description: Route=NM528 \nDate=6/29 - "
     "10/2 \nTime=24/7 SB Right Lane Restriction through 8/28\n24/7 restrictions along Barbara Lp "
     "with left or right decelerations lanes restricted along NM528---Detours Provided<br/>Post "
     "Date: Tue Jun 16 09:30:10 MDT 2026<br/>Update Date: 2026-08-12 10:04:12.0<br/>Expiration "
     "Date: Fri Oct 02 15:00:00 MDT 2026"),
    ("cae2d585-5c02-4680-bcfc-0f9e858451c0", "Alert", "Alert", "US-380",
     "Alert, US 380 eastbound and westbound at mile marker 43, 13 miles east of Bingham.",
     "2021-06-30T18:00:00-06:00", "eastbound, westbound", [-106.18536, 33.80001],
     "Alert, US 380 eastbound and westbound at mile marker 43, 13 miles east of Bingham.",
     "Thu, 01 Jul 2021 00:00:00 -0600", (33.80001, -106.18536), ("US", "380"), (43, 0), "both",
     "Title: Alert, US 380 eastbound and westbound at mile marker 43, 13 miles east of Bingham."
     "<br/>Description: White Sands Missile Range periodically has missile firings over US 70 "
     "and/or US 380. During the firings the road or roads will be temporarily closed. Please call "
     "575-678-1178 or 575-678-2222  for daily information on possible closure dates and times for "
     "firings. THIS IS A PERMANENT ALERT. <br/>Post Date: 2021-07-01 00:00:00.0<br/>Update Date: "
     "2026-09-22 06:49:12.492<br/>Expiration Date: unknown<br/>"),
    ("e1dc21ee-fa51-4aed-83c1-4d15ebd6bd10", "Alert", "Alert", "null-null", "Alert, .",
     "2026-08-03T11:23:30-06:00", "", [-105.84445, 35.0111],
     "", "Mon, 03 Aug 2026 17:23:30 -0600", (35.0111, -105.84445), ("null", "null"), (0, 0), "NA",
     "Title: Alert, .<br/>Description: The Rattlesnake Draw Rest area is closed for construction "
     "of a new facility. Work is expected to be completed by Fall of 2026. Rest Room facilities "
     "can be found in either Moriarty or Clines Corners while this rest area is closed.<br/>Post "
     "Date: 2026-08-03 17:23:30.0<br/>Update Date: 2026-08-03 17:23:30.0<br/>Expiration Date: "
     "unknown<br/>"),
]
# NMDOT really lists some events twice under two ids (the US 60 mm 331 roadwork pair is in the
# live feed); the NM 333 pair below copies that pattern onto an item the page lists.
_US60_BODY = (
    "Title: Roadwork, US 60 eastbound and westbound from mile marker 331, 3 miles east of Ft "
    "Sumner to mile marker 342 Taiban.<br/>Description: Mill and fill operations have begun on US "
    "60/84 from milepost 331 to milepost 342 (Taiban). Motorists should expect single-lane "
    "closures with traffic control.<br/>Post Date: 2026-09-09 15:11:41.0<br/>Update Date: "
    "2026-09-22 09:27:11.512<br/>Expiration Date: unknown<br/>")
for _fid in ("15c417d3-c9a4-499d-969a-8c68d2162ccc", "0c4a2d89-62e7-4d10-9102-3d706e11cb76"):
    NM_EVENTS.append(
        (_fid, "work-zone", "Roadwork", "US-60",
         "Roadwork, US 60 eastbound and westbound from mile marker 331, 3 miles east of Ft Sumner "
         "to mile marker 342 Taiban.", "2026-09-09T09:11:41-06:00", "eastbound, westbound",
         [[-104.19037, 34.45385], [-104.13579, 34.4463], [-104.04277, 34.43216], [-104.00507, 34.44033]],
         "", "Wed, 09 Sep 2026 15:11:41 -0600", (34.43352, -104.0625), ("US", "60"), (331, 342),
         "both", _US60_BODY))
NM_EVENTS.append((NM333_DUP_ID,) + NM_EVENTS[1][1:])


def nmroads_doc(events=None):
    feats = []
    for (fid, etype, name, road, desc, created, direction, coords, *_rest) in (events or NM_EVENTS):
        gtype = "Point" if isinstance(coords[0], float) else "LineString"
        core = {"data_source_id": fid, "event_type": etype, "road_names": [road],
                "description": desc, "creation_date": created, "update_date": created,
                "direction": direction, "vehicle_impact": "unknown", "name": name, "District": "1"}
        feats.append({"id": fid, "type": "Feature",
                      "properties": {"start_date": created, "end_date": "2099-12-31T00:00:00-07:00",
                                     "core_details": core},
                      "geometry": {"type": gtype, "coordinates": coords}})
    return {"feed_info": {"update_date": "2020-06-18T15:00:00Z", "publisher": "NMDOT",
                          "contact_name": "NMDOT", "contact_email": "nmdot@nmdot.gov",
                          "update_frequency": 60, "version": "4.2",
                          "license": "https://creativecommons.org/publicdomain/zero/1.0/"},
            "type": "FeatureCollection", "features": feats}


def nmroads_rss(events=None):
    items = []
    for (fid, _et, name, _road, _desc, _created, _dir, _coords, title_suffix, pub, (lat, lon),
         (rname, rnum), (mm0, mm1), odir, body) in (events or NM_EVENTS):
        items.append(
            "<item>\n<title>%s - %s</title>\n<pubDate>%s</pubDate>\n"
            "<link>http://nmroads.com?lon=%s&amp;lat=%s&amp;zoom=10</link>\n"
            "<description><![CDATA[%s]]></description>\n<category>%s</category>\n"
            '<guid isPermaLink="false">%s</guid>\n<geo:lat>%s</geo:lat><geo:long>%s</geo:long>\n'
            "<osow:routeName>%s</osow:routeName>\n<osow:routeNumber>%s</osow:routeNumber>\n"
            "<osow:mileMarkerFrom>%s</osow:mileMarkerFrom>\n<osow:mileMarkerTo>%s</osow:mileMarkerTo>\n"
            "<osow:direction>%s</osow:direction>\n<osow:height>0</osow:height>\n"
            "<osow:width>0</osow:width>\n<osow:length>0</osow:length>\n<osow:weight>0</osow:weight>\n"
            "</item>\n" % (name, title_suffix, pub, lon, lat, body, name, fid, lat, lon, rname, rnum,
                           mm0, mm1, odir))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0" '
            'xmlns:osow="http://nmroads.com/osow/" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#" '
            'xmlns:atom="http://www.w3.org/2005/Atom">\n<channel>\n'
            "<title>New Mexico Traffic Conditions</title>\n<link>http://nmroads.com</link>\n"
            "<description>Traffic incident and event information for all of New Mexico</description>\n"
            "<language>en-us</language>\n"
            '<atom:link href="http://nmroads.com/rss.xml" rel="self" type="application/rss+xml" />\n'
            "<pubDate>Wed, 23 Sep 2026 15:34:00 -0600</pubDate>\n"
            "<lastBuildDate>Wed, 23 Sep 2026 15:34:00 -0600</lastBuildDate>\n"
            + "".join(items) + "</channel>\n</rss>\n")


# IEM storm reports: (feature id, hours before now, wfo, typetext, state, county, city, source,
# remark, lon, lat, product id). Times are relative to the real clock so the reports stay inside
# the 24 h window whenever the tests run.
LSR_ROWS = [
    ("8", 3, "ABQ", "FLASH FLOOD", "NM", "Socorro", "Las Nutrias", "Dept of Highways",
     "NMDOT reports NM State Highway 304 closed at Mile Marker 11 (Las Nutrias) due to standing water.",
     -106.77, 34.48, "202609222339-KABQ-NWUS55-LSRABQ"),
    ("3", 5, "ABQ", "FLASH FLOOD", "NM", "Lincoln", "13 W Oscuro", "Other Federal",
     "Range Route 8, 9 and 12 impassable due to water over roads.", -106.28, 33.48,
     "202609222141-KABQ-NWUS55-LSRABQ"),
    ("9", 2, "ABQ", "FLASH FLOOD", "NM", "Quay", "2 N House", "Broadcast Media",
     "Video shows water over NM 252 north of House, NM from Alamosa Creek.", -103.9, 34.67,
     "202609230139-KABQ-NWUS55-LSRABQ"),
    ("2", 6, "ABQ", "FLASH FLOOD", "NM", "Mora", "Monte Aplanado", "Emergency Mngr",     # no road
     "Water rescue due to flooding of a home. Location and time are estimated.", -105.39, 35.95,
     "202609222228-KABQ-NWUS55-LSRABQ"),
    ("38", 1, "EPZ", "FLASH FLOOD", "TX", "El Paso", "El Paso", "Emergency Mngr",        # Texas
     "Following streets closed: Doniphan/Racetrack, Brown/Murchison-also a water rescue at this site.",
     -106.49, 31.76, "202609231248-KEPZ-NWUS54-LSREPZ"),
]


def lsr_doc():
    now = util.utcnow()
    feats = []
    for fid, hours, wfo, typetext, st, county, city, source, remark, lon, lat, product in LSR_ROWS:
        valid = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:00Z")
        feats.append({"id": fid, "type": "Feature", "properties": {
            "wfo": wfo, "type": "F", "magf": None, "county": county, "typetext": typetext,
            "state": st, "remark": remark, "city": city, "source": source, "unit": None,
            "valid": valid, "lon": lon, "lat": lat, "qualifier": None, "product_id": product,
            "st": st, "magnitude": ""}, "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    return {"type": "FeatureCollection", "features": feats}


def add_road_routes(fake_http):
    fake_http.add("nmroads.com/nmroads.json", nmroads_doc())
    fake_http.add("nmroads.com/rss.xml", nmroads_rss())
    fake_http.add("/geojson/lsr.geojson", lsr_doc())


@pytest.fixture
def road_sites(cfg, fake_http, monkeypatch):
    """Socorro, Albuquerque and Lubbock (outside New Mexico) with road fixtures, map tiles and
    no radar frame; the NWS products are left unavailable (they do not matter here)."""
    cfg.sites = [dict(SOCORRO, tz="America/Denver"), dict(ALBUQUERQUE),
                 dict(LUBBOCK, tz="America/Chicago")]
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    fake_http.add("openstreetmap", _png())
    add_road_routes(fake_http)
    return cfg


def render_spy(monkeypatch):
    """Record every SiteMap.render call as (slug, theme, kwargs) and let it run."""
    calls = []
    real = radar.SiteMap.render

    def spy(self, *a, **k):
        calls.append((self.slug, self.theme, dict(k)))
        return real(self, *a, **k)

    monkeypatch.setattr(radar.SiteMap, "render", spy)
    return calls


def _map_pixels(cfg, slug, theme):
    with Image.open(os.path.join(cfg.out_dir, cfg.map_basename(slug, theme))) as im:
        return im.convert("RGB")


def _changed_near(a, b, x, y, r=16):
    """Pixels that differ between two same-size images inside the (2r+1)^2 box around (x, y)."""
    n = 0
    for yy in range(max(0, int(y) - r), min(a.size[1], int(y) + r + 1)):
        for xx in range(max(0, int(x) - r), min(a.size[0], int(x) + r + 1)):
            n += a.getpixel((xx, yy)) != b.getpixel((xx, yy))
    return n


def by_site(run):
    return {s["site"]["slug"]: s for s in run["sites"]}


def test_roads_end_to_end(road_sites, fake_http, monkeypatch):
    """NMDOT feed + RSS + storm reports -> run["roads"], each site's road items (emergencies
    only, New Mexico only, deduplicated), both theme maps drawn with them, status.json and
    the page. A second run with WEATHER_ROADS off shows what the road layer drew."""
    cfg = road_sites
    spy = render_spy(monkeypatch)
    run = mwp.run_once(cfg)

    # one fetch of each road source per run
    for frag in ("nmroads.com/nmroads.json", "nmroads.com/rss.xml",
                 "lsr.geojson?hours=24&states=NM"):
        assert sum(1 for c in fake_http.calls if frag in c) == 1, frag

    # run["roads"]: status dicts without their item lists
    assert set(run["roads"]) == {"nmdot", "lsr"}
    nm, lsr = run["roads"]["nmdot"], run["roads"]["lsr"]
    assert nm["ok"] is True and nm["stale"] is False and nm["error"] is None
    assert "events" not in nm and nm["count_raw"] == len(NM_EVENTS)
    assert nm["count_region"] == len(NM_EVENTS)          # every fixture event lies in NM
    assert nm["feed_time"] is None or (isinstance(nm["feed_time"], datetime)
                                       and nm["feed_time"].tzinfo is not None)
    assert nm["excluded"].get("Roadwork", 0) >= 1 and sum(nm["excluded"].values()) >= 5
    assert lsr["ok"] is True and lsr["stale"] is False and "reports" not in lsr

    sites = by_site(run)
    # Albuquerque: the crash closure first, then NM 333's standing water (listed once
    # although NMDOT carries it twice); no roadwork, rest-area or lane items
    abq = sites["albuquerque"]["roads"]
    assert set(abq) == {"covers_nm", "events", "reports", "drawn_ids"} and abq["covers_nm"] is True
    assert [(e["kind"], e["route"]) for e in abq["events"]] == [("closure", "NM 41"), ("water", "NM 333")]
    closure, water = abq["events"]
    assert closure["id"] == NM41_ID and closure["cause_known"] is True and "crash" in closure["cause"]
    assert "<" not in closure["cause"] and "autodescriptiondelimiter" not in water["cause"]
    assert water["id"] in (NM333_ID, NM333_DUP_ID) and "Standing water" in water["cause"]
    assert all(e["source"] == "NMDOT" and e["link"] == "https://nmroads.com/" for e in abq["events"])
    assert [r["kind"] for r in abq["reports"]] == ["report"]
    assert "Highway 304" in abq["reports"][0]["remark"]
    # Socorro: the area-wide standing water (a 'null-null' point, listed but not drawn) and
    # the NM 304 storm report; the missile-range alert, the WSMR "Range Route" report and the
    # rescue/Texas reports are not listed
    soc = sites["socorro"]["roads"]
    assert soc["covers_nm"] is True
    assert [(e["id"], e["kind"]) for e in soc["events"]] == [(SOCORRO_AREA_ID, "water")]
    assert soc["events"][0]["route"] is None and soc["events"][0]["area_wide"] is True
    assert ["Highway 304" in r["remark"] for r in soc["reports"]] == [True]
    assert all(r["source"] == "NWS ABQ" for r in soc["reports"])
    # Lubbock's map does not reach New Mexico
    assert sites["lubbock"]["roads"] == {"covers_nm": False, "events": [], "reports": [],
                                         "drawn_ids": []}
    listed = [e["id"] for s in sites.values() for e in s["roads"]["events"]]
    assert NM94_ID not in listed                        # closed by a flood, but on no test map

    # every road item of a site is on its written maps (an area-wide one never is), and the
    # renderer got the site's items
    for slug in ("socorro", "albuquerque"):
        r = sites[slug]["roads"]
        assert r["drawn_ids"] == sorted([e["id"] for e in r["events"] if not e["area_wide"]]
                                        + [x["id"] for x in r["reports"]], key=str)
    assert len(spy) == 6
    for slug, theme, kw in spy:
        want = {k: v for k, v in sites[slug]["roads"].items() if k != "drawn_ids"}
        assert kw["roads"] == want, (slug, theme)
    assert_outputs(cfg, ["socorro", "albuquerque", "lubbock"])

    st = read_status(cfg)
    assert st["roads"]["nmdot"]["ok"] is True and st["roads"]["lsr"]["ok"] is True
    assert st["sites"]["albuquerque"]["roads_events"] == 2
    assert st["sites"]["albuquerque"]["roads_reports"] == 1
    assert st["sites"]["socorro"]["roads_events"] == 1 and st["sites"]["socorro"]["roads_reports"] == 1
    assert st["sites"]["lubbock"]["roads_events"] == 0
    assert not any("road" in p.lower() or "nmdot" in p.lower() for p in st["problems"])
    html = read_page(cfg)
    for text in ("Road closures (New Mexico, emergencies only)", "ROAD CLOSED", "WATER ON ROAD",
                 "outside New Mexico", "Highway 304", "may have reopened", "https://nmroads.com/"):
        assert text in html, text
    for text in ("Rattlesnake Draw", "Missile Range", "Mill and fill", "Doniphan", "Ledoux",
                 "Water rescue due to flooding of a home"):
        assert text not in html, text

    # the same run without the road layer: the maps differ where the items are, and a map
    # with no road items (Lubbock) is pixel-identical
    with_roads = {(s, t): _map_pixels(cfg, s, t) for s in sites for t in ("dark", "light")}
    cfg.roads_enabled = False
    run2 = mwp.run_once(cfg)
    assert "roads" not in run2 and all("roads" not in s for s in run2["sites"])
    for theme in ("dark", "light"):
        frame = geo.MapFrame(ALBUQUERQUE["lat"], ALBUQUERQUE["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
        x, y = frame.lonlat_to_px(-106.053, 34.861)          # NM 41 near McIntosh
        assert _changed_near(with_roads[("albuquerque", theme)], _map_pixels(cfg, "albuquerque", theme), x, y) > 20
        frame = geo.MapFrame(SOCORRO["lat"], SOCORRO["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
        x, y = frame.lonlat_to_px(-106.77, 34.48)             # the NM 304 storm report
        assert _changed_near(with_roads[("socorro", theme)], _map_pixels(cfg, "socorro", theme), x, y) > 20
        x, y = frame.lonlat_to_px(-106.66111, 33.95906)       # the area-wide point: no symbol
        assert _changed_near(with_roads[("socorro", theme)], _map_pixels(cfg, "socorro", theme), x, y, r=6) == 0
        assert list(with_roads[("lubbock", theme)].getdata()) == list(_map_pixels(cfg, "lubbock", theme).getdata())


def test_roads_last_good_copy_survives_a_dead_feed(road_sites, fake_http):
    cfg = road_sites
    first = by_site(mwp.run_once(cfg))
    fake_http.routes = [r for r in fake_http.routes if not _is_road_url(r[0])]
    fake_http.fail("nmroads.com")
    fake_http.fail("lsr.geojson")
    run = mwp.run_once(cfg)
    nm = run["roads"]["nmdot"]
    assert nm["ok"] is True                              # served from the cache
    assert by_site(run)["albuquerque"]["roads"]["events"] == first["albuquerque"]["roads"]["events"]
    if nm["stale"]:
        assert any(e.startswith("road closures (NMDOT) stale") for e in run["errors"])


def test_roads_sources_down_page_still_written(road_sites, fake_http):
    cfg = road_sites
    fake_http.routes = [r for r in fake_http.routes if not _is_road_url(r[0])]
    fake_http.fail("nmroads.com")
    fake_http.fail("lsr.geojson")
    run = mwp.run_once(cfg)
    nm, lsr = run["roads"]["nmdot"], run["roads"]["lsr"]
    assert nm["ok"] is False and nm["error"] and "events" not in nm
    assert lsr["ok"] is False and lsr["error"]
    assert any(e.startswith("road closures (NMDOT) unavailable") for e in run["errors"])
    assert any(e.startswith("storm reports (NWS) unavailable") for e in run["errors"])
    for slug, s in by_site(run).items():
        assert s["roads"]["events"] == [] and s["roads"]["reports"] == []
        assert s["roads"]["covers_nm"] is (slug != "lubbock")
        assert all(m["ok"] for m in s["maps"].values())
    assert_outputs(cfg, ["socorro", "albuquerque", "lubbock"])
    st = read_status(cfg)
    assert_status_degraded(st)
    assert st["roads"]["nmdot"]["ok"] is False
    assert any("road" in p.lower() or "nmdot" in p.lower() for p in st["problems"])


def _ev(eid, kind="closure", lon=-106.8, lat=34.0):
    """A RoadEvent in the DESIGN.md shape (for tests that stub weather.roads)."""
    now = util.utcnow()
    return {"id": eid, "kind": kind, "category": "Closure", "title": "Closure, NM 1",
            "route": "NM 1", "mm_from": 1.0, "mm_to": 2.0, "direction": "both",
            "cause": "Roadway is closed due to flooding.", "cause_known": True, "updated": now,
            "posted": now, "geometry": {"type": "LineString", "coordinates": [[lon, lat], [lon + 0.05, lat]]},
            "bbox": (lon, lat, lon + 0.05, lat), "source": "NMDOT", "link": "https://nmroads.com/"}


def _stub_roads(monkeypatch, events, reports=(), view=None):
    nm = {"ok": True, "stale": False, "error": None, "age_s": 0.0, "events": list(events),
          "count_raw": len(events), "count_region": len(events), "feed_time": None, "excluded": {}}
    lsr = {"ok": True, "stale": False, "error": None, "age_s": 0.0, "reports": list(reports)}
    monkeypatch.setattr(roads, "fetch_nm_roads", lambda cfg, cache: dict(nm))
    monkeypatch.setattr(roads, "fetch_storm_reports", lambda cfg, cache: dict(lsr))
    if view is not None:
        monkeypatch.setattr(roads, "site_roads", lambda nm_, lsr_, frame: dict(view))


def test_road_drawn_ids_come_from_written_maps_only(cfg, fake_http, monkeypatch):
    cfg.sites = [dict(SOCORRO, tz="America/Denver")]
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    e1, e2 = _ev("e1"), _ev("e2", "water")
    view = {"covers_nm": True, "events": [e1, e2], "reports": []}
    _stub_roads(monkeypatch, [e1, e2], view=view)
    seen = []

    def fake_render(self, radar_res, recs, out_path, notes=(), roads=None):
        seen.append((self.theme, roads))
        if self.theme == "light":           # a failed map shows nothing: its ids do not count
            return {"ok": False, "error": "disk full", "path": out_path, "drawn_ids": [],
                    "radar_drawn": False, "roads_drawn_ids": ["e2"]}
        return {"ok": True, "error": None, "path": out_path, "drawn_ids": [],
                "radar_drawn": False, "roads_drawn_ids": ["e1", None]}

    monkeypatch.setattr(radar.SiteMap, "render", fake_render)
    run = mwp.run_once(cfg)
    soc = run["sites"][0]
    assert soc["roads"] == {"covers_nm": True, "events": [e1, e2], "reports": [], "drawn_ids": ["e1"]}
    assert [t for t, _ in seen] == ["dark", "light"] and all(r == view for _, r in seen)
    assert run["roads"]["nmdot"] == {"ok": True, "stale": False, "error": None, "age_s": 0.0,
                                     "count_raw": 2, "count_region": 2, "feed_time": None,
                                     "excluded": {}}

    # a renderer that reports garbage road ids: ignored, never a crash
    def odd_render(self, radar_res, recs, out_path, notes=(), roads=None):
        return {"ok": True, "error": None, "path": out_path, "drawn_ids": [],
                "radar_drawn": False, "roads_drawn_ids": "e1"}

    monkeypatch.setattr(radar.SiteMap, "render", odd_render)
    assert mwp.run_once(cfg)["sites"][0]["roads"]["drawn_ids"] == []


def test_road_overlay_failure_redraws_the_map_without_it(cfg, fake_http, monkeypatch):
    cfg.sites = [dict(SOCORRO, tz="America/Denver")]
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    fake_http.add("openstreetmap", _png())
    _stub_roads(monkeypatch, [_ev("e1")], view={"covers_nm": True, "events": [_ev("e1")], "reports": []})
    real = radar.SiteMap.render
    calls = []

    def fragile(self, *a, **k):
        calls.append((self.theme, "roads" in k))
        if "roads" in k:
            raise RuntimeError("road drawing bug")
        return real(self, *a, **k)

    monkeypatch.setattr(radar.SiteMap, "render", fragile)
    run = mwp.run_once(cfg)
    soc = run["sites"][0]
    assert calls == [("dark", True), ("dark", False), ("light", True), ("light", False)]
    assert all(m["ok"] for m in soc["maps"].values())            # radar/alert map survived
    assert soc["roads"]["drawn_ids"] == [] and [e["id"] for e in soc["roads"]["events"]] == ["e1"]
    for theme in ("dark", "light"):
        assert ("socorro %s map drawn without road closures: RuntimeError: road drawing bug" % theme
                in run["errors"])
    assert_outputs(cfg, ["socorro"])

    # a road-layer failure the renderer handled itself (map written, reported in its error
    # text) is listed as well, so status.json does not call the run healthy
    def handled(self, radar_res, recs, out_path, notes=(), roads=None):
        return {"ok": True, "path": out_path, "drawn_ids": [], "radar_drawn": False,
                "roads_drawn_ids": [],
                "error": "radar unavailable: IEM down; road layer failed: bad vertex"}

    monkeypatch.setattr(radar.SiteMap, "render", handled)
    run = mwp.run_once(cfg)
    assert "socorro dark map: road layer failed: bad vertex" in run["errors"]
    assert "socorro light map: road layer failed: bad vertex" in run["errors"]
    assert_status_degraded(read_status(cfg))


def test_road_steps_that_crash_or_return_garbage(cfg, fake_http, monkeypatch):
    cfg.sites = [dict(SOCORRO, tz="America/Denver"), dict(ALBUQUERQUE)]
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    fake_http.add("openstreetmap", _png())
    garbage = {"ok": True, "stale": False, "error": None, "age_s": 0.0, "events": "garbage"}
    monkeypatch.setattr(roads, "fetch_nm_roads", lambda cfg, cache: dict(garbage))
    monkeypatch.setattr(roads, "fetch_storm_reports", lambda cfg, cache: ["not", "a", "dict"])
    real_site_roads = roads.site_roads

    def flaky(nm, lsr, frame):
        assert nm["events"] == [] and lsr["reports"] == []       # sanitised before use
        if abs(frame.lat0 - SOCORRO["lat"]) < 1e-6:
            raise ValueError("bad geometry")
        return real_site_roads(nm, lsr, frame)

    monkeypatch.setattr(roads, "site_roads", flaky)
    calls = render_spy(monkeypatch)
    run = mwp.run_once(cfg)
    soc, abq = run["sites"]
    assert "roads" not in soc                                     # unknown: no block, no "none"
    assert abq["roads"]["events"] == [] and abq["roads"]["covers_nm"] is True
    assert "socorro road closures failed: ValueError: bad geometry" in run["errors"]
    assert "storm reports (NWS) returned list instead of a dict" in " ".join(run["errors"])
    assert run["roads"]["lsr"]["ok"] is False
    assert [("roads" in k) for slug, _t, k in calls] == [False, False, True, True]
    assert all(m["ok"] for s in run["sites"] for m in s["maps"].values())
    assert_outputs(cfg, ["socorro", "albuquerque"])


def test_roads_partial_feed_is_reported(cfg, fake_http, monkeypatch):
    """rss.xml down, nmroads.json fine: the module still returns closures (ok) but says what
    is missing; the run lists it as a problem instead of passing it over silently."""
    cfg.sites = [dict(SOCORRO, tz="America/Denver")]
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    _stub_roads(monkeypatch, [])
    monkeypatch.setattr(roads, "fetch_nm_roads", lambda cfg, cache: {
        "ok": True, "stale": False, "error": "rss.xml unavailable: HTTP 503", "age_s": 0.0,
        "events": [], "count_raw": 0, "count_region": 0, "feed_time": None, "excluded": {}})
    run = mwp.run_once(cfg)
    assert "road closures (NMDOT) incomplete: rss.xml unavailable: HTTP 503" in run["errors"]


def test_roads_disabled_fetches_nothing(cfg, fake_http, monkeypatch, caplog):
    """WEATHER_ROADS=0: no road request at all, no roads in the run dict or the site entries,
    the renderer is called exactly as before (no roads= argument)."""
    caplog.set_level(logging.INFO, logger="weather")
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    fake_http.add("openstreetmap", _png())
    add_road_routes(fake_http)                   # would answer, but must not be asked
    monkeypatch.setenv("WEATHER_ROADS", "0")
    monkeypatch.setenv("WEATHER_SITES", "socorro|Socorro, NM|34.0584|-106.8914|America/Denver")
    c = Config.from_env(out_dir=cfg.out_dir, cache_dir=cfg.cache_dir, map_px=240, tile_zoom=8)
    assert c.roads_enabled is False
    calls = render_spy(monkeypatch)
    run = mwp.run_once(c)
    assert "roads" not in run and "roads" not in run["sites"][0]
    assert calls and all("roads" not in k for _s, _t, k in calls)
    assert not any(_is_road_url(c_) for c_ in fake_http.calls)
    assert not any("road" in e for e in run["errors"])
    # the same through the CLI
    fake_http.calls.clear()
    assert mwp.main(_argv(cfg)) == 0
    assert fake_http.calls and not any(_is_road_url(c_) for c_ in fake_http.calls)
    st = read_status(cfg)
    assert not any("road" in p.lower() or "nmdot" in p.lower() for p in st["problems"])
    assert "Road closures (New Mexico" not in read_page(cfg)
    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("done:")]
    assert summary and all("roads off" in m for m in summary)


def test_storm_reports_and_water_items_can_be_switched_off(road_sites, fake_http):
    """WEATHER_LSR=0: no storm-report request and no reports, without making the run
    degraded; WEATHER_ROADS_WATER=0: only real closures are listed."""
    cfg = road_sites
    cfg.lsr_enabled = False
    cfg.roads_water = False
    run = mwp.run_once(cfg)
    assert not any("lsr.geojson" in c for c in fake_http.calls)
    assert any("nmroads.com/nmroads.json" in c for c in fake_http.calls)
    assert run["roads"]["lsr"] == {"ok": True, "stale": False, "error": None, "age_s": None,
                                   "disabled": True}
    sites = by_site(run)
    assert [e["id"] for e in sites["albuquerque"]["roads"]["events"]] == [NM41_ID]
    assert sites["socorro"]["roads"]["events"] == []
    assert all(s["roads"]["reports"] == [] for s in sites.values())
    assert not any("storm reports" in e for e in run["errors"])


# ---- config: road-closure settings -----------------------------------------------------------------
def test_config_road_defaults_and_environment(cfg, monkeypatch):
    c = Config.from_env()
    assert (c.roads_enabled, c.roads_water, c.lsr_enabled) == (True, True, True)
    assert (c.lsr_hours, c.roads_max_stale, c.roads_old_days) == (24, 3600, 3.0)
    assert c.nmroads_json_url == "https://nmroads.com/nmroads.json"
    assert c.nmroads_rss_url == "https://nmroads.com/rss.xml"
    assert c.lsr_url == "https://mesonet.agron.iastate.edu/geojson/lsr.geojson?hours={hours}&states=NM"
    assert c.lsr_feed_url == "https://mesonet.agron.iastate.edu/geojson/lsr.geojson?hours=24&states=NM"
    env = {"WEATHER_ROADS": "off", "WEATHER_ROADS_WATER": "no", "WEATHER_LSR": "0",
           "WEATHER_LSR_HOURS": "48", "WEATHER_ROADS_MAX_STALE": "0",
           "WEATHER_ROADS_OLD_DAYS": "1.5",
           "WEATHER_NMROADS_JSON_URL": "http://mirror.example:8080/nm/nmroads.json",
           "WEATHER_NMROADS_RSS_URL": "https://mirror.example/nm/rss.xml",
           "WEATHER_LSR_URL": "https://mirror.example/lsr?h={hours}&states=NM"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    c = Config.from_env()
    assert (c.roads_enabled, c.roads_water, c.lsr_enabled) == (False, False, False)
    assert (c.lsr_hours, c.roads_max_stale, c.roads_old_days) == (48, 0, 1.5)
    assert c.nmroads_json_url == env["WEATHER_NMROADS_JSON_URL"]
    assert c.nmroads_rss_url == env["WEATHER_NMROADS_RSS_URL"]
    assert c.lsr_feed_url == "https://mirror.example/lsr?h=48&states=NM"
    monkeypatch.setenv("WEATHER_LSR_URL", "https://mirror.example/lsr.geojson")   # no placeholder
    assert Config.from_env().lsr_feed_url == "https://mirror.example/lsr.geojson"
    for k in env:                                # an empty value means "the default"
        monkeypatch.setenv(k, "")
    c = Config.from_env()
    assert c.roads_enabled is True and c.lsr_hours == 24 and c.lsr_url.endswith("{hours}&states=NM")


@pytest.mark.parametrize("var,value", [
    ("WEATHER_LSR_HOURS", "0"), ("WEATHER_LSR_HOURS", "169"), ("WEATHER_LSR_HOURS", "24h"),
    ("WEATHER_LSR_HOURS", "2.5"), ("WEATHER_ROADS_MAX_STALE", "-1"),
    ("WEATHER_ROADS_MAX_STALE", "604801"), ("WEATHER_ROADS_MAX_STALE", "1h"),
    ("WEATHER_ROADS_OLD_DAYS", "0"), ("WEATHER_ROADS_OLD_DAYS", "-2"),
    ("WEATHER_ROADS_OLD_DAYS", "nan"), ("WEATHER_ROADS_OLD_DAYS", "inf"),
    ("WEATHER_ROADS_OLD_DAYS", "366"), ("WEATHER_ROADS_OLD_DAYS", "three"),
    ("WEATHER_NMROADS_JSON_URL", "nmroads.com/nmroads.json"),
    ("WEATHER_NMROADS_JSON_URL", "ftp://nmroads.com/nmroads.json"),
    ("WEATHER_NMROADS_RSS_URL", "https://nm roads.com/rss.xml"),
    ("WEATHER_NMROADS_RSS_URL", "javascript:alert(1)"),
    ("WEATHER_LSR_URL", "https://mesonet.agron.iastate.edu/lsr?hours={hour}"),
    ("WEATHER_LSR_URL", "https://mesonet.agron.iastate.edu/lsr?hours={hours"),
    ("WEATHER_LSR_URL", "https://mesonet.agron.iastate.edu/lsr?h={0}"),
    ("WEATHER_LSR_URL", "file:///etc/passwd"),
])
def test_config_road_settings_are_validated(cfg, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError):
        Config.from_env()


def test_config_road_overrides_are_validated_even_when_roads_are_off(cfg):
    for bad in ({"lsr_hours": 0}, {"lsr_hours": True}, {"roads_old_days": float("nan")},
                {"roads_max_stale": -5}, {"lsr_url": "https://x.example/{nope}"},
                {"nmroads_json_url": ""}):
        with pytest.raises(ValueError):
            Config.from_env(roads_enabled=False, **bad)
    assert Config.from_env(lsr_hours=168, roads_old_days=0.5).lsr_hours == 168


def test_main_exits_2_on_a_bad_road_setting(cfg, fake_http, monkeypatch):
    monkeypatch.setenv("WEATHER_LSR_HOURS", "1000")
    assert mwp.main(_argv(cfg)) == 2
    assert not fake_http.calls


# ---- config fields used by the run -----------------------------------------------------------------
def test_default_sites_carry_static_time_zones():
    tzs = {s["slug"]: s["tz"] for s in config.DEFAULT_SITES}
    assert tzs == {"lubbock": "America/Chicago", "clovis": "America/Denver",
                   "fort_sumner": "America/Denver", "socorro": "America/Denver",
                   "albuquerque": "America/Denver"}
    if ZoneInfo is not None:
        for tz in tzs.values():
            ZoneInfo(tz)
    assert [s["tz"] for s in Config().sites] == list(tzs.values())


def test_parse_sites_optional_time_zone_field():
    a, b, c = parse_sites("a|A|34|-106|America/Denver; b|B|33|-101; c|C|33|-101|")
    assert a == {"slug": "a", "name": "A", "lat": 34.0, "lon": -106.0, "tz": "America/Denver"}
    assert b["tz"] is None and c["tz"] is None
    for bad in ("a|A|34|-106|Mars/Olympus_Mons", "a|A|34|-106|../../etc/passwd",
                "a|A|34|-106|America/Denver|extra", "a|A|34"):
        with pytest.raises(ValueError):
            parse_sites(bad)


def test_config_budget_colours_and_alpha_defaults(cfg, monkeypatch):
    c = Config.from_env()                       # the cfg fixture cleared WEATHER_*
    assert c.run_budget_s == 240 and c.alert_colors == "" and c.alert_color_overrides == {}
    assert c.alert_fill_alpha == 55 and c.radar_alpha == 170
    monkeypatch.setenv("WEATHER_RUN_BUDGET", "120")
    monkeypatch.setenv("WEATHER_ALERT_COLORS",
                       " Flood Watch=#e53935 ;flash  FLOOD warning = #8b0000;; ")
    c = Config.from_env()
    assert c.run_budget_s == 120
    assert c.alert_color_overrides == {"flood watch": "#E53935", "flash flood warning": "#8B0000"}
    for bad in ("Flood Watch", "Flood Watch=green", "Flood Watch=#12345", "Flood Watch=E53935",
                "=#123456", "A=#123456=x", "Flood Watch=#GG0000"):
        monkeypatch.setenv("WEATHER_ALERT_COLORS", bad)
        with pytest.raises(ValueError):
            Config.from_env()
    monkeypatch.delenv("WEATHER_ALERT_COLORS")
    for bad in ("abc", "5"):
        monkeypatch.setenv("WEATHER_RUN_BUDGET", bad)
        with pytest.raises(ValueError):
            Config.from_env()
    monkeypatch.delenv("WEATHER_RUN_BUDGET")
    with pytest.raises(ValueError):             # keyword overrides are validated too
        Config.from_env(alert_colors="Tornado Warning=red")
    assert Config.from_env(alert_colors="Tornado Warning=#ff0000").alert_color_overrides == {
        "tornado warning": "#FF0000"}


# ---- sites anywhere: nothing depends on the example deployment's five -------------------------------
DENVER = {"slug": "denver", "name": "Denver, CO", "lat": 39.7392, "lon": -104.9903,
          "tz": "America/Denver"}
FLAGSTAFF = {"slug": "flagstaff", "name": "Flagstaff, AZ", "lat": 35.1983, "lon": -111.6513,
             "tz": "America/Phoenix"}
HONOLULU = {"slug": "honolulu", "name": "Honolulu, HI", "lat": 21.3069, "lon": -157.8583,
            "tz": "Pacific/Honolulu"}
EXAMPLE_NAMES = ("Lubbock", "Clovis", "Fort Sumner", "Socorro", "Albuquerque")


def test_no_map_in_new_mexico_means_no_road_fetch_and_no_road_blocks(cfg, fake_http, monkeypatch,
                                                                     caplog):
    """Sites far from New Mexico: NMDOT and the storm reports are never asked (the road
    routes would answer), the run and the sites carry no road data, the renderer is called
    without roads=, the page has no road block, banner line, pill or credit, status.json
    reports roads "not applicable" without a problem, and the log says why. The alert query
    covers the states on these maps; the title is theirs; no example site is named."""
    caplog.set_level(logging.INFO, logger="weather")
    cfg.sites = [dict(DENVER), dict(FLAGSTAFF)]
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    fake_http.add("openstreetmap", _png())
    fake_http.add("/alerts/active?area=", alert_feed())
    add_road_routes(fake_http)                   # would answer, but must not be asked
    calls = render_spy(monkeypatch)
    run = mwp.run_once(cfg)

    assert not any(_is_road_url(c) for c in fake_http.calls)
    assert [c for c in fake_http.calls if "/alerts/active" in c] == [
        alerts.BASE + "/alerts/active?area=AZ,CO,NE"]
    assert "roads" not in run and run["roads_not_applicable"] == mwp.ROADS_NOT_APPLICABLE
    assert all("roads" not in s for s in run["sites"])
    assert len(calls) == 4 and all("roads" not in k for _s, _t, k in calls)
    assert not any("road" in e.lower() for e in run["errors"])
    assert_outputs(cfg, ["denver", "flagstaff"])

    st = read_status(cfg)
    assert st["roads"] == "not applicable"
    assert not any("road" in p.lower() or "nmdot" in p.lower() for p in st["problems"])
    assert all(v["roads_events"] == 0 and v["roads_reports"] == 0 for v in st["sites"].values())
    html = read_page(cfg)
    body = html.split("</style>", 1)[1].split("<script>", 1)[0]  # not the static CSS / JS
    for absent in ("Road closures", "road closures", "NMDOT", "NMRoads", "New Mexico",
                   "storm report", 'class="roads"', "roadbox"):
        assert absent not in body, absent
    assert not [n for n in EXAMPLE_NAMES if n in html]
    assert "<title>Local weather: Denver · Flagstaff</title>" in html

    msgs = [r.getMessage() for r in caplog.records]
    assert "road closures not loaded: " + mwp.ROADS_NOT_APPLICABLE in msgs
    assert ("alert areas: AZ,CO,NE (auto: the states, territories and marine areas on the "
            "sites' maps)") in msgs
    assert sum("roads not applicable (no map in NM)" in m for m in msgs) == 3   # 2 sites + done


def test_a_map_outside_new_mexico_next_to_one_inside_keeps_its_note(cfg, fake_http, monkeypatch):
    """One site in New Mexico is enough: the roads are fetched, the NM map lists its items
    and the other map keeps the one-line "outside New Mexico" note."""
    cfg.sites = [dict(DENVER), dict(SOCORRO, tz="America/Denver")]
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    fake_http.add("openstreetmap", _png())
    add_road_routes(fake_http)
    run = mwp.run_once(cfg)
    assert any("nmroads.com" in c for c in fake_http.calls)
    assert any("lsr.geojson" in c for c in fake_http.calls)
    assert "roads_not_applicable" not in run and isinstance(run["roads"], dict)
    den, soc = run["sites"]
    assert den["roads"]["covers_nm"] is False and den["roads"]["events"] == []
    assert soc["roads"]["covers_nm"] is True and soc["roads"]["events"]
    html = read_page(cfg)
    block = html.split('id="roads-denver"', 1)[1].split("</div>", 1)[0]
    assert page._ROADS_OUTSIDE in block
    assert isinstance(read_status(cfg)["roads"], dict)


def test_maps_outside_the_mrms_grid_say_they_have_no_radar(cfg, fake_http, frame_png):
    """Honolulu next to Denver: a radar frame is loaded for Denver, but the MRMS grid
    (contiguous US) does not reach the Honolulu map, so that map and its caption say there
    is no radar there (an empty radar layer must not read as "no rain"). The alert query
    asks for Hawaii and its waters (PH) plus Denver's states."""
    cfg.sites = [dict(HONOLULU), dict(DENVER)]
    cfg.map_px = 120
    fake_http.add("/GIS/mrms/lcref_", frame_png)
    fake_http.add("openstreetmap", _png())
    run = mwp.run_once(cfg)
    assert run["radar"]["ok"]
    assert run["sites"][0]["map_notes"] == [mwp.MRMS_OUTSIDE_NOTE]
    assert run["sites"][1]["map_notes"] == []
    assert mwp.MRMS_OUTSIDE_NOTE in read_page(cfg)
    assert any(c.endswith("/alerts/active?area=CO,HI,NE,PH") for c in fake_http.calls)
    assert "roads" not in run and run["roads_not_applicable"]
    # inside the grid, or without a radar frame, there is no such note
    frame = geo.MapFrame(DENVER["lat"], DENVER["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
    assert mwp._radar_coverage_notes(frame, {"ok": True}) == []
    hnl = geo.MapFrame(HONOLULU["lat"], HONOLULU["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)
    assert mwp._radar_coverage_notes(hnl, {"ok": False}) == []
    assert mwp._radar_coverage_notes(None, {"ok": True}) == []


def test_no_map_in_the_mrms_grid_fetches_no_radar(cfg, fake_http, frame_png, caplog):
    """Honolulu alone: no map reaches the MRMS grid, so no MRMS request is made (the frame
    would answer), the radar is "not applicable" rather than failed, the maps and the page
    say there is no radar here, and status.json neither calls the radar a problem nor marks
    the run degraded for it."""
    caplog.set_level(logging.INFO, logger="weather")
    cfg.sites = [dict(HONOLULU)]
    cfg.map_px = 120
    fake_http.add("/GIS/mrms/lcref_", frame_png)       # would answer, but must not be asked
    fake_http.add("openstreetmap", _png())
    fake_http.add("/alerts/active?area=", {"type": "FeatureCollection", "features": []})
    run = mwp.run_once(cfg)
    assert not any("mrms" in c.lower() for c in fake_http.calls)
    assert run["radar"]["not_applicable"] and not run["radar"]["ok"]
    assert run["radar"]["error"] == mwp.RADAR_NOT_APPLICABLE
    assert not any("radar" in e for e in run["errors"])
    assert run["sites"][0]["map_notes"] == []           # the caption itself says it
    assert all(m["ok"] and not m["error"] for m in run["sites"][0]["maps"].values())
    html = read_page(cfg)
    assert "No radar here: MRMS covers only the contiguous US" in html
    assert "Radar: not applicable (no map in the MRMS grid)" in html
    assert "Radar unavailable" not in html
    st = read_status(cfg)
    assert st["radar"] == "not applicable"
    assert not any(p.startswith("radar") for p in st["problems"])
    msgs = [r.getMessage() for r in caplog.records]
    assert any("radar not applicable (no map in the MRMS grid)" in m for m in msgs)
    # the run skips the radar before anything else decides: --skip-radar changes nothing here
    fake_http.calls.clear()
    assert mwp.run_once(cfg, skip_radar=True)["radar"]["not_applicable"]
    assert mwp._maps_reach_mrms(cfg) is False
    cfg.sites = [dict(HONOLULU), dict(DENVER)]
    assert mwp._maps_reach_mrms(cfg) is True


PAGO_PAGO = {"slug": "pago_pago", "name": "Pago Pago, AS", "lat": -14.2756, "lon": -170.7020,
             "tz": None}


def test_a_site_nws_gives_no_forecast_grid_is_a_healthy_run(cfg, fake_http):
    """American Samoa: /points answers with zones and a time zone but no grid. The run
    uses the time zone (no UTC fall-back) and the zones, asks for no forecast, hourly
    forecast or station list, lists nothing as a problem, and status.json reports ok:
    nothing is broken, NWS simply provides no forecast there (radar and roads are not
    applicable either)."""
    cfg.sites = [dict(PAGO_PAGO)]
    cfg.map_px = 200
    fake_http.add("openstreetmap", _png())
    fake_http.add("/alerts/active?area=AS,PS", {"type": "FeatureCollection", "features": []})
    fake_http.add("/points/-14.2756,-170.7020", {"properties": {
        "cwa": "PPG", "gridId": None, "forecast": None, "forecastHourly": None,
        "observationStations": None, "timeZone": "Pacific/Pago_Pago",
        "forecastZone": "https://api.weather.gov/zones/forecast/ASZ001",
        "county": "https://api.weather.gov/zones/county/ASC010",
        "fireWeatherZone": "https://api.weather.gov/zones/fire/ASZ001"}})
    run = mwp.run_once(cfg)
    assert run["errors"] == []
    site = run["sites"][0]
    assert site["meta"]["ok"] and site["meta"]["no_grid"] and site["meta"]["tz"] == "Pacific/Pago_Pago"
    assert site["sun"]["tz"] == "Pacific/Pago_Pago"
    assert all(site[k]["not_provided"] for k in ("obs", "forecast", "hourly"))
    assert not any("gridpoints" in c or "/stations/" in c for c in fake_http.calls)
    st = read_status(cfg)
    assert st["ok"] and not st["degraded"] and st["problems"] == [], st["problems"]
    assert st["radar"] == "not applicable" and st["roads"] == "not applicable"
    html = read_page(cfg)
    assert "NWS PPG, no forecast grid" in html and "SST" in html      # Samoa Standard Time
    assert "7-day forecast: not provided by NWS for this location" in html


def test_a_site_outside_an_explicit_alert_area_list_is_reported(cfg, fake_http, monkeypatch):
    """WEATHER_ALERT_AREAS=NM while a site is in Colorado (the site list changed, the area
    list did not): the run reports it for that site, so status.json is degraded, instead of
    the page quietly saying "No NWS alerts for these sites"."""
    cfg.sites = [dict(DENVER)]
    cfg.alert_areas = "NM"
    monkeypatch.setattr(radar, "load_frame", _no_frame)
    fake_http.add("openstreetmap", _png())
    fake_http.add("/alerts/active?area=NM", {"type": "FeatureCollection", "features": []})
    denver_points = json.loads(json.dumps(POINTS))
    denver_points["properties"]["forecastZone"] = "https://api.weather.gov/zones/forecast/COZ039"
    denver_points["properties"]["county"] = "https://api.weather.gov/zones/county/COC031"
    fake_http.add("/points/39.7392,-104.9903", denver_points)
    run = mwp.run_once(cfg)
    gap = [e for e in run["errors"] if "alerts not fetched" in e]
    assert gap == ["denver alerts not fetched: its NWS zone COZ039 is in CO, which the alert "
                   "query (NM) leaves out; set WEATHER_ALERT_AREAS=auto or add CO"]
    st = read_status(cfg)
    assert st["degraded"] and gap[0] in st["errors"]


# ---- run_local.sh ------------------------------------------------------------------------------------
RUN_LOCAL = os.path.join(ROOT, "run_local.sh")
needs_bash = pytest.mark.skipif(not shutil.which("bash") or not hasattr(os, "killpg"),
                                reason="needs bash and POSIX process groups")


@pytest.fixture
def run_local_env(tmp_path):
    """Environment for run_local.sh with a stub ``python3`` first on PATH: a generator run
    only records its pid and sleeps (a long run, no network); anything else (the preview
    server) execs the real interpreter."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    stub = stub_dir / "python3"
    stub.write_text('#!/bin/bash\n'
                    'if [ "$1" = "./make_weather_page.py" ]; then\n'
                    '    echo $$ >> "$STUB_PIDS"\n'
                    '    exec sleep 60\n'
                    'fi\n'
                    'exec %s "$@"\n' % shlex.quote(sys.executable))
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("WEATHER_")}
    env.update(PATH=str(stub_dir) + os.pathsep + env.get("PATH", ""),
               STUB_PIDS=str(tmp_path / "gen.pids"), BIND="127.0.0.1", INTERVAL="300",
               WEATHER_CACHE_DIR=str(tmp_path / "cache"))
    return env, tmp_path / "gen.pids", tmp_path / "out"


def _gen_pids(path):
    try:
        return [int(x) for x in path.read_text().split()]
    except (OSError, ValueError):
        return []


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"     # a zombie is dead
    except (OSError, IndexError):
        return True


def _wait_until(pred, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        return True
    except OSError:
        return False


def _cleanup(proc, pids_file):
    for pid in _gen_pids(pids_file):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait(timeout=5)


def _start_run_local(env, port, out):
    return subprocess.Popen(["bash", RUN_LOCAL, str(port), str(out)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)


@needs_bash
def test_run_local_ctrl_c_stops_server_and_a_running_generator(run_local_env):
    env, pids_file, out = run_local_env
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = _start_run_local(env, port, out)
    try:
        assert _wait_until(lambda: _listening(port) and _gen_pids(pids_file), 15), \
            "preview server or generator did not start"
        gen = _gen_pids(pids_file)
        assert all(_alive(p) for p in gen)
        os.killpg(proc.pid, signal.SIGINT)      # Ctrl-C: SIGINT to the foreground process group
        text = proc.communicate(timeout=15)[0].decode("utf-8", "replace")
        assert proc.returncode == 130
        assert _wait_until(lambda: not any(_alive(p) for p in gen), 5), \
            "generator run survived Ctrl-C"
        assert not _listening(port)
        assert "http://127.0.0.1:%d/" % port in text        # BIND is honoured
    finally:
        _cleanup(proc, pids_file)


@needs_bash
def test_run_local_exits_with_the_server_status(run_local_env):
    """Port already taken: the server fails, and so must the script (it used to exit 0),
    taking the generator loop down with it."""
    env, pids_file, out = run_local_env
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        proc = _start_run_local(env, port, out)
        try:
            text = proc.communicate(timeout=20)[0].decode("utf-8", "replace")
        finally:
            _cleanup(proc, pids_file)
    assert proc.returncode == 1, text
    assert "Address already in use" in text
    assert _wait_until(lambda: not any(_alive(p) for p in _gen_pids(pids_file)), 5)


def test_run_local_binds_every_interface_by_default():
    with open(RUN_LOCAL, encoding="utf-8") as f:
        text = f.read()
    assert 'BIND="${BIND:-0.0.0.0}"' in text and '--bind "$BIND"' in text
