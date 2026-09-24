"""The site list is configuration, and nothing else depends on which sites are configured.

Covered here: WEATHER_SITES_FILE and its precedence (--sites > WEATHER_SITES >
WEATHER_SITES_FILE > the built-in example list), the page title derived from the site
names, WEATHER_ALERT_AREAS=auto (the states and territories on the sites' maps, from the
Census bounding boxes in weather/states.py) and the generator that made that table. The
run-level checks (no road fetch without a map in New Mexico, the radar note outside the
MRMS grid, a whole page for other sites) are at the end of test_main.py.
"""
from __future__ import annotations

import ast
import glob
import logging
import os
import struct
import subprocess
import sys

import pytest

import make_weather_page as mwp
from weather import geo, page, roads, states
from weather.config import (DEFAULT_SITES, TITLE_NAMES_MAX, Config, default_title,
                            load_sites_file, parse_alert_areas)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DENVER = "denver|Denver, CO|39.7392|-104.9903"
FLAGSTAFF = "flagstaff|Flagstaff, AZ|35.1983|-111.6513"
FOUR_CORNERS = "four_corners|Four Corners Monument|36.99902|-109.04519"
EXAMPLE_NAMES = ("Lubbock", "Clovis", "Fort Sumner", "Socorro", "Albuquerque")


@pytest.fixture
def env(monkeypatch):
    """monkeypatch with every WEATHER_* variable removed."""
    for k in list(os.environ):
        if k.startswith("WEATHER_"):
            monkeypatch.delenv(k)
    return monkeypatch


def _file(tmp_path, text, name="sites"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ---- the sites file ----------------------------------------------------------------------
def test_sites_example_is_the_built_in_list():
    """sites.example ships the example deployment's sites: exactly DEFAULT_SITES."""
    assert load_sites_file(os.path.join(ROOT, "sites.example")) == DEFAULT_SITES


def test_sites_file_format(tmp_path):
    path = _file(tmp_path, "﻿# my sites\n\n   # an indented comment\n"
                           "denver|Denver, CO|39.7392|-104.9903|America/Denver\n"
                           "  flagstaff | Flagstaff, AZ | 35.1983 | -111.6513  \n\n")
    assert load_sites_file(path) == [
        {"slug": "denver", "name": "Denver, CO", "lat": 39.7392, "lon": -104.9903,
         "tz": "America/Denver"},
        {"slug": "flagstaff", "name": "Flagstaff, AZ", "lat": 35.1983, "lon": -111.6513,
         "tz": None}]


@pytest.mark.parametrize("text, message", [
    ("", "lists no sites"),
    ("# only a comment\n\n", "lists no sites"),
    ("a|A|34\n", "line 1: an entry must be slug|Name|lat|lon"),
    ("\n\na|A|x|2\n", "line 3: lat/lon must be numbers"),
    ("a|A|34|-106  # an inline comment\n", "line 1: lat/lon must be numbers"),
    ("a|A|95|-106\n", "line 1: lat/lon out of range"),
    ("a b|A|34|-106\n", "line 1: the slug must be"),
    ("a|A|34|-106|Mars/Olympus_Mons\n", "unknown time zone"),
    ("a|A|34|-106;b|B|35|-106\n", "line 1: one site per line"),
    ("a|A|34|-106\n# b\na|B|35|-106\n", "line 3: slug 'a' is already used on line 1"),
])
def test_sites_file_errors_name_the_file_and_line(tmp_path, text, message):
    path = _file(tmp_path, text)
    with pytest.raises(ValueError) as ei:
        load_sites_file(path)
    assert message in str(ei.value) and path in str(ei.value)


def test_unreadable_sites_file(tmp_path):
    bad = tmp_path / "latin1"
    bad.write_bytes(b"a|Espa\xf1ola, NM|36|-106\n")
    for path in (str(tmp_path / "missing"), str(tmp_path), str(bad)):
        with pytest.raises(ValueError, match="cannot be read"):
            load_sites_file(path)


def test_site_list_precedence(env, tmp_path):
    """--sites > WEATHER_SITES > WEATHER_SITES_FILE > the built-in list. The file is read
    only when it is the one used, and a broken one is an error, never skipped."""
    c = Config.from_env()
    assert c.sites == DEFAULT_SITES and c.sites_source.startswith("built-in default list")
    path = _file(tmp_path, DENVER + "\n" + FLAGSTAFF + "\n")
    env.setenv("WEATHER_SITES_FILE", path)
    c = Config.from_env()
    assert [s["slug"] for s in c.sites] == ["denver", "flagstaff"]
    assert c.sites_file == path and c.sites_source == "WEATHER_SITES_FILE " + path
    env.setenv("WEATHER_SITES", FOUR_CORNERS)
    c = Config.from_env()
    assert [s["slug"] for s in c.sites] == ["four_corners"]
    assert c.sites_source == "WEATHER_SITES (WEATHER_SITES_FILE not read: this takes precedence)"
    c = Config.from_env(sites=[dict(DEFAULT_SITES[0])])
    assert [s["slug"] for s in c.sites] == ["lubbock"]
    assert c.sites_source.startswith("the command line (--sites)")
    # the file is not even opened while something with precedence is set ...
    env.setenv("WEATHER_SITES_FILE", str(tmp_path / "missing"))
    assert Config.from_env().sites[0]["slug"] == "four_corners"
    # ... and is an error, not a fall-back to the built-in list, when it is the one to use
    env.delenv("WEATHER_SITES")
    with pytest.raises(ValueError, match="WEATHER_SITES_FILE .*missing cannot be read"):
        Config.from_env()
    env.setenv("HOME", str(tmp_path))                    # ~ is expanded
    env.setenv("WEATHER_SITES_FILE", "~/sites")
    assert [s["slug"] for s in Config.from_env().sites] == ["denver", "flagstaff"]


def test_main_exits_2_on_a_bad_sites_file(env, tmp_path, fake_http, caplog):
    caplog.set_level(logging.ERROR, logger="weather")
    path = _file(tmp_path, DENVER + "\ndenver|Denver again|39|-105\n")
    env.setenv("WEATHER_SITES_FILE", path)
    argv = ["--out", str(tmp_path / "out"), "--cache", str(tmp_path / "cache")]
    assert mwp.main(argv) == 2
    assert not fake_http.calls and not os.path.exists(str(tmp_path / "out"))
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("configuration error: WEATHER_SITES_FILE %s line 2: slug 'denver'"
                            % path) for m in msgs), msgs


