"""Tests for the shared foundation: config, geo, http cache, util."""
import email.message
import http.client as httpclient
import os
import socket
import urllib.error
import urllib.request

import pytest

from weather import geo, http, util
from weather.config import Config, parse_sites


def test_parse_sites_roundtrip_and_errors():
    assert parse_sites("") == []
    s = parse_sites("a|A Town|33.1|-101.2; b|B|34|-105")
    assert [x["slug"] for x in s] == ["a", "b"] and s[0]["lat"] == 33.1
    for bad in ("a|A|33", "a b|A|1|2", "a|A|x|2", "a|A|95|2"):
        with pytest.raises(ValueError):
            parse_sites(bad)


def test_config_env_and_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("WEATHER_MAP_MILES", "80")
    monkeypatch.setenv("WEATHER_SITES", "x|X|30|-100")
    c = Config.from_env(out_dir=str(tmp_path))
    assert c.map_miles == 80 and c.sites[0]["slug"] == "x" and c.out_dir == str(tmp_path)
    assert c.lock_file.endswith("run.lock")
    with pytest.raises(TypeError):
        Config.from_env(nonsense=1)
    monkeypatch.setenv("WEATHER_UNITS", "furlongs")
    with pytest.raises(ValueError):
        Config.from_env()


def test_map_frame_is_square_in_km():
    f = geo.MapFrame(34.0584, -106.8914, 160.9344, 600, 9)
    assert f.size == (600, 600)
    assert abs(geo.haversine_km(f.south, f.lon0, f.north, f.lon0) - 160.93) < 0.05
    assert abs(geo.haversine_km(f.lat0, f.west, f.lat0, f.east) - 160.93) < 0.05
    cx, cy = f.lonlat_to_px(f.lon0, f.lat0)
    assert abs(cx - 300) < 0.01 and abs(cy - 300) < 3      # Mercator: centre slightly low
    lon, lat = f.px_to_lonlat(cx, cy)
    assert abs(lon - f.lon0) < 1e-6 and abs(lat - f.lat0) < 1e-6
    assert f.lonlat_to_px(f.west, f.north) == pytest.approx((0.0, 0.0), abs=1e-6)
    assert f.lonlat_to_px(f.east, f.south) == pytest.approx((600.0, 600.0), abs=1e-6)
    assert abs(f.px_for_km(16.0934) - 60.0) < 0.2
    l, t, r, b = f.canvas_crop()
    assert abs((r - l) - (f.gx1 - f.gx0)) < 1 and abs((b - t) - (f.gy1 - f.gy0)) < 1
    tx0, ty0, tx1, ty1 = f.tile_range()
    assert f.canvas_size() == ((tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256)
    assert f.tile_paste_origin(tx0, ty0) == (0, 0)


def test_mrms_grid():
    assert geo.mrms_cell(54.995, -129.995) == (0, 0)
    assert geo.mrms_cell(34.0584, -106.8914) == (2310, 2094)
    assert geo.mrms_dbz(0) == -32.0 and geo.mrms_dbz(84) == 10.0 and geo.mrms_dbz(255) is None
    assert geo.mrms_covers(34, -106) and not geo.mrms_covers(10, -106)


def test_point_in_polygon_and_bbox():
    sq = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
    hole = [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]
    assert geo.point_in_polygon(5, 5, [sq])
    assert not geo.point_in_polygon(5, 5, [sq, hole])
    assert not geo.point_in_polygon(11, 5, [sq])
    assert geo.point_in_ring(0, 5, sq)              # on the edge counts
    mp = {"type": "MultiPolygon", "coordinates": [[sq, hole], [[[20, 20], [30, 20], [30, 30], [20, 20]]]]}
    assert geo.point_in_geometry(1, 1, mp) and geo.point_in_geometry(25, 22, mp)
    assert not geo.point_in_geometry(5, 5, mp)
    assert geo.geometry_bbox(mp) == (0, 0, 30, 30)
    assert geo.geometry_bbox(None) is None and not geo.point_in_geometry(1, 1, None)
    assert geo.bbox_intersects((0, 0, 1, 1), (0.5, 0.5, 2, 2))
    assert not geo.bbox_intersects((0, 0, 1, 1), (2, 2, 3, 3))
    merged = geo.merge_geometries([{"type": "Polygon", "coordinates": [sq]}, None, mp])
    assert merged["type"] == "MultiPolygon" and len(merged["coordinates"]) == 3


def test_cache_and_cached_json(cfg, cache, fake_http):
    cache.put("k", {"a": 1})
    assert cache.get("k", 10) == {"a": 1}
    assert cache.get("k", -1) is None and cache.get_any("missing") == (None, None)
    fake_http.add("api.weather.gov/points/1,1", {"properties": {"gridId": "X"}})
    r = http.cached_json(cfg, cache, "p", "https://api.weather.gov/points/1,1", 60, 600)
    assert r["ok"] and not r["stale"] and not r["from_cache"] and r["data"]["properties"]["gridId"] == "X"
    r = http.cached_json(cfg, cache, "p", "https://api.weather.gov/points/1,1", 60, 600)
    assert r["from_cache"] and len(fake_http.calls) == 1
    # fetch fails -> last good within max_stale
    fake_http.routes.clear()
    fake_http.fail("api.weather.gov")
    r = http.cached_json(cfg, cache, "p", "https://api.weather.gov/points/1,1", 0, 600)
    assert r["ok"] and r["stale"] and r["error"] and r["data"]["properties"]["gridId"] == "X"
    # ... but not beyond it
    r = http.cached_json(cfg, cache, "p", "https://api.weather.gov/points/1,1", 0, 0)
    assert not r["ok"] and r["data"] is None
    # validate rejects wrong shape and never caches it
    fake_http.routes.clear()
    fake_http.add("api.weather.gov", {"nope": 1})
    r = http.cached_json(cfg, cache, "q", "https://api.weather.gov/x", 0, 0,
                         validate=lambda d: "properties" in d)
    assert not r["ok"] and cache.get_any("q") == (None, None)


def test_cache_binary_and_corrupt(cache):
    p = cache.put_bytes("bin", ".png", b"\x89PNG")
    assert os.path.exists(p) and cache.get_bytes("bin", ".png") == b"\x89PNG"
    assert cache.get_bytes("bin", ".png", max_age=-1) is None
    with open(cache.path("k"), "w") as f:
        f.write("{not json")
    assert cache.get_any("k") == (None, None)


def test_run_lock(tmp_path):
    p = str(tmp_path / "lock")
    with http.run_lock(p):
        with pytest.raises(http.AlreadyRunning):
            with http.run_lock(p):
                pass
    with http.run_lock(p):          # released after the block
        pass


def test_util_time_and_units():
    dt = util.parse_iso("2026-09-23T09:08:00-06:00")
    assert util.fmt_local(dt, "America/Chicago").endswith("10:08 CDT")
    assert util.fmt_local(None, "America/Chicago") == "?"
    assert util.parse_iso("garbage") is None and util.parse_iso(None) is None
    assert util.parse_iso("2026-09-23T15:00:00Z").tzinfo is not None
    assert util.fmt_age(30) == "just now" and util.fmt_age(3700) == "1 h 01 min"
    assert util.fmt_age(3 * 86400) == "3 d"
    assert util.deg_to_cardinal(140) == "SE" and util.deg_to_cardinal(None) == ""
    assert util.deg_to_cardinal(359) == "N" and util.deg_to_cardinal(140, points=8) == "SE"
    assert util.quantity({"value": 23}, util.c_to_f) == pytest.approx(73.4)
    assert util.quantity({"value": None}) is None and util.quantity(None) is None
    assert util.quantity({"value": float("nan")}) is None
    assert util.round_or_none(2.6) == 3 and util.round_or_none(None) is None
    assert util.pa_to_inhg(101325) == pytest.approx(29.92, abs=0.01)
    assert util.tzinfo_for("Not/AZone") is not None


# ---- http transport: retries, run budget, circuit breaker ---------------------------------
class _Resp:
    def __init__(self, body=b"ok", status=200, headers=None):
        self.body, self.status = body, status
        if headers is not None:
            self.headers = _msg(headers)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class FakeUrlopen:
    """Replaces urllib.request.urlopen: each call consumes the next scripted action (an
    exception instance to raise or bytes to return; the last one repeats) and is recorded
    as (method, url, timeout)."""

    def __init__(self, *actions):
        self.actions = list(actions)
        self.calls = []
        self.requests = []              # the Request objects, for their headers

    def __call__(self, req, timeout=None):
        self.calls.append((req.get_method(), req.full_url, timeout))
        self.requests.append(req)
        act = self.actions.pop(0) if len(self.actions) > 1 else self.actions[0]
        if isinstance(act, BaseException):
            raise act
        return act if isinstance(act, _Resp) else _Resp(act)


@pytest.fixture
def net(monkeypatch):
    """Fresh (inactive) run state, a controllable clock, backoff sleeps that only advance
    that clock, and a helper that installs a scripted urlopen."""
    monkeypatch.setattr(http, "_RUN", http._RunState())
    clock = {"t": 1000.0, "slept": []}
    monkeypatch.setattr(http, "_clock", lambda: clock["t"])

    def fake_sleep(s):                  # backoff "passes" instantly but moves the clock
        clock["slept"].append(s)
        clock["t"] += s
    monkeypatch.setattr(http, "_sleep", fake_sleep)

    def install(*actions):
        fake = FakeUrlopen(*actions)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        return fake
    clock["install"] = install
    return clock


def _msg(headers):
    m = email.message.Message()
    for k, v in (headers or {}).items():
        m[k] = v
    return m


def _http_error(code, headers=None):
    return urllib.error.HTTPError("https://x/", code, "status %d" % code, _msg(headers), None)


NWS = "https://api.weather.gov/alerts/active?area=NM"
IEM = "https://mesonet.agron.iastate.edu/archive/x.png"


def test_get_bytes_retries_http_client_exceptions(cfg, net):
    fake = net["install"](httpclient.IncompleteRead(b"par", 10), b"body")
    assert http.get_bytes(NWS, cfg) == b"body" and len(fake.calls) == 2
    assert net["slept"] == [1.5]
    for exc in (httpclient.BadStatusLine("garbage"), httpclient.LineTooLong("header")):
        fake = net["install"](exc)
        with pytest.raises(http.HttpError) as ei:
            http.get_bytes(NWS, cfg, retries=2)
        assert not isinstance(ei.value, httpclient.HTTPException)
        assert len(fake.calls) == 3
    # outside a run (begin_run never called) nothing is marked down: the next call goes out
    fake = net["install"](b"again")
    assert http.get_bytes(NWS, cfg) == b"again" and len(fake.calls) == 1
    assert http.run_status() == {"budget_s": None, "left_s": None, "exhausted": False,
                                 "down_hosts": []}


def test_run_budget_and_circuit_breaker(cfg, cache, net):
    cfg.run_budget_s = 240
    http.begin_run(cfg)
    fake = net["install"](socket.timeout("timed out"))
    with pytest.raises(http.HttpError):
        http.get_bytes(NWS, cfg, retries=2)
    assert len(fake.calls) == 3 and all(t == cfg.http_timeout for _, _, t in fake.calls)
    assert http.run_status()["down_hosts"] == ["api.weather.gov"]
    # the same host is now refused without touching the network, for GET and HEAD
    fake = net["install"](b"never")
    with pytest.raises(http.HttpError, match="skipped: api.weather.gov unreachable earlier in this run"):
        http.get_bytes("https://API.weather.gov/points/1,1", cfg)
    assert http.head_ok("https://api.weather.gov/x", cfg) is None
    # cached_json serves the last good copy instead
    cache.put("alerts", {"features": []})
    r = http.cached_json(cfg, cache, "alerts", NWS, 0, 600)
    assert r["ok"] and r["stale"] and r["error"].startswith("skipped:") and r["data"] == {"features": []}
    assert fake.calls == []
    # other hosts are unaffected
    assert http.get_bytes(IEM, cfg) == b"never" and len(fake.calls) == 1
    # budget spent -> every request is refused up front
    net["t"] += 241
    with pytest.raises(http.HttpSkipped, match="skipped: run budget exhausted"):
        http.get_bytes(IEM, cfg)
    assert http.head_ok(IEM, cfg) is None and len(fake.calls) == 1
    assert http.run_status()["exhausted"]
    # a new run starts clean
    http.begin_run(cfg)
    assert http.get_bytes(NWS, cfg) == b"never" and len(fake.calls) == 2
    assert http.run_status() == {"budget_s": 240.0, "left_s": 240.0, "exhausted": False,
                                 "down_hosts": []}


def test_budget_caps_attempt_timeout_and_backoff(cfg, net):
    cfg.run_budget_s = 100
    http.begin_run(cfg)
    fake = net["install"](b"x")
    http.get_bytes(NWS, cfg)
    net["t"] += 90                               # 10 s left
    http.get_bytes(NWS, cfg)
    net["t"] += 9                                # 1 s left: floor of 3 s
    http.get_bytes(NWS, cfg)
    http.get_bytes(NWS, cfg, timeout=2)          # a smaller explicit timeout is kept
    assert [t for _, _, t in fake.calls] == [cfg.http_timeout, 10, 3, 2]
    # backoff never sleeps past the deadline; the next attempt is then skipped
    fake = net["install"](_http_error(503))
    with pytest.raises(http.HttpSkipped):
        http.get_bytes(NWS, cfg, retries=2)
    assert len(fake.calls) == 1 and net["slept"][-1] == pytest.approx(1.0)


def test_non_positive_budget_means_unlimited(cfg, net):
    for budget in (0, -5, None):
        cfg.run_budget_s = budget
        http.begin_run(cfg)
        net["t"] += 10 ** 6
        assert http.budget_left() is None
        fake = net["install"](b"x")
        assert http.get_bytes(NWS, cfg) == b"x" and fake.calls[0][2] == cfg.http_timeout


def test_http_status_errors_do_not_trip_the_breaker(cfg, net):
    cfg.run_budget_s = 240
    http.begin_run(cfg)
    fake = net["install"](_http_error(404))
    with pytest.raises(http.HttpError, match="HTTP 404"):
        http.get_bytes(NWS, cfg, retries=2)
    assert len(fake.calls) == 1                  # 4xx: no retry
    fake = net["install"](_http_error(503))
    with pytest.raises(http.HttpError):
        http.get_bytes(NWS, cfg, retries=2)
    assert len(fake.calls) == 3                  # 5xx: retried
    fake = net["install"](socket.timeout("t"), socket.timeout("t"), _http_error(503))
    with pytest.raises(http.HttpError):
        http.get_bytes(NWS, cfg, retries=2)      # the host did answer in the end
    assert http.run_status()["down_hosts"] == []


@pytest.mark.parametrize("exc", [
    urllib.error.URLError(ConnectionRefusedError(111, "refused")),
    ConnectionResetError(104, "reset"), httpclient.IncompleteRead(b"", 5),
])
def test_transport_failures_trip_the_breaker(cfg, net, exc):
    cfg.run_budget_s = 240
    http.begin_run(cfg)
    net["install"](exc)
    with pytest.raises(http.HttpError):
        http.get_bytes(IEM, cfg, retries=1)
    assert http.run_status()["down_hosts"] == ["mesonet.agron.iastate.edu"]


def test_head_ok_is_tristate(cfg, net):
    fake = net["install"](b"")
    assert http.head_ok(IEM, cfg) is True
    assert fake.calls == [("HEAD", IEM, cfg.http_timeout)]
    http.head_ok(IEM, cfg, timeout=8)
    assert fake.calls[-1][2] == 8
    net["install"](_http_error(404))
    assert http.head_ok(IEM, cfg) is False
    # no answer: None; outside a run that does not mark the host down...
    fake = net["install"](urllib.error.URLError(socket.timeout("timed out")))
    assert http.head_ok(IEM, cfg) is None and http.head_ok(IEM, cfg) is None
    assert len(fake.calls) == 2
    # ...inside a run it does, and the next probe never leaves the process
    cfg.run_budget_s = 240
    http.begin_run(cfg)
    assert http.head_ok(IEM, cfg) is None and http.head_ok(IEM, cfg) is None
    assert len(fake.calls) == 3
    with pytest.raises(http.HttpSkipped):
        http.get_bytes(IEM, cfg)


def test_cached_json_keeps_fresh_data_when_cache_write_fails(cfg, cache, fake_http, monkeypatch):
    fake_http.add("api.weather.gov/points/1,1", {"properties": {"gridId": "X"}})

    def disk_full(key, data, ts=None):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(cache, "put", disk_full)
    r = http.cached_json(cfg, cache, "p", "https://api.weather.gov/points/1,1", 60, 600)
    assert r["ok"] and not r["stale"] and r["error"] is None and not r["from_cache"]
    assert r["data"] == {"properties": {"gridId": "X"}} and r["age_s"] == 0.0


def test_atomic_write_fsyncs_before_replace_and_cleans_up(tmp_path, monkeypatch):
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    target = tmp_path / "d" / "f.json"
    http._atomic_write(str(target), b"new")
    assert events == ["fsync", "replace"] and target.read_bytes() == b"new"

    def boom(a, b):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        http._atomic_write(str(target), b"newer")
    assert target.read_bytes() == b"new" and os.listdir(str(target.parent)) == ["f.json"]


# ---- conditional GET and cached_bytes ------------------------------------------------------
NMR = "https://nmroads.com/nmroads.json"
LM = "Wed, 23 Sep 2026 21:34:00 GMT"
LM2 = "Wed, 23 Sep 2026 21:39:00 GMT"


def test_fetch_conditional_validators_and_304(cfg, net):
    fake = net["install"](_Resp(b"body", 200, {"ETag": '"a"', "Last-Modified": LM}))
    assert http.fetch_conditional(NMR, cfg, accept="application/json") == (200, b"body", '"a"', LM)
    req = fake.requests[-1]
    assert req.get_header("If-none-match") is None and req.get_header("If-modified-since") is None
    assert req.get_header("Accept") == "application/json"
    assert req.get_header("User-agent") == cfg.user_agent
    # the copy is current: 304, validators from the reply (or the ones sent when it has none)
    fake = net["install"](_http_error(304, {"ETag": '"a"', "Last-Modified": LM}))
    assert http.fetch_conditional(NMR, cfg, etag='"a"', last_modified=LM) == (304, None, '"a"', LM)
    req = fake.requests[-1]
    assert req.get_header("If-none-match") == '"a"' and req.get_header("If-modified-since") == LM
    assert len(fake.calls) == 1                  # a 304 is an answer, not retried
    net["install"](_http_error(304))
    assert http.fetch_conditional(NMR, cfg, etag='"x"', last_modified=LM2) == \
        (304, None, '"x"', LM2)
    net["install"](b"no headers at all")         # a response object without .headers
    assert http.fetch_conditional(NMR, cfg) == (200, b"no headers at all", None, None)


def test_fetch_conditional_retry_budget_and_breaker(cfg, net):
    fake = net["install"](_http_error(503), _Resp(b"ok", 200, {"ETag": '"b"'}))
    assert http.fetch_conditional(NMR, cfg)[:3] == (200, b"ok", '"b"')      # retried
    assert len(fake.calls) == 2 and net["slept"] == [1.5]
    fake = net["install"](_http_error(404))
    with pytest.raises(http.HttpError, match="HTTP 404"):
        http.fetch_conditional(NMR, cfg)
    assert len(fake.calls) == 1
    cfg.run_budget_s = 240
    http.begin_run(cfg)
    fake = net["install"](socket.timeout("timed out"))
    with pytest.raises(http.HttpError):
        http.fetch_conditional(NMR, cfg)
    assert len(fake.calls) == cfg.http_retries + 1
    assert http.run_status()["down_hosts"] == ["nmroads.com"]
    with pytest.raises(http.HttpSkipped, match="nmroads.com unreachable earlier"):
        http.fetch_conditional(NMR, cfg)
    with pytest.raises(http.HttpSkipped):
        http.get_bytes("https://nmroads.com/rss.xml", cfg)    # shared breaker
    assert len(fake.calls) == cfg.http_retries + 1
    http.begin_run(cfg)
    net["t"] += 241
    with pytest.raises(http.HttpSkipped, match="run budget exhausted"):
        http.fetch_conditional(NMR, cfg)


def _meta(cache, key="nmr"):
    return cache.get_any(key + "__meta")[0]


def test_cached_bytes_lifecycle(cfg, cache, net):
    fake = net["install"](_Resp(b"v1", 200, {"ETag": '"1"', "Last-Modified": LM}))
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600)
    assert r == {"data": b"v1", "ok": True, "stale": False, "age_s": 0.0, "error": None,
                 "from_cache": False, "not_modified": False, "last_modified": LM}
    assert cache.get_bytes("nmr", http.BYTES_EXT) == b"v1"
    assert _meta(cache)["etag"] == '"1"' and _meta(cache)["last_modified"] == LM
    assert fake.requests[-1].get_header("If-none-match") is None
    # younger than ttl: nothing is sent
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 60, 600)
    assert r["ok"] and r["from_cache"] and not r["not_modified"] and r["data"] == b"v1"
    assert r["last_modified"] == LM and len(fake.calls) == 1
    # due: revalidated with the stored validators; a 304 returns the cached body, age 0
    ts_before = _meta(cache)["ts"]
    fake = net["install"](_http_error(304))
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600)
    assert r["ok"] and not r["stale"] and r["not_modified"] and r["from_cache"]
    assert r["data"] == b"v1" and r["age_s"] == 0.0 and r["last_modified"] == LM
    req = fake.requests[-1]
    assert req.get_header("If-none-match") == '"1"' and req.get_header("If-modified-since") == LM
    assert _meta(cache)["ts"] >= ts_before and _meta(cache)["etag"] == '"1"'
    # changed upstream: the new body replaces the old one
    net["install"](_Resp(b"v2", 200, {"ETag": '"2"', "Last-Modified": LM2}))
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600)
    assert r["data"] == b"v2" and not r["from_cache"] and r["last_modified"] == LM2
    assert cache.get_bytes("nmr", http.BYTES_EXT) == b"v2" and _meta(cache)["etag"] == '"2"'
    # fetch fails: the last good copy within max_stale, with the error
    net["install"](urllib.error.URLError(ConnectionRefusedError(111, "refused")))
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600)
    assert r["ok"] and r["stale"] and r["data"] == b"v2" and "refused" in r["error"]
    assert r["from_cache"] and not r["not_modified"] and r["last_modified"] == LM2
    assert r["age_s"] is not None and r["age_s"] >= 0
    # ... but not beyond it
    meta = _meta(cache)
    meta["ts"] -= 1000
    cache.put("nmr__meta", meta)
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600)
    assert not r["ok"] and r["data"] is None and r["error"] and r["age_s"] >= 1000


