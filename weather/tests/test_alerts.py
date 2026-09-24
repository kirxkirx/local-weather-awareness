"""Tests for weather.alerts: classification, colours, normalisation, feed filtering,
zone-shape resolution and per-site AT/candidate logic. No network (conftest blocks it)."""
from __future__ import annotations

import colorsys
import logging
import copy
import re
from datetime import datetime, timezone

import pytest

from weather import alerts, geo, http, util
from weather.config import Config

# ---- fixtures: real-shaped api.weather.gov payloads, trimmed --------------------------
SOCORRO = {"slug": "socorro", "name": "Socorro, NM", "lat": 34.0584, "lon": -106.8914}
FUTURE = "2099-09-23T12:15:00-06:00"
PAST = "2020-01-01T00:00:00-06:00"

# a box around Socorro (real FFW polygons look like this: ~20 vertices, 2-decimal coords)
SOCORRO_POLY = [[-107.20, 33.80], [-106.50, 33.80], [-106.55, 34.10], [-106.50, 34.30],
                [-107.20, 34.30], [-107.20, 33.80]]
# north-west of Socorro: inside the 100-mile frame but not containing the town
NEARBY_POLY = [[-107.60, 34.40], [-107.40, 34.40], [-107.40, 34.60], [-107.60, 34.60],
               [-107.60, 34.40]]
# Lubbock area: far outside Socorro's frame
FAR_POLY = [[-102.0, 33.4], [-101.7, 33.4], [-101.7, 33.7], [-102.0, 33.7], [-102.0, 33.4]]


def feature(event, geometry=None, zones=(), status="Actual", mtype="Alert", ends=FUTURE,
            expires=FUTURE, severity="Severe", urgency="Immediate", certainty="Likely",
            fid=None, **extra):
    """Build a feature with the field structure of the live feed (2026-09-23 sample)."""
    fid = fid or "urn:oid:2.49.0.1.840.0.%s.001.1" % abs(hash((event, ends, tuple(zones))))
    props = {
        "@id": "https://api.weather.gov/alerts/" + fid, "@type": "wx:Alert", "id": fid,
        "areaDesc": "Socorro, NM",
        "geocode": {"SAME": ["035053"], "UGC": [alerts.zone_id_from_url(z) for z in zones]},
        "affectedZones": list(zones), "references": [],
        "sent": "2026-09-23T09:08:00-06:00", "effective": "2026-09-23T09:08:00-06:00",
        "onset": "2026-09-23T09:08:00-06:00", "expires": expires, "ends": ends,
        "status": status, "messageType": mtype, "category": "Met", "severity": severity,
        "certainty": certainty, "urgency": urgency, "event": event,
        "sender": "w-nws.webmaster@noaa.gov", "senderName": "NWS Albuquerque NM",
        "headline": "%s issued September 23 at 9:08AM MDT by NWS Albuquerque NM" % event,
        "description": "The National Weather Service in Albuquerque has issued a\n\n* %s" % event,
        "instruction": "Turn around, don't drown.", "response": "Avoid", "note": None,
        "parameters": {"AWIPSidentifier": ["FFWABQ"]}, "scope": "Public", "code": "IPAWSv1.0",
        "language": "en-US", "web": "http://www.weather.gov",
        "eventCode": {"SAME": ["FFW"], "NationalWeatherService": ["FFW"]},
    }
    props.update(extra)
    return {"id": props["@id"], "type": "Feature",
            "geometry": None if geometry is None else {"type": "Polygon", "coordinates": [geometry]},
            "properties": props}


ZONE_FC = "https://api.weather.gov/zones/forecast/NMZ220"
ZONE_CO = "https://api.weather.gov/zones/county/NMC053"
ZONE_MARINE = "https://api.weather.gov/zones/forecast/GMZ001"


def ffw():
    return feature("Flash Flood Warning", geometry=SOCORRO_POLY, zones=[ZONE_CO],
                   fid="urn:oid:ffw")


def flood_watch():
    """Zone-based, as every Flood Watch in the live feed: geometry null, no instruction."""
    f = feature("Flood Watch", zones=[ZONE_FC, ZONE_CO], mtype="Update", urgency="Future",
                certainty="Possible", ends="2099-09-24T06:00:00-06:00",
                expires="2099-09-23T13:00:00-06:00", fid="urn:oid:watch")
    del f["properties"]["instruction"]
    del f["properties"]["web"]
    return f


def feed(*feats):
    return {"@context": [], "type": "FeatureCollection", "features": list(feats),
            "title": "Current watches, warnings, and advisories for NM, TX",
            "updated": "2026-09-23T17:00:00+00:00"}