# ---- the title ---------------------------------------------------------------------------
def test_title_is_derived_from_the_site_names(env):
    c = Config.from_env()
    assert c.title == ""
    assert c.page_title == "Local weather: Lubbock · Clovis · Fort Sumner · Socorro · Albuquerque"
    env.setenv("WEATHER_SITES", DENVER + ";" + FLAGSTAFF)
    assert Config.from_env().page_title == "Local weather: Denver · Flagstaff"
    env.setenv("WEATHER_TITLE", "  Our weather  ")
    assert Config.from_env().page_title == "Our weather"


def test_derived_title_edge_cases():
    def sites(*names):
        return [{"slug": "s%d" % i, "name": n} for i, n in enumerate(names)]
    # same short name twice: both keep their full names, so they can be told apart
    assert default_title(sites("Springfield, IL", "Springfield, MO", "Salem, OR")) == (
        "Local weather: Springfield, IL · Springfield, MO · Salem")
    assert default_title(sites("Mauna Kea Observatories")) == "Local weather: Mauna Kea Observatories"
    # many sites: as many names as fit in TITLE_NAMES_MAX characters, then "+N more"
    many = ["Town %02d, ST" % i for i in range(1, 21)]
    t = default_title(sites(*many))
    names = t[len("Local weather: "):]
    assert names == ("Town 01 · Town 02 · Town 03 · Town 04 · Town 05 · Town 06 · Town 07"
                     " · +13 more")
    assert len(names) <= TITLE_NAMES_MAX < len(names) + len(" · Town 08")
    # one name longer than the limit is shortened, alone or with others
    long = "X" * 200
    assert len(default_title(sites(long))) == len("Local weather: ") + TITLE_NAMES_MAX
    assert default_title(sites(long))[-1] == "…"
    t = default_title(sites(long, "B"))
    assert t.endswith("… · +1 more") and len(t) - len("Local weather: ") <= TITLE_NAMES_MAX
    assert default_title([]) == "Local weather"


