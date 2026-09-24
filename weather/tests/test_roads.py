"""Tests for weather.roads: New Mexico emergency road closures (NMRoads JSON + RSS) and the
road-related NWS storm reports.

The fixtures at the bottom are REAL payloads fetched on 2026-09-23 (nmroads.json, rss.xml
and IEM's lsr.geojson), cut down to representative items: long line geometries thinned to
8 vertices and long roadwork texts shortened, everything else verbatim. The stale I-25 mm 290
ramp "Closure" (a resurfacing job) comes from the research snapshot taken an hour earlier.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from weather import geo, http, roads, util
from weather.config import DEFAULT_SITES

JSON_URL = "https://nmroads.com/nmroads.json"
RSS_URL = "https://nmroads.com/rss.xml"
LAST_MOD = "Wed, 23 Sep 2026 21:34:00 GMT"
NOW = datetime(2026, 9, 23, 21, 40, tzinfo=timezone.utc)      # when the fixtures were taken
MDT = timezone(timedelta(hours=-6))
MST = timezone(timedelta(hours=-7))

# NMDOT event ids in the fixture (first 8 hex digits)
NM41_CRASH = "dd4b80e9"         # Closure: crash, emergency services on scene (ABQ map)
NM94_FLOOD = "b3f0d585"         # Closure: "Roadway is closed due to flooding"
NM273_FLOOD = "16c96136"        # Closure: "Roads are impassible" + title repeated in the text
I25_RAMP = "f996224f"           # Closure: resurfacing project (construction) -> excluded
STERN_RD = "110f7cf2"           # Difficult Driving Conditions, route 'null-null', both lanes closed
NM252_WATER = "dfbcf2f3"        # Difficult Driving Conditions: standing water (Clovis, Ft Sumner)
NM333_WATER = "1140ecaf"        # Difficult Driving Conditions: standing water (ABQ)
SOCORRO_AREA = "829db998"       # area-wide standing water, 'null-null' point (Socorro)
I25_LANE_WATER = "50aa3542"     # Lane Closure because of standing water -> water on road
US60_DUP_A, US60_DUP_B = "15c417d3", "0c4a2d89"     # the same roadwork posted twice
US60_CLOVIS = "c7b95069"        # Roadwork titled "Roadway closed." (reconstruction)
KEPT = {NM41_CRASH: "closure", NM94_FLOOD: "closure", NM273_FLOOD: "closure",
        STERN_RD: "closure", NM252_WATER: "water", NM333_WATER: "water",
        SOCORRO_AREA: "water", I25_LANE_WATER: "water"}


def _features():
    return [json.loads(line) for line in NM_FEATURES.splitlines() if line.strip()]


def nm_json(features=None) -> bytes:
    doc = {"feed_info": NM_FEED_INFO, "type": "FeatureCollection",
           "features": _features() if features is None else features}
    return json.dumps(doc).encode("utf-8")


def nm_rss(drop=(), extra="") -> bytes:
    """The fixture rss.xml, without the items whose guid starts with one of ``drop``."""
    text = NM_RSS
    for pre in drop:
        text = re.sub(r"<item>(?:(?!</item>).)*<guid[^>]*>%s[^<]*</guid>.*?</item>\n?" % pre,
                      "", text, flags=re.S)
    return text.replace("</channel>", extra + "</channel>").encode("utf-8")


def lsr_json(features=None) -> bytes:
    feats = [json.loads(line) for line in LSR_FEATURES.splitlines() if line.strip()]
    return json.dumps({"type": "FeatureCollection",
                       "features": feats if features is None else features}).encode("utf-8")


def ids(events):
    return [e["id"][:8] for e in events]


def frame_for(cfg, slug):
    s = next(x for x in DEFAULT_SITES if x["slug"] == slug)
    return geo.MapFrame(s["lat"], s["lon"], cfg.map_km, cfg.map_px, cfg.tile_zoom)


@pytest.fixture
def now(monkeypatch):
    monkeypatch.setattr(util, "utcnow", lambda: NOW)
    return NOW


@pytest.fixture
def feeds(fake_http):
    """Both NMRoads files and the storm reports served by the fake, with validators."""
    fake_http.add("nmroads.com/nmroads.json", nm_json())
    fake_http.add("nmroads.com/rss.xml", nm_rss())
    fake_http.add("mesonet.agron.iastate.edu/geojson/lsr.geojson", lsr_json())
    fake_http.validators("nmroads.com/nmroads.json", etag='"j1"', last_modified=LAST_MOD)
    fake_http.validators("nmroads.com/rss.xml", etag='"r1"', last_modified=LAST_MOD)
    return fake_http


# ---- dates and text ----------------------------------------------------------------------
@pytest.mark.parametrize("text, expected", [
    ("2026-09-16 12:26:04.998", datetime(2026, 9, 16, 12, 26, 4, 998000, MDT)),
    ("2026-07-20 06:40:46.0", datetime(2026, 7, 20, 6, 40, 46, tzinfo=MDT)),
    ("2026-01-15 08:00:00.0", datetime(2026, 1, 15, 8, 0, tzinfo=MST)),      # winter: MST
    ("2026-09-23 15:07", datetime(2026, 9, 23, 15, 7, tzinfo=MDT)),
    ("Tue Jun 16 09:30:10 MDT 2026", datetime(2026, 6, 16, 9, 30, 10, tzinfo=MDT)),
    ("Tue Feb 10 12:58:29 MST 2026", datetime(2026, 2, 10, 12, 58, 29, tzinfo=MST)),
    ("Tue Feb 10 12:58:29 XYZ 2026", datetime(2026, 2, 10, 12, 58, 29, tzinfo=MST)),  # local
    ("Mon, 20 Jul 2026 06:40:46 -0600", datetime(2026, 7, 20, 6, 40, 46, tzinfo=MDT)),
    ("2026-09-23T21:34:00Z", datetime(2026, 9, 23, 21, 34, tzinfo=timezone.utc)),
    ("2026-09-23T15:34:00-06:00", datetime(2026, 9, 23, 21, 34, tzinfo=timezone.utc)),
    ("unknown", None), ("", None), (None, None), ("yesterday", None),
    ("Tue Foo 10 12:58:29 MST 2026", None), ("2026-13-45 99:00:00", None),
])
def test_parse_nm_date_formats(text, expected):
    got = roads.parse_nm_date(text)
    assert got == expected
    if got is not None:
        assert got.tzinfo is not None


def test_clean_text_and_cause():
    raw = ("Standing water on roadway. Use extreme caution. Cloudy conditions exist."
           "<autodescriptiondelimiter>Roadway is closed due to flooding.&nbsp;<br/> ")
    assert roads.clean_text(raw) == ("Standing water on roadway. Use extreme caution. Cloudy "
                                     "conditions exist. Roadway is closed due to flooding.")
    assert roads.clean_text("a &lt;autodescriptiondelimiter&gt; b &amp; <b>c</b>") == "a b & c"
    segs = roads._segments(raw)
    assert segs == ["Standing water on roadway. Use extreme caution. Cloudy conditions exist.",
                    "Roadway is closed due to flooding."]
    # boilerplate goes, the repeated title at the start of the free text goes
    title = ("Closure, NM 273 northbound and southbound from mile marker 9, 2 miles north of "
             "Santa Teresa to mile marker 14, 1 mile west of TX State Line.")
    cause = roads.make_cause(["Roads are impassible.", title[:-1] + "., due to flooding. "
                              "Traffic is being detoured onto Borderland or Westside Road. "
                              "Use extreme caution."], title)
    assert cause == ("Roads are impassible. Due to flooding. Traffic is being detoured onto "
                     "Borderland or Westside Road.")
    assert roads.make_cause(segs + ["Standing water on roadway."], "") == \
        "Standing water on roadway. Roadway is closed due to flooding."
    assert roads.make_cause(["Use extreme caution."], "") == ""
    long = roads.make_cause(["word " * 200], "")
    assert len(long) <= roads.CAUSE_MAX and long.endswith("…")


# ---- classification ------------------------------------------------------------------------
@pytest.mark.parametrize("category, title, narrative, kind, known", [
    # real NMDOT texts (2026-09-23)
    ("Closure", "Closure, NM 94 northbound and southbound from mile marker 14, Ledoux to mile "
     "marker 16, 2 miles north of Ledoux. Roadway closed.",
     "Standing water on roadway. Use extreme caution. Cloudy conditions exist. Roadway is "
     "closed due to flooding.", "closure", True),
    ("Closure", "Closure, NM 41 northbound and southbound from mile marker 17",
     "Emergency services are on the scene and working to clear the crash scene as soon as "
     "possible.", "closure", True),
    ("Closure", "Closure, I 25 northbound at mile marker 290, (US 285).",
     "The NMDOT and Albuquerque Asphalt will be closing the Northbound off ramp on I-25 at MM "
     "290 for resurfacing project starting on Monday September 21st going through Tuesday, "
     "September 22nd. Please obey all traffic control personnel and proceed with caution "
     "through the roadwork zone.", None, False),
    ("Closure", "Closure, NM 14 at mile marker 3.", "", "closure", False),
    ("Closure", "Closure, NM 14 at mile marker 3.", "Roads are wet and may become slick.",
     "closure", False),
    ("Closure", "Closure, NM 434 at mile marker 21.", "Closed for the season.", None, False),
    ("Roadwork", "Roadwork, US 60 from mile marker 387, 1 mile west of Clovis to mile marker "
     "389, eastside of Clovis. Roadway closed.",
     "Roadwork~~~Road Reconstruction project on US60/84 from MP 387.800 to MP 389.120. "
     "Westbound lanes of US 60 closed for reconstruction from Grand St to Prince St.",
     None, False),
    ("Roadwork", "Roadwork, I 25 at mile marker 414, (SPRINGER NORTH). Roadway closed.",
     "For about the first month, crews will be demolishing the bridge at night.", None, False),
    ("Roadwork", "Roadwork, NM 26 from mile marker 0 to mile marker 46.",
     "Centerline rumble strips are being installed to help reduce head-on and crossover "
     "crashes. The road will be closed nightly.", None, False),
    ("Construction Closure", "Construction Closure, NM 118 at mile marker 9.",
     "NM 118 is closed at mile marker 9 (east of Manuelito) due to bridge rehabilitation "
     "project. Please use I-40 as a detour.", None, False),
    ("Lane Closure", "Lane Closure, I 25 northbound from mile marker 276",
     "Standing water on roadway. Use extreme caution. The left lane is closed due to standing "
     "water on the roadway.", "water", True),
    ("Lane Closure", "Lane Closure, NM 3 southbound from mile marker 2",
     "One lane is closed due to a stalled crane.", None, False),
    ("Lane Closure", "Lane Closure, I 25 northbound at mile marker 9, (DONA ANA).",
     "Lane Closure, I 25 northbound at mile marker 9, (DONA ANA) will have driving right lane "
     "closed with maintainers in the area clearing debris from roadway.", None, False),
    ("Alert", "Alert, NM 247 from mile marker 0 Corona to mile marker 48.",
     "Motorists be advised NM 247 will be closed intermittently to allow vehicles carrying "
     "wind turbine blades to safely access the roadway.", None, False),
    ("Alert", "Alert, US 380 at mile marker 43, 13 miles east of Bingham.",
     "White Sands Missile Range periodically has missile firings over US 70 and/or US 380. "
     "During the firings the road or roads will be temporarily closed.", None, False),
    ("Alert", "Alert, I 40 eastbound at mile marker 251.",
     "Currently, Anton Chico Rest Area (Eastbound) is closed due to flooding.", None, False),
    ("Alert", "Alert, NM 120 from mile marker 12 to mile marker 3.",
     "The roadway has limited width clearance. Tree cover will produce prolonged icy and "
     "snow-packed road conditions, four-wheel drive vehicles are recommended.", None, False),
    ("Difficult Driving Conditions", "Difficult Driving Conditions, Frontage Road.",
     "Roads are wet and may become slick. Difficult Driving Conditions, Frontage Road 1035, "
     "Stern Rd., both lanes are closed from Cholla Rd to NM 225 (Mesquite). Standing water "
     "on roadway.", "closure", True),
    ("Difficult Driving Conditions", "Difficult Driving Conditions, NM 252 from mile marker 24",
     "Standing water on roadway. Use extreme caution. Roadway flooding.", "water", True),
    ("Difficult Driving Conditions", "Difficult Driving Conditions, NM 273 from mile marker 10",
     "Standing water on roadway. Water running over roadway. When the roadway is flooded do "
     "not cross and seek an alternate route. Turn around, don't drown.", "water", True),
    ("Difficult Driving Conditions",
     "Difficult Driving Conditions exist throughout the Las Cruces - 41-51 area.",
     "Roads are wet and may become slick.", None, False),
    ("Difficult Driving Conditions", "Difficult Driving Conditions, NM 107 from mile marker 41",
     "Roads are wet and may become slick. Roadway very muddy due to ongoing rains. 4 x 4 "
     "vehicle recommended.", None, False),
    ("Fair Driving Conditions", "Fair Driving Conditions exist throughout the Silver City area.",
     "The Silver City Patrol has reported roadways with a light rain & wet.", None, False),
    # plausible texts NMRoads has not carried today
    ("Roadwork", "Roadwork, NM 4 from mile marker 30.",
     "NM 4 is closed in both directions due to a rock slide.", "closure", True),
    ("Alert", "Alert, NM 152.", "NM 152 is impassable, washed out at mile marker 20.",
     "closure", True),
    ("Roadwork", "Roadwork, NM 4.",
     "Road closed for paving. Damaged pavement will be replaced as part of the project.",
     None, False),
    ("Severe Driving Conditions", "Severe Driving Conditions, I 40 from mile marker 300",
     "I-40 is closed in both directions due to blowing snow and ice.", "closure", True),
    ("Crash", "Crash, I 25 northbound at mile marker 200. Roadway closed.", "", "closure", True),
    ("Crash", "Crash, I 25 northbound at mile marker 200.", "Right lane blocked.", None, False),
    ("Closure", "Closure, NM 6.", "Road closed due to storm damage; crews on scene.",
     "closure", True),
    ("Closure", "Closure, NM 6.", "Closed for guardrail repairs on the storm drain project.",
     None, False),
])
def test_classify_event(category, title, narrative, kind, known):
    got_kind, got_known, reason = roads.classify_event(category, title, narrative)
    assert (got_kind, got_known) == (kind, known), reason


def test_water_items_follow_the_switch():
    for cat, text in (("Difficult Driving Conditions", "Standing water on roadway."),
                      ("Lane Closure", "The left lane is closed due to standing water.")):
        assert roads.classify_event(cat, "t", text, water=True)[0] == "water"
        assert roads.classify_event(cat, "t", text, water=False)[0] is None
    # a closure stays a closure without the water switch
    assert roads.classify_event("Closure", "t", "Roadway is closed due to flooding.",
                                water=False)[0] == "closure"


@pytest.mark.parametrize("text, emergency", [
    ("Road closed due to a wildfire.", True), ("Brush fire near the road.", True),
    ("Road closed due to storm damage.", True), ("Rock slide at mile marker 4.", True),
    ("Hazmat spill, road closed.", True), ("Icy spots.", True),
    ("Blowing dust, zero visibility.", True), ("Law enforcement on scene.", True),
    ("A sinkhole opened.", True), ("The road washed out.", True), ("Downed power lines.", True),
    ("Crews are clearing debris.", True),
    ("Replace the fire hydrant.", False), ("Storm drain installation.", False),
    ("Wind turbine blades convoy.", False), ("Crash attenuator repairs.", False),
    ("Dust control measures in place.", False), ("Visit the office for service notices.", False),
    ("Missile firings.", False), ("Completion expected in 2027, weather permitting.", False),
    ("Damaged pavement will be replaced as part of the reconstruction project.", False),
    ("Rumble strips reduce crossover crashes.", False),
])
def test_emergency_words(text, emergency):
    assert roads.emergency_in([text]) is emergency


# ---- geometry --------------------------------------------------------------------------------
def test_clean_geometry_and_touches():
    assert roads.clean_geometry({"type": "Point", "coordinates": [-106.5, 35.1, 1600]}) == \
        {"type": "Point", "coordinates": [-106.5, 35.1]}
    line = roads.clean_geometry({"type": "LineString", "coordinates": [
        [-106.5, 35.1], ["x", 1], [-200, 35], None, [-106.4, float("nan")], [-106.3, 35.2]]})
    assert line == {"type": "LineString", "coordinates": [[-106.5, 35.1], [-106.3, 35.2]]}
    assert roads.clean_geometry({"type": "LineString", "coordinates": [[-106.5, 35.1]]}) == \
        {"type": "Point", "coordinates": [-106.5, 35.1]}
    mls = roads.clean_geometry({"type": "MultiLineString",
                                "coordinates": [[[0, 0], [1, 1]], [], [[2, 2], [3, 3]]]})
    assert mls["coordinates"] == [[[0, 0], [1, 1]], [[2, 2], [3, 3]]]
    assert roads.line_bbox(mls) == (0, 0, 3, 3)
    for bad in (None, {}, {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [0, 1]]]},
                {"type": "Point", "coordinates": [True, 1]}, {"type": "LineString"}):
        assert roads.clean_geometry(bad) is None
    box = (0.0, 0.0, 1.0, 1.0)
    assert roads.geometry_touches({"type": "Point", "coordinates": [0.5, 0.5]}, box)
    assert roads.geometry_touches({"type": "Point", "coordinates": [1.0, 0.2]}, box)   # edge
    assert not roads.geometry_touches({"type": "Point", "coordinates": [1.5, 0.5]}, box)
    # a segment crossing the box without a vertex inside it counts; one passing by does not
    assert roads.geometry_touches({"type": "LineString", "coordinates": [[-1, 0.5], [2, 0.5]]},
                                  box)
    assert roads.geometry_touches({"type": "LineString", "coordinates": [[-1, 2], [2, -1]]}, box)
    assert not roads.geometry_touches({"type": "LineString",
                                       "coordinates": [[-1, 1.5], [0.4, 3]]}, box)
    assert roads.geometry_touches(mls, (2.5, 2.5, 4, 4)) and not roads.geometry_touches(None, box)


# ---- NMRoads end to end ----------------------------------------------------------------------
def test_fetch_nm_roads_joins_json_and_rss(cfg, cache, feeds):
    res = roads.fetch_nm_roads(cfg, cache)
    assert res["ok"] and not res["stale"] and res["error"] is None and res["age_s"] == 0.0
    assert res["count_raw"] == 23 and res["count_region"] == 23
    assert res["feed_time"] == datetime(2026, 9, 23, 21, 34, tzinfo=timezone.utc)
    assert res["excluded"] == {"Alert": 3, "Closure": 1, "Construction Closure": 1,
                               "Difficult Driving Conditions": 2, "Fair Driving Conditions": 1,
                               "Lane Closure": 2, "Roadwork": 4, "duplicate": 1}
    assert sum(res["excluded"].values()) + len(res["events"]) == res["count_region"]
    got = {e["id"][:8]: e["kind"] for e in res["events"]}
    assert got == KEPT
    # order: closures with a known cause (newest first), then water (newest first)
    assert ids(res["events"]) == [NM41_CRASH, NM273_FLOOD, NM94_FLOOD, STERN_RD,
                                  I25_LANE_WATER, SOCORRO_AREA, NM252_WATER, NM333_WATER]
    for e in res["events"]:
        assert set(e) == {"id", "kind", "category", "title", "route", "mm_from", "mm_to",
                          "direction", "cause", "cause_known", "updated", "posted",
                          "time_approx", "area_wide", "geometry", "bbox", "source", "link"}
        assert e["time_approx"] is False
        assert e["source"] == "NMDOT" and e["link"] == "https://nmroads.com/"
        assert "autodescriptiondelimiter" not in e["cause"] and "<" not in e["cause"]
        assert len(e["cause"]) <= 400 and e["cause_known"]
        assert e["updated"].tzinfo is not None and e["geometry"]["type"] in ("Point",
                                                                             "LineString")
        assert e["bbox"] == roads.line_bbox(e["geometry"])
    ev = {e["id"][:8]: e for e in res["events"]}
    nm94 = ev[NM94_FLOOD]
    assert nm94["category"] == "Closure" and nm94["route"] == "NM 94"
    assert (nm94["mm_from"], nm94["mm_to"], nm94["direction"]) == (14.0, 16.0, "both directions")
    assert nm94["cause"] == "Standing water on roadway. Roadway is closed due to flooding."
    assert nm94["title"].startswith("Closure, NM 94 northbound and southbound from mile marker")
    # ages come from the RSS "Update Date" read as Denver time, never from the JSON's
    # update_date (10:59:16-06:00 in the feed: 6 h early)
    assert nm94["updated"] == datetime(2026, 9, 22, 22, 59, 16, tzinfo=timezone.utc)
    assert nm94["posted"] == datetime(2026, 9, 22, 22, 59, 16, tzinfo=timezone.utc)
    assert ev[NM273_FLOOD]["cause"].startswith("Roads are impassible. Due to flooding.")
    # 'null-null' route: no route, no mile markers, the location is in the cause text
    stern = ev[STERN_RD]
    assert stern["route"] is None and stern["mm_from"] is None and stern["direction"] is None
    assert stern["title"] == "Difficult Driving Conditions"
    assert "both lanes are closed from Cholla Rd" in stern["cause"]
    assert ev[SOCORRO_AREA]["geometry"]["type"] == "Point" and ev[SOCORRO_AREA]["route"] is None
    lane = ev[I25_LANE_WATER]
    assert lane["category"] == "Lane Closure" and lane["direction"] == "northbound"
    assert (lane["route"], lane["mm_from"], lane["mm_to"]) == ("I 25", 276.0, 277.0)
    # nothing that is roadwork or construction slipped through
    for e in res["events"]:
        assert not roads.CONSTRUCTION.search(e["cause"]), e["cause"]
    # both files were fetched with a conditional GET
    assert feeds.calls == ["COND " + JSON_URL, "COND " + RSS_URL]


def test_conditional_refetch_uses_the_cached_copy(cfg, cache, feeds):
    first = roads.fetch_nm_roads(cfg, cache)
    second = roads.fetch_nm_roads(cfg, cache)       # the fake answers 304 to the stored ETag
    assert second["ok"] and not second["stale"] and ids(second["events"]) == ids(first["events"])
    assert feeds.calls.count("COND " + JSON_URL) == 2
    meta, _ = cache.get_any("nmroads_json__meta")
    assert meta["etag"] == '"j1"' and meta["last_modified"] == LAST_MOD


def test_duplicates_keep_the_most_recently_updated(cfg, cache, fake_http):
    feats = _features()
    orig = next(f for f in feats if f["id"].startswith(NM94_FLOOD))
    twin = copy.deepcopy(orig)
    twin["id"] = "0bad0000-0000-0000-0000-000000000000"
    feats.append(twin)
    block = re.search(r"<item>(?:(?!</item>).)*%s.*?</item>" % NM94_FLOOD, NM_RSS, re.S).group(0)
    older = block.replace(orig["id"], twin["id"]).replace("Update Date: 2026-09-22 16:59:16",
                                                          "Update Date: 2026-09-21 16:59:16")
    fake_http.add("nmroads.json", nm_json(feats))
    fake_http.add("rss.xml", nm_rss(extra=older + "\n"))
    res = roads.fetch_nm_roads(cfg, cache)
    nm94 = [e for e in res["events"] if e["route"] == "NM 94"]
    assert [e["id"][:8] for e in nm94] == [NM94_FLOOD]
    assert res["excluded"]["duplicate"] == 2 and res["count_region"] == 24
    # the newer copy wins whichever comes first
    fake_http.routes.clear()
    fake_http.add("nmroads.json", nm_json([twin] + [f for f in feats if f is not twin]))
    fake_http.add("rss.xml", nm_rss(extra=older.replace("2026-09-21", "2026-09-23") + "\n"))
    res = roads.fetch_nm_roads(cfg, cache)
    assert [e["id"][:8] for e in res["events"] if e["route"] == "NM 94"] == ["0bad0000"]


def test_region_filter(cfg, cache, fake_http):
    feats = _features()
    tx = copy.deepcopy(next(f for f in feats if f["id"].startswith(NM94_FLOOD)))
    tx["id"] = "7e7a5000-0000-0000-0000-000000000000"
    tx["geometry"] = {"type": "Point", "coordinates": [-101.85, 33.58]}        # Lubbock, TX
    fake_http.add("nmroads.json", nm_json(feats + [tx]))
    fake_http.add("rss.xml", nm_rss())
    res = roads.fetch_nm_roads(cfg, cache)
    assert res["count_raw"] == 24 and res["count_region"] == 23
    assert "7e7a5000" not in ids(res["events"])


def test_json_only_is_degraded_but_usable(cfg, cache, fake_http):
    fake_http.add("nmroads.json", nm_json())
    fake_http.fail("rss.xml")
    res = roads.fetch_nm_roads(cfg, cache)
    assert res["ok"] and not res["stale"]
    assert "rss.xml" in res["error"] and "causes unknown" in res["error"]
    ev = {e["id"][:8]: e for e in res["events"]}
    # full closures are still listed (from the JSON title), but their cause is not known and
    # no age can be given (the JSON dates are 6 h off); water items need the RSS text
    # (the I-25 ramp resurfacing "Closure" too: without the text nothing tells it apart, and
    # hiding a possible emergency closure would be worse than listing it "cause not stated")
    assert set(ev) == {NM41_CRASH, NM94_FLOOD, NM273_FLOOD, I25_RAMP}
    assert all(not e["cause_known"] and e["updated"] is None and e["cause"] == ""
               for e in ev.values())
    assert ev[NM94_FLOOD]["route"] == "NM 94" and ev[NM94_FLOOD]["mm_from"] == 14.0
    assert ev[NM94_FLOOD]["geometry"]["type"] == "LineString"
    assert res["feed_time"] is None                  # no Last-Modified from this server


def test_rss_only_uses_points(cfg, cache, fake_http):
    fake_http.fail("nmroads.json")
    fake_http.add("rss.xml", nm_rss())
    res = roads.fetch_nm_roads(cfg, cache)
    assert res["ok"] and "nmroads.json" in res["error"]
    assert {e["id"][:8]: e["kind"] for e in res["events"]} == KEPT
    assert all(e["geometry"]["type"] == "Point" for e in res["events"])
    # feed time from the RSS lastBuildDate when there is no Last-Modified header
    assert res["feed_time"] == datetime(2026, 9, 23, 21, 34, tzinfo=timezone.utc)


def test_last_good_copy_and_its_limit(cfg, cache, feeds):
    good = roads.fetch_nm_roads(cfg, cache)
    feeds.routes.clear()
    feeds.fail("nmroads.com")
    res = roads.fetch_nm_roads(cfg, cache)
    assert res["ok"] and res["stale"] and "nmroads.json" in res["error"]
    assert ids(res["events"]) == ids(good["events"]) and res["age_s"] >= 0
    cfg.roads_max_stale = 0
    for key in ("nmroads_json__meta", "nmroads_rss__meta"):     # make the copy 10 s old
        meta, _ = cache.get_any(key)
        meta["ts"] -= 10
        cache.put(key, meta)
    res = roads.fetch_nm_roads(cfg, cache)
    assert not res["ok"] and res["events"] == [] and res["excluded"] == {}
    assert res["count_raw"] == 0 and res["feed_time"] is None


def test_the_fresher_file_decides_which_events_exist(cfg, cache, fake_http):
    fake_http.add("nmroads.json", nm_json())
    fake_http.add("rss.xml", nm_rss())
    roads.fetch_nm_roads(cfg, cache)
    meta, _ = cache.get_any("nmroads_json__meta")
    meta["ts"] -= 600                                   # the JSON copy is 10 min old ...
    cache.put("nmroads_json__meta", meta)
    fake_http.routes.clear()
    fake_http.fail("nmroads.json")                      # ... and cannot be refreshed,
    fake_http.add("rss.xml", nm_rss(drop=[NM94_FLOOD]))  # while NM 94 has reopened
    res = roads.fetch_nm_roads(cfg, cache)
    assert res["ok"] and res["stale"]
    assert NM94_FLOOD not in ids(res["events"]) and NM41_CRASH in ids(res["events"])
    nm41 = next(e for e in res["events"] if e["id"].startswith(NM41_CRASH))
    assert nm41["geometry"]["type"] == "LineString"     # the stale JSON still adds the line


def test_bad_payloads_are_rejected_not_cached(cfg, cache, fake_http):
    fake_http.add("nmroads.json", b"<html>maintenance</html>")
    fake_http.add("rss.xml", b'{"not": "rss"}')
    res = roads.fetch_nm_roads(cfg, cache)
    assert not res["ok"] and "unexpected payload" in res["error"]
    assert cache.get_bytes("nmroads_json", http.BYTES_EXT) is None
    fake_http.routes.clear()
    fake_http.add("nmroads.json", b'{"type": "FeatureCollection"}')     # no feature list
    fake_http.add("rss.xml", b"<rss><nochannel/></rss>")
    assert not roads.fetch_nm_roads(cfg, cache)["ok"]
    # an RSS file with a DOCTYPE (entity declarations) is refused before parsing
    evil = nm_rss().replace(b"<rss ", b'<!DOCTYPE rss [<!ENTITY a "aaaa">]>\n<rss ', 1)
    with pytest.raises(ValueError, match="DOCTYPE"):
        roads.parse_rss(evil)


def test_fetch_nm_roads_never_raises(cfg, cache, feeds, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("surprise")
    monkeypatch.setattr(roads, "build_event", boom)
    res = roads.fetch_nm_roads(cfg, cache)
    assert not res["ok"] and "RuntimeError: surprise" in res["error"] and res["events"] == []


def test_odd_features_are_skipped(cfg, cache, fake_http):
    feats = _features()[:2] + [
        None, {"type": "Feature"}, {"id": "", "properties": {}},
        {"id": "no-geom", "properties": {"core_details": {"name": "Closure"}}, "geometry": None},
        {"id": "bad", "properties": "x", "geometry": {"type": "Point", "coordinates": [1]}}]
    fake_http.add("nmroads.json", nm_json(feats))
    fake_http.add("rss.xml", nm_rss())
    res = roads.fetch_nm_roads(cfg, cache)
    assert res["ok"] and res["count_raw"] == 4 and res["count_region"] == 2


# ---- storm reports -----------------------------------------------------------------------------
def test_storm_reports(cfg, cache, fake_http, now):
    fake_http.add("lsr.geojson", lsr_json())
    res = roads.fetch_storm_reports(cfg, cache)
    assert res["ok"] and not res["stale"] and res["error"] is None
    assert fake_http.calls == ["https://mesonet.agron.iastate.edu/geojson/lsr.geojson"
                               "?hours=24&states=NM"]
    got = [(r["place"], r["time"].strftime("%d %H:%M")) for r in res["reports"]]
    # newest first; the Texas report, "water rescue ... flooding of a home" and "flooding in
    # the Rio Penasco and nearby low lying areas" name no road; Range Route and the Mora
    # school are older than 24 h; the corrected Ruidoso report replaces the first one
    assert got == [("6 ESE Elk", "23 20:53"), ("1 NW Ruidoso", "23 16:00"),
                   ("2 N House", "23 00:50"), ("Las Nutrias", "22 23:19"),
                   ("Monte Aplanado", "22 22:38"), ("1 SE Cleveland", "22 22:15"),
                   ("1 SSW Mora", "22 22:05")]
    r = next(x for x in res["reports"] if x["place"] == "Las Nutrias")
    assert r["remark"].startswith("NMDOT reports NM State Highway 304 closed at Mile Marker 11")
    assert set(r) == {"id", "kind", "type", "time", "place", "county", "remark", "lat", "lon",
                      "geometry", "bbox", "source"}
    assert r["kind"] == "report" and r["type"] == "FLASH FLOOD" and r["county"] == "Socorro"
    assert r["source"] == "NWS ABQ" and r["id"].startswith("lsr-")
    assert (r["lon"], r["lat"]) == (-106.77, 34.48)
    assert r["bbox"] == (-106.77, 34.48, -106.77, 34.48)
    assert r["geometry"] == {"type": "Point", "coordinates": [-106.77, 34.48]}
    assert r["time"] == datetime(2026, 9, 22, 23, 19, tzinfo=timezone.utc)
    assert "Corrected time" in next(x for x in res["reports"]
                                    if x["place"] == "1 NW Ruidoso")["remark"]
    again = roads.fetch_storm_reports(cfg, cache)
    assert [x["id"] for x in again["reports"]] == [x["id"] for x in res["reports"]]


def test_storm_report_window_and_switch(cfg, cache, fake_http, now):
    fake_http.add("lsr.geojson", lsr_json())
    cfg.lsr_hours = 72
    res = roads.fetch_storm_reports(cfg, cache)
    assert "hours=72" in fake_http.calls[-1]
    places = {r["place"] for r in res["reports"]}
    # "Range Route 8, 9 and 12 impassable" (13 W Oscuro) is on White Sands Missile Range:
    # military range routes, left out like NMDOT's missile-range alerts
    assert "Mora" in places and "13 W Oscuro" not in places
    assert len(res["reports"]) == 8
    cfg.lsr_enabled = False
    n = len(fake_http.calls)
    res = roads.fetch_storm_reports(cfg, cache)
    assert res == {"ok": True, "stale": False, "error": None, "age_s": None, "reports": [],
                   "disabled": True}
    assert len(fake_http.calls) == n


def test_storm_reports_failures(cfg, cache, fake_http, now, monkeypatch):
    fake_http.fail("lsr.geojson")
    res = roads.fetch_storm_reports(cfg, cache)
    assert not res["ok"] and res["reports"] == [] and res["error"]
    # a last good copy is used (and still cut to the time window)
    cache.put("lsr", json.loads(lsr_json()))
    monkeypatch.setattr(util, "utcnow", lambda: NOW + timedelta(hours=18))
    res = roads.fetch_storm_reports(cfg, cache)
    assert res["ok"] and res["stale"] and [r["place"] for r in res["reports"]] == \
        ["6 ESE Elk", "1 NW Ruidoso"]
    # garbage never raises
    fake_http.routes.clear()
    fake_http.add("lsr.geojson", {"type": "FeatureCollection", "features": [
        None, 5, {"properties": None}, {"properties": {"state": "NM", "remark": "road closed",
                                                       "valid": "garbage"}},
        {"properties": {"state": "NM", "remark": "NM 6 closed", "valid": "2026-09-23T20:00:00Z",
                        "lon": "x", "lat": 34}, "geometry": None}]})
    monkeypatch.setattr(util, "utcnow", lambda: NOW)
    res = roads.fetch_storm_reports(cfg, cache)
    assert res["ok"] and res["reports"] == []


@pytest.mark.parametrize("remark, keep", [
    ("NMDOT reports NM State Highway 304 closed at Mile Marker 11 due to standing water.", True),
    ("Video shows water over NM 252 north of House, NM from Alamosa Creek.", True),
    ("Range Route 8, 9 and 12 impassable due to water over roads.", False),     # on WSMR
    ("Muddy floodwaters going across the roadway at Ash Drive.", True),
    ("Arroyo crossing on Pueblo Rd washed out.", True),
    ("I-40 closed in both directions due to blowing snow.", True),
    ("Water rescue due to flooding of a home.", False),
    ("Flooding in the Rio Penasco and nearby low lying areas.", False),
    ("Measured 2.1 inches of rain.", False), ("Tree down on a house.", False),
])
def test_storm_report_road_filter(remark, keep):
    f = {"properties": {"state": "NM", "remark": remark, "valid": "2026-09-23T20:00:00Z",
                        "typetext": "FLASH FLOOD", "wfo": "ABQ", "city": "X", "county": "Y"},
         "geometry": {"type": "Point", "coordinates": [-106.8, 34.1]}}
    assert (roads._lsr_report(f) is not None) is keep
    f["properties"]["state"] = "TX"
    assert roads._lsr_report(f) is None


# ---- per site ------------------------------------------------------------------------------------
def test_site_roads_per_map(cfg, cache, feeds, now):
    nm = roads.fetch_nm_roads(cfg, cache)
    lsr = roads.fetch_storm_reports(cfg, cache)
    view = {s["slug"]: roads.site_roads(nm, lsr, frame_for(cfg, s["slug"])) for s in DEFAULT_SITES}
    assert view["lubbock"] == {"covers_nm": False, "events": [], "reports": []}
    assert all(view[s]["covers_nm"] for s in ("clovis", "fort_sumner", "socorro", "albuquerque"))
    assert ids(view["clovis"]["events"]) == [NM252_WATER]
    assert ids(view["fort_sumner"]["events"]) == [NM252_WATER]
    assert ids(view["socorro"]["events"]) == [SOCORRO_AREA]
    assert ids(view["albuquerque"]["events"]) == [NM41_CRASH, I25_LANE_WATER, NM333_WATER]
    places = {k: [r["place"] for r in v["reports"]] for k, v in view.items()}
    assert places == {"lubbock": [], "clovis": ["2 N House"], "fort_sumner": ["2 N House"],
                      "socorro": ["Las Nutrias"], "albuquerque": ["Las Nutrias"]}
    # the roadwork, the missile-range alert and the muddy NM 107 on these maps are not listed
    shown = {e["id"][:8] for v in view.values() for e in v["events"]}
    assert not shown & {US60_DUP_A, US60_DUP_B, US60_CLOVIS, "cae2d585", "bab415b4",
                        "2f2dae34", "11fc0557"}


def test_site_roads_order_and_bad_input(cfg):
    f = frame_for(cfg, "socorro")
    x, y = f.lon0, f.lat0
    pt = {"type": "Point", "coordinates": [x, y]}
    t = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)
    evs = [{"id": "w-old", "kind": "water", "updated": t, "geometry": pt},
           {"id": "c-unknown", "kind": "closure", "cause_known": False, "updated": t,
            "geometry": pt},
           {"id": "w-new", "kind": "water", "updated": t + timedelta(hours=1), "geometry": pt},
           {"id": "c-old", "kind": "closure", "cause_known": True, "updated": None,
            "geometry": pt},
           {"id": "c-new", "kind": "closure", "cause_known": True, "updated": t, "geometry": pt},
           {"id": "far", "kind": "closure", "cause_known": True, "updated": t,
            "geometry": {"type": "Point", "coordinates": [x + 5, y]}},
           {"id": "excluded", "kind": None, "geometry": pt}, "junk", None]
    reps = [{"id": "r1", "time": t, "geometry": pt},
            {"id": "r2", "time": t + timedelta(hours=2), "geometry": pt},
            {"id": "r3", "time": t, "geometry": None}]
    v = roads.site_roads({"events": evs}, {"reports": reps}, f)
    assert [e["id"] for e in v["events"]] == ["c-new", "c-old", "c-unknown", "w-new", "w-old"]
    assert [r["id"] for r in v["reports"]] == ["r2", "r1"]
    assert roads.site_roads(None, None, f) == {"covers_nm": True, "events": [], "reports": []}
    assert roads.site_roads({"ok": False}, {"reports": "x"}, f)["events"] == []


def _map(lat, lon, miles=100):
    return geo.MapFrame(lat, lon, miles * geo.MI_TO_KM, 600, 9).bbox


@pytest.mark.parametrize("name, lat, lon, covers", [
    # Texas maps inside NM_BOX but with no New Mexico land on them: south of 32 N, or with
    # the west edge in the strip between the Texas line (-103.04) and -103.00
    ("Van Horn, TX", 31.0399, -104.8307, False),
    ("Fort Stockton, TX", 30.8940, -102.8793, False),
    ("Balmorhea, TX", 30.9843, -103.7452, False),
    ("Olton, TX", 34.0, -102.148, False),                 # map west edge -103.021
    ("Lubbock, TX", 33.5779, -101.8552, False),
    # maps that do reach New Mexico
    ("Farwell, TX", 34.3834, -103.0380, True),
    ("Texline, TX", 36.3775, -103.0224, True),
    ("Pecos, TX", 31.4229, -103.4932, True),              # map north edge 32.14 N
    ("El Paso, TX", 31.7619, -106.4850, True),
    ("Hobbs, NM", 32.7026, -103.1360, True),
    ("Four Corners", 36.99902, -109.04519, True),
    ("Raton, NM", 36.9034, -104.4391, True),
])
def test_bbox_covers_nm_follows_the_state_line(name, lat, lon, covers):
    """"Does this map reach New Mexico" uses the state's outline, not NM_BOX: a Texas-only
    map inside the box (Van Horn, Olton) makes no NMDOT or storm-report request and shows no
    New Mexico road block. The outline holds every vertex of the Census polygon, so a map
    that does reach the state is never missed."""
    assert roads.bbox_covers_nm(_map(lat, lon)) is covers, name


def test_bbox_covers_nm_edges():
    """Small maps just either side of a border; a map around the whole state; the outline
    itself lies around the Census extents (-109.0504 .. -103.0020, 31.3322 .. 37.0002)."""
    assert roads.bbox_covers_nm(_map(34.5, -103.02, 10))          # 5-mile half-width: in
    assert not roads.bbox_covers_nm(_map(34.5, -102.90, 10))
    assert roads.bbox_covers_nm(_map(31.9, -106.60, 10))          # Sunland Park
    assert not roads.bbox_covers_nm(_map(31.60, -106.25, 10))     # east El Paso, south of 32 N
    assert not roads.bbox_covers_nm(_map(31.20, -108.60, 10))     # Mexico, below the bootheel
    assert roads.bbox_covers_nm((-120.0, 20.0, -90.0, 45.0))      # the outline inside the box
    assert roads.bbox_covers_nm((-106.0, 34.0, -105.9, 34.1))     # the box inside the outline
    xs = [x for x, _ in roads.NM_OUTLINE]
    ys = [y for _, y in roads.NM_OUTLINE]
    assert -109.07 < min(xs) < -109.0504 and -103.0020 < max(xs) < -102.98
    assert 31.31 < min(ys) < 31.3322 and 37.0002 < max(ys) < 37.02


# ---- review findings: classification ----------------------------------------------------------
ROADWAY = "Closure, I 40 eastbound at mile marker 251. Roadway closed."


@pytest.mark.parametrize("category, narrative", [
    # the owner's exclusions name the event, not every word nearby: a closure caused by an
    # emergency stays one when the text also mentions a rest area, White Sands, a turbine
    # load, an oversize load or a convoy
    ("Closure", "Roadway closed due to a crash. Traffic is being diverted at the Anton Chico "
                "rest area."),
    ("Closure", "US 70 is closed near White Sands due to flooding."),
    ("Closure", "Roadway closed due to blowing dust near White Sands Missile Range."),
    ("Closure", "Roadway closed due to a crash involving a truck hauling wind turbine blades."),
    ("Closure", "Roadway closed due to a rollover of an oversize load."),
    ("Closure", "NM 4 closed due to wildfire. Traffic escorted in convoys by NMSP."),
    ("Alert", "Roadway closed due to a crash. Traffic is being diverted at the rest area."),
])
def test_owner_exclusions_do_not_hide_an_emergency_closure(category, narrative):
    kind, known, reason = roads.classify_event(category, ROADWAY, narrative)
    assert (kind, known) == ("closure", True), reason


@pytest.mark.parametrize("category, narrative", [
    ("Alert", "Currently, Anton Chico Rest Area (Eastbound) is closed due to flooding."),
    ("Closure", "The Gage rest area is closed due to flooding."),
    ("Closure", "US 70 will be closed for up to one hour during missile firings; flooding "
                "possible."),
    ("Closure", "Road closed intermittently for vehicles carrying wind turbine blades."),
    ("Closure", "Road closed for an oversize load convoy."),
])
def test_owner_exclusions_still_apply_without_an_emergency_closure(category, narrative):
    kind, _, reason = roads.classify_event(category, ROADWAY, narrative)
    assert kind is None, reason


@pytest.mark.parametrize("category, narrative", [
    # construction / scheduled work that mentions a hazard word is still roadwork
    ("Closure", "NM 14 will be closed for construction of a new flood control channel."),
    ("Closure", "Road closed for installation of a new snow fence."),
    ("Closure", "Road closed for the rockfall fence installation project."),
    ("Closure", "The ramp will be closed nightly to replace the crash cushion damaged in an "
                "earlier accident."),
    ("Closure", "Scheduled closure: US 64 will be closed for avalanche control and snow removal "
                "operations."),
    ("Closure", "Road closed for burn scar flood mitigation project; closure is scheduled "
                "through October."),
    ("Construction Closure", "NM 53 is closed near Ice Caves Road for paving."),
    ("Roadwork", "The road will be closed nightly for construction of a flood control "
                 "structure."),
    # scheduled, event and seasonal closures without a hazard word
    ("Closure", "The off ramp will be closed Sept 21-22 from 7 a.m. to 5 p.m. for girder "
                "placement."),
    ("Closure", "Road closed for the annual Balloon Fiesta."),
    ("Closure", "Closed for blasting operations."),
    ("Closure", "Road closed for culvert replacement."),
    ("Closure", "Road will be closed daily 9 a.m. to 3 p.m. for rock scaling."),
    ("Closure", "Road closed for night work."),
    ("Closure", "Road closed for the film shoot."),
    ("Closure", "Winter closure: the road is closed from November to May."),
    ("Closure", "Road closed Monday through Friday for utility work."),
])
def test_planned_work_is_not_an_emergency(category, narrative):
    kind, _, reason = roads.classify_event(category, "%s, NM 14." % category, narrative)
    assert kind is None, reason


@pytest.mark.parametrize("narrative", [
    # a hazard named as the cause still counts next to construction wording
    "Road closed due to flooding in the construction zone.",
    "Roadway closed because of a crash in the work zone.",
    "The road was closed overnight due to heavy snow.",
])
def test_a_hazard_named_as_the_cause_beats_construction_context(narrative):
    kind, known, reason = roads.classify_event("Closure", "Closure, NM 14.", narrative)
    assert (kind, known) == ("closure", True), reason


@pytest.mark.parametrize("category, narrative, kind", [
    # abbreviations do not split a sentence into "closed" and "due to ..." halves
    ("Alert", "US 60 has been closed since 3 a.m. due to a jackknifed semi.", "closure"),
    ("Alert", "NM 14 is closed at St. Francis Dr. due to a crash.", "closure"),
    ("Lane Closure", "All eastbound lanes closed until approx. 6 p.m. because of a fatal crash.",
     "closure"),
    ("Alert", "US 60 has been closed since 3 a.m. Tuesday due to flooding.", "closure"),
    ("Alert", "U. S. 70 is closed at mile marker 5 due to a rock slide.", "closure"),
])
def test_abbreviations_keep_a_sentence_whole(category, narrative, kind):
    got, known, reason = roads.classify_event(category, "%s, US 60." % category, narrative)
    assert (got, known) == (kind, True), reason


def test_sentences_split_at_real_boundaries_only():
    s = roads._sentences
    assert s("US 60 has been closed since 3 a.m. due to a jackknifed semi.") == [
        "US 60 has been closed since 3 a.m. due to a jackknifed semi."]
    assert s("Closed at St. Francis Dr. Crews on scene. Expect delays; use caution.") == [
        "Closed at St. Francis Dr. Crews on scene.", "Expect delays;", "use caution."]
    assert s("Road closed. Flooding reported.~~~Use caution") == [
        "Road closed.", "Flooding reported.", "Use caution"]
    assert s("NM 14 closed. due to a crash") == ["NM 14 closed. due to a crash"]
    assert s("") == [] and s(None) == []


# ---- review findings: storm reports -------------------------------------------------------------
def _lsr_feature(remark, typetext="FLASH FLOOD", valid="2026-09-23T20:00:00Z", lat=34.48,
                 lon=-106.77, city="Las Nutrias", wfo="ABQ", product="202609232000-KABQ"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"state": "NM", "remark": remark, "valid": valid,
                           "typetext": typetext, "wfo": wfo, "city": city, "county": "Socorro",
                           "product_id": product, "lat": lat, "lon": lon}}


@pytest.mark.parametrize("remark, keep", [
    # real New Mexico reports of summer 2026 (IEM) that name a flooded or blocked road
    ("Flood waters reported over Maple and Ash Drive.", True),
    ("Police unable to cross McDaniel Bridge on McDaniel Drive due to flood waters.", True),
    ("Several areas of Gallup with 3 to 5 feet of water on roadways. At least 12 vehicles "
     "stuck in flood waters and water rescues were performed.", True),
    ("Flash flood on Cameron Creek deeply inundated the low water crossing on Fellner Road in "
     "Santa Clara, with one vehicle stuck. Delayed report, Time estimated.", True),
    ("Ruidoso EM reports rocks and mud on road due to hillside becoming waterlogged near "
     "intersection of Gavilan Canyon Road and U. S. Highway 70.", True),
    ("Pictures show a flow of mud and rocks onto Main Road.", True),
    ("Ruidoso EM reports of water and mud in the roadway at the 1000 block of Main Rd.", True),
    ("Ruidoso EM reports of water overtopping Johnson Rd near Perk Canyon.", True),
    ("Emergency Manager reported U.S. 491 flooded north of Gallup.", True),
    ("Debris flowing down road. Water over sidewalk.", True),
    # ... and ones that only mention a road as a place
    ("Marshalls parking lot flooded near US 491 and West Lincoln Ave. At least calf-high "
     "water throughout the parking lot.", False),
    ("Mescalero EM reported a person trapped in their home due to high water and that FD was "
     "in route to assist.", False),
    ("Geolocated video showed flood waters reached a road grader 750 feet from the main "
     "channel.", False),
    # (the real Redrock report goes on "Flooding made roads north and west of the Gila River
    # Bridge on Game Dept Road impassable." and is rightly kept)
    ("Geolocated video showed flood waters reached a road grader 750 feet from the main "
     "channel. Flooding made roads north and west of the Gila River Bridge on Game Dept Road "
     "impassable.", True),
    # roads on White Sands Missile Range are range routes, not public roads
    ("Running water and road washouts along several roads in southwest Lincoln County on "
     "WSMR. Time approximate.", False),
    ("WSMR Met reports flooding along Dripping Springs Rd.", True),
])
def test_storm_report_phrasings(remark, keep):
    assert (roads._lsr_report(_lsr_feature(remark)) is not None) is keep


def test_storm_report_reissues_are_folded_in():
    orig = ("NMDOT reports NM State Highway 304 closed at Mile Marker 11 (Las Nutrias) due to "
            "standing water.")
    base = _lsr_feature(orig, valid="2026-09-22T23:19:00Z",
                        product="202609222339-KABQ-NWUS55-LSRABQ")
    # "Corrects previous <type> report from <place>." with the same text: one report
    corr = _lsr_feature("Corrects previous flash flood report from Las Nutrias. " + orig,
                        valid="2026-09-22T23:19:00Z", product="202609230105-KABQ-NWUS55-LSRABQ")
    got = roads.lsr_road_reports([base, corr])
    assert len(got) == 1 and got[0]["remark"].startswith("Corrects previous")
    # "Corrects time on previous ..." moves the time
    moved = _lsr_feature("Corrects time on previous flash flood report from Las Nutrias. "
                         + orig, valid="2026-09-22T22:40:00Z",
                         product="202609230110-KABQ-NWUS55-LSRABQ")
    got = roads.lsr_road_reports([base, moved])
    assert [r["time"].strftime("%H:%M") for r in got] == ["22:40"]
    # a correction that only says what was wrong keeps the corrected report's text
    deming = _lsr_feature("NM 149 closed between MM2 and MM6 because of flooded roadway.",
                          valid="2026-08-07T03:30:00Z", lat=32.19, lon=-107.64,
                          city="8 SE Deming", wfo="EPZ", product="202608070430-KEPZ")
    fix = _lsr_feature("Corrects previous flash flood report from 8 SE Deming. NM-149 should "
                       "read NM-143. Also, report from NMDOT was delayed by nearly an hour "
                       "versus local VFD report.", valid="2026-08-07T03:30:00Z", lat=32.19,
                       lon=-107.64, city="8 SE Deming", wfo="EPZ", product="202608070845-KEPZ")
    got = roads.lsr_road_reports([fix, deming])
    assert len(got) == 1
    assert got[0]["remark"] == ("NM 149 closed between MM2 and MM6 because of flooded roadway. "
                                "(Corrected by NWS: NM-149 should read NM-143. Also, report "
                                "from NMDOT was delayed by nearly an hour versus local VFD "
                                "report.)")
    # a correction that changes the type (flash flood -> hail) replaces the report outright
    hail = _lsr_feature("Corrects previous flash flood report from Las Nutrias. Hail on NM 304 "
                        "closed the road briefly.", typetext="HAIL", valid="2026-09-22T23:19:00Z",
                        product="202609230200-KABQ")
    got = roads.lsr_road_reports([base, hail])
    assert [r["type"] for r in got] == ["HAIL"]
    # the neighbouring office's duplicate (same time, 0.01 degrees away) is dropped
    abq = _lsr_feature("Ruidoso Emergency Management reports that State Police have closed "
                       "U. S. 70 down to one lane at the Otero/Lincoln County line.",
                       valid="2026-09-17T19:17:00Z", lat=33.31, lon=-105.65,
                       city="1 SSW Hollywood", product="202609171923-KABQ-NWUS55-LSRABQ")
    epz = _lsr_feature("Report duplicated with WFO ABQ. Ruidoso Emergency Management reports "
                       "that State Police have closed U. S. 70 down to one lane at the "
                       "Otero/Lincoln County line.", valid="2026-09-17T19:17:00Z", lat=33.30,
                       lon=-105.65, city="13 NE Mescalero", wfo="EPZ",
                       product="202609171940-KEPZ-NWUS54-LSREPZ")
    got = roads.lsr_road_reports([epz, abq])
    assert [(r["place"], r["source"]) for r in got] == [("1 SSW Hollywood", "NWS ABQ")]
    # several earlier reports at the place and no telling which one is meant: none dropped
    a = _lsr_feature("Water over Brady Canyon Rd.", valid="2026-09-18T00:21:00Z",
                     city="1 NNW Ruidoso", product="202609180030-KABQ")
    b = _lsr_feature("Water over the road at Ash Dr.", valid="2026-09-18T00:25:00Z",
                     city="1 NNW Ruidoso", product="202609180031-KABQ")
    c = _lsr_feature("Corrects previous flash flood report from 1 NNW Ruidoso. Lat/lon fixed.",
                     valid="2026-09-18T02:00:00Z", city="1 NNW Ruidoso",
                     product="202609180300-KABQ")
    assert len(roads.lsr_road_reports([a, b, c])) == 2


# ---- review findings: dates, JSON-only mode, area-wide events ---------------------------------
def test_batch_update_stamps_are_not_update_times(cfg, cache, fake_http):
    """NMDOT re-saves many events with one millisecond stamp: that "Update Date" says
    nothing about the event, which is then aged from its post date."""
    stamp = "Update Date: 2026-09-22 06:49:12.492"
    text = NM_RSS
    for pre in (NM94_FLOOD, NM41_CRASH):
        block = re.search(r"<item>(?:(?!</item>).)*%s.*?</item>" % pre, text, re.S).group(0)
        text = text.replace(block, re.sub(r"Update Date: [^<&]*", stamp, block))
    fake_http.add("nmroads.json", nm_json())
    fake_http.add("rss.xml", text.encode("utf-8"))
    res = roads.fetch_nm_roads(cfg, cache)
    ev = {e["id"][:8]: e for e in res["events"]}
    nm94 = ev[NM94_FLOOD]
    assert nm94["updated"] is None
    assert nm94["posted"] == datetime(2026, 9, 22, 22, 59, 16, tzinfo=timezone.utc)
    assert ev[NM41_CRASH]["updated"] is None and ev[NM41_CRASH]["posted"] is not None
    assert ev[NM273_FLOOD]["updated"] is not None                    # its own stamp
    items, _ = roads.parse_rss(text.encode("utf-8"))
    assert roads.batch_stamps(items.values()) == {"2026-09-22 06:49:12.492",
                                                  "2026-09-22 09:27:11.512"}
    # two events sharing a stamp, or a whole-second stamp, are real updates
    assert roads.batch_stamps([{"updated_raw": "2026-09-23 15:30:43.0"}] * 5) == set()
    assert roads.batch_stamps([{"updated_raw": "2026-09-23 15:30:43.512"}] * 2) == set()


def test_json_only_events_get_an_approximate_post_time(cfg, cache, fake_http, now):
    fake_http.add("nmroads.json", nm_json())
    fake_http.fail("rss.xml")
    res = roads.fetch_nm_roads(cfg, cache)
    ev = {e["id"][:8]: e for e in res["events"]}
    ramp = ev[I25_RAMP]
    # the resurfacing ramp closure cannot be told apart without the text, but it is dated
    # (the JSON creation date: 0-7 h before the RSS post date), so the page flags it
    assert ramp["kind"] == "closure" and not ramp["cause_known"] and ramp["updated"] is None
    assert ramp["posted"] == datetime(2026, 9, 18, 18, 55, 51, tzinfo=timezone.utc)
    assert ramp["time_approx"] is True
    assert all(e["time_approx"] for e in ev.values())
    assert "planned work" in res["error"] and "times approximate" in res["error"]


def test_area_wide_events_are_marked(cfg, cache, feeds):
    res = roads.fetch_nm_roads(cfg, cache)
    flags = {e["id"][:8]: e["area_wide"] for e in res["events"]}
    assert flags[SOCORRO_AREA] is True
    assert not any(v for k, v in flags.items() if k != SOCORRO_AREA)


# ---- fixtures: real payloads of 2026-09-23, trimmed ------------------------------------------
NM_FEED_INFO = {"publisher": "NMDOT", "version": "4.2", "license": "https://creativecommons.org/publicdomain/zero/1.0/"}
NM_FEATURES = r"""
{"id": "11fc0557-e294-44c9-bdff-042f07ecc605", "type": "Feature", "properties": {"start_date": "2026-06-16T09:30:10-06:00", "end_date": "2026-10-02T15:00:00-06:00", "core_details": {"data_source_id": "11fc0557-e294-44c9-bdff-042f07ecc605", "event_type": "work-zone", "road_names": ["null-null"], "description": "Roadwork,  northbound and southbound.", "creation_date": "2026-06-16T09:30:10-06:00", "update_date": "2026-08-12T04:04:12-06:00", "direction": "northbound, southbound", "vehicle_impact": "unknown", "name": "Roadwork", "District": "0"}}, "geometry": {"type": "Point", "coordinates": [-106.65698, 35.23675]}}
{"id": "2f2dae34-b294-4ec9-9dc6-5e580fbb9034", "type": "Feature", "properties": {"start_date": "2026-02-10T12:58:29-07:00", "end_date": "2029-02-28T14:00:00-07:00", "core_details": {"data_source_id": "2f2dae34-b294-4ec9-9dc6-5e580fbb9034", "event_type": "work-zone", "road_names": ["NM-500"], "description": "Roadwork, NM 500 eastbound and westbound at mile marker 2.", "creation_date": "2026-02-10T12:58:29-07:00", "update_date": "2026-09-11T03:43:04-06:00", "direction": "eastbound, westbound", "vehicle_impact": "unknown", "name": "Roadwork", "District": "3"}}, "geometry": {"type": "Point", "coordinates": [-106.67787, 35.027]}}
{"id": "15c417d3-c9a4-499d-969a-8c68d2162ccc", "type": "Feature", "properties": {"start_date": "2026-09-09T09:11:41-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "15c417d3-c9a4-499d-969a-8c68d2162ccc", "event_type": "work-zone", "road_names": ["US-60"], "description": "Roadwork, US 60 eastbound and westbound from mile marker 331, 3 miles east of Ft Sumner to mile marker 342 Taiban.", "creation_date": "2026-09-09T09:11:41-06:00", "update_date": "2026-09-22T03:27:11.512-06:00", "direction": "eastbound, westbound", "vehicle_impact": "unknown", "name": "Roadwork", "District": "2"}}, "geometry": {"type": "LineString", "coordinates": [[-104.19037, 34.45385], [-104.16639, 34.44698], [-104.14053, 34.44702], [-104.10084, 34.44021], [-104.05065, 34.43153], [-104.03975, 34.43318], [-104.02037, 34.44006], [-104.00507, 34.44033]]}}
{"id": "0c4a2d89-62e7-4d10-9102-3d706e11cb76", "type": "Feature", "properties": {"start_date": "2026-09-09T09:11:41-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "0c4a2d89-62e7-4d10-9102-3d706e11cb76", "event_type": "work-zone", "road_names": ["US-60"], "description": "Roadwork, US 60 eastbound and westbound from mile marker 331, 3 miles east of Ft Sumner to mile marker 342 Taiban.", "creation_date": "2026-09-09T09:11:41-06:00", "update_date": "2026-09-22T03:27:11.512-06:00", "direction": "eastbound, westbound", "vehicle_impact": "unknown", "name": "Roadwork", "District": "2"}}, "geometry": {"type": "LineString", "coordinates": [[-104.19037, 34.45385], [-104.16639, 34.44698], [-104.14053, 34.44702], [-104.10084, 34.44021], [-104.05065, 34.43153], [-104.03975, 34.43318], [-104.02037, 34.44006], [-104.00507, 34.44033]]}}
{"id": "c7b95069-c172-40de-a6bb-ac45579fd7de", "type": "Feature", "properties": {"start_date": "2023-07-16T18:00:00-06:00", "end_date": "2099-12-30T17:00:00-07:00", "core_details": {"data_source_id": "c7b95069-c172-40de-a6bb-ac45579fd7de", "event_type": "work-zone", "road_names": ["US-60"], "description": "Roadwork, US 60  from mile marker 387, 1 mile west of Clovis to mile marker 389, eastside of Clovis. Roadway closed.", "creation_date": "2023-07-16T18:00:00-06:00", "update_date": "2026-09-22T03:27:11.512-06:00", "direction": "", "vehicle_impact": "Roadway lane closed.", "name": "Roadwork", "District": "2"}}, "geometry": {"type": "LineString", "coordinates": [[-103.22296, 34.40066], [-103.21225, 34.40062], [-103.21106, 34.39991], [-103.20937, 34.39869], [-103.20857, 34.39857], [-103.19644, 34.3986], [-103.19302, 34.39864], [-103.19139, 34.39841]]}}
{"id": "3051fd7d-688d-49f9-80b6-5f16c759fc28", "type": "Feature", "properties": {"start_date": "2026-03-18T13:18:28-06:00", "end_date": "2099-12-30T17:00:00-07:00", "core_details": {"data_source_id": "3051fd7d-688d-49f9-80b6-5f16c759fc28", "event_type": "work-zone", "road_names": ["NM-118"], "description": "Construction Closure, NM 118 eastbound and westbound from mile marker 30, Church Rock to mile marker 31, 1 miles east of Church Rock.", "creation_date": "2026-03-18T13:18:28-06:00", "update_date": "2026-08-28T01:42:16.257-06:00", "direction": "eastbound, westbound", "vehicle_impact": "unknown", "name": "Construction Closure", "District": "6"}}, "geometry": {"type": "LineString", "coordinates": [[-108.60015, 35.52971], [-108.59841, 35.52934], [-108.59724, 35.52903], [-108.59389, 35.52804], [-108.59, 35.52686], [-108.5855, 35.52549], [-108.58459, 35.52527], [-108.58363, 35.52503]]}}
{"id": "dd4b80e9-93bb-4549-b38d-e545867c920c", "type": "Feature", "properties": {"start_date": "2026-09-23T09:30:43-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "dd4b80e9-93bb-4549-b38d-e545867c920c", "event_type": "Closure", "road_names": ["NM-41"], "description": "Closure, NM 41 northbound and southbound from mile marker 17, 1 mile south of McIntosh to mile marker 19, 1 mile north McIntosh.", "creation_date": "2026-09-23T09:30:43-06:00", "update_date": "2026-09-23T09:30:43-06:00", "direction": "northbound, southbound", "vehicle_impact": "unknown", "name": "Closure", "District": "5"}}, "geometry": {"type": "LineString", "coordinates": [[-106.05339, 34.84876], [-106.05328, 34.85376], [-106.0532, 34.85799], [-106.05311, 34.86244], [-106.05301, 34.86772], [-106.05295, 34.87044], [-106.05291, 34.8717], [-106.05286, 34.87409]]}}
{"id": "b3f0d585-8422-471b-b0e9-de0b36a0f489", "type": "Feature", "properties": {"start_date": "2026-09-22T10:59:16-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "b3f0d585-8422-471b-b0e9-de0b36a0f489", "event_type": "Closure", "road_names": ["NM-94"], "description": "Closure, NM 94 northbound and southbound from mile marker 14, Ledoux to mile marker 16, 2 miles north of Ledoux.  Roadway  closed.", "creation_date": "2026-09-22T10:59:16-06:00", "update_date": "2026-09-22T10:59:16-06:00", "direction": "northbound, southbound", "vehicle_impact": "Roadway 2 lanes closed.", "name": "Closure", "District": "4"}}, "geometry": {"type": "LineString", "coordinates": [[-105.35828, 35.91939], [-105.36016, 35.9224], [-105.36046, 35.924], [-105.35909, 35.92625], [-105.35717, 35.92934], [-105.35419, 35.93511], [-105.35463, 35.93664], [-105.34979, 35.94441]]}}
{"id": "16c96136-0118-4f1d-8d26-ead877f43fad", "type": "Feature", "properties": {"start_date": "2026-09-23T01:18:03-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "16c96136-0118-4f1d-8d26-ead877f43fad", "event_type": "Closure", "road_names": ["NM-273"], "description": "Closure, NM 273 northbound and southbound from mile marker 9, 2 miles north of Santa Teresa to mile marker 14, 1 mile west of TX State Line.", "creation_date": "2026-09-23T01:18:03-06:00", "update_date": "2026-09-23T01:18:03-06:00", "direction": "northbound, southbound", "vehicle_impact": "unknown", "name": "Closure", "District": "1"}}, "geometry": {"type": "LineString", "coordinates": [[-106.64983, 31.87185], [-106.65034, 31.88654], [-106.6566, 31.89752], [-106.65762, 31.90554], [-106.64989, 31.91318], [-106.64223, 31.91632], [-106.64073, 31.92098], [-106.62669, 31.91923]]}}
{"id": "dfbcf2f3-65a7-4b7b-a3cb-8a9df0997b7a", "type": "Feature", "properties": {"start_date": "2026-09-22T13:26:48-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "dfbcf2f3-65a7-4b7b-a3cb-8a9df0997b7a", "event_type": "Difficult Driving Conditions", "road_names": ["NM-252"], "description": "Difficult Driving Conditions, NM 252 northbound and southbound from mile marker 24 to mile marker 25.", "creation_date": "2026-09-22T13:26:48-06:00", "update_date": "2026-09-22T13:26:48-06:00", "direction": "northbound, southbound", "vehicle_impact": "unknown", "name": "Difficult Driving Conditions", "District": "4"}}, "geometry": {"type": "LineString", "coordinates": [[-103.90066, 34.67584], [-103.90067, 34.67789], [-103.90067, 34.67891], [-103.90068, 34.68099], [-103.90069, 34.68205], [-103.9007, 34.68473], [-103.9007, 34.686], [-103.90071, 34.6891]]}}
{"id": "1140ecaf-4d65-4036-a4b7-7a09bbc64154", "type": "Feature", "properties": {"start_date": "2026-09-22T09:46:56-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "1140ecaf-4d65-4036-a4b7-7a09bbc64154", "event_type": "Difficult Driving Conditions", "road_names": ["NM-333"], "description": "Difficult Driving Conditions, NM 333 eastbound and westbound from mile marker 21 to mile marker 20.", "creation_date": "2026-09-22T09:46:56-06:00", "update_date": "2026-09-22T09:46:56-06:00", "direction": "eastbound, westbound", "vehicle_impact": "unknown", "name": "Difficult Driving Conditions", "District": "0"}}, "geometry": {"type": "LineString", "coordinates": [[-106.18503, 35.05879], [-106.18367, 35.05824], [-106.18199, 35.05756], [-106.18059, 35.05699], [-106.17812, 35.05599], [-106.17673, 35.05544], [-106.17301, 35.05393], [-106.17104, 35.05312]]}}
{"id": "829db998-2f7d-4517-becb-4130ebd67cbc", "type": "Feature", "properties": {"start_date": "2026-09-23T08:15:38-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "829db998-2f7d-4517-becb-4130ebd67cbc", "event_type": "Difficult Driving Conditions", "road_names": ["null-null"], "description": "Difficult Driving Conditions  exist throughout the Socorro - 41-57 area.", "creation_date": "2026-09-23T08:15:38-06:00", "update_date": "2026-09-23T08:15:38-06:00", "direction": "", "vehicle_impact": "unknown", "name": "Difficult Driving Conditions", "District": "1"}}, "geometry": {"type": "Point", "coordinates": [-106.66111, 33.95906]}}
{"id": "110f7cf2-382a-4fcf-beda-ab05a2eb97f3", "type": "Feature", "properties": {"start_date": "2026-09-22T09:08:31-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "110f7cf2-382a-4fcf-beda-ab05a2eb97f3", "event_type": "Difficult Driving Conditions", "road_names": ["null-null"], "description": "Difficult Driving Conditions, .", "creation_date": "2026-09-22T09:08:31-06:00", "update_date": "2026-09-22T09:08:31-06:00", "direction": "", "vehicle_impact": "unknown", "name": "Difficult Driving Conditions", "District": "0"}}, "geometry": {"type": "Point", "coordinates": [-106.70502, 32.22092]}}
{"id": "1fa8a5bd-6a11-492d-9f9c-8877a07f55f5", "type": "Feature", "properties": {"start_date": "2026-09-22T09:36:22-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "1fa8a5bd-6a11-492d-9f9c-8877a07f55f5", "event_type": "Difficult Driving Conditions", "road_names": ["null-null"], "description": "Difficult Driving Conditions  exist throughout the Las Cruces - 41-51 area.", "creation_date": "2026-09-22T09:36:22-06:00", "update_date": "2026-09-22T09:36:22-06:00", "direction": "", "vehicle_impact": "unknown", "name": "Difficult Driving Conditions", "District": "1"}}, "geometry": {"type": "Point", "coordinates": [-106.6938, 32.37538]}}
{"id": "bab415b4-522d-4df8-bf5a-ecdb274aac08", "type": "Feature", "properties": {"start_date": "2026-09-17T07:11:31-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "bab415b4-522d-4df8-bf5a-ecdb274aac08", "event_type": "Difficult Driving Conditions", "road_names": ["NM-107"], "description": "Difficult Driving Conditions, NM 107 northbound and southbound from mile marker 41, Magdalena to mile marker 0, at I-25.", "creation_date": "2026-09-17T07:11:31-06:00", "update_date": "2026-09-22T00:49:12.492-06:00", "direction": "northbound, southbound", "vehicle_impact": "unknown", "name": "Difficult Driving Conditions", "District": "1"}}, "geometry": {"type": "LineString", "coordinates": [[-107.11961, 33.63585], [-107.19318, 33.72571], [-107.27714, 33.76269], [-107.31947, 33.84532], [-107.34011, 33.9179], [-107.33636, 33.96514], [-107.30436, 34.03367], [-107.25235, 34.10949]]}}
{"id": "cae2d585-5c02-4680-bcfc-0f9e858451c0", "type": "Feature", "properties": {"start_date": "2021-06-30T18:00:00-06:00", "end_date": "2099-12-30T17:00:00-07:00", "core_details": {"data_source_id": "cae2d585-5c02-4680-bcfc-0f9e858451c0", "event_type": "Alert", "road_names": ["US-380"], "description": "Alert, US 380 eastbound and westbound at mile marker 43, 13 miles east of Bingham.", "creation_date": "2021-06-30T18:00:00-06:00", "update_date": "2026-09-22T00:49:12.492-06:00", "direction": "eastbound, westbound", "vehicle_impact": "unknown", "name": "Alert", "District": "1"}}, "geometry": {"type": "Point", "coordinates": [-106.18536, 33.80001]}}
{"id": "0d732b92-291e-4bdd-9224-d0e814d02cb3", "type": "Feature", "properties": {"start_date": "2024-11-06T17:00:00-07:00", "end_date": "2099-12-30T17:00:00-07:00", "core_details": {"data_source_id": "0d732b92-291e-4bdd-9224-d0e814d02cb3", "event_type": "Alert", "road_names": ["NM-247"], "description": "Alert, NM 247 eastbound and westbound from mile marker 0 Corona to mile marker 48.", "creation_date": "2024-11-06T17:00:00-07:00", "update_date": "2025-08-06T09:26:12-06:00", "direction": "eastbound, westbound", "vehicle_impact": "unknown", "name": "Alert", "District": "0"}}, "geometry": {"type": "LineString", "coordinates": [[-105.59538, 34.24471], [-105.50912, 34.17394], [-105.42912, 34.12721], [-105.33354, 34.11965], [-105.20603, 34.1352], [-105.08427, 34.1553], [-104.97403, 34.15173], [-104.83754, 34.12695]]}}
{"id": "68b8b37f-1ebd-47d8-a46d-579bab9e0dc7", "type": "Feature", "properties": {"start_date": "2026-08-04T02:59:44-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "68b8b37f-1ebd-47d8-a46d-579bab9e0dc7", "event_type": "Alert", "road_names": ["I-40"], "description": "Alert, I 40 eastbound at mile marker 251, 22 miles west of Santa Rosa.", "creation_date": "2026-08-04T02:59:44-06:00", "update_date": "2026-08-04T02:59:44-06:00", "direction": "eastbound", "vehicle_impact": "unknown", "name": "Alert", "District": "4"}}, "geometry": {"type": "Point", "coordinates": [-105.08799, 34.9815]}}
{"id": "50aa3542-a63e-463c-835f-6e1ad43a5a2f", "type": "Feature", "properties": {"start_date": "2026-09-23T09:07:00-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "50aa3542-a63e-463c-835f-6e1ad43a5a2f", "event_type": "work-zone", "road_names": ["I-25"], "description": "Lane Closure, I 25 northbound from mile marker 276, (NM 599) to mile marker 277, 1 mile south of Santa Fe.", "creation_date": "2026-09-23T09:07:00-06:00", "update_date": "2026-09-23T09:07:00-06:00", "direction": "northbound", "vehicle_impact": "unknown", "name": "Lane Closure", "District": "5"}}, "geometry": {"type": "LineString", "coordinates": [[-106.05816, 35.59308], [-106.05629, 35.59432], [-106.05361, 35.59609], [-106.05292, 35.59651], [-106.05188, 35.5971], [-106.05095, 35.59755], [-106.04927, 35.59827], [-106.04586, 35.59963]]}}
{"id": "070a5151-2d27-4d41-98b1-3ebd348e488e", "type": "Feature", "properties": {"start_date": "2026-08-27T10:42:01-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "070a5151-2d27-4d41-98b1-3ebd348e488e", "event_type": "work-zone", "road_names": ["NM-3"], "description": "Lane Closure, NM 3 southbound from mile marker 2, 2 miles north of Duran to mile marker 3, 3 miles north of Duran.", "creation_date": "2026-08-27T10:42:01-06:00", "update_date": "2026-09-16T06:26:04.998-06:00", "direction": "southbound", "vehicle_impact": "unknown", "name": "Lane Closure", "District": "5"}}, "geometry": {"type": "LineString", "coordinates": [[-105.42529, 34.48071], [-105.43102, 34.48303], [-105.43161, 34.48332], [-105.4321, 34.48368], [-105.43393, 34.4861], [-105.43575, 34.48854], [-105.43626, 34.48903], [-105.4379, 34.49028]]}}
{"id": "827a9f74-a62a-4646-95d4-b124c0b468b7", "type": "Feature", "properties": {"start_date": "2026-09-23T01:23:43-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "827a9f74-a62a-4646-95d4-b124c0b468b7", "event_type": "work-zone", "road_names": ["I-25"], "description": "Lane Closure, I 25 northbound at mile marker 9, (DONA ANA).", "creation_date": "2026-09-23T01:23:43-06:00", "update_date": "2026-09-23T01:23:43-06:00", "direction": "northbound", "vehicle_impact": "unknown", "name": "Lane Closure", "District": "1"}}, "geometry": {"type": "Point", "coordinates": [-106.79978, 32.3822]}}
{"id": "bf4cca4d-78ce-4314-88b2-da6169980b13", "type": "Feature", "properties": {"start_date": "2026-09-23T02:04:21-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "bf4cca4d-78ce-4314-88b2-da6169980b13", "event_type": "Fair Driving Conditions", "road_names": ["null-null"], "description": "Fair Driving Conditions  exist throughout the Silver City - 41-45 area.", "creation_date": "2026-09-23T02:04:21-06:00", "update_date": "2026-09-23T02:04:21-06:00", "direction": "", "vehicle_impact": "unknown", "name": "Fair Driving Conditions", "District": "1"}}, "geometry": {"type": "Point", "coordinates": [-108.07072, 32.85992]}}
{"id": "f996224f-d56a-43e6-990c-8bc324dd16a6", "type": "Feature", "properties": {"start_date": "2026-09-18T12:55:51-06:00", "end_date": "2099-12-31T00:00:00-07:00", "core_details": {"data_source_id": "f996224f-d56a-43e6-990c-8bc324dd16a6", "event_type": "Closure", "road_names": ["I-25"], "description": "Closure, I 25 northbound at mile marker 290, (US 285).", "creation_date": "2026-09-18T12:55:51-06:00", "update_date": "2026-09-18T08:01:55-06:00", "direction": "northbound", "vehicle_impact": "unknown", "name": "Closure", "District": "5"}}, "geometry": {"type": "Point", "coordinates": [-105.88234, 35.56035]}}
"""

NM_RSS = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:osow="http://nmroads.com/osow/" xmlns:geo="http://www.w3.org/2003/01/geo/wgs84_pos#" xmlns:atom="http://www.w3.org/2005/Atom">
<channel>
<title>New Mexico Traffic Conditions</title>
<link>http://nmroads.com</link>
<description>Traffic incident and event information for all of New Mexico</description>
<language>en-us</language>
<atom:link href="http://nmroads.com/rss.xml" rel="self" type="application/rss+xml" />
<pubDate>Wed, 23 Sep 2026 15:34:00 -0600</pubDate>
<lastBuildDate>Wed, 23 Sep 2026 15:34:00 -0600</lastBuildDate>
<item>
<title>Roadwork - </title>
<pubDate>Tue, 16 Jun 2026 09:30:10 -0600</pubDate>
<link>http://nmroads.com?lon=-106.656982421875&amp;lat=35.236751556396484&amp;zoom=10</link>
<description><![CDATA[Title: Roadwork,  northbound and southbound.<br/>Description: Route=NM528
Date=6/29 - 10/2
Time=24/7 SB Right Lane Restriction through 8/28
24/7 restrictions along Barbara Lp with left or right decelerations lanes restricted along NM528---Detours Provided<br/>Post Date: Tue Jun 16 09:30:10 MDT 2026<br/>Update Date: 2026-08-12 10:04:12.0<br/>Expiration Date: Fri Oct 02 15:00:00 MDT 2026]]></description>
<category>Roadwork</category>
<guid isPermaLink="false">11fc0557-e294-44c9-bdff-042f07ecc605</guid>
<geo:lat>35.236751556396484</geo:lat><geo:long>-106.656982421875</geo:long>
<osow:routeName>null</osow:routeName>
<osow:routeNumber>null</osow:routeNumber>
<osow:mileMarkerFrom>0</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Roadwork - </title>
<pubDate>Tue, 10 Feb 2026 12:58:29 -0700</pubDate>
<link>http://nmroads.com?lon=-106.6778684970194&amp;lat=35.027002266890314&amp;zoom=10</link>
<description><![CDATA[Title: Roadwork, NM 500 eastbound and westbound at mile marker 2.<br/>Description: NM 500 at River Bridge milepost 2 with various lane restrictions. Expect delays. Work is being performed for bridge replacement.
Project: A301001
Contractor: AMES                                 <br/>Post Date: Tue Feb 10 12:58:29 MST 2026<br/>Update Date: 2026-09-11 09:43:04.0<br/>Expiration Date: Wed Feb 28 14:00:00 MST 2029]]></description>
<category>Roadwork</category>
<guid isPermaLink="false">2f2dae34-b294-4ec9-9dc6-5e580fbb9034</guid>
<geo:lat>35.027002266890314</geo:lat><geo:long>-106.6778684970194</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>500</osow:routeNumber>
<osow:mileMarkerFrom>2</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Roadwork - </title>
<pubDate>Wed, 09 Sep 2026 15:11:41 -0600</pubDate>
<link>http://nmroads.com?lon=-104.06249541252814&amp;lat=34.43351978579119&amp;zoom=10</link>
<description><![CDATA[Title: Roadwork, US 60 eastbound and westbound from mile marker 331, 3 miles east of Ft Sumner to mile marker 342 Taiban.<br/>Description: Mill and fill operations have begun on US 60/84 from milepost 331 to milepost 342 (Taiban). Motorists should expect single-lane closures with traffic control. This is a 2-week project and is expected to be complete end of September, weather permitting.<br/>Post Date: 2026-09-09 15:11:41.0<br/>Update Date: 2026-09-22 09:27:11.512<br/>Expiration Date: unknown<br/>]]></description>
<category>Roadwork</category>
<guid isPermaLink="false">15c417d3-c9a4-499d-969a-8c68d2162ccc</guid>
<geo:lat>34.43351978579119</geo:lat><geo:long>-104.06249541252814</geo:long>
<osow:routeName>US</osow:routeName>
<osow:routeNumber>60</osow:routeNumber>
<osow:mileMarkerFrom>331</osow:mileMarkerFrom>
<osow:mileMarkerTo>342</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Roadwork - </title>
<pubDate>Wed, 09 Sep 2026 15:11:41 -0600</pubDate>
<link>http://nmroads.com?lon=-104.06249541252814&amp;lat=34.43351978579119&amp;zoom=10</link>
<description><![CDATA[Title: Roadwork, US 60 eastbound and westbound from mile marker 331, 3 miles east of Ft Sumner to mile marker 342 Taiban.<br/>Description: Mill and fill operations have begun on US 60/84 from milepost 331 to milepost 342 (Taiban). Motorists should expect single-lane closures with traffic control. This is a 2-week project and is expected to be complete end of September, weather permitting.<br/>Post Date: 2026-09-09 15:11:41.0<br/>Update Date: 2026-09-22 09:27:11.512<br/>Expiration Date: unknown<br/>]]></description>
<category>Roadwork</category>
<guid isPermaLink="false">0c4a2d89-62e7-4d10-9102-3d706e11cb76</guid>
<geo:lat>34.43351978579119</geo:lat><geo:long>-104.06249541252814</geo:long>
<osow:routeName>US</osow:routeName>
<osow:routeNumber>60</osow:routeNumber>
<osow:mileMarkerFrom>331</osow:mileMarkerFrom>
<osow:mileMarkerTo>342</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Roadwork - Roadwork, US 60  from mile marker 387, 1 mile west of Clovis to mile marker 389, eastside of Clovis. Roadway closed.</title>
<pubDate>Mon, 17 Jul 2023 00:00:00 -0600</pubDate>
<link>http://nmroads.com?lon=-103.20744299249182&amp;lat=34.398595019690845&amp;zoom=10</link>
<description><![CDATA[Title: Roadwork, US 60  from mile marker 387, 1 mile west of Clovis to mile marker 389, eastside of Clovis. Roadway closed.<br/>Description: Roadwork~~~Road Reconstruction project on US60/84 from MP 387.800 to MP 389.120. Westbound lanes of US 60  closed for reconstruction from Grand St to Prince St. Traffic will be reduced to one lane in each direction on the existing Eastbound Lanes.<br/>Post Date: 2023-07-17 00:00:00.0<br/>Update Date: 2026-09-22 09:27:11.512<br/>Expiration Date: unknown<br/>]]></description>
<category>Roadwork</category>
<guid isPermaLink="false">c7b95069-c172-40de-a6bb-ac45579fd7de</guid>
<geo:lat>34.398595019690845</geo:lat><geo:long>-103.20744299249182</geo:long>
<osow:routeName>US</osow:routeName>
<osow:routeNumber>60</osow:routeNumber>
<osow:mileMarkerFrom>387</osow:mileMarkerFrom>
<osow:mileMarkerTo>389</osow:mileMarkerTo>
<osow:direction>NA</osow:direction>
<osow:width>144</osow:width>
</item>
<item>
<title>Construction Closure - Construction Closure, NM 118 eastbound and westbound from mile marker 30, Church Rock to mile marker 31, 1 miles east of Church Rock.</title>
<pubDate>Wed, 18 Mar 2026 19:18:28 -0600</pubDate>
<link>http://nmroads.com?lon=-108.59194480550423&amp;lat=35.52743152437208&amp;zoom=10</link>
<description><![CDATA[Title: Construction Closure, NM 118 eastbound and westbound from mile marker 30, Church Rock to mile marker 31, 1 miles east of Church Rock.<br/>Description: NM 118 is closed at mile marker 29.5-31 (NM 566 Intersection to Navajo Blvd) due to bridge replacement project. Please use I-40 as a detour. <br/>Post Date: 2026-03-18 19:18:28.0<br/>Update Date: 2026-08-28 07:42:16.257<br/>Expiration Date: unknown<br/>]]></description>
<category>Construction Closure</category>
<guid isPermaLink="false">3051fd7d-688d-49f9-80b6-5f16c759fc28</guid>
<geo:lat>35.52743152437208</geo:lat><geo:long>-108.59194480550423</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>118</osow:routeNumber>
<osow:mileMarkerFrom>30</osow:mileMarkerFrom>
<osow:mileMarkerTo>31</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Closure - </title>
<pubDate>Wed, 23 Sep 2026 15:30:43 -0600</pubDate>
<link>http://nmroads.com?lon=-106.05300936853871&amp;lat=34.867717648924064&amp;zoom=10</link>
<description><![CDATA[Title: Closure, NM 41 northbound and southbound from mile marker 17, 1 mile south of McIntosh to mile marker 19, 1 mile north McIntosh.<br/>Description: Emergency services are on the scene and working to clear the crash scene as soon as possible.<br/>Post Date: 2026-09-23 15:30:43.0<br/>Update Date: 2026-09-23 15:30:43.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Closure</category>
<guid isPermaLink="false">dd4b80e9-93bb-4549-b38d-e545867c920c</guid>
<geo:lat>34.867717648924064</geo:lat><geo:long>-106.05300936853871</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>41</osow:routeNumber>
<osow:mileMarkerFrom>17</osow:mileMarkerFrom>
<osow:mileMarkerTo>19</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Closure - </title>
<pubDate>Tue, 22 Sep 2026 16:59:16 -0600</pubDate>
<link>http://nmroads.com?lon=-105.35666230974778&amp;lat=35.93028550351621&amp;zoom=10</link>
<description><![CDATA[Title: Closure, NM 94 northbound and southbound from mile marker 14, Ledoux to mile marker 16, 2 miles north of Ledoux.  Roadway  closed.<br/>Description: Standing water on roadway. Use extreme caution. Cloudy conditions exist.<autodescriptiondelimiter>Roadway is closed due to flooding.  <br/>Post Date: 2026-09-22 16:59:16.0<br/>Update Date: 2026-09-22 16:59:16.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Closure</category>
<guid isPermaLink="false">b3f0d585-8422-471b-b0e9-de0b36a0f489</guid>
<geo:lat>35.93028550351621</geo:lat><geo:long>-105.35666230974778</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>94</osow:routeNumber>
<osow:mileMarkerFrom>14</osow:mileMarkerFrom>
<osow:mileMarkerTo>16</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Closure - </title>
<pubDate>Wed, 23 Sep 2026 07:18:03 -0600</pubDate>
<link>http://nmroads.com?lon=-106.65571604211301&amp;lat=31.912020629158278&amp;zoom=10</link>
<description><![CDATA[Title: Closure, NM 273 northbound and southbound from mile marker 9, 2 miles north of Santa Teresa to mile marker 14, 1 mile west of TX State Line.<br/>Description: Roads are impassible.<autodescriptiondelimiter>Closure, NM 273 northbound and southbound from mile marker 9, 2 miles north of Santa Teresa to mile marker 14, 1 mile west of TX State Line., due to flooding. Traffic is being detoured onto Borderland or Westside Road. Use extreme caution.

<br/>Post Date: 2026-09-23 07:18:03.0<br/>Update Date: 2026-09-23 07:18:03.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Closure</category>
<guid isPermaLink="false">16c96136-0118-4f1d-8d26-ead877f43fad</guid>
<geo:lat>31.912020629158278</geo:lat><geo:long>-106.65571604211301</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>273</osow:routeNumber>
<osow:mileMarkerFrom>9</osow:mileMarkerFrom>
<osow:mileMarkerTo>14</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Difficult Driving Conditions - </title>
<pubDate>Tue, 22 Sep 2026 19:26:48 -0600</pubDate>
<link>http://nmroads.com?lon=-103.90069086355254&amp;lat=34.68204617882492&amp;zoom=10</link>
<description><![CDATA[Title: Difficult Driving Conditions, NM 252 northbound and southbound from mile marker 24 to mile marker 25.<br/>Description: Standing water on roadway. Use extreme caution. Cloudy conditions exist.<autodescriptiondelimiter>Roadway flooding. <br/>Post Date: 2026-09-22 19:26:48.0<br/>Update Date: 2026-09-22 19:26:48.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Difficult Driving Conditions</category>
<guid isPermaLink="false">dfbcf2f3-65a7-4b7b-a3cb-8a9df0997b7a</guid>
<geo:lat>34.68204617882492</geo:lat><geo:long>-103.90069086355254</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>252</osow:routeNumber>
<osow:mileMarkerFrom>24</osow:mileMarkerFrom>
<osow:mileMarkerTo>25</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Difficult Driving Conditions - </title>
<pubDate>Tue, 22 Sep 2026 15:46:56 -0600</pubDate>
<link>http://nmroads.com?lon=-106.17956402576546&amp;lat=35.0565814644124&amp;zoom=10</link>
<description><![CDATA[Title: Difficult Driving Conditions, NM 333 eastbound and westbound from mile marker 21 to mile marker 20.<br/>Description: Standing water on roadway. Use extreme caution.<autodescriptiondelimiter>Expect delays and use caution while travelling through the area.<br/>Post Date: 2026-09-22 15:46:56.0<br/>Update Date: 2026-09-22 15:46:56.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Difficult Driving Conditions</category>
<guid isPermaLink="false">1140ecaf-4d65-4036-a4b7-7a09bbc64154</guid>
<geo:lat>35.0565814644124</geo:lat><geo:long>-106.17956402576546</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>333</osow:routeNumber>
<osow:mileMarkerFrom>21</osow:mileMarkerFrom>
<osow:mileMarkerTo>20</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Difficult Driving Conditions - </title>
<pubDate>Wed, 23 Sep 2026 14:15:38 -0600</pubDate>
<link>http://nmroads.com?lon=-106.66111491697058&amp;lat=33.959059193616376&amp;zoom=10</link>
<description><![CDATA[Title: Difficult Driving Conditions  exist throughout the Socorro - 41-57 area.<br/>Description: Standing water on roadway. Use extreme caution.<autodescriptiondelimiter><br/>Post Date: 2026-09-23 14:15:38.0<br/>Update Date: 2026-09-23 14:15:38.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Difficult Driving Conditions</category>
<guid isPermaLink="false">829db998-2f7d-4517-becb-4130ebd67cbc</guid>
<geo:lat>33.959059193616376</geo:lat><geo:long>-106.66111491697058</geo:long>
<osow:routeName>null</osow:routeName>
<osow:routeNumber>null</osow:routeNumber>
<osow:mileMarkerFrom>0</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>NA</osow:direction>
</item>
<item>
<title>Difficult Driving Conditions - </title>
<pubDate>Tue, 22 Sep 2026 15:08:31 -0600</pubDate>
<link>http://nmroads.com?lon=-106.70502471923828&amp;lat=32.22092056274414&amp;zoom=10</link>
<description><![CDATA[Title: Difficult Driving Conditions, .<br/>Description: Roads are wet and may become slick.<autodescriptiondelimiter>Difficult Driving Conditions, Frontage Road 1035, Stern Rd., both lanes are closed from Cholla Rd to NM 225 (Mesquite). Standing water on roadway. Use extreme caution.

When the roadway is flooded do not cross and seek an alternate route. Turn around, don’t drown.
<br/>Post Date: 2026-09-22 15:08:31.0<br/>Update Date: 2026-09-22 15:08:31.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Difficult Driving Conditions</category>
<guid isPermaLink="false">110f7cf2-382a-4fcf-beda-ab05a2eb97f3</guid>
<geo:lat>32.22092056274414</geo:lat><geo:long>-106.70502471923828</geo:long>
<osow:routeName>null</osow:routeName>
<osow:routeNumber>null</osow:routeNumber>
<osow:mileMarkerFrom>0</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>NA</osow:direction>
</item>
<item>
<title>Difficult Driving Conditions - </title>
<pubDate>Tue, 22 Sep 2026 15:36:22 -0600</pubDate>
<link>http://nmroads.com?lon=-106.69380461015967&amp;lat=32.37537561294302&amp;zoom=10</link>
<description><![CDATA[Title: Difficult Driving Conditions  exist throughout the Las Cruces - 41-51 area.<br/>Description: Roads are wet and may become slick.<autodescriptiondelimiter><br/>Post Date: 2026-09-22 15:36:22.0<br/>Update Date: 2026-09-22 15:36:22.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Difficult Driving Conditions</category>
<guid isPermaLink="false">1fa8a5bd-6a11-492d-9f9c-8877a07f55f5</guid>
<geo:lat>32.37537561294302</geo:lat><geo:long>-106.69380461015967</geo:long>
<osow:routeName>null</osow:routeName>
<osow:routeNumber>null</osow:routeNumber>
<osow:mileMarkerFrom>0</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>NA</osow:direction>
</item>
<item>
<title>Difficult Driving Conditions - </title>
<pubDate>Thu, 17 Sep 2026 07:11:31 -0600</pubDate>
<link>http://nmroads.com?lon=-107.32714383492247&amp;lat=33.87714308690151&amp;zoom=10</link>
<description><![CDATA[Title: Difficult Driving Conditions, NM 107 northbound and southbound from mile marker 41, Magdalena to mile marker 0, at I-25.<br/>Description: Roads are wet and may become slick.<autodescriptiondelimiter>Roadway very muddy due to ongoing rains. 4 x 4 vehicle recommended.  Crews will clear once roadway has dried. <br/>Post Date: Thu Sep 17 07:11:31 MDT 2026<br/>Update Date: 2026-09-22 06:49:12.492<br/>Expiration Date: unknown<br/>]]></description>
<category>Difficult Driving Conditions</category>
<guid isPermaLink="false">bab415b4-522d-4df8-bf5a-ecdb274aac08</guid>
<geo:lat>33.87714308690151</geo:lat><geo:long>-107.32714383492247</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>107</osow:routeNumber>
<osow:mileMarkerFrom>41</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Alert - Alert, US 380 eastbound and westbound at mile marker 43, 13 miles east of Bingham.</title>
<pubDate>Thu, 01 Jul 2021 00:00:00 -0600</pubDate>
<link>http://nmroads.com?lon=-106.18535815934803&amp;lat=33.80001176792746&amp;zoom=10</link>
<description><![CDATA[Title: Alert, US 380 eastbound and westbound at mile marker 43, 13 miles east of Bingham.<br/>Description: White Sands Missile Range periodically has missile firings over US 70 and/or US 380. During the firings the road or roads will be temporarily closed. Please call 575-678-1178 or 575-678-2222  for daily information on possible closure dates and times for firings. THIS IS A PERMANENT ALERT. <br/>Post Date: 2021-07-01 00:00:00.0<br/>Update Date: 2026-09-22 06:49:12.492<br/>Expiration Date: unknown<br/>]]></description>
<category>Alert</category>
<guid isPermaLink="false">cae2d585-5c02-4680-bcfc-0f9e858451c0</guid>
<geo:lat>33.80001176792746</geo:lat><geo:long>-106.18535815934803</geo:long>
<osow:routeName>US</osow:routeName>
<osow:routeNumber>380</osow:routeNumber>
<osow:mileMarkerFrom>43</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Alert - Alert, NM 247 eastbound and westbound from mile marker 0 Corona to mile marker 48.</title>
<pubDate>Thu, 07 Nov 2024 00:00:00 -0700</pubDate>
<link>http://nmroads.com?lon=-105.24122879573842&amp;lat=34.12106719259205&amp;zoom=10</link>
<description><![CDATA[Title: Alert, NM 247 eastbound and westbound from mile marker 0 Corona to mile marker 48.<br/>Description: Motorists be advised NM 247 will be closed intermittently to allow vehicles carrying wind turbine blades to safely access the roadway. Expect delays and use caution while travelling through the area.
<br/>Post Date: 2024-11-07 00:00:00.0<br/>Update Date: 2025-08-06 15:26:12.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Alert</category>
<guid isPermaLink="false">0d732b92-291e-4bdd-9224-d0e814d02cb3</guid>
<geo:lat>34.12106719259205</geo:lat><geo:long>-105.24122879573842</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>247</osow:routeNumber>
<osow:mileMarkerFrom>0</osow:mileMarkerFrom>
<osow:mileMarkerTo>48</osow:mileMarkerTo>
<osow:direction>both</osow:direction>
</item>
<item>
<title>Alert - </title>
<pubDate>Tue, 04 Aug 2026 08:59:44 -0600</pubDate>
<link>http://nmroads.com?lon=-105.08799417457331&amp;lat=34.98149556859268&amp;zoom=10</link>
<description><![CDATA[Title: Alert, I 40 eastbound at mile marker 251, 22 miles west of Santa Rosa.<br/>Description: Currently, Anton Chico Rest Area (Eastbound) is closed due to water-related issues. <br/>Post Date: 2026-08-04 08:59:44.0<br/>Update Date: 2026-08-04 08:59:44.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Alert</category>
<guid isPermaLink="false">68b8b37f-1ebd-47d8-a46d-579bab9e0dc7</guid>
<geo:lat>34.98149556859268</geo:lat><geo:long>-105.08799417457331</geo:long>
<osow:routeName>I</osow:routeName>
<osow:routeNumber>40</osow:routeNumber>
<osow:mileMarkerFrom>251</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>eastbound</osow:direction>
</item>
<item>
<title>Lane Closure - </title>
<pubDate>Wed, 23 Sep 2026 15:07:00 -0600</pubDate>
<link>http://nmroads.com?lon=-106.05225478370005&amp;lat=35.59690178376669&amp;zoom=10</link>
<description><![CDATA[Title: Lane Closure, I 25 northbound from mile marker 276, (NM 599) to mile marker 277, 1 mile south of Santa Fe.<br/>Description: Standing water on roadway. Use extreme caution.<autodescriptiondelimiter>The left lane is closed due to standing water on the roadway. Expect delays and use caution while travelling through the area.<br/>Post Date: 2026-09-23 15:07:00.0<br/>Update Date: 2026-09-23 15:07:00.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Lane Closure</category>
<guid isPermaLink="false">50aa3542-a63e-463c-835f-6e1ad43a5a2f</guid>
<geo:lat>35.59690178376669</geo:lat><geo:long>-106.05225478370005</geo:long>
<osow:routeName>I</osow:routeName>
<osow:routeNumber>25</osow:routeNumber>
<osow:mileMarkerFrom>276</osow:mileMarkerFrom>
<osow:mileMarkerTo>277</osow:mileMarkerTo>
<osow:direction>northbound</osow:direction>
</item>
<item>
<title>Lane Closure - </title>
<pubDate>Thu, 27 Aug 2026 16:42:01 -0600</pubDate>
<link>http://nmroads.com?lon=-105.43267774908998&amp;lat=34.484423655395105&amp;zoom=10</link>
<description><![CDATA[Title: Lane Closure, NM 3 southbound from mile marker 2, 2 miles north of Duran to mile marker 3, 3 miles north of Duran.<br/>Description: One lane is closed due to a stalled crane. There is no ETA on its removal. Expect delays and use caution while travelling through the area.<br/>Post Date: 2026-08-27 16:42:01.0<br/>Update Date: 2026-09-16 12:26:04.998<br/>Expiration Date: unknown<br/>]]></description>
<category>Lane Closure</category>
<guid isPermaLink="false">070a5151-2d27-4d41-98b1-3ebd348e488e</guid>
<geo:lat>34.484423655395105</geo:lat><geo:long>-105.43267774908998</geo:long>
<osow:routeName>NM</osow:routeName>
<osow:routeNumber>3</osow:routeNumber>
<osow:mileMarkerFrom>2</osow:mileMarkerFrom>
<osow:mileMarkerTo>3</osow:mileMarkerTo>
<osow:direction>southbound</osow:direction>
</item>
<item>
<title>Lane Closure - </title>
<pubDate>Wed, 23 Sep 2026 07:23:43 -0600</pubDate>
<link>http://nmroads.com?lon=-106.79977886422726&amp;lat=32.38220347029061&amp;zoom=10</link>
<description><![CDATA[Title: Lane Closure, I 25 northbound at mile marker 9, (DONA ANA).<br/>Description: Lane Closure, I 25 northbound at mile marker 9, (DONA ANA) will have driving right lane closed with maintainers in the area clearing debris from roadway.<br/>Post Date: 2026-09-23 07:23:43.0<br/>Update Date: 2026-09-23 07:23:43.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Lane Closure</category>
<guid isPermaLink="false">827a9f74-a62a-4646-95d4-b124c0b468b7</guid>
<geo:lat>32.38220347029061</geo:lat><geo:long>-106.79977886422726</geo:long>
<osow:routeName>I</osow:routeName>
<osow:routeNumber>25</osow:routeNumber>
<osow:mileMarkerFrom>9</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>northbound</osow:direction>
</item>
<item>
<title>Fair Driving Conditions - </title>
<pubDate>Wed, 23 Sep 2026 08:04:21 -0600</pubDate>
<link>http://nmroads.com?lon=-108.07071532819957&amp;lat=32.8599244327909&amp;zoom=10</link>
<description><![CDATA[Title: Fair Driving Conditions  exist throughout the Silver City - 41-45 area.<br/>Description: Roads are wet and may become slick.<autodescriptiondelimiter>The Silver City Patrol has reported roadways with a light rain & wet.  Please drive with caution, reduce speed, and obey all posted traffic signs.  The NMDOT will continue to monitor as needed. This event will be updated as conditions change.


<br/>Post Date: 2026-09-23 08:04:21.0<br/>Update Date: 2026-09-23 08:04:21.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Fair Driving Conditions</category>
<guid isPermaLink="false">bf4cca4d-78ce-4314-88b2-da6169980b13</guid>
<geo:lat>32.8599244327909</geo:lat><geo:long>-108.07071532819957</geo:long>
<osow:routeName>null</osow:routeName>
<osow:routeNumber>null</osow:routeNumber>
<osow:mileMarkerFrom>0</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>NA</osow:direction>
</item>
<item>
<title>Closure - </title>
<pubDate>Fri, 18 Sep 2026 12:55:51 -0600</pubDate>
<link>http://nmroads.com?lon=-105.88233844770883&amp;lat=35.56034907290247&amp;zoom=10</link>
<description><![CDATA[Title: Closure, I 25 northbound at mile marker 290, (US 285).<br/>Description: The NMDOT and Albuquerque Asphalt will be closing the Northbound off ramp on I-25 at MM 290 for resurfacing project starting on Monday September 21st going through Tuesday, September 22nd.<br/>Post Date: Fri Sep 18 12:55:51 MDT 2026<br/>Update Date: 2026-09-18 14:01:55.0<br/>Expiration Date: unknown<br/>]]></description>
<category>Closure</category>
<guid isPermaLink="false">f996224f-d56a-43e6-990c-8bc324dd16a6</guid>
<geo:lat>35.56034907290247</geo:lat><geo:long>-105.88233844770883</geo:long>
<osow:routeName>I</osow:routeName>
<osow:routeNumber>25</osow:routeNumber>
<osow:mileMarkerFrom>290</osow:mileMarkerFrom>
<osow:mileMarkerTo>0</osow:mileMarkerTo>
<osow:direction>northbound</osow:direction>
</item>
</channel>
</rss>
"""

LSR_FEATURES = r"""
{"id": "0", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-23 13:00:00-05\",E,,\"4 E Elk\",Chaves,NM,Public,\"Flooding in the Rio Penasco and nearby low lying areas.\",ABQ,FLOOD,0101000020E6100000E17A14AE47515AC0D7A3703D0A774040,202609232139-KABQ-NWUS55-LSRABQ,\"2026-09-23 16:39:00-05\",,,76989,)", "county": "Chaves", "typetext": "FLOOD", "state": "NM", "remark": "Flooding in the Rio Penasco and nearby low lying areas.", "city": "4 E Elk", "valid": "2026-09-23T18:00:00Z", "lon": -105.27, "lat": 32.93, "product_id": "202609232139-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-105.27, 32.93]}}
{"id": "1", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-23 15:53:00-05\",E,,\"6 ESE Elk\",Chaves,NM,Public,\"A photo from a spotter showing floodwaters in the Rio Penasco going through a low water crossing dirt road/bridge at Runyan Ranches.\",ABQ,FLOOD,0101000020E61000008FC2F5285C4F5AC03333333333734040,202609232134-KABQ-NWUS55-LSRABQ,\"2026-09-23 16:34:00-05\",,,76989,)", "county": "Chaves", "typetext": "FLOOD", "state": "NM", "remark": "A photo from a spotter showing floodwaters in the Rio Penasco going through a low water crossing dirt road/bridge at Runyan Ranches.", "city": "6 ESE Elk", "valid": "2026-09-23T20:53:00Z", "lon": -105.24, "lat": 32.9, "product_id": "202609232134-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-105.24, 32.9]}}
{"id": "2", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-22 16:00:00-05\",F,,\"Monte Aplanado\",Mora,NM,\"Emergency Mngr\",\"Water rescue due to flooding of a home. Location and time are estimated.\",ABQ,\"FLASH FLOOD\",0101000020E6100000295C8FC2F5585AC09A99999999F94140,202609222228-KABQ-NWUS55-LSRABQ,\"2026-09-22 17:28:00-05\",,,77005,)", "county": "Mora", "typetext": "FLASH FLOOD", "state": "NM", "remark": "Water rescue due to flooding of a home. Location and time are estimated.", "city": "Monte Aplanado", "valid": "2026-09-22T21:00:00Z", "lon": -105.39, "lat": 35.95, "product_id": "202609222228-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-105.39, 35.95]}}
{"id": "3", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-22 16:35:00-05\",F,,\"13 W Oscuro\",Lincoln,NM,\"Other Federal\",\"Range Route 8, 9 and 12 impassable due to water over roads.\",ABQ,\"FLASH FLOOD\",0101000020E610000052B81E85EB915AC03D0AD7A370BD4040,202609222141-KABQ-NWUS55-LSRABQ,\"2026-09-22 16:41:00-05\",,,77001,)", "county": "Lincoln", "typetext": "FLASH FLOOD", "state": "NM", "remark": "Range Route 8, 9 and 12 impassable due to water over roads.", "city": "13 W Oscuro", "valid": "2026-09-22T21:35:00Z", "lon": -106.28, "lat": 33.48, "product_id": "202609222141-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-106.28, 33.48]}}
{"id": "4", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-22 16:35:00-05\",F,,Mora,Mora,NM,\"Trained Spotter\",\"Mora High School parking lot flooded with water into the school building. Water was completely covering several of the roads south of NM Highway 518 with a few becoming impassable. Spotter also had a residential sidewalk that was washed out.\",ABQ,\"FLASH FLOOD\",0101000020E610000085EB51B81E555AC03D0AD7A370FD4140,202609222211-KABQ-NWUS55-LSRABQ,\"2026-09-22 17:11:00-05\",,,77005,)", "county": "Mora", "typetext": "FLASH FLOOD", "state": "NM", "remark": "Mora High School parking lot flooded with water into the school building. Water was completely covering several of the roads south of NM Highway 518 with a few becoming impassable. Spotter also had a residential sidewalk that was washed out.", "city": "Mora", "valid": "2026-09-22T21:35:00Z", "lon": -105.33, "lat": 35.98, "product_id": "202609222211-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-105.33, 35.98]}}
{"id": "5", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-22 17:05:00-05\",F,,\"1 SSW Mora\",Mora,NM,\"Trained Spotter\",\"NM Highway 94 is flooded and impassable south of Mora.\",ABQ,\"FLASH FLOOD\",0101000020E6100000F6285C8FC2555AC07B14AE47E1FA4140,202609222217-KABQ-NWUS55-LSRABQ,\"2026-09-22 17:17:00-05\",,,77005,)", "county": "Mora", "typetext": "FLASH FLOOD", "state": "NM", "remark": "NM Highway 94 is flooded and impassable south of Mora.", "city": "1 SSW Mora", "valid": "2026-09-22T22:05:00Z", "lon": -105.34, "lat": 35.96, "product_id": "202609222217-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-105.34, 35.96]}}
{"id": "6", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-22 17:15:00-05\",F,,\"1 SE Cleveland\",Mora,NM,\"Emergency Mngr\",\"Torrent of water flowing right in front of the Mora Volunteer Fire Department, making roads impassable.\",ABQ,\"FLASH FLOOD\",0101000020E61000006666666666565AC03D0AD7A370FD4140,202609222226-KABQ-NWUS55-LSRABQ,\"2026-09-22 17:26:00-05\",,,77005,)", "county": "Mora", "typetext": "FLASH FLOOD", "state": "NM", "remark": "Torrent of water flowing right in front of the Mora Volunteer Fire Department, making roads impassable.", "city": "1 SE Cleveland", "valid": "2026-09-22T22:15:00Z", "lon": -105.35, "lat": 35.98, "product_id": "202609222226-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-105.35, 35.98]}}
{"id": "7", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-22 17:38:00-05\",F,,\"Monte Aplanado\",Mora,NM,\"Emergency Mngr\",\"Significant flash flooding with Monte Aplanado Rd (County Road A005) being impassable in several spots.\",ABQ,\"FLASH FLOOD\",0101000020E61000009A99999999595AC09A99999999F94140,202609222251-KABQ-NWUS55-LSRABQ,\"2026-09-22 17:51:00-05\",,,77005,)", "county": "Mora", "typetext": "FLASH FLOOD", "state": "NM", "remark": "Significant flash flooding with Monte Aplanado Rd (County Road A005) being impassable in several spots.", "city": "Monte Aplanado", "valid": "2026-09-22T22:38:00Z", "lon": -105.4, "lat": 35.95, "product_id": "202609222251-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-105.4, 35.95]}}
{"id": "8", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-22 18:19:00-05\",F,,\"Las Nutrias\",Socorro,NM,\"Dept of Highways\",\"NMDOT reports NM State Highway 304 closed at Mile Marker 11 (Las Nutrias) due to standing water.\",ABQ,\"FLASH FLOOD\",0101000020E6100000E17A14AE47B15AC03D0AD7A3703D4140,202609222339-KABQ-NWUS55-LSRABQ,\"2026-09-22 18:39:00-05\",,,77015,)", "county": "Socorro", "typetext": "FLASH FLOOD", "state": "NM", "remark": "NMDOT reports NM State Highway 304 closed at Mile Marker 11 (Las Nutrias) due to standing water.", "city": "Las Nutrias", "valid": "2026-09-22T23:19:00Z", "lon": -106.77, "lat": 34.48, "product_id": "202609222339-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-106.77, 34.48]}}
{"id": "9", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-22 19:50:00-05\",F,,\"2 N House\",Quay,NM,\"Broadcast Media\",\"Video shows water over NM 252 north of House, NM from Alamosa Creek.\",ABQ,\"FLASH FLOOD\",0101000020E61000009A99999999F959C0F6285C8FC2554140,202609230139-KABQ-NWUS55-LSRABQ,\"2026-09-22 20:39:00-05\",,,77007,)", "county": "Quay", "typetext": "FLASH FLOOD", "state": "NM", "remark": "Video shows water over NM 252 north of House, NM from Alamosa Creek.", "city": "2 N House", "valid": "2026-09-23T00:50:00Z", "lon": -103.9, "lat": 34.67, "product_id": "202609230139-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-103.9, 34.67]}}
{"id": "10", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-23 11:00:00-05\",F,,\"1 NW Ruidoso\",Lincoln,NM,\"Emergency Mngr\",\"Corrected time. Muddy floodwaters going across the roadway at Ash Drive and Brady Canyon Road.\",ABQ,\"FLASH FLOOD\",0101000020E61000005C8FC2F5286C5AC0EC51B81E85AB4040,202609231725-KABQ-NWUS55-LSRABQ,\"2026-09-23 12:25:00-05\",,,77001,)", "county": "Lincoln", "typetext": "FLASH FLOOD", "state": "NM", "remark": "Corrected time. Muddy floodwaters going across the roadway at Ash Drive and Brady Canyon Road.", "city": "1 NW Ruidoso", "valid": "2026-09-23T16:00:00Z", "lon": -105.69, "lat": 33.34, "product_id": "202609231725-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-105.69, 33.34]}}
{"id": "11", "type": "Feature", "properties": {"wfo": "ABQ", "l": "(\"2026-09-23 12:05:00-05\",F,,\"1 NW Ruidoso\",Lincoln,NM,\"Emergency Mngr\",\"Muddy floodwaters going across the roadway at Ash Drive and Brady Canyon Road.\",ABQ,\"FLASH FLOOD\",0101000020E61000005C8FC2F5286C5AC0EC51B81E85AB4040,202609231710-KABQ-NWUS55-LSRABQ,\"2026-09-23 12:10:00-05\",,,77001,)", "county": "Lincoln", "typetext": "FLASH FLOOD", "state": "NM", "remark": "Muddy floodwaters going across the roadway at Ash Drive and Brady Canyon Road.", "city": "1 NW Ruidoso", "valid": "2026-09-23T17:05:00Z", "lon": -105.69, "lat": 33.34, "product_id": "202609231710-KABQ-NWUS55-LSRABQ"}, "geometry": {"type": "Point", "coordinates": [-105.69, 33.34]}}
"""