def test_cached_bytes_validate_and_cache_trouble(cfg, cache, net, monkeypatch):
    net["install"](_Resp(b"<html>oops</html>", 200, {"ETag": '"x"'}))
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600, validate=lambda b: b.startswith(b"{"))
    assert not r["ok"] and "unexpected payload" in r["error"]
    assert cache.get_bytes("nmr", http.BYTES_EXT) is None and _meta(cache) is None
    # a good copy, then a rejected one: the good copy stays and is served as stale
    net["install"](_Resp(b"{}", 200, {"ETag": '"g"'}))
    assert http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600, validate=lambda b: b == b"{}")["ok"]
    net["install"](_Resp(b"junk", 200, {"ETag": '"j"'}))
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600, validate=lambda b: b == b"{}")
    assert r["ok"] and r["stale"] and r["data"] == b"{}" and _meta(cache)["etag"] == '"g"'
    # body file gone (meta still there): no validators are sent, a full GET follows
    os.unlink(cache.path("nmr", http.BYTES_EXT))
    fake = net["install"](_Resp(b"{}", 200, {"ETag": '"g"'}))
    assert http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600)["data"] == b"{}"
    assert fake.requests[-1].get_header("If-none-match") is None
    # a body without its meta record is dated by the file and revalidated without validators
    os.unlink(cache.path("nmr__meta"))
    fake = net["install"](_Resp(b"{}", 200))
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 60, 600)
    assert r["ok"] and r["from_cache"] and fake.calls == []
    # a 304 with nothing cached is an error, not an empty success
    net["install"](_http_error(304))
    assert not http.cached_bytes(cfg, cache, "other", NMR, 0, 600)["ok"]

    # storing fails (disk full): the fresh body is still returned
    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(cache, "put_bytes", disk_full)
    net["install"](_Resp(b"fresh", 200, {"ETag": '"f"'}))
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600)
    assert r["ok"] and r["data"] == b"fresh" and not r["stale"]