def test_page_title_and_masthead_follow_the_sites(env):
    env.setenv("WEATHER_SITES", DENVER + ";" + FLAGSTAFF)
    c = Config.from_env()
    html = page.render_html(c, {"sites": [{"site": s} for s in c.sites]})
    assert "<title>Local weather: Denver · Flagstaff</title>" in html
    assert "<h1>Local weather: Denver · Flagstaff</h1>" in html
    assert not [n for n in EXAMPLE_NAMES if n in html]


# ---- alert areas -------------------------------------------------------------------------
def _areas(env, spec, **kw):
    env.setenv("WEATHER_SITES", spec)
    return Config.from_env(**kw).alert_area_codes


def test_parse_alert_areas():
    for auto in ("auto", "AUTO", " Auto "):
        assert parse_alert_areas(auto) == (True, [])
    assert parse_alert_areas("nm,tx") == (False, ["NM", "TX"])
    assert parse_alert_areas(" NM, tx ,nm,") == (False, ["NM", "TX"])
    # auto and extra codes combine, in any order
    assert parse_alert_areas("auto,lm") == (True, ["LM"])
    assert parse_alert_areas("PK, Auto, pk") == (True, ["PK"])
    for bad in ("", " , ", "N", "NMX", "N1", "NM;TX", "New Mexico", "NM TX", "auto;LM", "autoLM"):
        with pytest.raises(ValueError):
            parse_alert_areas(bad)


def test_auto_alert_areas_for_the_example_sites(env):
    """The example deployment's five maps give NM and TX, plus OK: Oklahoma's box runs from
    its panhandle (west edge -103.00) down to the Red River (south edge 33.61), so it covers
    the north-east of the Lubbock map and the east of the Clovis map, where there is no
    Oklahoma land. Harmless: alerts are kept by their own geometry, never by state."""
    c = Config.from_env()
    assert c.alert_areas == "auto" and c.alert_area_codes == ["NM", "OK", "TX"]
    frames = dict(zip([s["slug"] for s in c.sites], c.frame_bboxes))
    ok_box = states.STATE_BOXES["OK"][0]
    assert {s for s, f in frames.items() if geo.bbox_intersects(f, ok_box)} == {"lubbock", "clovis"}
    assert all(f[3] < 36.5 for f in (frames["lubbock"], frames["clovis"]))   # south of the panhandle
    assert all(f[2] < -100.0 for f in (frames["lubbock"], frames["clovis"]))  # west of the main body