def zone_payload(zid, ring, ztype="forecast"):
    """Shape of GET /zones/{type}/{id}: top-level Feature with geometry + properties."""
    return {"@context": [], "id": "https://api.weather.gov/zones/%s/%s" % (ztype, zid),
            "type": "Feature",
            "geometry": None if ring is None else {"type": "Polygon", "coordinates": [ring]},
            "properties": {"@id": "https://api.weather.gov/zones/%s/%s" % (ztype, zid),
                           "id": zid, "type": ztype, "name": "Socorro", "state": "NM",
                           "effectiveDate": "2026-04-16T18:00:00+00:00",
                           "expirationDate": "2200-01-01T00:00:00+00:00"}}


@pytest.fixture
def frame(cfg):
    return geo.MapFrame(SOCORRO["lat"], SOCORRO["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)


NO_META = {"ok": False, "forecast_zone": None, "county_zone": None, "fire_zone": None}
META = {"ok": True, "forecast_zone": "NMZ220", "county_zone": "NMC053", "fire_zone": "NMZ106"}


# ---- classification and colours ------------------------------------------------------
@pytest.mark.parametrize("event,kind", [
    ("Tornado Warning", "warning"), ("Red Flag Warning", "warning"),
    ("Tornado Watch", "watch"), ("Flood Watch", "watch"),
    ("Wind Advisory", "advisory"), ("Special Weather Statement", "statement"),
    ("Air Quality Alert", "statement"), ("Hydrologic Outlook", "statement"),
    ("Hazardous Weather Outlook", "statement"), ("Civil Emergency Message", "statement"),
    ("Child Abduction Emergency", "statement"), ("Short Term Forecast", "statement"),
    ("Test", "other"), ("Evacuation Immediate", "other"), ("", "other"), (None, "other"),
    ("tornado WARNING", "warning"), ("  Flood Watch  ", "watch"),
])
def test_event_kind(event, kind):
    assert alerts.event_kind(event) == kind


def test_color_table_has_the_standard_entries():
    listed = {
        "Tornado Warning": "#FF0000", "Severe Thunderstorm Warning": "#FFA500",
        "Flash Flood Warning": "#8B0000", "Flood Warning": "#00FF00", "Flood Watch": "#2E8B57",
        "Flood Advisory": "#00FF7F", "Severe Thunderstorm Watch": "#DB7093",
        "Tornado Watch": "#FFFF00", "Winter Storm Warning": "#FF69B4",
        "Winter Storm Watch": "#4682B4", "Winter Weather Advisory": "#7B68EE",
        "Blizzard Warning": "#FF4500", "Ice Storm Warning": "#8B008B",
        "High Wind Warning": "#DAA520", "High Wind Watch": "#B8860B", "Wind Advisory": "#D2B48C",
        "Red Flag Warning": "#FF1493", "Fire Weather Watch": "#FFDEAD",
        "Dust Storm Warning": "#FFE4C4", "Blowing Dust Advisory": "#BDB76B",
        "Dense Fog Advisory": "#708090", "Freeze Warning": "#483D8B", "Freeze Watch": "#00FFFF",
        "Frost Advisory": "#6495ED", "Hard Freeze Warning": "#9400D3",
        "Extreme Heat Warning": "#C71585", "Excessive Heat Warning": "#C71585",
        "Heat Advisory": "#FF7F50", "Extreme Cold Warning": "#0000FF",
        "Cold Weather Advisory": "#AFEEEE", "Wind Chill Advisory": "#AFEEEE",
        "Air Quality Alert": "#808080", "Special Weather Statement": "#FFE4B5",
        "Hydrologic Outlook": "#90EE90", "Severe Weather Statement": "#00FFFF",
        "Flash Flood Statement": "#8B0000", "Flood Statement": "#00FF00",
        "Extreme Wind Warning": "#FF8C00", "Snow Squall Warning": "#C71585",
        "Lake Effect Snow Warning": "#008B8B", "Avalanche Warning": "#1E90FF",
        "Hazardous Weather Outlook": "#EEE8AA", "Child Abduction Emergency": "#FFFFFF",
        "Civil Emergency Message": "#FFB6C1", "Test": "#F0FFFF",
        # rows that once disagreed with weather.gov/help-map (review 2026-09-23)
        "Storm Surge Warning": "#B524F7", "Rip Current Statement": "#40E0D0",
        "Tropical Cyclone Local Statement": "#FFE4B5",
    }
    for k, v in listed.items():
        assert alerts.NWS_COLORS.get(k) == v, k
    assert len(alerts.NWS_COLORS) >= 100
    hexre = re.compile(r"^#[0-9A-F]{6}$")
    assert all(hexre.match(v) for v in alerts.NWS_COLORS.values())
    assert set(alerts.KIND_COLORS) == {"warning", "watch", "advisory", "statement", "other"}


def _hue_deg(hexcolor):
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(r, g, b)[0] * 360.0


def test_flood_products_are_red_not_green():
    """The owner's request: flood areas must not be green (green reads as "good"). The
    chart itself stays verbatim in NWS_COLORS; FLOOD_COLORS is layered on top."""
    assert alerts.color_for("Flood Watch") == "#E53935"
    assert alerts.color_for("flash flood watch") == "#E53935"
    assert alerts.color_for("Flood Warning") == "#C62828"
    assert alerts.color_for("Flash Flood Warning") == "#8B0000"          # official, unchanged
    assert alerts.color_for("Flood Advisory") == "#FA8072"
    assert alerts.color_for("Hydrologic Outlook") == "#F4A6A6"
    for event, colour in alerts.FLOOD_COLORS.items():
        assert event in alerts.NWS_COLORS, event                   # only real event names
        assert alerts.EVENT_COLORS[event] == colour
        assert alerts.color_for(event, alerts.event_kind(event)) == colour
        h = _hue_deg(colour)
        assert h < 20 or h > 340, (event, colour, h)                # red, not green
    # every inland flood/hydrologic product the table knows is covered
    for event in alerts.NWS_COLORS:
        low = event.lower()
        if ("flood" in low or "hydrologic" in low) and not low.startswith(("coastal", "lakeshore")):
            assert event in alerts.FLOOD_COLORS, event
    # coastal / lakeshore flood products keep the official colours
    for event in ("Coastal Flood Warning", "Coastal Flood Watch", "Coastal Flood Advisory",
                  "Lakeshore Flood Warning", "Lakeshore Flood Statement"):
        assert alerts.color_for(event) == alerts.NWS_COLORS[event]
    # darker = more serious within the flood family
    lum = {e: colorsys.rgb_to_hls(*(int(c[i:i + 2], 16) / 255.0 for i in (1, 3, 5)))[1]
           for e, c in alerts.FLOOD_COLORS.items()}
    assert (lum["Flood Advisory"] > lum["Flood Watch"] > lum["Flood Warning"]
            > lum["Flash Flood Warning"])
    # everything else is still straight from the chart
    for event, colour in alerts.NWS_COLORS.items():
        if event not in alerts.FLOOD_COLORS:
            assert alerts.color_for(event) == colour, event


def test_color_for_lookup_and_fallback():
    assert alerts.color_for("Flash Flood Warning", "warning") == "#8B0000"
    assert alerts.color_for("flash flood warning", "warning") == "#8B0000"     # case-insensitive
    assert alerts.color_for("Zombie Warning", "warning") == alerts.KIND_COLORS["warning"]
    assert alerts.color_for("Zombie Watch", "watch") == alerts.KIND_COLORS["watch"]
    assert alerts.color_for("Whatever", "nonsense-kind") == alerts.KIND_COLORS["other"]
    assert alerts.color_for("Zombie Advisory") == alerts.KIND_COLORS["advisory"]  # kind derived
    assert alerts.color_for(None, None) == alerts.KIND_COLORS["other"]


def test_alert_rank():
    assert alerts.alert_rank("warning", "Extreme") == 0
    assert alerts.alert_rank("warning", "Severe") == 1
    assert alerts.alert_rank("watch", "Severe") == 11
    assert alerts.alert_rank("advisory", "Minor") == 23
    assert alerts.alert_rank("statement", "Unknown") == 34
    assert alerts.alert_rank("other", None) == 44
    assert alerts.alert_rank("bogus", "bogus") == 44


# ---- normalize -----------------------------------------------------------------------
def test_normalize_polygon_warning():
    r = alerts.normalize(ffw())
    assert r["id"] == "urn:oid:ffw" and r["event"] == "Flash Flood Warning"
    assert r["kind"] == "warning" and r["rank"] == 1 and r["color"] == "#8B0000"
    assert r["severity"] == "Severe" and r["urgency"] == "Immediate" and r["certainty"] == "Likely"
    assert r["headline"].startswith("Flash Flood Warning issued")
    assert r["instruction"] == "Turn around, don't drown." and r["web"] == "http://www.weather.gov"
    assert r["sender"] == "NWS Albuquerque NM" and r["area_desc"] == "Socorro, NM"
    assert r["message_type"] == "Alert" and r["status"] == "Actual"
    assert r["sent"] == r["effective"] == r["onset"] == "2026-09-23T09:08:00-06:00"
    assert r["ends"] == FUTURE and r["end_dt"] == util.parse_iso(FUTURE)
    assert r["end_dt"].tzinfo is not None
    assert r["affected_zone_urls"] == [ZONE_CO] and r["affected_zone_ids"] == ["NMC053"]
    assert r["geometry"]["type"] == "Polygon" and r["geometry_source"] == "polygon"
    assert r["bbox"] == (-107.2, 33.8, -106.5, 34.3)


def test_normalize_zone_based_watch_and_missing_keys():
    r = alerts.normalize(flood_watch())
    assert r["kind"] == "watch" and r["rank"] == 11 and r["color"] == "#E53935"
    assert r["geometry"] is None and r["geometry_source"] is None and r["bbox"] is None
    assert r["affected_zone_ids"] == ["NMZ220", "NMC053"]
    assert r["instruction"] is None and r["web"] is None
    assert r["message_type"] == "Update"
    # ends wins over expires for "until"
    assert r["end_dt"] == util.parse_iso("2099-09-24T06:00:00-06:00")
    # ends missing -> expires; both missing -> None
    f = feature("Air Quality Alert", zones=[ZONE_FC], ends=None, expires=FUTURE,
                severity="Unknown", urgency="Unknown", certainty="Unknown")
    r = alerts.normalize(f)
    assert r["end_dt"] == util.parse_iso(FUTURE) and r["ends"] is None
    assert r["kind"] == "statement" and r["rank"] == 34 and r["color"] == "#808080"
    r = alerts.normalize(feature("Special Weather Statement", ends=None, expires=None))
    assert r["end_dt"] is None
    # nearly empty feature: every key present, nothing raised
    r = alerts.normalize({"type": "Feature", "geometry": None, "properties": {"event": "Test"}})
    assert r["id"] is None and r["kind"] == "other" and r["rank"] == 44
    assert r["headline"] == "" and r["description"] == "" and r["affected_zone_ids"] == []
    assert r["severity"] == "Unknown" and r["end_dt"] is None and r["bbox"] is None
    assert r["threat"] is None and r["nws_headline"] is None
    assert set(alerts.normalize(None)) == set(r)
    # a geometry without polygons (a Point) is treated as no geometry
    f = ffw()
    f["geometry"] = {"type": "Point", "coordinates": [-106.9, 34.0]}
    assert alerts.normalize(f)["geometry"] is None


@pytest.mark.parametrize("event,params,threat", [
    ("Tornado Warning", {"tornadoDetection": ["OBSERVED"], "tornadoDamageThreat": ["CATASTROPHIC"]},
     "TORNADO EMERGENCY"),
    ("Tornado Warning", {"tornadoDamageThreat": ["CONSIDERABLE"]}, "PDS"),
    ("Tornado Warning", {"tornadoDetection": ["RADAR INDICATED"]}, None),       # base warning
    ("Flash Flood Warning", {"flashFloodDetection": ["OBSERVED"],
                             "flashFloodDamageThreat": ["CATASTROPHIC"]}, "FLASH FLOOD EMERGENCY"),
    ("Flash Flood Warning", {"flashFloodDamageThreat": ["CONSIDERABLE"]},
     "CONSIDERABLE FLASH FLOODING"),
    ("Flash Flood Warning", {"flashFloodDetection": ["RADAR INDICATED"]}, None),
    ("Severe Thunderstorm Warning", {"thunderstormDamageThreat": ["DESTRUCTIVE"],
                                     "maxHailSize": ["2.75"]}, "DESTRUCTIVE"),
    # the middle SVR tier is not a "particularly dangerous situation" (NWS reserves PDS
    # for tornadoes), so it gets its own label
    ("Severe Thunderstorm Warning", {"thunderstormDamageThreat": ["CONSIDERABLE"]},
     "CONSIDERABLE DAMAGE"),
    # a follow-up statement carrying the tag is still an emergency
    ("Severe Weather Statement", {"tornadoDamageThreat": ["CATASTROPHIC"]}, "TORNADO EMERGENCY"),
    # most serious tag wins; case and a bare string are tolerated
    ("Severe Thunderstorm Warning", {"thunderstormDamageThreat": ["DESTRUCTIVE"],
                                     "tornadoDamageThreat": ["considerable"]}, "PDS"),
    ("Flash Flood Warning", {"flashFloodDamageThreat": "catastrophic"}, "FLASH FLOOD EMERGENCY"),
    ("Flood Watch", {"flashFloodDamageThreat": [], "NWSheadline": []}, None),
    ("Flood Watch", {"flashFloodDamageThreat": [None, 3]}, None),
    ("Flood Watch", None, None),
    ("Flood Watch", ["not", "a", "dict"], None),
])
def test_normalize_threat(event, params, threat):
    r = alerts.normalize(feature(event, geometry=SOCORRO_POLY, parameters=params))
    assert r["threat"] == threat
    assert alerts.alert_threat(params) == threat


def test_normalize_nws_headline():
    f = feature("Red Flag Warning", parameters={
        "AWIPSidentifier": ["RFWABQ"],
        "NWSheadline": ["RED FLAG WARNING IN EFFECT FROM NOON TO 8 PM MDT THURSDAY", "second"]})
    r = alerts.normalize(f)
    assert r["nws_headline"] == "RED FLAG WARNING IN EFFECT FROM NOON TO 8 PM MDT THURSDAY"
    assert r["headline"].startswith("Red Flag Warning issued")    # API headline untouched
    assert alerts.normalize(feature("Red Flag Warning"))["nws_headline"] is None
    for bad in ([""], ["   "], [None], "", None, 5):
        assert alerts.normalize(feature("X Warning", parameters={"NWSheadline": bad}))[
            "nws_headline"] is None, bad
    assert alerts.normalize(feature("X Warning", parameters={"NWSheadline": " FLOOD WATCH "}))[
        "nws_headline"] == "FLOOD WATCH"


# ---- fetch_active --------------------------------------------------------------------
def make_feed():
    return feed(
        flood_watch(),
        ffw(),
        feature("Flood Advisory", geometry=SOCORRO_POLY, ends=PAST, expires=PAST, fid="expired"),
        feature("Flash Flood Warning", geometry=SOCORRO_POLY, mtype="Cancel", fid="cancel"),
        feature("Tornado Warning", geometry=SOCORRO_POLY, status="Test", fid="test"),
        feature("Air Quality Alert", zones=[ZONE_FC], ends=None, expires=FUTURE, fid="aqa",
                severity="Unknown", urgency="Unknown", certainty="Unknown"),
        feature("Hydrologic Outlook", zones=[ZONE_FC], ends=None, expires=None, fid="hyo",
                severity="Unknown"),
    )


def test_fetch_active_filters_and_sorts(cfg, cache, fake_http):
    fake_http.add("api.weather.gov/alerts/active?area=", make_feed())
    r = alerts.fetch_active(cfg, cache)
    assert r["ok"] and not r["stale"] and r["error"] is None and r["age_s"] == 0.0
    assert r["fetched_ts"] is not None and abs(r["fetched_ts"] - util.unix_ts()) < 5
    assert r["count_raw"] == 7
    assert [a["id"] for a in r["alerts"]] == ["urn:oid:ffw", "urn:oid:watch", "aqa", "hyo"]
    # the default WEATHER_ALERT_AREAS=auto, resolved for the default sites' maps
    assert len(fake_http.calls) == 1 and fake_http.calls[0].endswith("area=NM,OK,TX")
    assert fake_http.calls[0].startswith(alerts.BASE + "/alerts/active")
    # ttl is 0: the next run asks NWS again
    alerts.fetch_active(cfg, cache)
    assert len(fake_http.calls) == 2


def test_fetch_active_stale_and_failed(cfg, cache, fake_http):
    fake_http.add("alerts/active", make_feed())
    assert alerts.fetch_active(cfg, cache)["ok"]
    fake_http.routes.clear()
    fake_http.fail("api.weather.gov")
    r = alerts.fetch_active(cfg, cache)
    assert r["ok"] and r["stale"] and r["error"] and len(r["alerts"]) == 4
    assert r["count_raw"] == 7 and r["fetched_ts"] is not None
    # nothing cached at all -> not ok, empty, no crash
    fresh = http.Cache(cfg.cache_dir + "_empty")
    r = alerts.fetch_active(cfg, fresh)
    assert not r["ok"] and r["stale"] and r["error"] and r["alerts"] == []
    assert r["count_raw"] == 0 and r["fetched_ts"] is None
    # wrong-shaped payload is rejected (and never cached)
    fake_http.routes.clear()
    fake_http.add("alerts/active", {"title": "oops"})
    r = alerts.fetch_active(cfg, fresh)
    assert not r["ok"] and "shape" in r["error"] and fresh.get_any("alerts") == (None, None)


def test_fetch_active_honours_alert_areas(cfg, cache, fake_http):
    cfg.alert_areas = "NM"
    fake_http.add("alerts/active?area=NM", feed())
    r = alerts.fetch_active(cfg, cache)
    assert r["ok"] and r["alerts"] == [] and r["count_raw"] == 0
    assert fake_http.calls[0].endswith("?area=NM")


def test_fetch_active_applies_colour_overrides_from_config(cfg, cache, fake_http):
    """WEATHER_ALERT_COLORS end to end: config string -> overrides -> AlertRec colour,
    matched case-insensitively; events not named keep the built-in colour."""
    cfg.alert_colors = "flood watch=#123456; Flash Flood Warning=#abcdef"
    fake_http.add("alerts/active", make_feed())
    got = {a["id"]: a["color"] for a in alerts.fetch_active(cfg, cache)["alerts"]}
    assert got["urn:oid:watch"].upper() == "#123456"
    assert got["urn:oid:ffw"].upper() == "#ABCDEF"
    assert got["aqa"] == alerts.color_for("Air Quality Alert")
    # the stale path (last-good copy) gets the same treatment
    fake_http.routes.clear()
    fake_http.fail("alerts/active")
    r = alerts.fetch_active(cfg, cache)
    assert r["stale"] and {a["id"]: a["color"] for a in r["alerts"]}["urn:oid:watch"] == "#123456"


def test_fetch_active_colour_overrides_are_defensive(cfg, cache, fake_http, monkeypatch):
    """A bad override entry costs only that override; a property that raises costs only
    the overrides. The alerts themselves always come through."""
    fake_http.add("alerts/active", make_feed())
    monkeypatch.setattr(Config, "alert_color_overrides", property(lambda self: {
        "FLOOD WATCH ": "#00aa00", "flash flood warning": "red", "": "#111111", 5: "#222222",
        "air quality alert": None}), raising=False)
    got = {a["id"]: a["color"] for a in alerts.fetch_active(cfg, cache)["alerts"]}
    assert got["urn:oid:watch"] == "#00AA00"                    # key normalised, value kept
    assert got["urn:oid:ffw"] == "#8B0000"                      # "red" is not #rrggbb
    assert got["aqa"] == alerts.color_for("Air Quality Alert")

    def boom(self):
        raise ValueError("WEATHER_ALERT_COLORS: bad entry")
    monkeypatch.setattr(Config, "alert_color_overrides", property(boom), raising=False)
    r = alerts.fetch_active(cfg, cache)
    assert r["ok"] and len(r["alerts"]) == 4
    assert {a["id"]: a["color"] for a in r["alerts"]}["urn:oid:watch"] == "#E53935"


# ---- resolve_geometries --------------------------------------------------------------
def test_resolve_geometries_fills_zones_and_caches(cfg, cache, fake_http):
    fake_http.add("zones/forecast/NMZ220", zone_payload("NMZ220", SOCORRO_POLY))
    fake_http.fail("zones/county/NMC053")
    fake_http.add("zones/forecast/GMZ001", zone_payload("GMZ001", None))
    watch = alerts.normalize(flood_watch())
    warning = alerts.normalize(ffw())
    marine = alerts.normalize(feature("Small Craft Advisory", zones=[ZONE_MARINE], fid="mar"))
    orphan = alerts.normalize(feature("Wind Advisory", zones=[ZONE_CO], fid="orphan"))
    alerts.resolve_geometries(cfg, cache, [watch, warning, marine, orphan, None])
    # one zone resolved, one failed: the watch gets the union of what resolved
    assert watch["geometry"]["type"] == "MultiPolygon"
    assert len(watch["geometry"]["coordinates"]) == 1
    assert watch["geometry_source"] == "zones" and watch["bbox"] == (-107.2, 33.8, -106.5, 34.3)
    # polygon alerts are left alone
    assert warning["geometry"]["type"] == "Polygon" and warning["geometry_source"] == "polygon"
    # zone with geometry null / all zones failing -> still None, still has ids for AT
    assert marine["geometry"] is None and marine["geometry_source"] is None
    assert orphan["geometry"] is None and orphan["affected_zone_ids"] == ["NMC053"]
    zone_calls = [c for c in fake_http.calls if "/zones/" in c]
    assert len(zone_calls) == 3                     # NMC053 shared by two alerts: memoised
    assert cache.get("zone/NMZ220", 10)["geometry"]["type"] == "Polygon"
    # a second run (fresh records) never re-fetches a cached zone
    alerts.resolve_geometries(cfg, cache, [alerts.normalize(flood_watch())])
    assert [c for c in fake_http.calls if "NMZ220" in c] == [ZONE_FC]
    assert [c for c in fake_http.calls if "NMC053" in c] == [ZONE_CO, ZONE_CO]
    # wrong-shaped zone payload is rejected by validate
    fake_http.routes.clear()
    fake_http.add("zones/fire/NMZ106", {"properties": {"id": "NMZ106"}})
    fire = alerts.normalize(feature("Red Flag Warning",
                                    zones=["https://api.weather.gov/zones/fire/NMZ106"]))
    alerts.resolve_geometries(cfg, cache, [fire])
    assert fire["geometry"] is None and cache.get_any("zone/NMZ106") == (None, None)
    alerts.resolve_geometries(cfg, cache, None)     # tolerated


def test_zone_outlines_keep_half_the_run_budget(cfg, cache, fake_http, monkeypatch, caplog):
    """A first run during a statewide event would spend the network budget on hundreds of
    zone outlines (~0.4 s each) and leave the forecasts unfetched. Outlines already on disk
    are always used; new ones are fetched only while half of the run's budget is left. An
    alert that then lacks an outline stays without a shape (listed, not shaded in part) and
    a later run fetches the rest."""
    other = "https://api.weather.gov/zones/forecast/NMZ221"
    fake_http.add("zones/forecast/NMZ220", zone_payload("NMZ220", SOCORRO_POLY))
    fake_http.add("zones/forecast/NMZ221", zone_payload("NMZ221", NEARBY_POLY))
    fake_http.add("zones/county/NMC053", zone_payload("NMC053", SOCORRO_POLY))
    first = alerts.normalize(feature("Flood Watch", zones=[ZONE_FC], fid="a1"))
    alerts.resolve_geometries(cfg, cache, [first])           # no run budget: fetched
    assert first["geometry"] is not None
    left = {"s": 100.0}
    monkeypatch.setattr(http, "budget_left", lambda: left["s"])
    monkeypatch.setattr(http, "run_status", lambda: {"budget_s": 240.0})
    caplog.set_level(logging.WARNING, logger="weather.alerts")
    del fake_http.calls[:]
    cached = alerts.normalize(feature("Flood Watch", zones=[ZONE_FC], fid="a2"))
    new = alerts.normalize(feature("Wind Advisory", zones=[ZONE_FC, other], fid="a3"))
    alerts.resolve_geometries(cfg, cache, [cached, new])
    assert cached["geometry"] is not None and cached["geometry_source"] == "zones"
    assert new["geometry"] is None and new["geometry_source"] is None and new["bbox"] is None
    assert new["affected_zone_ids"] == ["NMZ220", "NMZ221"]      # still AT by zone id
    assert fake_http.calls == []                                  # nothing was fetched
    assert any("1 alert(s) left without a shape" in r.getMessage() for r in caplog.records)
    left["s"] = 120.0                                             # half the budget left
    alerts.resolve_geometries(cfg, cache, [new])
    assert new["geometry"] is not None and fake_http.calls == [other]
    assert alerts.ZONE_BUDGET_KEEP == 0.5


# ---- site_alerts ---------------------------------------------------------------------
def test_site_alerts_at_via_polygon(frame):
    a = alerts.normalize(ffw())
    r = alerts.site_alerts([a], SOCORRO, NO_META, frame)
    assert [x["id"] for x in r["at"]] == ["urn:oid:ffw"] and r["candidates"] == []
    r = alerts.site_alerts([a], SOCORRO, None, frame)         # meta missing entirely
    assert len(r["at"]) == 1


def test_site_alerts_polygon_warning_ignores_listed_county(frame):
    """Storm-based warnings list every county the polygon touches; Socorro County is
    bigger than the whole map, so a warning drawn 50 km away must stay NEAR, not AT."""
    corner = [[-106.40, 34.30], [-106.20, 34.30], [-106.20, 34.45], [-106.40, 34.45],
              [-106.40, 34.30]]
    a = alerts.normalize(feature("Flash Flood Warning", geometry=corner,
                                 zones=[ZONE_CO, ZONE_FC], fid="corner"))
    assert a["geometry_source"] == "polygon"
    assert not geo.point_in_geometry(SOCORRO["lon"], SOCORRO["lat"], a["geometry"])
    r = alerts.site_alerts([a], SOCORRO, META, frame)
    assert r["at"] == [] and r["candidates"] == [a]
    # the same polygon over the town is AT, with or without the county listed
    b = alerts.normalize(feature("Flash Flood Warning", geometry=SOCORRO_POLY, zones=[],
                                 fid="over"))
    assert alerts.site_alerts([b], SOCORRO, META, frame)["at"] == [b]


def test_site_alerts_zone_based_at_via_outline_without_meta(frame):
    """Zone-based alerts keep both tests: a resolved outline over the town makes the
    site AT even when its own zone ids are unknown (metadata down)."""
    w = alerts.normalize(flood_watch())
    w["geometry"] = {"type": "MultiPolygon", "coordinates": [[SOCORRO_POLY]]}
    w["geometry_source"] = "zones"
    w["bbox"] = geo.geometry_bbox(w["geometry"])
    assert alerts.site_alerts([w], SOCORRO, NO_META, frame)["at"] == [w]
    # ... and a resolved outline elsewhere does not cancel the zone-id match
    w["geometry"] = {"type": "MultiPolygon", "coordinates": [[NEARBY_POLY]]}
    w["bbox"] = geo.geometry_bbox(w["geometry"])
    assert alerts.site_alerts([w], SOCORRO, META, frame)["at"] == [w]
    assert alerts.site_alerts([w], SOCORRO, NO_META, frame)["candidates"] == [w]


def test_site_alerts_at_via_zone_id(frame):
    w = alerts.normalize(flood_watch())                       # geometry None, zones listed
    assert alerts.site_alerts([w], SOCORRO, META, frame)["at"] == [w]
    fire = alerts.normalize(feature("Red Flag Warning",
                                    zones=["https://api.weather.gov/zones/fire/NMZ106"]))
    assert alerts.site_alerts([fire], SOCORRO, META, frame)["at"] == [fire]
    # without usable metadata a zone-only alert cannot be placed at all
    r = alerts.site_alerts([w], SOCORRO, NO_META, frame)
    assert r["at"] == [] and r["candidates"] == []


def test_site_alerts_candidate_via_bbox(frame):
    near = alerts.normalize(feature("Severe Thunderstorm Warning", geometry=NEARBY_POLY, fid="near"))
    assert geo.bbox_intersects(near["bbox"], frame.bbox)
    assert not geo.point_in_geometry(SOCORRO["lon"], SOCORRO["lat"], near["geometry"])
    r = alerts.site_alerts([near], SOCORRO, META, frame)
    assert r["at"] == [] and r["candidates"] == [near]
    # a site object with attributes works too

    class Site:
        lat, lon = SOCORRO["lat"], SOCORRO["lon"]
    assert alerts.site_alerts([near], Site(), META, frame)["candidates"] == [near]


def test_site_alerts_none_and_sorted(frame):
    far = alerts.normalize(feature("Tornado Warning", geometry=FAR_POLY, fid="far"))
    r = alerts.site_alerts([far], SOCORRO, META, frame)
    assert r == {"at": [], "candidates": []}
    assert alerts.site_alerts([], SOCORRO, META, frame) == {"at": [], "candidates": []}
    assert alerts.site_alerts([far], SOCORRO, META, None)["candidates"] == []
    # mixed bag comes back sorted in both lists
    a_watch = alerts.normalize(flood_watch())
    a_ffw = alerts.normalize(ffw())
    a_stmt = alerts.normalize(feature("Special Weather Statement", geometry=SOCORRO_POLY,
                                      severity="Minor", fid="sws"))
    n_adv = alerts.normalize(feature("Wind Advisory", geometry=NEARBY_POLY, fid="nadv"))
    n_warn = alerts.normalize(feature("Severe Thunderstorm Warning", geometry=NEARBY_POLY,
                                      fid="nwarn"))
    r = alerts.site_alerts([a_stmt, n_adv, a_watch, far, n_warn, a_ffw], SOCORRO, META, frame)
    assert [x["id"] for x in r["at"]] == ["urn:oid:ffw", "urn:oid:watch", "sws"]
    assert [x["id"] for x in r["candidates"]] == ["nwarn", "nadv"]


# ---- sort_alerts ---------------------------------------------------------------------
def test_sort_alerts_order():
    def rec(event, severity="Severe", ends=FUTURE, fid=None):
        return alerts.normalize(feature(event, severity=severity, ends=ends, expires=None,
                                        fid=fid or event + str(ends)))
    tor = rec("Tornado Warning", "Extreme")
    ffw_early = rec("Flash Flood Warning", ends="2099-01-01T00:00:00+00:00", fid="e")
    ffw_late = rec("Flash Flood Warning", ends="2099-06-01T00:00:00+00:00", fid="l")
    ffw_open = rec("Flash Flood Warning", ends=None, fid="o")
    stw = rec("Severe Thunderstorm Warning")           # same rank as FFW: by event name
    watch = rec("Flood Watch")
    adv = rec("Wind Advisory", "Minor")
    stmt = rec("Special Weather Statement", "Minor")
    other = rec("Test", "Unknown")
    shuffled = [other, ffw_open, stmt, watch, ffw_late, adv, stw, ffw_early, tor]
    got = alerts.sort_alerts(shuffled)
    assert got == [tor, ffw_early, ffw_late, ffw_open, stw, watch, adv, stmt, other]
    assert shuffled[0] is other                           # input not mutated
    assert alerts.sort_alerts(None) == [] and alerts.sort_alerts(iter([])) == []
    # hand-built records with missing keys sort last instead of crashing
    weird = [{"event": "X"}, {"rank": 1, "event": "Y", "end_dt": "not a datetime"}, tor]
    assert alerts.sort_alerts(weird)[0] is tor
    # reverse order is the map draw order: warnings on top
    assert list(reversed(alerts.sort_alerts([watch, tor])))[-1] is tor
    # a naive datetime (should not happen) is treated as open-ended, no TypeError
    naive = copy.deepcopy(ffw_early)
    naive["end_dt"] = datetime(2099, 1, 1)
    assert alerts.sort_alerts([naive, ffw_late])[0] is ffw_late
    assert alerts.FAR_FUTURE.tzinfo is timezone.utc