def test_cached_bytes_skipped_serves_last_good(cfg, cache, net):
    net["install"](_Resp(b"good", 200, {"ETag": '"1"'}))
    http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600)
    cfg.run_budget_s = 240
    http.begin_run(cfg)
    net["t"] += 241
    fake = net["install"](b"never")
    r = http.cached_bytes(cfg, cache, "nmr", NMR, 0, 600)
    assert r["ok"] and r["stale"] and r["data"] == b"good" and r["error"].startswith("skipped:")
    assert fake.calls == []


@pytest.mark.parametrize("ua", [
    # the string the example deployment first used (split so no address sits in the file)
    "local-weather-awareness (+https://github.com/kirxkirx/local-weather-awareness; you" "@example.org)",
    "local-weather-awareness (+https://example.com)",
    "local-weather-awareness (CONTACT_EMAIL)",
    "local-weather-awareness (your-email)",
])
def test_placeholder_user_agent_is_rejected(ua, tmp_path):
    """OpenStreetMap blocks a User-Agent with a placeholder contact (the example deployment's
    maps showed the 'Access blocked' tile until it was replaced): refuse it at start-up."""
    with pytest.raises(ValueError, match="placeholder"):
        Config.from_env(out_dir=str(tmp_path), user_agent=ua)


def test_real_contact_user_agent_is_accepted(tmp_path):
    for ua in ("local-weather-awareness (+https://github.com/kirxkirx/local-weather-awareness)",
               "local-weather-awareness (+https://github.com/kirxkirx/local-weather-awareness; "
               "https://tau.kirx.net/myweather/)"):
        assert Config.from_env(out_dir=str(tmp_path), user_agent=ua).user_agent == ua


def test_cache_policy_parsing():
    class H(dict):
        def get(self, k, d=None):
            return super().get(k, d)
    assert http.cache_policy(H({"Cache-Control": "no-cache"})) == (True, None)
    assert http.cache_policy(H({"Cache-Control": "max-age=524662, stale-while-revalidate=604800"})) == (False, 524662)
    assert http.cache_policy(H({"Cache-Control": "max-age=0"})) == (True, None)
    assert http.cache_policy(H({})) == (False, None)
    assert http.cache_policy(None) == (False, None)