def test_auto_alert_areas_elsewhere(env):
    # Denver + Flagstaff: CO and AZ, plus NE: the Denver map's east edge (-104.049) is just
    # inside Nebraska's box, whose west edge (-104.05) is set by the panhandle further north
    assert _areas(env, DENVER + ";" + FLAGSTAFF) == ["AZ", "CO", "NE"]
    assert _areas(env, FOUR_CORNERS) == ["AZ", "CO", "NM", "UT"]
    # maps on the water also query the NWS marine areas, whose alerts (Small Craft
    # Advisory, Gale Warning, Special Marine Warning, ...) are in no state's feed
    assert _areas(env, "dc|Washington, DC|38.8951|-77.0364") == ["AN", "DC", "MD", "VA", "WV"]
    assert _areas(env, "chi|Chicago, IL|41.8781|-87.6298") == ["IL", "IN", "LM", "MI", "WI"]
    assert _areas(env, "hnl|Honolulu, HI|21.3069|-157.8583") == ["HI", "PH"]
    assert _areas(env, "sju|San Juan, PR|18.4655|-66.1057") == ["AM", "PR"]
    assert _areas(env, "gum|Hagatna, GU|13.4443|144.7937") == ["GU", "MP", "PM"]
    assert _areas(env, "ppg|Pago Pago, AS|-14.2756|-170.7020") == ["AS", "PS"]
    assert _areas(env, "sea|Seattle, WA|47.6062|-122.3321") == ["PZ", "WA"]
    # ... but inland maps do not, even where one box per marine area would reach
    assert _areas(env, "tus|Tucson, AZ|32.2226|-110.9747") == ["AZ"]
    assert _areas(env, "atl|Atlanta, GA|33.749|-84.388") == ["AL", "GA", "NC"]
    # the Aleutians: west of 180 degrees and east of it (a map across it is refused, see
    # test_site_checks; the tables still handle such a frame)
    assert _areas(env, "adak|Adak, AK|51.88|-176.66") == ["AK", "PK"]
    assert _areas(env, "attu|Attu, AK|52.93|173.2") == ["AK", "PK"]
    wrapped = geo.MapFrame(52.0, 179.9, 160.9344, 600, 9).bbox
    assert wrapped[0] > wrapped[2] and states.areas_touching([wrapped]) == ["AK", "PK"]
    # explicit codes are used as given (upper-cased, without repeats) ...
    env.setenv("WEATHER_ALERT_AREAS", "nm, tx, NM")
    assert _areas(env, DENVER) == ["NM", "TX"]
    # ... and added to the automatic list when listed with auto
    env.setenv("WEATHER_ALERT_AREAS", "auto,LM,co")
    assert _areas(env, DENVER) == ["CO", "NE", "LM"]


def test_auto_alert_areas_outside_the_us_is_a_config_error(env, tmp_path, fake_http):
    with pytest.raises(ValueError, match="entry 1: site 'paris' .*reaches no US state"):
        _areas(env, "paris|Paris, France|48.8566|2.3522")
    assert mwp.main(["--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c"),
                     "--sites", "paris|Paris|48.8566|2.3522"]) == 2
    env.setenv("WEATHER_ALERT_AREAS", "X")                   # malformed: a startup error
    with pytest.raises(ValueError, match="two-letter"):
        _areas(env, DENVER)
    # an explicit list does not make a site outside the US acceptable: NWS has nothing there
    env.setenv("WEATHER_ALERT_AREAS", "NM")
    with pytest.raises(ValueError, match="reaches no US state"):
        _areas(env, "paris|Paris, France|48.8566|2.3522")
    # the auto check itself is still there for a config built without from_env
    c = Config(sites=[{"slug": "p", "name": "P", "lat": 48.8566, "lon": 2.3522, "tz": None}])
    with pytest.raises(ValueError, match="WEATHER_ALERT_AREAS=auto: no US state or territory"):
        c.alert_area_codes
    assert not fake_http.calls


def test_frame_bboxes_are_the_renderer_frames(cfg):
    for site, box in zip(cfg.sites, cfg.frame_bboxes):
        assert box == geo.MapFrame(site["lat"], site["lon"], cfg.map_km, cfg.map_px,
                                   cfg.tile_zoom).bbox


# ---- the state table and its generator ---------------------------------------------------------
STATES_50 = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
                "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split())


def test_state_boxes_table():
    assert len(STATES_50) == 50
    assert set(states.STATE_BOXES) == STATES_50 | {"DC", "PR", "AS", "GU", "MP", "VI"}
    for code, boxes in states.STATE_BOXES.items():
        assert len(boxes) == (2 if code == "AK" else 1), code
        for lonmin, latmin, lonmax, latmax in boxes:
            assert -180 <= lonmin < lonmax <= 180 and -90 <= latmin < latmax <= 90, code
    # spot checks against well-known borders (37 N / 41 N / 109.05 W / 103 W / 102.05 W)
    assert states.STATE_BOXES["CO"] == ((-109.07, 36.99, -102.04, 41.01),)
    assert states.STATE_BOXES["WY"] == ((-111.06, 40.99, -104.05, 45.01),)
    nm = states.STATE_BOXES["NM"][0]
    assert all(abs(a - b) <= 0.1 for a, b in zip(nm, roads.NM_BOX))


