"""Tests for weather.nws: point metadata, forecasts, hourly window, observations.
Fixtures are trimmed copies of real api.weather.gov payloads (Socorro, NM, 2026-09-23)."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from weather import nws, util

SITE = {"slug": "socorro", "name": "Socorro, NM", "lat": 34.0584, "lon": -106.8914}

POINTS = {"properties": {
    "gridId": "ABQ", "gridX": 86, "gridY": 76, "cwa": "ABQ",
    "forecastOffice": "https://api.weather.gov/offices/ABQ",
    "forecast": "https://api.weather.gov/gridpoints/ABQ/86,76/forecast",
    "forecastHourly": "https://api.weather.gov/gridpoints/ABQ/86,76/forecast/hourly",
    "observationStations": "https://api.weather.gov/gridpoints/ABQ/86,76/stations",
    "relativeLocation": {"type": "Feature",
                         "geometry": {"type": "Point", "coordinates": [-106.906, 34.054]},
                         "properties": {"city": "Socorro", "state": "NM"}},
    "timeZone": "America/Denver", "radarStation": "KABX",
    "forecastZone": "https://api.weather.gov/zones/forecast/NMZ220",
    "county": "https://api.weather.gov/zones/county/NMC053",
    "fireWeatherZone": "https://api.weather.gov/zones/fire/NMZ106"}}

FORECAST = {"properties": {
    "updated": None, "updateTime": "2026-09-23T12:02:03+00:00",
    "generatedAt": "2026-09-23T17:01:30+00:00", "periods": [
        {"number": 1, "name": "Today", "startTime": "2026-09-23T11:00:00-06:00",
         "endTime": "2026-09-23T18:00:00-06:00", "isDaytime": True, "temperature": 69,
         "temperatureUnit": "F", "temperatureTrend": None,
         "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 85},
         "windSpeed": "5 to 10 mph", "windDirection": "SE",
         "icon": "https://api.weather.gov/icons/land/day/tsra,80/tsra,90?size=medium",
         "shortForecast": "Showers And Thunderstorms",
         "detailedForecast": "Showers and thunderstorms. Cloudy, with a high near 69."},
        {"number": 2, "name": "Tonight", "startTime": "2026-09-23T18:00:00-06:00",
         "endTime": "2026-09-24T06:00:00-06:00", "isDaytime": False, "temperature": 56,
         "temperatureUnit": "F", "temperatureTrend": "falling",
         "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": None},
         "windSpeed": "5 mph", "windDirection": "SE",
         "icon": "https://api.weather.gov/icons/land/night/tsra,60?size=medium",
         "shortForecast": "Chance Showers", "detailedForecast": "A chance of showers."},
        {"number": 3, "name": "Thursday", "startTime": "2026-09-24T06:00:00-06:00",
         "endTime": "2026-09-24T18:00:00-06:00", "isDaytime": True, "temperature": 72,
         "temperatureUnit": "F", "temperatureTrend": None,
         "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": 20},
         "windSpeed": "10 mph", "windDirection": "S", "icon": None,
         "shortForecast": "Partly Sunny", "detailedForecast": "Partly sunny."}]}}


def _hour(n, temp, pop, wind, direction="E"):
    """One hourly period starting at 2026-09-23 <n>:00 local (America/Denver, UTC-6)."""
    return {"number": n, "name": "", "startTime": "2026-09-23T%02d:00:00-06:00" % n,
            "endTime": "2026-09-23T%02d:00:00-06:00" % (n + 1), "isDaytime": n < 19,
            "temperature": temp, "temperatureUnit": "F", "temperatureTrend": None,
            "probabilityOfPrecipitation": {"unitCode": "wmoUnit:percent", "value": pop},
            "dewpoint": {"unitCode": "wmoUnit:degC", "value": 15.555555555555555},
            "relativeHumidity": {"unitCode": "wmoUnit:percent", "value": 89},
            "windSpeed": wind, "windDirection": direction,
            "icon": "https://api.weather.gov/icons/land/day/tsra,80?size=small",
            "shortForecast": "Showers And Thunderstorms", "detailedForecast": ""}


HOURLY = {"properties": {
    "updateTime": "2026-09-23T12:02:03+00:00", "periods": [
        _hour(11, 64, 82, "5 mph"), _hour(12, 66, 80, "5 mph"),
        _hour(13, 68, 70, "5 to 10 mph"), _hour(14, 70, 60, "10 to 15 mph", "SE"),
        _hour(15, 71, 55, "15 mph"), _hour(16, 70, None, "10 mph")]}}


def _station(sid, name):
    return {"id": "https://api.weather.gov/stations/%s" % sid, "type": "Feature",
            "properties": {"stationIdentifier": sid, "name": name}}


STATIONS = {"type": "FeatureCollection",
            "features": [_station("KONM", "Socorro Municipal Airport"),
                         _station("KBRG", "Belen Regional Airport"),
                         _station("KABQ", "Albuquerque International Sunport"),
                         _station("KTCS", "Truth Or Consequences")],
            "observationStations": ["https://api.weather.gov/stations/KONM",
                                    "https://api.weather.gov/stations/KBRG",
                                    "https://api.weather.gov/stations/KABQ",
                                    "https://api.weather.gov/stations/KTCS"]}


def _q(unit, value):
    return {"unitCode": "wmoUnit:" + unit, "value": value, "qualityControl": "V"}


def _obs(sid, name, ts, temp_c=15.7, wind=("km_h-1", 16.56), temp_unit="degC"):
    return {"properties": {
        "station": "https://api.weather.gov/stations/%s" % sid, "stationId": sid,
        "stationName": name, "timestamp": ts, "textDescription": "Rain",
        "icon": "https://api.weather.gov/icons/land/day/rain?size=medium",
        "temperature": _q(temp_unit, temp_c), "dewpoint": _q("degC", 15.1),
        "windDirection": _q("degree_(angle)", 350), "windSpeed": _q(wind[0], wind[1]),
        "windGust": _q(wind[0], None),
        "barometricPressure": _q("Pa", 102340), "seaLevelPressure": _q("Pa", None),
        "visibility": _q("m", 16090), "relativeHumidity": _q("percent", 96.225059235871),
        "windChill": _q("degC", None), "heatIndex": _q("degC", None),
        "cloudLayers": [{"base": _q("m", 180), "amount": "BKN"},
                        {"base": _q("m", 460), "amount": "FEW"},
                        {"base": _q("m", None), "amount": "CLR"}]}}


NOW = datetime(2026, 9, 23, 19, 30, tzinfo=timezone.utc)      # 13:30 MDT, a Wednesday
FRESH_TS = "2026-09-23T19:15:00+00:00"                          # 15 min before NOW
STALE_TS = "2026-09-23T13:00:00+00:00"                          # 6.5 h before NOW


@pytest.fixture
def fixed_now(monkeypatch):
    monkeypatch.setattr(util, "utcnow", lambda: NOW)
    return NOW


@pytest.fixture
def meta(cfg, cache, fake_http):
    fake_http.add("/points/34.0584,-106.8914", POINTS)
    return nws.site_metadata(cfg, cache, SITE)


# ---- /points ---------------------------------------------------------------------------
def test_site_metadata_parsing(cfg, cache, fake_http, meta):
    assert fake_http.calls == ["https://api.weather.gov/points/34.0584,-106.8914"]
    assert meta["ok"] and not meta["stale"] and meta["error"] is None and meta["age_s"] == 0
    assert (meta["grid_id"], meta["grid_x"], meta["grid_y"], meta["office"]) == ("ABQ", 86, 76, "ABQ")
    assert meta["forecast_url"].endswith("/gridpoints/ABQ/86,76/forecast")
    assert meta["hourly_url"].endswith("/forecast/hourly")
    assert meta["stations_url"].endswith("/stations")
    assert meta["tz"] == "America/Denver" and meta["radar_station"] == "KABX"
    assert (meta["city"], meta["state"]) == ("Socorro", "NM")
    assert (meta["forecast_zone"], meta["county_zone"], meta["fire_zone"]) == ("NMZ220", "NMC053", "NMZ106")
    assert meta["zone_urls"] == ["https://api.weather.gov/zones/forecast/NMZ220",
                                 "https://api.weather.gov/zones/county/NMC053",
                                 "https://api.weather.gov/zones/fire/NMZ106"]
    # second call is served from the cache
    again = nws.site_metadata(cfg, cache, SITE)
    assert again["grid_id"] == "ABQ" and len(fake_http.calls) == 1


def test_site_metadata_url_uses_four_decimals(cfg, cache, fake_http):
    fake_http.add("/points/", POINTS)
    nws.site_metadata(cfg, cache, {"slug": "x", "name": "X", "lat": 35, "lon": -106.65})
    assert fake_http.calls[-1].endswith("/points/35.0000,-106.6500")


def test_site_metadata_not_ok(cfg, cache, fake_http):
    fake_http.fail("api.weather.gov")
    m = nws.site_metadata(cfg, cache, SITE)
    assert not m["ok"] and m["stale"] and m["error"]
    assert m["tz"] == "UTC" and m["grid_id"] is None and m["forecast_url"] is None
    assert m["zone_urls"] == [] and m["forecast_zone"] is None
    # a 200 with a problem document is not metadata and must not be cached
    fake_http.routes.clear()
    fake_http.add("api.weather.gov", {"title": "Unexpected Problem", "status": 500})
    m = nws.site_metadata(cfg, cache, SITE)
    assert not m["ok"] and "unexpected payload" in m["error"]
    assert cache.get_any("points/34.0584,-106.8914") == (None, None)


# ---- forecast --------------------------------------------------------------------------
def test_forecast_fahrenheit(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("86,76/forecast", FORECAST)
    cfg.forecast_periods = 2
    fc = nws.forecast(cfg, cache, SITE, meta)
    assert fc["ok"] and not fc["stale"] and fc["error"] is None
    assert fc["updated"] == "2026-09-23T12:02:03+00:00"
    assert len(fc["periods"]) == 2
    p = fc["periods"][0]
    assert p["number"] == 1 and p["name"] == "Today" and p["is_day"] is True
    assert p["start"] == "2026-09-23T11:00:00-06:00" and p["end"] == "2026-09-23T18:00:00-06:00"
    assert p["temp_f"] == 69 and p["temp_c"] == pytest.approx(20.6, abs=0.05)
    assert p["pop"] == 85 and p["wind"] == "5 to 10 mph" and p["wind_dir"] == "SE"
    assert p["short"] == "Showers And Thunderstorms" and p["detailed"].startswith("Showers")
    assert p["icon"].startswith("https://") and p["temp_trend"] is None
    n = fc["periods"][1]
    assert n["is_day"] is False and n["pop"] is None and n["temp_trend"] == "falling"
    assert n["temp_f"] == 56


def test_forecast_celsius(cfg, cache, fake_http, meta, fixed_now):
    body = copy.deepcopy(FORECAST)
    for p in body["properties"]["periods"]:
        p["temperatureUnit"] = "C"
    body["properties"]["periods"][0]["temperature"] = 27
    body["properties"]["periods"][2]["icon"] = None
    fake_http.add("86,76/forecast", body)
    fc = nws.forecast(cfg, cache, SITE, meta)
    assert fc["ok"]
    assert fc["periods"][0]["temp_c"] == 27 and fc["periods"][0]["temp_f"] == pytest.approx(80.6)
    assert fc["periods"][2]["icon"] is None


def test_forecast_last_good_fallback(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("86,76/forecast", FORECAST)
    assert nws.forecast(cfg, cache, SITE, meta)["ok"]
    cfg.forecast_ttl = 0                      # force a refetch...
    fake_http.routes.clear()
    fake_http.fail("api.weather.gov")         # ...which now fails
    fc = nws.forecast(cfg, cache, SITE, meta)
    assert fc["ok"] and fc["stale"] and fc["error"] and fc["age_s"] >= 0
    assert [p["name"] for p in fc["periods"]] == ["Today", "Tonight", "Thursday"]
    cfg.forecast_max_stale = 0                # beyond max_stale nothing is usable
    fc = nws.forecast(cfg, cache, SITE, meta)
    assert not fc["ok"] and fc["periods"] == [] and fc["updated"] is None


def test_forecast_bad_shape_not_cached(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("86,76/forecast", {"properties": {"periods": []}})
    fc = nws.forecast(cfg, cache, SITE, meta)
    assert not fc["ok"] and fc["stale"] and fc["error"] and fc["periods"] == []
    assert cache.get_any("forecast/34.0584,-106.8914") == (None, None)
    fake_http.routes.clear()
    fake_http.add("86,76/forecast", {"nope": 1})
    assert not nws.forecast(cfg, cache, SITE, meta)["ok"]
    assert cache.get_any("forecast/34.0584,-106.8914") == (None, None)


def test_forecast_without_metadata(cfg, cache, fake_http, meta, fixed_now):
    no_meta = {"ok": False, "forecast_url": None, "hourly_url": None, "tz": "UTC"}
    fc = nws.forecast(cfg, cache, SITE, no_meta)
    assert not fc["ok"] and "metadata" in fc["error"] and len(fake_http.calls) == 1
    # ...but a last-good copy is still served (stale) when metadata later disappears
    fake_http.add("86,76/forecast", FORECAST)
    assert nws.forecast(cfg, cache, SITE, meta)["ok"]
    fc = nws.forecast(cfg, cache, SITE, no_meta)
    assert fc["ok"] and fc["stale"] and fc["error"] and len(fc["periods"]) == 3
    assert nws.forecast(cfg, cache, SITE, None)["ok"]


def _at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_forecast_drops_periods_that_already_ended(cfg, cache, fake_http, meta, monkeypatch):
    fake_http.add("86,76/forecast", FORECAST)
    cfg.forecast_periods = 2
    # 18:30 MDT: "Today" (ends 18:00 MDT) is over; the cap applies AFTER dropping it
    monkeypatch.setattr(util, "utcnow", lambda: _at(2026, 9, 24, 0, 30))
    fc = nws.forecast(cfg, cache, SITE, meta)
    assert fc["ok"] and fc["error"] is None
    assert [p["name"] for p in fc["periods"]] == ["Tonight", "Thursday"]
    assert fc["periods"][0]["number"] == 2
    # a period ending exactly now has ended
    monkeypatch.setattr(util, "utcnow", lambda: _at(2026, 9, 24, 0, 0))
    assert [p["name"] for p in nws.forecast(cfg, cache, SITE, meta)["periods"]] == ["Tonight", "Thursday"]
    # one second earlier it has not
    monkeypatch.setattr(util, "utcnow", lambda: datetime(2026, 9, 23, 23, 59, 59, tzinfo=timezone.utc))
    assert [p["name"] for p in nws.forecast(cfg, cache, SITE, meta)["periods"]] == ["Today", "Tonight"]


def test_forecast_all_periods_ended_is_not_ok(cfg, cache, fake_http, meta, monkeypatch):
    fake_http.add("86,76/forecast", FORECAST)
    monkeypatch.setattr(util, "utcnow", lambda: _at(2026, 9, 25, 1, 0))
    fc = nws.forecast(cfg, cache, SITE, meta)
    assert not fc["ok"] and fc["periods"] == [] and "already ended" in fc["error"]
    # a stale last-good copy keeps the fetch error in the explanation
    cfg.forecast_ttl = 0
    cfg.forecast_max_stale = 10 ** 9
    fake_http.routes.clear()
    fake_http.fail("api.weather.gov")
    fc = nws.forecast(cfg, cache, SITE, meta)
    assert not fc["ok"] and fc["stale"] and "already ended" in fc["error"]
    assert "fake: no route" in fc["error"]


def test_forecast_keeps_period_with_unparsable_end(cfg, cache, fake_http, meta, monkeypatch):
    body = copy.deepcopy(FORECAST)
    body["properties"]["periods"][0]["endTime"] = "garbage"
    fake_http.add("86,76/forecast", body)
    monkeypatch.setattr(util, "utcnow", lambda: _at(2026, 9, 24, 3, 0))
    assert [p["name"] for p in nws.forecast(cfg, cache, SITE, meta)["periods"]] == [
        "Today", "Tonight", "Thursday"]


# ---- hourly ----------------------------------------------------------------------------
def test_hourly_window_from_current_hour(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("forecast/hourly", HOURLY)
    cfg.hourly_hours = 3
    h = nws.hourly(cfg, cache, SITE, meta)
    assert h["ok"] and h["updated"] == "2026-09-23T12:02:03+00:00"
    assert [x["hour"] for x in h["hours"]] == ["13:00", "14:00", "15:00"]
    first = h["hours"][0]
    assert first["start"] == "2026-09-23T13:00:00-06:00"
    assert first["local"] == "Wed 13:00" and first["day"] == "Wed" and first["hour"] == "13:00"
    assert first["is_day"] is True and first["temp_f"] == 68
    assert first["temp_c"] == pytest.approx(20.0, abs=0.05)
    assert first["pop"] == 70 and first["rh"] == 89
    assert first["dewpoint_c"] == pytest.approx(15.6, abs=0.05)
    assert first["dewpoint_f"] == pytest.approx(60.0, abs=0.05)
    assert first["wind_mph"] == 10 and first["wind_kmh"] == pytest.approx(16.1, abs=0.05)
    assert first["wind_dir"] == "E" and first["short"] == "Showers And Thunderstorms"
    assert first["icon"].startswith("https://")
    second = h["hours"][1]
    assert second["wind_mph"] == 15 and second["wind_dir"] == "SE"
    # the whole window when hourly_hours is generous
    cfg.hourly_hours = 24
    h = nws.hourly(cfg, cache, SITE, meta)
    assert [x["hour"] for x in h["hours"]] == ["13:00", "14:00", "15:00", "16:00"]
    assert h["hours"][-1]["pop"] is None and len(fake_http.calls) == 2  # served from cache


def test_hourly_labels_follow_site_timezone(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("forecast/hourly", HOURLY)
    chicago = dict(meta, tz="America/Chicago")          # 13:00 MDT == 14:00 CDT
    h = nws.hourly(cfg, cache, SITE, chicago)
    assert h["hours"][0]["local"] == "Wed 14:00"
    # meta without tz -> UTC labels (13:00-06:00 == 19:00Z)
    h = nws.hourly(cfg, cache, SITE, dict(meta, tz=None))
    assert h["hours"][0]["local"] == "Wed 19:00"


def test_hourly_entirely_in_the_past_is_not_ok(cfg, cache, fake_http, meta, monkeypatch):
    fake_http.add("forecast/hourly", HOURLY)
    monkeypatch.setattr(util, "utcnow", lambda: datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc))
    h = nws.hourly(cfg, cache, SITE, meta)
    assert not h["ok"] and h["hours"] == [] and "current hour" in h["error"]


def test_hourly_not_ok_shapes(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("forecast/hourly", {"properties": {"periods": "nope"}})
    h = nws.hourly(cfg, cache, SITE, meta)
    assert not h["ok"] and h["hours"] == [] and h["updated"] is None
    assert cache.get_any("hourly/34.0584,-106.8914") == (None, None)
    fake_http.routes.clear()
    fake_http.fail("api.weather.gov")
    h = nws.hourly(cfg, cache, SITE, meta)
    assert not h["ok"] and h["stale"] and h["error"]


@pytest.mark.parametrize("text,mph", [
    ("10 mph", 10.0), ("10 to 15 mph", 15.0), ("5 to 10 mph", 10.0), ("0 mph", 0.0),
    ("Calm", None), ("", None), (None, None), ("16 km/h", pytest.approx(9.94, abs=0.01)),
    ({"unitCode": "wmoUnit:km_h-1", "value": 16.0934}, pytest.approx(10.0, abs=0.01)),
    ({"unitCode": "wmoUnit:km_h-1", "value": None}, None),
])
def test_parse_wind_mph(text, mph):
    assert nws.parse_wind_mph(text) == mph


# ---- observation -----------------------------------------------------------------------
def test_observation_units_kmh(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", _obs("KONM", "Socorro Municipal Airport", FRESH_TS))
    o = nws.observation(cfg, cache, SITE, meta)
    assert o["ok"] and not o["stale"] and o["error"] is None and o["age_s"] == 0
    assert o["station_id"] == "KONM" and o["station_name"] == "Socorro Municipal Airport"
    assert o["timestamp"] == FRESH_TS and o["age_min"] == pytest.approx(15.0)
    assert o["text"] == "Rain" and o["icon"].startswith("https://")
    assert o["temp_c"] == 15.7 and o["temp_f"] == pytest.approx(60.3, abs=0.05)
    assert o["dewpoint_c"] == 15.1 and o["dewpoint_f"] == pytest.approx(59.2, abs=0.05)
    assert o["rh"] == pytest.approx(96.2, abs=0.05)
    assert o["wind_kmh"] == pytest.approx(16.6, abs=0.05)
    assert o["wind_mph"] == pytest.approx(10.3, abs=0.05)
    assert o["gust_mph"] is None and o["gust_kmh"] is None
    assert o["wind_dir_deg"] == 350 and o["wind_dir"] == "N"
    assert o["pressure_inhg"] == pytest.approx(30.22, abs=0.01)
    assert o["pressure_hpa"] == pytest.approx(1023.4, abs=0.05)
    assert o["visibility_mi"] == pytest.approx(10.0, abs=0.05)
    assert o["visibility_km"] == pytest.approx(16.1, abs=0.05)
    assert o["heat_index_f"] is None and o["wind_chill_f"] is None
    assert o["cloud_layers"] == ["BKN 600 ft", "FEW 1500 ft", "CLR"]
    # only the first station was asked
    assert not any("KBRG" in c for c in fake_http.calls)


def test_observation_units_ms_and_degf(cfg, cache, fake_http, meta, fixed_now):
    body = _obs("KONM", "Socorro Municipal Airport", FRESH_TS, temp_c=60.0,
                wind=("m_s-1", 5.0), temp_unit="degF")
    p = body["properties"]
    p["windGust"] = _q("m_s-1", 10.0)
    p["heatIndex"] = _q("degC", 30.0)
    p["windChill"] = _q("degF", 41.0)
    p["barometricPressure"] = _q("Pa", None)
    p["seaLevelPressure"] = _q("hPa", 1013.25)
    p["visibility"] = _q("km", 8.0)
    p["cloudLayers"] = []
    fake_http.add("86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", body)
    o = nws.observation(cfg, cache, SITE, meta)
    assert o["ok"]
    assert o["temp_f"] == 60 and o["temp_c"] == pytest.approx(15.6, abs=0.05)
    assert o["wind_kmh"] == 18 and o["wind_mph"] == pytest.approx(11.2, abs=0.05)
    assert o["gust_kmh"] == 36 and o["gust_mph"] == pytest.approx(22.4, abs=0.05)
    assert o["heat_index_f"] == 86 and o["wind_chill_f"] == 41
    assert o["pressure_hpa"] == pytest.approx(1013.2, abs=0.05)
    assert o["pressure_inhg"] == pytest.approx(29.92, abs=0.01)
    assert o["visibility_km"] == 8 and o["visibility_mi"] == pytest.approx(5.0, abs=0.05)
    assert o["cloud_layers"] == []


def test_observation_missing_values_are_none(cfg, cache, fake_http, meta, fixed_now):
    body = _obs("KONM", "Socorro Municipal Airport", FRESH_TS)
    p = body["properties"]
    for k in ("dewpoint", "windDirection", "windSpeed", "barometricPressure", "visibility",
              "relativeHumidity"):
        p[k]["value"] = None
    p["cloudLayers"] = None
    p["textDescription"] = None
    p["icon"] = None
    fake_http.add("86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", body)
    o = nws.observation(cfg, cache, SITE, meta)
    assert o["ok"] and o["temp_c"] == 15.7
    for k in ("dewpoint_f", "dewpoint_c", "wind_mph", "wind_kmh", "wind_dir_deg",
              "pressure_inhg", "pressure_hpa", "visibility_mi", "visibility_km", "rh", "icon"):
        assert o[k] is None, k
    assert o["wind_dir"] == "" and o["cloud_layers"] == [] and o["text"] == ""


def test_observation_calm_has_no_direction(cfg, cache, fake_http, meta, fixed_now):
    """NWS reports calm as speed 0 + direction 0; a 0.0 bearing must not become "N"."""
    body = _obs("KONM", "Socorro Municipal Airport", FRESH_TS, wind=("km_h-1", 0))
    body["properties"]["windDirection"] = _q("degree_(angle)", 0)
    fake_http.add("86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", body)
    o = nws.observation(cfg, cache, SITE, meta)
    assert o["ok"] and o["wind_mph"] == 0 and o["wind_kmh"] == 0
    assert o["wind_dir_deg"] is None and o["wind_dir"] == ""
    # calm with the direction missing altogether is calm too
    p = nws._parse_observation(dict(body["properties"], windDirection=_q("degree_(angle)", None)))
    assert p["wind_dir_deg"] is None and p["wind_dir"] == ""
    # a real north wind keeps its 0 deg bearing
    p = nws._parse_observation(dict(body["properties"], windSpeed=_q("km_h-1", 9.36)))
    assert p["wind_dir_deg"] == 0 and p["wind_dir"] == "N"


def test_observation_first_station_stale_second_used(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", _obs("KONM", "Socorro", STALE_TS))
    fake_http.add("/stations/KBRG/observations/latest", _obs("KBRG", "Belen Regional Airport", FRESH_TS))
    o = nws.observation(cfg, cache, SITE, meta)
    assert o["ok"] and o["station_id"] == "KBRG" and o["station_name"] == "Belen Regional Airport"
    assert o["age_min"] == pytest.approx(15.0)
    asked = [c for c in fake_http.calls if "/observations/latest" in c]
    assert [c.split("/stations/")[1].split("/")[0] for c in asked] == ["KONM", "KBRG"]


def test_observation_skips_missing_temperature_and_failed_stations(cfg, cache, fake_http, meta, fixed_now):
    no_temp = _obs("KONM", "Socorro", FRESH_TS)
    no_temp["properties"]["temperature"]["value"] = None
    fake_http.add("86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", no_temp)
    fake_http.fail("/stations/KBRG/observations/latest")
    fake_http.add("/stations/KABQ/observations/latest", _obs("KABQ", "Sunport", FRESH_TS))
    o = nws.observation(cfg, cache, SITE, meta)
    assert o["ok"] and o["station_id"] == "KABQ"


def test_observation_none_usable(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", _obs("KONM", "Socorro", STALE_TS))
    fake_http.add("/stations/KBRG/observations/latest", {"properties": {"nope": 1}})
    fake_http.fail("/stations/KABQ/observations/latest")
    o = nws.observation(cfg, cache, SITE, meta)
    assert not o["ok"] and o["stale"] and o["station_id"] is None and o["temp_f"] is None
    assert o["cloud_layers"] == []
    for sid in ("KONM", "KBRG", "KABQ"):
        assert sid in o["error"]
    assert "old" in o["error"]
    # only MAX_STATIONS (3) were tried: KTCS was never asked, even though it is listed
    assert not any("KTCS" in c for c in fake_http.calls)
    # a bad-shape observation is never cached
    assert cache.get_any("obs/socorro/KBRG") == (None, None)


def test_observation_stale_last_good_fallback(cfg, cache, fake_http, meta, fixed_now):
    fake_http.add("86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", _obs("KONM", "Socorro", FRESH_TS))
    assert nws.observation(cfg, cache, SITE, meta)["ok"]
    cfg.obs_ttl = 0
    fake_http.routes.clear()
    fake_http.fail("api.weather.gov")
    o = nws.observation(cfg, cache, SITE, meta)
    assert o["ok"] and o["stale"] and o["error"] and o["station_id"] == "KONM"
    assert o["temp_c"] == 15.7
    # ...until the report itself is older than obs_max_age_min
    cfg.obs_max_age_min = 10
    o = nws.observation(cfg, cache, SITE, meta)
    assert not o["ok"] and "old" in o["error"]


def test_observation_without_station_list(cfg, cache, fake_http, meta, fixed_now):
    fake_http.fail("86,76/stations")
    o = nws.observation(cfg, cache, SITE, meta)
    assert not o["ok"] and o["error"].startswith("station list unavailable")
    assert not any("/observations/" in c for c in fake_http.calls)
    fake_http.routes.clear()
    fake_http.add("86,76/stations", {"features": [], "observationStations": []})
    o = nws.observation(cfg, cache, SITE, meta)
    assert not o["ok"] and "no observation stations" in o["error"]
    # metadata gone and nothing cached -> not ok, no network
    calls = len(fake_http.calls)
    o = nws.observation(cfg, cache, SITE, {"ok": False, "stations_url": None, "tz": "UTC"})
    assert not o["ok"] and len(fake_http.calls) == calls


def test_station_list_order_and_names():
    stations = nws._station_list(STATIONS)
    assert stations[:2] == [("KONM", "Socorro Municipal Airport"), ("KBRG", "Belen Regional Airport")]
    only_features = {"features": STATIONS["features"][:2]}
    assert [s for s, _ in nws._station_list(only_features)] == ["KONM", "KBRG"]
    # ids go into a URL path: anything not [A-Za-z0-9_-] is dropped, duplicates collapse
    bad = {"observationStations": ["https://api.weather.gov/stations/K%20NM",
                                   "https://api.weather.gov/stations/KABQ",
                                   "https://api.weather.gov/stations/KABQ", ""]}
    assert nws._station_list(bad) == [("KABQ", "")]
    assert nws._station_list({"features": "nope", "observationStations": None}) == []


# ---- cache keys follow the location, not the slug ---------------------------------------------
def test_a_site_that_moves_gets_its_new_metadata_at_once(cfg, cache, fake_http, fixed_now):
    """Same slug, new coordinates (the README invites exactly this edit): /points is asked
    again at once instead of serving the old place's grid, zones and time zone for up to 7
    days (60 while NWS is down), and the forecast, hourly forecast and station list come
    from the new grid. Every cache key names the point, none the slug."""
    fake_http.add("/points/34.0584,-106.8914", POINTS)
    fake_http.add("86,76/forecast/hourly", HOURLY)
    fake_http.add("86,76/forecast", FORECAST)
    fake_http.add("86,76/stations", STATIONS)
    fake_http.add("/stations/KONM/observations/latest", _obs("KONM", "Socorro", FRESH_TS))
    m = nws.site_metadata(cfg, cache, SITE)
    assert m["forecast_zone"] == "NMZ220" and m["no_grid"] is False
    assert nws.forecast(cfg, cache, SITE, m)["ok"] and nws.hourly(cfg, cache, SITE, m)["ok"]
    assert nws.observation(cfg, cache, SITE, m)["ok"]
    for kind in ("points", "forecast", "hourly", "stations"):
        assert cache.get_any("%s/34.0584,-106.8914" % kind)[0] is not None, kind
        assert cache.get_any("%s/socorro" % kind) == (None, None), kind

    moved = dict(SITE, lat=32.7026, lon=-103.1360)          # same slug, another town
    elsewhere = copy.deepcopy(POINTS)
    elsewhere["properties"].update({
        "gridId": "MAF", "gridX": 7, "gridY": 81, "cwa": "MAF", "timeZone": "America/Chicago",
        "forecast": "https://api.weather.gov/gridpoints/MAF/7,81/forecast",
        "forecastHourly": "https://api.weather.gov/gridpoints/MAF/7,81/forecast/hourly",
        "observationStations": "https://api.weather.gov/gridpoints/MAF/7,81/stations",
        "forecastZone": "https://api.weather.gov/zones/forecast/NMZ034",
        "county": "https://api.weather.gov/zones/county/NMC025",
        "fireWeatherZone": "https://api.weather.gov/zones/fire/NMZ111"})
    fake_http.add("/points/32.7026,-103.1360", elsewhere)
    fake_http.add("7,81/forecast/hourly", HOURLY)
    fake_http.add("7,81/forecast", FORECAST)
    fake_http.add("7,81/stations", STATIONS)
    del fake_http.calls[:]
    m2 = nws.site_metadata(cfg, cache, moved)
    assert fake_http.calls == ["https://api.weather.gov/points/32.7026,-103.1360"]
    assert (m2["grid_id"], m2["tz"], m2["forecast_zone"]) == ("MAF", "America/Chicago", "NMZ034")
    for fn in (nws.forecast, nws.hourly, nws.observation):
        assert fn(cfg, cache, moved, m2)["ok"]
    assert any("/gridpoints/MAF/7,81/forecast" in c for c in fake_http.calls)
    assert any("/gridpoints/MAF/7,81/stations" in c for c in fake_http.calls)
    assert not any("/gridpoints/ABQ/" in c for c in fake_http.calls)
    # and back: the first place's copies are still there, nothing is asked again
    del fake_http.calls[:]
    assert nws.site_metadata(cfg, cache, SITE)["grid_id"] == "ABQ" and fake_http.calls == []


# ---- a point with zones but no forecast grid (American Samoa) -----------------------------------
SAMOA = {"slug": "pago_pago", "name": "Pago Pago, AS", "lat": -14.2756, "lon": -170.7020}
SAMOA_POINTS = {"properties": {   # api.weather.gov/points/-14.2756,-170.702 on 2026-09-23
    "cwa": "PPG", "gridId": None, "gridX": None, "gridY": None, "forecast": None,
    "forecastHourly": None, "forecastGridData": None, "observationStations": None,
    "relativeLocation": {"properties": {"city": "Pago Pago", "state": "AS"}},
    "forecastZone": "https://api.weather.gov/zones/forecast/ASZ001",
    "county": "https://api.weather.gov/zones/county/ASC010",
    "fireWeatherZone": "https://api.weather.gov/zones/fire/ASZ001",
    "timeZone": "Pacific/Pago_Pago", "radarStation": None}}


def test_a_point_without_a_forecast_grid(cfg, cache, fake_http, fixed_now):
    """NWS answers /points for American Samoa with zones and a time zone but no grid: that
    is valid metadata (local times in Samoa time, alerts AT the site by zone id), and the
    forecast, hourly forecast and observations are "not provided", with no request and no
    error, instead of "unexpected payload shape" and UTC times on every run."""
    fake_http.add("/points/-14.2756,-170.7020", SAMOA_POINTS)
    m = nws.site_metadata(cfg, cache, SAMOA)
    assert m["ok"] and not m["stale"] and m["error"] is None and m["no_grid"] is True
    assert m["tz"] == "Pacific/Pago_Pago" and m["office"] == "PPG" and m["grid_id"] is None
    assert (m["forecast_zone"], m["county_zone"]) == ("ASZ001", "ASC010")
    assert cache.get_any("points/-14.2756,-170.7020")[0] is not None
    calls = list(fake_http.calls)
    for fn, empty in ((nws.forecast, "periods"), (nws.hourly, "hours"),
                      (nws.observation, "cloud_layers")):
        res = fn(cfg, cache, SAMOA, m)
        assert not res["ok"] and res["not_provided"] is True and not res["stale"], fn
        assert res["error"] == nws.NOT_PROVIDED and res[empty] == [], fn
    assert fake_http.calls == calls
    # a payload with neither a grid nor zones and a time zone is still refused
    assert not nws._valid_points({"properties": {"gridId": None, "timeZone": "UTC"}})
    assert not nws._valid_points({"properties": {"forecastZone": "x"}})