def _shapefile(tmp_path):
    """A two-record polygon shapefile: 'NM' (a square) and 'AK' (one ring each side of the
    antimeridian), written with the same struct layout the Census files use."""
    recs = [("NM", "New Mexico", [[(-109.0503, 31.3322), (-103.0019, 31.3322),
                                   (-103.0019, 37.0002), (-109.0503, 31.3322)]]),
            ("AK", "Alaska", [[(-179.2311, 51.1751), (-129.9795, 71.4398), (-179.2311, 51.1751)],
                              [(172.3446, 52.9), (179.8597, 51.3), (172.3446, 53.0)]])]
    shp = b""
    for n, (_c, _name, parts) in enumerate(recs, 1):
        pts = [p for part in parts for p in part]
        starts, k = [], 0
        for part in parts:
            starts.append(k)
            k += len(part)
        body = struct.pack("<i4dii", 5, 0, 0, 0, 0, len(parts), len(pts))
        body += struct.pack("<%di" % len(starts), *starts)
        body += b"".join(struct.pack("<2d", x, y) for x, y in pts)
        shp += struct.pack(">ii", n, len(body) // 2) + body
    (tmp_path / "t.shp").write_bytes(b"\0" * 100 + shp)
    fields = [(b"STUSPS", 2), (b"NAME", 20)]
    rlen = 1 + sum(f[1] for f in fields)
    hlen = 32 + 32 * len(fields) + 1
    dbf = struct.pack("<4xIHH20x", len(recs), hlen, rlen)
    for name, flen in fields:
        dbf += name.ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([flen]) + b"\0" * 15
    dbf += b"\r"
    for code, name, _p in recs:
        dbf += b" " + code.encode().ljust(2) + name.encode().ljust(20)
    (tmp_path / "t.dbf").write_bytes(dbf)
    return str(tmp_path / "t")


def test_state_bboxes_tool(tmp_path):
    """tools/state_bboxes.py, which made STATE_BOXES from the Census shapefile, on a tiny
    synthetic shapefile: boxes rounded outward, Alaska split at the antimeridian, and lines
    in the table's own format."""
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "state_bboxes.py"),
                        _shapefile(tmp_path)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        '    "AK": ((-179.24, 51.17, -129.97, 71.44), (172.34, 51.30, 179.86, 53.00)),  # Alaska',
        '    "NM": ((-109.06, 31.33, -103.00, 37.01),),  # New Mexico']
    table = ast.literal_eval("{%s}" % "".join(ln.split("#")[0] for ln in r.stdout.splitlines()))
    assert table["AK"][0] == states.STATE_BOXES["AK"][0] and table["NM"] == states.STATE_BOXES["NM"]


# ---- nothing else names the example sites -----------------------------------------------------
def test_no_code_names_an_example_site():
    """The five example sites live in config.DEFAULT_SITES (and sites.example, the tests and
    the docs) only: no string in the code may name one, so changing the site list can never
    leave a Lubbock or a Socorro behind on the page. Docstrings and comments may use them as
    examples; string constants may not."""
    hits = []
    for path in sorted(glob.glob(os.path.join(ROOT, "weather", "*.py"))) + [
            os.path.join(ROOT, "make_weather_page.py")]:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        skip = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))                      # a docstring
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "DEFAULT_SITES" for t in node.targets):
                skip.update(id(n) for n in ast.walk(node))       # the example table itself
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in skip:
                low = node.value.lower().replace("_", " ")
                hits += ["%s:%d %r" % (os.path.basename(path), node.lineno, node.value[:60])
                         for n in EXAMPLE_NAMES if n.lower() in low]
    assert not hits, hits


# ---- site checks: typos, the 180th meridian, repeated slugs, empty lists -------------------------
def test_site_checks_name_the_entry(env, tmp_path):
    """A site whose map reaches no US state or territory (typically a longitude without its
    minus sign) is a startup error naming the site and where it was configured, even when
    another site keeps WEATHER_ALERT_AREAS=auto non-empty (it used to be accepted and got a
    map of China and a 404 from NWS every run)."""
    env.setenv("WEATHER_SITES", DENVER + ";oops|Oops, NM|32.7026|103.1360")
    with pytest.raises(ValueError, match=r"^WEATHER_SITES entry 2: site 'oops' \(32\.7026, "
                                         r"103\.136\): its map reaches no US state or territory.*"
                                         r"US longitudes are negative"):
        Config.from_env()
    env.delenv("WEATHER_SITES")
    path = _file(tmp_path, "# mine\n" + DENVER + "\n\na|A|34|106\n")
    env.setenv("WEATHER_SITES_FILE", path)
    with pytest.raises(ValueError, match="^WEATHER_SITES_FILE %s line 4: site 'a'" % path):
        Config.from_env()
    with pytest.raises(ValueError, match=r"^--sites entry 1: site 'x'"):
        Config.from_env(sites=[{"slug": "x", "name": "X", "lat": 34.0, "lon": 106.0, "tz": None}])


def test_site_checks_refuse_a_map_across_the_180th_meridian(env):
    """The renderer cannot draw a map across the antimeridian (it came out 600 x 1 pixels
    and was reported as written): such a site is refused at startup, with the remedy."""
    env.setenv("WEATHER_SITES", "amchitka|Amchitka, AK|51.5|179.0")
    with pytest.raises(ValueError, match="entry 1: site 'amchitka' .*100-mile map would cross "
                                         "the 180th meridian.*WEATHER_MAP_MILES"):
        Config.from_env()
    env.setenv("WEATHER_SITES", "adak|Adak, AK|51.88|-176.658")
    assert Config.from_env().sites[0]["slug"] == "adak"             # 100 mi: fine
    env.setenv("WEATHER_MAP_MILES", "1000")
    with pytest.raises(ValueError, match="1000-mile map would cross the 180th meridian"):
        Config.from_env()


def test_slugs_are_unique_without_regard_to_case(env, tmp_path):
    """radar_a_dark.png and radar_A_dark.png are one file on macOS: slugs that differ only
    in case are refused, and every duplicate message names the slug and its source."""
    env.setenv("WEATHER_SITES", "a|A|34|-106;b|B|35|-106;a|C|33|-106")
    with pytest.raises(ValueError, match=r"^WEATHER_SITES: slug 'a' is used twice "
                                         r"\(entries 1 and 3\)$"):
        Config.from_env()
    env.setenv("WEATHER_SITES", "a|A|34|-106;A|B|35|-106")
    with pytest.raises(ValueError, match="slug 'A' repeats 'a' .entries 1 and 2"):
        Config.from_env()
    env.delenv("WEATHER_SITES")
    path = _file(tmp_path, "a|A|34|-106\nA|B|35|-106\n")
    with pytest.raises(ValueError, match="line 2: slug 'A' repeats 'a' of line 1"):
        load_sites_file(path)
    c = Config(sites=[{"slug": "Den", "name": "D", "lat": 39.7, "lon": -105.0, "tz": None},
                      {"slug": "den", "name": "E", "lat": 39.8, "lon": -105.0, "tz": None}])
    with pytest.raises(ValueError, match="site 2: slug 'den' repeats 'Den' of site 1"):
        c.validate()


def test_a_site_list_that_is_set_but_empty_is_an_error(env, tmp_path, fake_http, caplog):
    """WEATHER_SITES=';' (or --sites ';') lists no site: an error, never a fall-back to the
    sites file or to the built-in list. A blank value is the same as unset."""
    env.setenv("WEATHER_SITES_FILE", _file(tmp_path, DENVER + "\n"))
    env.setenv("WEATHER_SITES", " ; ;")
    with pytest.raises(ValueError, match="^WEATHER_SITES lists no sites"):
        Config.from_env()
    env.setenv("WEATHER_SITES", "  ")
    assert [s["slug"] for s in Config.from_env().sites] == ["denver"]
    caplog.set_level(logging.ERROR, logger="weather")
    argv = ["--out", str(tmp_path / "o"), "--cache", str(tmp_path / "c"), "--sites", ";"]
    assert mwp.main(argv) == 2
    assert any("--sites lists no sites" in r.getMessage() for r in caplog.records)
    assert mwp.main(argv[:-1] + ["a|A|34"]) == 2
    assert any(r.getMessage().startswith("configuration error: --sites: an entry must be")
               for r in caplog.records)
    assert not fake_http.calls


def test_sites_override_a_malformed_environment_list(env):
    """--sites wins over WEATHER_SITES, so it also works while WEATHER_SITES is broken: a
    source is parsed only when it is the one used."""
    env.setenv("WEATHER_SITES", "broken")
    with pytest.raises(ValueError, match="WEATHER_SITES: an entry must be"):
        Config.from_env()
    c = Config.from_env(sites=[{"slug": "x", "name": "X", "lat": 35.0, "lon": -106.0,
                                "tz": None}])
    assert [s["slug"] for s in c.sites] == ["x"] and c.sites_source.startswith("the command line")


# ---- an explicit WEATHER_ALERT_AREAS that leaves out a site's state -------------------------------
def test_explicit_alert_areas_that_miss_a_site_are_reported(env, caplog):
    """The site list changed, the explicit area list did not: the startup log warns per site
    (by the state boxes), and each run reports the site whose NWS zone lies outside the
    query (an error in the run, so status.json is degraded) instead of a silent "No NWS
    alerts". auto never misses a site's own state."""
    caplog.set_level(logging.INFO, logger="weather")
    env.setenv("WEATHER_SITES", DENVER + ";" + FLAGSTAFF)
    env.setenv("WEATHER_ALERT_AREAS", "NM,AZ")
    c = Config.from_env()
    assert [(s["slug"], here) for s, here in c.sites_outside_alert_areas] == [("denver", ["CO"])]
    mwp._log_alert_areas(c)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["alert areas: WEATHER_ALERT_AREAS=NM,AZ leaves out CO, where site "
                        "denver lies: its alerts are not fetched (use auto, or add the code)"]
    meta = {"ok": True, "forecast_zone": "COZ039", "county_zone": "COC031"}
    gap = mwp._alert_area_gap(c, c.sites[0], meta)
    assert gap == ("denver alerts not fetched: its NWS zone COZ039 is in CO, which the alert "
                   "query (NM,AZ) leaves out; set WEATHER_ALERT_AREAS=auto or add CO")
    assert mwp._alert_area_gap(c, c.sites[1], {"ok": True, "forecast_zone": "AZZ015"}) is None
    assert mwp._alert_area_gap(c, c.sites[0], {"ok": False, "forecast_zone": None}) is None
    for spec in ("auto", "NM,AZ,CO", "auto,LM"):
        env.setenv("WEATHER_ALERT_AREAS", spec)
        c = Config.from_env()
        assert c.sites_outside_alert_areas == []
        assert mwp._alert_area_gap(c, c.sites[0], meta) is None
    caplog.clear()
    mwp._log_alert_areas(c)
    assert [r.getMessage() for r in caplog.records] == [
        "alert areas: AZ,CO,NE,LM (auto: the areas on the sites' maps, plus LM from "
        "WEATHER_ALERT_AREAS)"]


# ---- the marine areas and their generator ---------------------------------------------------------
MARINE_CODES = set("AM AN GM LC LE LH LM LO LS PH PK PM PS PZ SL".split())


def test_marine_boxes_table():
    """The 15 NWS marine area codes (exactly those api.weather.gov accepts), valid boxes on
    one side of the prime meridian each, no code shared with the states, and spot checks:
    Chicago's lakefront is in LM, Honolulu's harbour in PH, Miami Beach in AM, Key West in
    GM, and no marine box reaches the maps of the example sites."""
    assert set(states.MARINE_BOXES) == MARINE_CODES
    assert not set(states.MARINE_BOXES) & set(states.STATE_BOXES)
    for code, boxes in states.MARINE_BOXES.items():
        assert boxes, code
        for lonmin, latmin, lonmax, latmax in boxes:
            assert -180 <= lonmin <= lonmax <= 180 and -90 <= latmin <= latmax <= 90, code
            assert (lonmin >= 0) == (lonmax >= 0) or lonmax == 0, code
    here = {code: states.marine_areas_touching([(lon, lat, lon, lat)])
            for code, lon, lat in (("LM", -87.60, 41.90), ("PH", -157.87, 21.30),
                                   ("AM", -80.13, 25.77), ("GM", -81.80, 24.50))}
    assert here == {"LM": ["LM"], "PH": ["PH"], "AM": ["AM"], "GM": ["GM"]}
    c = Config()
    assert states.marine_areas_touching(c.frame_bboxes) == []
    assert states.states_at(-106.6504, 35.0844) == ["NM"]
    assert states.states_at(-101.8313, 35.222) == ["OK", "TX"]      # a box is not the state
    assert states.states_at(2.35, 48.86) == []


def _marine_shapefile(tmp_path):
    """Marine zones as NWS writes them (an ID field): two neighbouring Lake Michigan zones
    (merged: the box around both adds no area), a third far north (kept apart), a Bering
    Sea zone across the antimeridian (one box each side, never merged) and one St.
    Lawrence zone."""
    def sq(x0, y0, x1, y1):
        return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]
    recs = [("LMZ740", "Chicago", sq(-88.0, 41.6, -87.5, 42.0)),
            ("LMZ741", "Gary", sq(-87.5, 41.6, -87.0, 42.0)),
            ("LMZ345", "Sturgeon Bay", sq(-86.0, 45.0, -85.5, 45.5)),
            ("PKZ411", "Bering Sea", [[(179.5, 51.0), (-179.5, 51.0), (-179.5, 51.5),
                                       (179.5, 51.5), (179.5, 51.0)]]),
            ("SLZ022", "St. Lawrence River", sq(-75.5, 44.5, -75.0, 45.0))]
    shp = b""
    for n, (_zid, _name, parts) in enumerate(recs, 1):
        pts = [p for part in parts for p in part]
        body = struct.pack("<i4dii", 5, 0, 0, 0, 0, len(parts), len(pts))
        body += struct.pack("<%di" % len(parts), *[0])
        body += b"".join(struct.pack("<2d", x, y) for x, y in pts)
        shp += struct.pack(">ii", n, len(body) // 2) + body
    (tmp_path / "mz.shp").write_bytes(b"\0" * 100 + shp)
    fields = [(b"ID", 6), (b"WFO", 3), (b"NAME", 20)]
    rlen = 1 + sum(f[1] for f in fields)
    hlen = 32 + 32 * len(fields) + 1
    dbf = struct.pack("<4xIHH20x", len(recs), hlen, rlen)
    for name, flen in fields:
        dbf += name.ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([flen]) + b"\0" * 15
    dbf += b"\r"
    for zid, name, _p in recs:
        dbf += b" " + zid.encode().ljust(6) + b"XXX" + name.encode().ljust(20)
    (tmp_path / "mz.dbf").write_bytes(dbf)
    return str(tmp_path / "mz")


def test_state_bboxes_tool_marine(tmp_path):
    """tools/state_bboxes.py --marine, which made MARINE_BOXES from the NWS marine zone
    files: zones grouped by the area code their ID starts with, neighbours merged, distant
    zones and the two sides of the antimeridian kept apart, lines in the table's format."""
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "state_bboxes.py"),
                        "--marine", _marine_shapefile(tmp_path)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        '    "LM": (  # Lake Michigan',
        '        (-88.00, 41.60, -87.00, 42.00), (-86.00, 45.00, -85.50, 45.50)),',
        '    "PK": (  # North Pacific, Alaskan waters',
        '        (-179.50, 51.00, -179.50, 51.50), (179.50, 51.00, 179.50, 51.50)),',
        '    "SL": ((-75.50, 44.50, -75.00, 45.00),),  # St. Lawrence River']
    table = ast.literal_eval("{%s}" % "".join(ln.split("#")[0] for ln in r.stdout.splitlines()))
    assert set(table) == {"LM", "PK", "SL"} and len(table["LM"]) == 2
    bad = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "state_bboxes.py"),
                          "--marine"], capture_output=True, text=True, timeout=30)
    assert bad.returncode != 0 and "usage" in bad.stderr
