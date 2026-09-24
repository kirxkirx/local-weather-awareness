# local-weather-awareness — module contract

This file is the binding interface between the modules. Anyone implementing or changing a
module keeps these signatures and dict shapes; if a shape must change, change it here first.
Python ≥ 3.9 (no `match`, no `X | Y` at runtime; `from __future__ import annotations` is
fine). Dependencies: standard library + Pillow. Line length 100, flake8 clean with
`--extend-ignore=E203,E501,W503,E402`.

Every module logs through `logging.getLogger("weather.<module>")`, never prints.
Every network access goes through `weather.http` **as module attributes**
(`http.get_bytes(...)`, `http.get_json(...)`, `http.head_ok(...)`, `http.cached_json(...)`,
`http.fetch_conditional(...)`, `http.cached_bytes(...)`), never `from .http import
get_bytes` — the test fixture `fake_http` monkeypatches those attributes, and
`conftest.py` blocks `urllib` outright in tests.

## Already implemented (do not rewrite; read them)

- `weather/config.py` — `Config` dataclass, `Config.from_env(**overrides)`; fields are the
  single source of truth for paths, intervals, URLs. `cfg.themes` → `(("dark", url),
  ("light", url))`; `cfg.map_basename(slug, theme)` → `radar_<slug>_<theme>.png`;
  `cfg.map_km`; `cfg.sites` = list of `{"slug","name","lat","lon","tz"}` where `tz` is a
  static IANA zone or None, used for local times whenever the NWS `/points` metadata is not
  available; `parse_sites(spec, what="WEATHER_SITES")` reads `slug|Name|lat|lon[|tz];…`
  (an unknown zone, or a slug repeated in any case, is a ValueError naming the entries;
  `what` prefixes the messages).
  **Sites are configuration; no module may depend on which sites are configured.**
  Precedence (`from_env` → `_choose_sites`, which parses only the source it uses, so
  `--sites` works while `WEATHER_SITES` is malformed; a `WEATHER_SITES` that is set but
  lists no site, only `;`, is a ValueError): a `sites` override (the CLI's `--sites`) >
  `WEATHER_SITES` > `WEATHER_SITES_FILE` (`cfg.sites_file`, read by
  `load_sites_file(path)` only when it is the one used: one entry per line, blank and `#`
  lines skipped, UTF-8 with an optional BOM, `~` expanded; ValueError naming the file and
  line for an unreadable file, a malformed line, a line with `;`, a slug repeated in any
  case or no sites: never a silent fall-back) > `DEFAULT_SITES`, **the example deployment's
  configuration** (Lubbock `America/Chicago`, four NM sites `America/Denver`; the same list
  as `sites.example`). `cfg.sites_source` names the source for the run log. Title:
  `cfg.title` default "" and `cfg.page_title` = `WEATHER_TITLE` when set, else
  `default_title(sites)`: "Local weather: " + the names up to their first comma joined
  with " · " (a short name shared by two sites keeps the full names), beyond
  `TITLE_NAMES_MAX` (80) characters as many as fit + " · +N more". Alert areas:
  `cfg.alert_areas` default `"auto"`; `parse_alert_areas(spec)` → `(auto, codes)`: whether
  `auto` is listed (any case) and the two-letter codes, upper-case without repeats, so
  `auto,LM` combines both (ValueError when empty or malformed); `cfg.frame_bboxes` = every
  site's `geo.MapFrame(lat, lon, map_km, map_px, tile_zoom).bbox`; `cfg.alert_area_codes` =
  without auto the listed codes, with auto `states.areas_touching(cfg.frame_bboxes)`
  (states, territories and marine areas) followed by the listed codes not already in it,
  ValueError when auto finds nothing (no map reaches the US). `cfg.sites_outside_alert_areas`
  → `[(site, codes)]`: for a list without auto, the sites whose point lies in no listed
  state's box (`states.states_at`; make_weather_page warns at startup). `validate()`
  checks every site first (`_check_sites`: a slug unique without regard to case, a map
  frame that can be built, does not cross the 180th meridian (`west > east`; the renderer
  cannot draw it) and touches a US state or territory (`states.states_touching`); each
  ValueError names the site's source and position, `WEATHER_SITES entry 2`,
  `WEATHER_SITES_FILE <path> line 4`, `--sites entry 1`, `built-in site 3`), then
  evaluates `alert_area_codes`, so all of these are startup errors (exit 2).
  `cfg.page_url` (`WEATHER_PAGE_URL`) default "": the footer then shows no address.
  `cfg.tile_dark_invert` (default True): the dark theme inverts the lightness
  of each **tile** that comes out light (decided per tile, not per composite), because no
  key-free tile service serves a dark style with readable labels (both theme URLs default
  to OpenStreetMap's standard tiles; CARTO's free rasters answer "API KEY REQUIRED").
  `cfg.run_budget_s` (default 240, `WEATHER_RUN_BUDGET`, must be ≥ 10): network time per
  run, see `http.begin_run`. `cfg.alert_colors` (default "", `WEATHER_ALERT_COLORS`,
  `"Event Name=#rrggbb;Other Event=#rrggbb"`) with the property `cfg.alert_color_overrides`
  → `{lowercased event (whitespace collapsed): "#RRGGBB"}`, parsed by
  `config.parse_alert_colors(spec)`; `validate()` raises ValueError on a bad entry.
  Defaults `alert_fill_alpha` 55, `radar_alpha` 170.
  Road closures (roads contract R6; see `weather/roads.py` below), each validated in
  `validate()` even while roads are off (a bad value is a startup ValueError, exit 2):
  `roads_enabled` True (`WEATHER_ROADS`), `roads_water` True (`WEATHER_ROADS_WATER`),
  `lsr_enabled` True (`WEATHER_LSR`), `lsr_hours` 24 (`WEATHER_LSR_HOURS`, int 1..168),
  `roads_max_stale` 3600 (`WEATHER_ROADS_MAX_STALE`, int 0..604800 s, so a week-old closure
  is never shown as current), `roads_old_days` 3.0 (`WEATHER_ROADS_OLD_DAYS`, > 0 and ≤ 365,
  NaN/inf rejected), `nmroads_json_url` `https://nmroads.com/nmroads.json`,
  `nmroads_rss_url` `https://nmroads.com/rss.xml`, `lsr_url`
  `https://mesonet.agron.iastate.edu/geojson/lsr.geojson?hours={hours}&states=NM`
  (`WEATHER_NMROADS_JSON_URL`, `WEATHER_NMROADS_RSS_URL`, `WEATHER_LSR_URL`: absolute
  http(s) URLs without whitespace; `lsr_url` may contain only the `{hours}` placeholder, a
  stray `{field}` or unbalanced brace is a ValueError). `config.lsr_feed_url(template,
  hours)` and the property `cfg.lsr_feed_url` fill in `{hours}`. **Deviation from the
  roads contract:** the contract's `...&wfos=ABQ,EPZ,MAF,AMA,LUB` is not used, because IEM
  silently ignores `wfos=` (checked live 2026-09-23: it returned all ~90 US reports,
  50-400 KB); `states=NM` returns only New Mexico (7-10 KB), and roads.py still keeps
  `state == "NM"` itself.
- `weather/states.py` — `STATE_BOXES = {"NM": ((lonmin, latmin, lonmax, latmax),), …}`:
  the 56 entities of the Census Bureau's 2024 TIGER/Line state file (50 states, DC, PR,
  AS, GU, MP, VI) keyed by USPS code = NWS area code, boxes rounded outward to 0.01°,
  Alaska as two boxes split at the antimeridian (source and SHA-256 in the module
  docstring; `tools/state_bboxes.py` regenerates the table). `states_touching(frames) ->
  sorted codes` of those whose box touches any frame box; a frame that wraps across the
  antimeridian (west > east, as `MapFrame` writes it there) is split in two. Boxes
  over-include near borders; that is harmless because alerts are kept per site by
  geometry. `MARINE_BOXES` = the same for the 15 NWS marine area codes (AM, AN, GM, LC,
  LE, LH, LM, LO, LS, PH, PK, PM, PS, PZ, SL; a state's alert feed has no marine zones):
  made by `tools/state_bboxes.py --marine` from the NWS coastal and offshore marine zone
  files (mz16ap26, oz16ap26), one box per zone, merged per area and hemisphere, closest
  pair first, while a merge adds at most 1 square degree (192 boxes). `marine_areas_touching
  (frames)`, `areas_touching(frames)` = sorted states + marine areas (what `auto` queries),
  `states_at(lon, lat)` = the states whose box holds the point.
- `weather/geo.py` — `MapFrame(lat0, lon0, size_km, width_px, zoom)` with
  `.size (w,h)`, `.bbox (lonmin,latmin,lonmax,latmax)`, `.lonlat_to_px(lon, lat)`,
  `.px_to_lonlat(x, y)`, `.ring_to_px(ring)`, `.px_for_km(km)`, `.tile_range()`,
  `.tile_paste_origin(tx, ty)`, `.canvas_size()`, `.canvas_crop()`, `.cache_key()`;
  MRMS grid helpers `mrms_cell(lat, lon)`, `mrms_in_grid(col,row)`, `mrms_dbz(idx)`,
  `mrms_covers(lat, lon)`, constants `MRMS_W/H/PX/LEFT/TOP/NODATA` and `MRMS_BBOX` (the
  grid's extent, -130..-60 / 20..55); GeoJSON helpers
  `iter_polygons(geom)` (yields list-of-rings), `point_in_geometry(lon, lat, geom)`,
  `geometry_bbox(geom)`, `bbox_intersects(a, b)`, `merge_geometries(iterable)`;
  `dest_point`, `haversine_km`, `deg2num`, `num2deg`.
- `weather/http.py` — `HttpError`, `HttpSkipped(HttpError)`, `get_bytes(url, cfg,
  accept=None, timeout=None, retries=None, method="GET")`, `head_ok(url, cfg, timeout=None)
  -> True|False|None`, `get_json(url, cfg, accept=GEOJSON)`, `Cache(dir)` with `.put(key,
  data)`, `.get(key, max_age)`, `.get_any(key) -> (data, age)`, `.put_bytes(key, ext, data)
  -> path`, `.get_bytes(key, ext, max_age=None)`, `.path(key, ext)`; `cached_json(cfg,
  cache, key, url, ttl, max_stale, accept=GEOJSON, validate=None) ->
  {"data","ok","stale","age_s","error","from_cache"}`; `run_lock(path)` context manager
  raising `AlreadyRunning`. Run budget and circuit breaker:
  * `begin_run(cfg)` (called by make_weather_page once per run, before any fetch): deadline
    = now + `cfg.run_budget_s` (monotonic clock; a missing or non-positive value = no
    deadline) and an empty set of "down" hosts. Before `begin_run` is ever called (tests,
    library use) there is no budget and no host is ever marked down.
    `budget_left() -> float|None` (None = unlimited); `run_status() -> {"budget_s",
    "left_s", "exhausted": bool, "down_hosts": [sorted]}`.
  * `get_bytes`: before each attempt, raises `HttpSkipped("skipped: run budget exhausted")`
    or `HttpSkipped("skipped: <host> unreachable earlier in this run")` without touching the
    network. Per-attempt timeout = `min(timeout, max(3 s, budget left))`; the retry backoff
    never sleeps past the deadline. Retries transport errors (timeout, URLError without a
    status, ConnectionError/other OSError, `http.client.HTTPException`) and HTTP 5xx/429; a
    transport failure after the last attempt marks the host down (HTTP status errors never
    do). Every failure is raised as `HttpError`.
  * `head_ok`: True = 2xx; False = a definite HTTP non-2xx answer; None = no answer
    (transport failure, timeout, or skipped) — callers must treat None as "server
    unreachable". Default timeout `cfg.http_timeout`, capped by the budget; a transport
    failure inside a run marks the host down.
  * `cached_json`: a fetched payload that cannot be cached (`cache.put` raises OSError) is
    still returned as ok (warning logged); a request refused by the budget/breaker counts
    as a failed fetch, so the last good copy is served (logged at INFO; the cause is logged
    once at WARNING: `<host> unreachable (…): skipping it for the rest of this run` /
    `run budget of N s exhausted: skipping all further network requests`).
  * `Cache` files are written tmp + flush + fsync + `os.replace` (temp removed on failure).
  * `fetch_conditional(url, cfg, etag=None, last_modified=None, accept=None, timeout=None)
    -> (status, body|None, etag|None, last_modified|None)` (roads contract R2): a GET that
    sends `If-None-Match` / `If-Modified-Since` when given. `(200, body, <response
    validators>)`, or `(304, None, <the 304's validators, else the ones passed in>)`. It runs
    on the same retry loop as `get_bytes` (`_request`): same retry policy, run budget,
    `HttpSkipped` and host marking; every failure is an `HttpError`.
  * `cached_bytes(cfg, cache, key, url, ttl, max_stale, accept=None, validate=None) ->
    {"ok","stale","error","age_s","from_cache","not_modified": bool, "data": bytes|None,
    "last_modified": str|None}`: `cached_json` for a raw body, revalidated with
    `fetch_conditional`. The body is stored as `cache.put_bytes(key, ".bin")`
    (`http.BYTES_EXT`), its validators as `cache.put(key + "__meta", {"etag",
    "last_modified", "ts"})`, body first. A copy younger than `ttl` is returned without a
    request. Validators are sent only while a cached body exists; a 304 refreshes the
    stamp and returns the cached body (`ok`, `age_s` 0, `not_modified` True). A 200 is
    checked with `validate(bytes)` (a rejected payload counts as a failed fetch and is never
    cached). A failed fetch (including a budget/breaker skip) serves the last copy younger
    than `max_stale` with `stale` True and `error` set, else `data` None / `ok` False.
    DEBUG log lines say `<key>: HTTP 304 not modified, cached copy reused (N bytes)` or
    `<key>: HTTP 200, N bytes (…)`.
- `weather/util.py` — `parse_iso`, `tzinfo_for`, `to_local`, `fmt_local(dt, tzname, fmt)`,
  `utcnow`, `age_minutes`, `fmt_age(seconds)`, unit conversions (`c_to_f`, `kmh_to_mph`,
  `ms_to_mph`, `pa_to_inhg`, `pa_to_hpa`, `m_to_mi`, `m_to_km`, `km_to_mi`, `mm_to_in`),
  `deg_to_cardinal(deg, points=16)`, `quantity(nws_quantity, convert=None)`,
  `round_or_none`.
- `weather/tests/conftest.py` — fixtures `cfg` (tmp out/cache dirs, map_px=240, zoom 8),
  `cache`, `fake_http` (registry: `.add(url_fragment, body)`, `.fail(fragment)`, `.calls`).
  For road closures (R2) it gained, additively: `FakeHttp.fetch_conditional` (200 + the
  registered body, recorded as `"COND <url>"` in `.calls`; 304 when a test called
  `.validators(fragment, etag, last_modified)` and the request sends that ETag back),
  monkeypatched in by `fake_http` like the others. Otherwise do not modify conftest; add
  module-specific fixtures inside your own test file.

## Common "source status" keys

Every fetched thing carries the same four keys so the page can label freshness uniformly:

```
"ok": bool          # usable data present (fresh or last-good)
"stale": bool       # data is a last-good copy older than its ttl (or fetch failed)
"error": str|None   # why the latest fetch failed (present even when stale data is shown)
"age_s": float|None # seconds since the data was fetched (0 for a fresh fetch)
```

## weather/nws.py

```python
def site_metadata(cfg, cache, site) -> dict
```
`GET {BASE}/points/{lat:.4f},{lon:.4f}` via `http.cached_json` (key
`points/<lat:.4f>,<lon:.4f>`: every NWS cache key names the site's point, never its slug, so
a site that keeps its slug but moves is never served the old place's grid, zones or
forecast; ttl `cfg.points_ttl`, max_stale `cfg.points_max_stale`). A payload is valid with
a forecast grid, or with a time zone and a zone but no grid (American Samoa); then
`no_grid` is True, and `forecast`, `hourly` and `observation` return, without a request,
`{"ok": False, "stale": False, "error": nws.NOT_PROVIDED, "age_s": None, "not_provided":
True}` plus their empty product keys: not an error (make_weather_page does not list it,
the page says "not provided by NWS for this location", status.json does not count it as a
problem). Returns (None values when not ok):
```
{ status keys..., "grid_id": "ABQ", "grid_x": 86, "grid_y": 76, "office": "ABQ",
  "forecast_url": str, "hourly_url": str, "stations_url": str,
  "tz": "America/Denver", "city": "Socorro", "state": "NM", "radar_station": "KABX",
  "forecast_zone": "NMZ220", "county_zone": "NMC053", "fire_zone": "NMZ106",
  "zone_urls": ["https://api.weather.gov/zones/forecast/NMZ220", ...county..., ...fire...],
  "no_grid": False }
```
`tz` falls back to `"UTC"` when metadata is unavailable (never None); make_weather_page then
substitutes the site's static `tz` (see there).

```python
def forecast(cfg, cache, site, meta) -> dict
```
`GET meta["forecast_url"]` (key `forecast/<lat>,<lon>`, ttl/max_stale `cfg.forecast_*`). Returns
```
{ status keys..., "updated": iso|None, "periods": [
   {"number": 1, "name": "Today", "start": iso, "end": iso, "is_day": bool,
    "temp_f": 80, "temp_c": 26.7, "temp_trend": str|None, "pop": 51|None,
    "wind": "10 to 15 mph", "wind_dir": "SE", "short": "Chance Showers And Thunderstorms",
    "detailed": "...", "icon": url|None}, ... up to cfg.forecast_periods ] }
```
NWS `temperatureUnit` may be "F" or "C" — convert so both keys are always filled.
`pop` is `probabilityOfPrecipitation.value` (may be None). Leading periods whose `endTime` is
at or before now (`util.utcnow()`) are dropped **before** the `cfg.forecast_periods` cap (a
cached or last-good copy must not lead with "Today" after the day has ended; a period whose
end cannot be parsed is kept). If none remain, `ok` is False and `error` reads
`"7-day forecast has only periods that have already ended"`, with any fetch error appended
in parentheses.

```python
def hourly(cfg, cache, site, meta) -> dict
```
`GET meta["hourly_url"]` (key `hourly/<lat>,<lon>`, ttl/max_stale `cfg.hourly_*`). Keep the
periods from the current hour (local time, `meta["tz"]`) onward, `cfg.hourly_hours` of them:
```
{ status keys..., "updated": iso|None, "hours": [
   {"start": iso, "local": "Wed 14:00", "day": "Wed", "hour": "14:00", "is_day": bool,
    "temp_f", "temp_c", "pop": int|None, "rh": int|None, "dewpoint_f", "dewpoint_c",
    "wind_mph": float|None, "wind_kmh": float|None, "wind_dir": "SE", "short": str,
    "icon": url|None} ] }
```
`windSpeed` in the hourly product is a string like `"10 mph"` or `"10 to 15 mph"`: parse
the last number (upper bound) as mph.

```python
def observation(cfg, cache, site, meta) -> dict
```
`GET meta["stations_url"]` (key `stations/<lat>,<lon>`, ttl 7 days) → ordered station list.
Then, for the first ≤ 3 stations, `GET {BASE}/stations/{id}/observations/latest` (key
`obs/<slug>/<id>`, ttl `cfg.obs_ttl`, max_stale `cfg.obs_max_stale`) and pick the first
whose `timestamp` is younger than `cfg.obs_max_age_min` minutes and whose temperature is
not None. Returns
```
{ status keys..., "station_id": "KONM", "station_name": "Socorro Municipal Airport",
  "timestamp": iso|None, "age_min": float|None, "text": "Mostly Clear", "icon": url|None,
  "temp_f", "temp_c", "dewpoint_f", "dewpoint_c", "rh": float|None,
  "wind_mph", "wind_kmh", "gust_mph", "gust_kmh", "wind_dir_deg": float|None, "wind_dir": "SE",
  "pressure_inhg", "pressure_hpa", "visibility_mi", "visibility_km",
  "heat_index_f", "wind_chill_f", "cloud_layers": ["FEW 1500 ft", ...] }
```
Wind values in observations are `wmoUnit:km_h-1` (occasionally `m_s-1` — honour
`unitCode`). All numeric fields None when unavailable. A calm wind (speed 0) has no
direction: `wind_dir_deg` None and `wind_dir` "" whatever direction NWS reports (it sends
0°, which would read as "N"); a real north wind keeps its bearing. `ok` is False when no station
yielded a usable observation (`error` explains).

## weather/alerts.py

```python
NWS_COLORS: dict    # "Flash Flood Warning" -> "#8B0000", ... the NWS WWA map chart, verbatim
FLOOD_COLORS: dict  # this page's reds for inland flood products (deliberate deviation)
EVENT_COLORS: dict  # dict(NWS_COLORS) updated with FLOOD_COLORS: what color_for uses
KIND_COLORS = {"warning": "#d00000", "watch": "#e6b800", "advisory": "#7b68ee",
               "statement": "#ffe4b5", "other": "#808080"}
def event_kind(event: str) -> str       # warning|watch|advisory|statement|other (suffix rules)
def color_for(event: str, kind: str) -> str   # EVENT_COLORS (case-insensitive), else KIND_COLORS
def alert_threat(params) -> str|None    # CAP damage-threat tags -> chip text (see below)
def normalize(feature: dict) -> dict     # one GeoJSON feature -> AlertRec (no network)
def fetch_active(cfg, cache) -> dict
def resolve_geometries(cfg, cache, alerts) -> None   # fills geometry for zone-based alerts
def site_alerts(alerts, site, meta, frame) -> dict
def sort_alerts(alerts) -> list
```
`NWS_COLORS` is the chart at https://www.weather.gov/help-map (checked row by row on
2026-09-23) plus a few legacy/marine names; at least the ~100 standard entries (Tornado
Warning #FF0000, Severe Thunderstorm Warning #FFA500, Flash Flood Warning #8B0000, Flood
Warning #00FF00, Flood Watch #2E8B57, Flood Advisory #00FF7F, Severe Thunderstorm Watch
#DB7093, Tornado Watch #FFFF00, Winter Storm Warning #FF69B4, Winter Storm Watch #4682B4,
Winter Weather Advisory #7B68EE, Blizzard Warning #FF4500, Ice Storm Warning #8B008B, High
Wind Warning #DAA520, High Wind Watch #B8860B, Wind Advisory #D2B48C, Red Flag Warning
#FF1493, Fire Weather Watch #FFDEAD, Dust Storm Warning #FFE4C4, Blowing Dust Advisory
#BDB76B, Dense Fog Advisory #708090, Freeze Warning #483D8B, Freeze Watch #00FFFF, Frost
Advisory #6495ED, Hard Freeze Warning #9400D3, Extreme Heat Warning #C71585, Excessive Heat
Warning #C71585, Heat Advisory #FF7F50, Extreme Cold Warning #0000FF, Cold Weather Advisory
#AFEEEE, Wind Chill Advisory #AFEEEE, Air Quality Alert #808080, Special Weather Statement
#FFE4B5, Hydrologic Outlook #90EE90, Severe Weather Statement #00FFFF, Flash Flood Statement
#8B0000, Flood Statement #00FF00, Extreme Wind Warning #FF8C00, Snow Squall Warning #C71585,
Lake Effect Snow Warning #008B8B, Avalanche Warning #1E90FF, Storm Surge Warning #B524F7,
Rip Current Statement #40E0D0, Tropical Cyclone Local Statement #FFE4B5, Hazardous Weather
Outlook #EEE8AA, Child Abduction Emergency #FFFFFF, Civil Emergency Message #FFB6C1, Test
#F0FFFF).

`FLOOD_COLORS` — **deliberate deviation from the chart, requested by the owner**: green reads
as "good" on this page (and a green fill vanishes into 15–35 dBZ radar greens), so inland
flood products are reds, darker = more serious: Flood Watch and Flash Flood Watch #E53935,
Flood Warning #C62828, Flash Flood Warning and Flash Flood Statement #8B0000 (the official
value, unchanged), Flood Advisory, Flood Statement, Arroyo and Small Stream / Urban and
Small Stream / Small Stream Flood Advisory and Hydrologic Advisory #FA8072, Hydrologic
Outlook #F4A6A6. Coastal and Lakeshore flood products keep their NWS colours. Unknown events
fall back to `KIND_COLORS[kind]`. Per-deployment overrides (`cfg.alert_color_overrides`) are
applied by `fetch_active`, not by `color_for`.

`event_kind`: name ends with "Warning" → warning; "Watch" → watch; "Advisory" → advisory;
ends with "Statement", "Outlook", "Alert", "Message", "Forecast", "Emergency" → statement;
otherwise other. Case-insensitive.

AlertRec (from `normalize`):
```
{ "id": properties.id, "event", "severity", "urgency", "certainty", "kind", "rank": int,
  "color": "#rrggbb", "headline": str,
  "nws_headline": str|None,   # first non-empty parameters.NWSheadline
  "threat": str|None,         # alert_threat(parameters), see below
  "description": str, "instruction": str|None,
  "area_desc": str, "sender": senderName, "web": url|None, "message_type", "status",
  "sent","effective","onset","ends","expires": iso strings or None,
  "end_dt": aware datetime|None   # ends, else expires — the "until" shown to viewers
  "affected_zone_urls": [urls], "affected_zone_ids": ["NMC027", ...],
  "geometry": geojson|None, "geometry_source": "polygon"|"zones"|None,
  "bbox": (lonmin,latmin,lonmax,latmax)|None }
```
`rank` = `(kind_rank, severity_rank)` flattened to an int, kind_rank warning 0 < watch 1 <
advisory 2 < statement 3 < other 4, severity Extreme 0 < Severe 1 < Moderate 2 < Minor 3 <
Unknown 4. `sort_alerts` orders by (rank, event, end_dt or far future). Map draw order is
the **reverse** of that (statements first, warnings last / on top).

`alert_threat(params)`: `properties.parameters` values are lists (a bare string is
tolerated; matching ignores case). Most serious first, the first match wins:
`tornadoDamageThreat` CATASTROPHIC → "TORNADO EMERGENCY" (on any event, so a follow-up
statement carrying the tag is not missed); `flashFloodDamageThreat` CATASTROPHIC → "FLASH
FLOOD EMERGENCY"; `tornadoDamageThreat` CONSIDERABLE → "PDS" (particularly dangerous
situation); `thunderstormDamageThreat` DESTRUCTIVE → "DESTRUCTIVE";
`flashFloodDamageThreat` CONSIDERABLE → "CONSIDERABLE FLASH FLOODING";
`thunderstormDamageThreat` CONSIDERABLE → "CONSIDERABLE DAMAGE" (the middle severe
thunderstorm tier, 70 mph / 1.75 in hail, which NWS does **not** call PDS); else None.

`fetch_active`: `GET {BASE}/alerts/active?area={",".join(cfg.alert_area_codes)}` (a
config object without that property: its `alert_areas` string split on commas) via
`http.cached_json(key "alerts", ttl 0, max_stale cfg.alerts_max_stale)`. Keep features
with `status == "Actual"`, `messageType in ("Alert", "Update")`, and (`ends` or `expires`)
in the future (or both None). Each kept record's `color` is replaced by
`cfg.alert_color_overrides[event.lower()]` when present (re-validated here: an invalid entry
is ignored with a warning, never costs the alerts). Returns `{status keys..., "fetched_ts":
float|None, "alerts": [AlertRec sorted], "count_raw": int}`.

`resolve_geometries`: for alerts with `geometry is None` and zone urls, fetch each zone via
`http.cached_json(key "zone/<id>", url, ttl 365 days, max_stale 10 years,
validate=lambda d: "geometry" in d)`, then `geometry = geo.merge_geometries(zone geoms)`,
`geometry_source = "zones"`, `bbox` recomputed. Zones that fail stay missing; if none of an
alert's zones resolve, its geometry stays None (it can still be listed "AT" via zone ids).
Outlines on disk are always used; a zone not on disk is fetched only while at least
`ZONE_BUDGET_KEEP` (0.5) of the run's network budget is left (`http.budget_left()`,
`http.run_status()["budget_s"]`; always without a budget). An alert that meets such a
zone after that keeps geometry None for this run (listed, not shaded in part; one WARNING
counts them) and later runs fetch the rest.

`site_alerts(alerts, site, meta, frame)` → `{"at": [...], "candidates": [...]}`:
* `at`, for `geometry_source == "polygon"`: **only** `geo.point_in_geometry(site.lon,
  site.lat, geometry)` decides. The county/forecast zones listed in `affectedZones` of a
  polygon warning are every county the polygon touches (Socorro County is larger than the
  map), so they do NOT make a site AT — NWS's own `?point=` query agrees.
* `at`, for zone-based alerts (or no geometry): any of `meta["forecast_zone"],
  meta["county_zone"], meta["fire_zone"]` in `affected_zone_ids` **or** the point is inside
  the resolved outline.
* `candidates`: not `at`, and `geo.bbox_intersects(alert.bbox, frame.bbox)`.
Both sorted with `sort_alerts`. `meta` may be a not-ok dict (zone ids None) — handle it.

## weather/roads.py (road closures: roads contract R1, as built)

Owner's decisions (binding): **New Mexico only** (no Texas source at all: no DriveTexas, no
TDEM mirror; no ABQRoads city streets), **only closures caused by an emergency** (flooding,
washouts, debris/rock/mud slides, fire, crashes, hazmat, snow/ice, wind/dust, storm damage,
law-enforcement closures), **drawn on the maps**. Excluded: roadwork, construction, lane
closures, maintenance, scheduled/nightly closures, seasonal closures, special events,
convoys/oversize loads, White Sands missile-range alerts, rest-area closures. The lead's
scoping calls, each switchable: water-on-road items (`cfg.roads_water`), NWS Local Storm
Reports (`cfg.lsr_enabled`, `cfg.lsr_hours`), NMDOT Closures without a stated cause shown
as "cause not stated".

```python
NM_BOX = (-109.1, 31.3, -103.0, 37.0)     # (lonmin, latmin, lonmax, latmax)
NM_OUTLINE = ((-109.06, 37.01), ...)      # 12 (lon, lat) vertices, see bbox_covers_nm
def bbox_covers_nm(bbox) -> bool          # does a map box reach New Mexico
def fetch_nm_roads(cfg, cache) -> dict    # never raises
def fetch_storm_reports(cfg, cache) -> dict   # never raises
def site_roads(nm, lsr, frame) -> dict
def classify_event(category, title, narrative, water=True) -> (kind|None, cause_known, reason)
def parse_nm_date(s) -> aware datetime|None
```

`fetch_nm_roads` fetches `cfg.nmroads_json_url` and `cfg.nmroads_rss_url` with
`http.cached_bytes` (keys `"nmroads_json"` / `"nmroads_rss"`, ttl 0, max_stale
`cfg.roads_max_stale`, `validate` = the file parses), joins GeoJSON feature id == RSS guid
and returns
```
{status keys..., "events": [RoadEvent kept], "count_raw": int (features/items considered),
 "count_region": int (kept + excluded inside NM_BOX, duplicates included),
 "feed_time": aware datetime|None (Last-Modified of the file used, else RSS lastBuildDate),
 "excluded": {NMDOT category or "duplicate": count}}
```
`stale` is True only when a copy actually used is a last-good one; when one file failed but
the other is fresh the result is ok, not stale, with `error` set ("incomplete": the page
and status.json say so). The **fresher file decides which events exist** (a stale copy of
the other may list an ended event); the other only adds detail. Degraded single-file modes:
JSON only → every "Closure" whose templated title has no construction wording is listed as
"cause not stated" (construction closures cannot be told apart without the text), no water
items, `updated` None, `posted` = the JSON `creation_date` (else `start_date`) with
`time_approx` True, so an old scheduled closure still gets the page's "may have reopened"
flag, and `error` says "causes unknown (closures listed may include planned work), water on
road and closures filed as driving conditions not detected, times approximate"; RSS only →
the item's `geo:lat`/`geo:long` point is the geometry. XML with a DOCTYPE/ENTITY is
refused.

RoadEvent:
```
{"id": str, "kind": "closure"|"water", "category": NMDOT event name ("Closure",
 "Difficult Driving Conditions", ...), "title": NMDOT's templated title (cleaned),
 "route": "NM 252"|"US 60"|"I 25"|None (RSS osow routeName/routeNumber, then JSON
 road_names, then the title; "null-null" and "-" give None), "mm_from", "mm_to": float|None
 (from the title's "from/to/at mile marker", else the RSS fields), "direction": "both
 directions"|"northbound"|...|None, "cause": str (RSS description: HTML and the
 "<autodescriptiondelimiter>" token stripped, entities decoded, whitespace collapsed,
 boilerplate sentences such as "Use extreme caution." and a repeated title dropped,
 <= 400 chars), "cause_known": bool, "updated": aware datetime|None (RSS "Update Date",
 America/Denver when it has no offset; formats "YYYY-MM-DD HH:MM:SS.f[ff]", Java style
 "Tue Jun 16 09:30:10 MDT 2026", RFC 822, ISO, "unknown"; None for a batch stamp, below),
 "posted": aware datetime|None (RSS "Post Date", else pubDate; JSON-only: the JSON creation
 date), "time_approx": bool (True only for a JSON-only event), "area_wide": bool (the
 title says "... exist throughout the <X> area": a condition for a whole NMDOT patrol area
 placed at one point, listed but never drawn), "geometry": GeoJSON
 LineString|MultiLineString|Point|MultiPoint (lon/lat rounded to 1e-6), "bbox": tuple,
 "source": "NMDOT", "link": "https://nmroads.com/"}
```
The JSON `update_date` / `creation_date` are not the RSS times: on 2026-09-23 the creation
dates of 69 of 94 events were 6 h before the RSS post date, 20 equal, 5 7 h before. They
are used only in JSON-only mode (above). NMDOT re-saves many events at once: an RSS
"Update Date" with a non-zero fraction of a second shared by `BATCH_MIN` (3) or more items
is a batch stamp (`batch_stamps`; live: 19, 14, 12 and 7 events on four stamps, posted
2021-2026) and gives `updated` None, so the event is aged from `posted`.

Classification (`classify_event`; the regexes are tuned on the live feed of 2026-09-23 and
unit-tested on trimmed real payloads and on the review's counter-examples):
1. A sentence of the narrative or the title that says the road is closed/impassable
   (`FULL_CLOSED` after removing lane/shoulder closures, `_PARTIAL`) **and** names an
   emergency in that same sentence (`_hazard`, below) → closure, cause known, whatever the
   event type and whatever else the text mentions ("Roadway closed due to a crash. Traffic
   is diverted at the rest area."; "closed due to blowing dust near White Sands Missile
   Range"). Not such a sentence: one about a rest area / welcome or visitor centre that
   names no road (`_FACILITY`, `_ROAD_WORD`), or one about missile-range activity
   (`_MISSILE_ACTIVITY`: firings, missile tests/missions, range operations).
2. Excluded types (`_EXCLUDED_TYPES`: Roadwork, Construction [Closure], Lane Closure,
   Seasonal [Closure], Alert, Fair Driving Conditions, Rest Area…, Maintenance, Special
   Event, Oversize…, Work Zone) → excluded, except, with `water`, a narrative sentence
   reporting WATER without planned/construction wording (unless the water is named as the
   cause) and without the owner's exclusions (`_water_sentence`) → water (so a Lane Closure
   "left lane is closed due to standing water" is a water item: a deliberate extension of
   the contract's EXCEPT clause).
3. `_HARD_EXCLUDE` (owner's list) anywhere in category, title or narrative → excluded: rest
   area / welcome / visitor center, missile, WSMR, firings, range roads/routes, wind turbine
   / turbine blades, convoy, oversize(d) / wide loads, special event, parade, marathon,
   fiesta, festival, rodeo, county/state fair, film shoot/production, seasonal(ly), winter /
   seasonal closure, "closed for the season/winter", "closed from <month> to <month>",
   PERMANENT ALERT. (Not "White Sands" alone: it is also a place and a national park.)
4. Closure types (`_CLOSURE_TYPES`: Closure, Road Closure, Road Closed, Closed, Full /
   Emergency / Weather Closure): a sentence names an emergency → closure, cause known;
   else construction or scheduling wording (`CONSTRUCTION` or `PLANNED`) → excluded; else →
   closure, `cause_known` False.
5. Anything else (driving conditions, Crash, ...): the text says the road is closed
   ("both lanes are closed" counts) → closure (cause known when an emergency is named or
   the type is a driving-conditions type; no emergency and construction wording →
   excluded; neither → cause not stated); else, with `water`, a water sentence → water;
   else excluded. Muddy-road advisories ("4x4 recommended") are excluded: weather-caused
   but neither closed nor water on the road (a "mud" kind would be a small addition).

`_hazard(sentence)` (and `emergency_in(sentences)` = any): `PLANNED` wording in the
sentence (scheduled, nightly, each/every night, daily, project(s), a clock-time window
"7 a.m. to 5 p.m.", a date range "Sept 21-22", a weekday range "Monday through Friday",
"from November to May") → never an emergency; else, with `CONSTRUCTION` wording in the
sentence, only a strong hazard named as the cause (`_CAUSED`: "due to / because of /
caused by / as a result of / following / after / from" + up to three adjectives + an
`EMERGENCY_STRONG` match) counts; else any `EMERGENCY_STRONG` (flood…, standing/high water,
water over/across, ponding, washout, rock/mud/land slide, debris flow, sinkhole, fire,
smoke, crash, accident, collision, rollover, jackknife, fatality, hazmat, spill, gas leak,
snow, ice, icy, blizzard, whiteout, freezing rain, sleet, high/strong/gusty winds, dust
storm, blowing dust/snow, low visibility, tornado, thunderstorm, downed lines/trees,
emergency services/responders, law enforcement, police, sheriff, NMSP, investigation,
evacuation, impassable/impassible, heavy rain, rainfall) or `EMERGENCY_WEAK` (damage,
emergency, storm, wind, debris, mud, dust, blowing, visibility, incident, collapse,
erosion, undermined, rain, boulders, stalled / disabled vehicle). `CONSTRUCTION` also
covers girders, blasting, culverts, rock scaling, replacement, installation, fencing,
crash cushions, stabilization, mitigation, flood/erosion/avalanche/rockfall control, night
work, fiestas, festivals and filming. `_BENIGN` phrases are neutralised first (wind
turbine/farm, storm drain/water, crash attenuator/cushion/barrier, fire hydrant/station,
dust control, "reduce crashes", crash rate, weather permitting, snow fence, Ice Caves).
Sentences (`_sentences`) end at ". ", "! ", "? ", "; ", "~~", bullets and newlines, but
not after an abbreviation or initial (`_ABBREV`: a.m., p.m., St., Dr., Rd., Ave., approx.,
U. S., N.M., Sept., …) and not before a lower-case word, so "closed since 3 a.m. due to a
jackknifed semi" stays one sentence.
NMDOT writes "impassible": the pattern is `impass[ai]ble`.

Dedupe by (normalised title, bbox rounded to 3 decimals), keeping the most recently
updated (`updated`, else `posted`); dropped copies count as `excluded["duplicate"]`. Only
features whose bbox intersects `NM_BOX` are kept. Event order (`_event_order`): closures
with a known cause, closures without, then water; newest first within each (`updated`,
else `posted`).

`fetch_storm_reports`: disabled (`not cfg.lsr_enabled`) → `{"ok": True, "stale": False,
"error": None, "age_s": None, "reports": [], "disabled": True}` without a request. Else
`http.cached_json(cfg, cache, "lsr", lsr_url with {hours}, 0, cfg.roads_max_stale)`, then
`lsr_road_reports(features)` and the `cfg.lsr_hours` window (also on a stale copy).
`lsr_road_reports`: every feature with `state == "NM"`, a time and a point, taken in
product order, folds in re-issues first (`_lsr_corrected`): a report whose text (without
an IEM prefix) equals an earlier one's at the same place (0.05°) and type replaces it
("Corrected time." copies, repeats); "Corrects [time on] previous <type> report from
<place>." replaces the most similar earlier report of that type at that place (difflib
ratio ≥ 0.6), else the one with the same time, else the only one within 48 h (never a
guess among several); when such a correction's own text no longer names the road ("NM-149
should read NM-143.") and the type is unchanged, it keeps the corrected report's text with
"(Corrected by NWS: …)" appended; a type change (flash flood → hail) replaces the report
outright; "Report duplicated with WFO X." is dropped when the same text at the same time
and place is there. Then a report is kept when one sentence of its remark (after
`_LSR_NOT_ROAD` removes "parking lot / home / yard ... flooded", "road grader", "in/en
route") matches `LSR_ROAD` (road, route, highway, hwy, street, crossing, arroyo, bridge,
culvert, county road, `NM 304` / `U.S. 491` / `N.M. 14` numbers, ...) **and** `LSR_CLOSED`
(closed/closure, impassable, flooded/flooding, flood waters, water(s) … over / across /
covering / overtopping, water on or in the road, inundated, washed out, standing water,
barricade, under water, submerged, blocked, debris/mud/rocks across or on/onto/into/down a
road, stranded motorists, damaged road), and it is not about White Sands Missile Range
roads (`_LSR_RANGE`: "Range Route", "on WSMR"; "WSMR Met reports flooding along Dripping
Springs Rd." stays). RoadReport:
```
{"id": "lsr-<12 hex of sha1(product|valid|lat|lon|type|remark)>", "kind": "report",
 "type": typetext, "time": aware datetime (valid), "place": city, "county", "remark",
 "lat", "lon", "geometry": {"type": "Point", ...}, "bbox", "source": "NWS <wfo>"}
```
newest first. Result `{status keys..., "reports": [RoadReport]}`.

`bbox_covers_nm(bbox)` = the box touches `NM_OUTLINE` (a segment of the outline crosses or
touches it, or the box lies inside the outline): New Mexico's outline in 12 vertices from
the Census 2024 TIGER/Line polygon, each moved outward by about 0.01° (every Census vertex
lies inside), not `NM_BOX`, which also holds West Texas south of 32° N and a strip east of
the Texas line; make_weather_page fetches no road source at all when no site's frame
touches it. `NM_BOX` still bounds which NMDOT events are kept.
`site_roads(nm, lsr, frame)` → `{"covers_nm": bbox_covers_nm(frame.bbox) (a map wholly in
another state: False), "events": [...], "reports": [...]}`: the items whose geometry touches `frame.bbox`
(a vertex inside, or a segment crossing it: Liang-Barsky), events in `_event_order`,
reports newest first. The Web-Mercator frame is exactly a lon/lat box, so this matches what
the renderer can draw.

## weather/sun.py (pure Python, NOAA solar equations)

```python
def sun_altitude_deg(lat, lon, dt_utc) -> float
def day_events(lat, lon, date_local, tzname) -> dict
def sun_phase(alt_deg) -> str
def upcoming_events(lat, lon, tzname, now_utc=None) -> list
def sun_summary(lat, lon, tzname, now_utc=None) -> dict
```
`day_events` returns aware local datetimes (or None if the event does not occur):
`{"sunrise","solar_noon","sunset","civil_dawn","civil_dusk","nautical_dawn",
"nautical_dusk","astro_dawn","astro_dusk"}` using altitudes −0.833°, −6°, −12°, −18°.
`sun_phase(alt)`: "day" (≥ −0.833), "civil twilight" (≥ −6), "nautical twilight" (≥ −12),
"astronomical twilight" (≥ −18), else "night" (`sun.PHASES`, `PHASE_NIGHT`).
`sun_summary` returns
```
{"tz": tzname, "now_local": dt, "sun_alt_deg": float,
 "phase": sun_phase(sun_alt),
 "is_night": sun_alt < -6,              # palette only (page night default)
 "today": day_events(today), "tomorrow": day_events(tomorrow),
 "upcoming": [{"label": str, "dt": aware local datetime, "passed": dt < now}, ...],
 "evening": [("Sunset", dt), ...], "morning": [("Astro dawn", dt), ...]}
```
`upcoming` (= `upcoming_events(...)`) holds, in time order, every event with
`now − 30 min ≤ dt ≤ now + 24 h` (absolute time, both ends inclusive), taken from
`day_events` of the local dates yesterday through the **day after tomorrow** (that last date
matters where a dawn falls before its own local midnight, e.g. eastern China on Beijing time;
elsewhere the window filter drops it). Labels (`sun.EVENT_LABELS`): "Astro dawn", "Nautical
dawn", "Civil dawn", "Sunrise", "Solar noon", "Sunset", "Civil dusk", "Nautical dusk",
"Astro dusk". Usually 9 items at mid latitudes, up to 11 with look-back repeats; polar
day/night gives fewer (possibly only solar noons). Items are sorted by `dt.timestamp()`:
Python compares same-zone aware datetimes by wall clock and ignores `fold`, which would
misorder events in a repeated fall-back hour — consumers that sort or compare should do the
same. `evening` (today's dusks) / `morning` (tomorrow's dawns) are kept for backward
compatibility only and are no longer rendered: between midnight and sunrise they show the
wrong morning. Times keep sub-minute precision; the page rounds for display. Accuracy
target: within 2 minutes of NOAA for these latitudes.

## weather/radar.py (needs Pillow; `deps_available()` False without it)

```python
DBZ_PALETTE: list  # [(threshold_dbz, (r,g,b)), ...] highest first, NWS-like colours
def dbz_color(dbz) -> tuple|None     # None below cfg-independent 5 dBZ floor
def find_latest_frame(cfg) -> dict|None   # {"ts": aware utc datetime, "url": str}
def load_frame(cfg, cache) -> dict
class SiteMap:
    def __init__(self, cfg, cache, site, theme, tile_url): ...
    frame: geo.MapFrame        # MapFrame(site.lat, site.lon, cfg.map_km, cfg.map_px, cfg.tile_zoom)
    def basemap(self) -> PIL.Image.Image (RGB, frame.size)
    def render(self, radar, alerts, out_path, notes=(), tz=None, roads=None) -> dict
```
`find_latest_frame`: like the template — start at now rounded down to an even minute
minus 4 min, HEAD up to `cfg.radar_max_back` 2-min steps back using
`cfg.mrms_archive` strftime pattern. Each candidate is probed with
`http.head_ok(url, cfg, timeout=radar.HEAD_TIMEOUT)` (8 s): True = found; False (a definite
"not there") = keep walking back; None (no answer: timeout, refused, host marked down, run
budget spent) = the archive is unreachable, so **stop at once** with a warning and return
None (older frames live on the same server). `load_frame`'s error then reads "MRMS archive
unreachable (no answer to HEAD for HH:MMZ)"; "no MRMS frame in the last N min" means only
that every probe answered "not there". `load_frame`: fetch the PNG bytes (`http.get_bytes`),
open with Pillow, keep as mode "P"/"L" (palette indices!), store the bytes and timestamp in
the cache (`cache.put_bytes("mrms_last", ".png", ...)`, `cache.put("mrms_last_meta",
{"ts": iso, "url": url})`) and on failure reuse them if younger than
`cfg.radar_max_stale`. Returns `{status keys..., "ts": datetime|None, "url": str|None,
"image": Image|None}`. Never raise.

`basemap()`: fetch the tiles in `frame.tile_range()` (`tile_url.format(z=, x=, y=)`; the
bytes of each tile that decodes are kept in the cache as `tile_<sha1(url)[:16]>` for 7 days,
so the two themes share one fetch and a rebuilt composite costs no requests), paste on the
tile canvas, crop `frame.canvas_crop()`, resize to `frame.size` (LANCZOS);
for the dark theme with `cfg.tile_dark_invert`, each **tile** whose mean luminance is above
128 (`radar.LIGHT_TILE_LUMINANCE`) goes through `invert_lightness()` (`c → c + 255 −
(max+min)` per channel: lightness flipped, hue and saturation kept) before it is pasted, and
a missing tile leaves the theme background colour (`THEMES[theme]["bg"]`), so a partial
basemap never comes out half inverted; cache as `cache.put_bytes(SiteMap.basemap_key, ".png")` where
`basemap_key = "basemap_<slug>_<theme>_<frame.cache_key()>_<8 hex of sha1(tile_url|invert)>"`
(so a changed tile URL or inversion setting never reuses an old composite), only when ALL
tiles were fetched (a partial basemap is used for this run but not persisted).

`render(radar, alerts, out_path, notes=(), tz=None, roads=None)`: `radar` is the
`load_frame` result (may be not-ok; with `not_applicable` True, no map of the run reaches
the MRMS grid: the caption shows its `error` text, "no radar here: MRMS covers only the
contiguous US", and the result's `error` does not list the radar); `tz` (else `site["tz"]`) adds the local time to the
caption; `roads` is a `roads.site_roads()` dict (or None; a plain list of items is also
accepted, anything else counts as no roads). Layers: basemap → radar (per-pixel lookup
table frame px → MRMS cell, built once per SiteMap; alpha `cfg.radar_alpha`; pixels <
`cfg.radar_dbz_min` transparent) → alerts in reverse `sort_alerts` order, each polygon
(with holes, via an "L" mask: outer 255, holes 0) filled with `alert.color` at `cfg.alert_fill_alpha` and outlined with a casing in the theme
stroke colour (`THEMES[theme]["stroke"]`: black on dark, white on light) at
`cfg.alert_outline_width + 2` under the alert colour at `cfg.alert_outline_width` (all
casings of one alert before its coloured lines, so a red flood outline stays visible over
red echoes) → **road items** (roads contract R3, below) → site marker (crosshair + name
label with a contrasting stroke) → road key (only when a road item was drawn) → scale bars
"10 mi" and "25 mi" bottom-left → dBZ colour bar with a few labels bottom-right
→ top-left caption "MRMS <UTC hh:mmZ> · <local hh:mm TZ>" (or "radar unavailable: …" /
"radar STALE (<age>)") → tiny attribution bottom edge (`cfg.attribution`). Fonts: the first loadable entry of
`radar.FONT_PATHS` (DejaVu Sans, Liberation Sans, FreeSans in RHEL and Debian locations,
then bare names Pillow searches under `$XDG_DATA_DIRS/fonts`), remembered after the first
search; else `ImageFont.load_default(size)` on Pillow ≥ 10.1, else `load_default()` (a
Latin-1-only bitmap font: text is reduced to Latin-1 for it, "…" → "...", dashes → "-").
Text measuring falls back through `textbbox` / `getbbox` / `getsize` / `getmask`, so old
Pillow releases never fail the decorations. Write atomically (tmp + flush + fsync +
`os.replace`). Returns
`{"ok": bool, "error": str|None, "path": out_path, "drawn_ids": [alert ids whose mask
produced ≥ 1 pixel inside the frame], "radar_drawn": bool, "roads_drawn_ids": [road item
ids drawn, unique, input order: events then reports]}`.
The map must still be produced when radar or tiles are missing (label it).

Road items (R3). Monochrome, because hazards are never green, flood alert areas are red and
the radar spans cyan..magenta: the theme's ink (`THEMES[theme]["ink"]`: white on dark,
near-black on light) over a halo in its stroke colour, told apart by line style and symbol.
* `"closure"`: solid line, halo `ROAD_HALO_W` 9 px under ink `ROAD_LINE_W` 4 px, round joins
  and caps; symbol "no entry": ink disc radius `ROAD_SYMBOL_R` 8 with a horizontal bar in
  the stroke colour and a 1 px stroke ring.
* `"water"`: continuous 9 px halo with 4 px ink dashes on top, `ROAD_DASH` (8, 6) in
  pixels (drawn 7/9 in geometry, `_DASH_DRAWN`, because Pillow fills both end pixels),
  continuing around corners; symbol: a stroke-filled disc with a 2 px ink ring, two ink
  wave strokes and a 1 px outer stroke ring.
* `"report"` (items under `"reports"`, or without a kind there): upward ink triangle, side
  `ROAD_REPORT_SIDE` 12 px, with a `ROAD_REPORT_HALO` 2 px halo. An item with no geometry
  but numeric `lat`/`lon` is drawn as a point.
Lines are clipped to the frame (vertices closer than 0.75 px thinned); anything whose
lon/lat box misses the frame, and points outside it, are skipped. A symbol sits halfway
along the visible part of its line (or on the point), pulled inward just enough to stay
whole on the map. Stacking: water lines, closure lines (each kind's halos before its ink),
report triangles, water discs, closure discs. A report triangle that a disc would cover is
moved just above that disc (or below it at the top edge), because storm reports often
repeat an NMDOT item at the same spot (live 2026-09-23: "water over NM 252" on NMDOT's
NM 252 item) and a hidden triangle would make the key and the page's "on map" marker
untrue. Everything goes on one RGBA layer composited only when all of it was drawn, so an id
in `roads_drawn_ids` always has pixels on the map; a failure there leaves the map without
roads and adds "road layer failed: …" to `error` (the map is still written). Bad items
(non-finite coordinates, unknown kind, unsupported geometry) are skipped with a warning.
An `area_wide` event is never drawn (and so never in `roads_drawn_ids`).
Key: rows "Road closed", "Water on road", "Storm report (NWS)" in that order for the kinds
drawn (a line sample only when a line of that kind was drawn), on a box in the theme
background colour at alpha `ROAD_KEY_ALPHA` 205, text in ink without an outline. Placed at
the first of bottom-left above the scale bars, bottom-right above the dBZ bar, top-right
and top-left under the caption that fits, covers neither the site marker nor its label and
no road symbol, then the fewest road pixels (`self._road_key = {"box", "labels",
"where"}`); when no place is free of the marker and every symbol (a small map, symbols in
every corner) the key is left out, so a drawn id's symbol is never hidden under it. A key
failure is logged and the other decorations still drawn.
With `roads` None, empty, `covers_nm` False or only off-frame items the PNG is
**byte-identical** to the output without the feature (unit-tested).

## weather/page.py

```python
def render_html(cfg, run) -> str
def write_page(cfg, run) -> str            # writes <out_dir>/index.html atomically, returns path
def write_status_json(cfg, run) -> str     # <out_dir>/status.json: small health summary
```
`run` (built by `make_weather_page.run_once`):
```
{ "generated": aware utc datetime, "generated_ts": float, "night_default": bool,
  "alerts": <fetch_active result>, "radar": {status keys..., "ts": datetime|None},
  "network": http.run_status() after the fetches | None,
  "roads": {"nmdot": <fetch_nm_roads result without "events">,
            "lsr": <fetch_storm_reports result without "reports">}   # absent: roads off,
                                                   # or no site's map reaches New Mexico
  "roads_not_applicable": str    # only in the second case: why nothing road-related ran
  "errors": [str],
  "sites": [ { "site": {"slug","name","lat","lon","tz"}, "meta": <site_metadata>,
               "obs": <observation>, "forecast": <forecast>, "hourly": <hourly>,
               "sun": <sun_summary>|None,
               "alerts_at": [AlertRec], "alerts_near": [AlertRec],
               "alerts_drawn_ids": [sorted ids whose mask was drawn on at least one theme
                                    map that was written; [] when no map was written],
               "maps": {"dark": {"basename": str, "ok": bool, "error": str|None},
                        "light": {...}},
               "map_notes": [str],
               "roads": {"covers_nm": bool, "events": [RoadEvent], "reports": [RoadReport],
                         "drawn_ids": [sorted ids drawn on at least one written map]}
                        # absent: roads off, or the site's selection failed } ] }
```
Older run dicts without `network`, `alerts_drawn_ids`, site `tz`, sun `phase`/`upcoming` or
AlertRec `threat`/`nws_headline` must still render without error boxes. A run dict without
`roads` / site `roads` renders exactly as before (apart from the added CSS): no road block,
banner line, pill, credit or status problem. Datetimes in road items may be aware
datetimes or ISO strings (naive = UTC).

Time zone of a site's local times: `meta["tz"]` when the metadata is ok, else `site["tz"]`,
else `meta["tz"]`, else UTC (the same rule as make_weather_page).

Page: single self-contained HTML (inline CSS/JS, no external assets except the NWS icon
URLs in `<img>` tags with `loading="lazy"` and a text fallback via `alt`), reusing the
template's visual language: read `PAGE_CSS`, `PAGE_JS`, `build_masthead_html` in
`make_status_page.py` of https://github.com/kirxkirx/ttustatus and keep the same palette,
typography, `.page`/`.page.night` variables, `.tile`, `.pill`, `.mono`, `h2` style, and the
day/night `sessionStorage` toggle. The page must stay fully readable without JavaScript.
Structure:
1. viewport, `<title>` (`cfg.page_title`, the same text as the masthead's `h1`; a config
   object without that property: `cfg.title`), and `<noscript><meta http-equiv="refresh"
   content="{cfg.refresh_seconds}"></noscript>` as the no-JS fallback. `#page` carries
   `data-night-default="1|0"` (= `run["night_default"]`) and `data-refresh` (seconds).
2. Masthead: title, generation time (UTC + the first site's local time), day/night button.
3. Sticky nav: one link per site (`#site-<slug>`; `page.site_anchor`, prefixed so that no
   slug collides with the page's own ids `page`, `alerts`, `modebtn` or with a road
   block), each with a small badge showing the number of
   alerts AT that site (colour of the top-ranked alert). Under 600 px it is one horizontally
   scrollable row; sections and the banner use `scroll-margin-top: calc(var(--navh, 58px) +
   8px)` and PAGE_JS keeps `--navh` equal to the nav's real height (load and resize).
4. Alerts banner: if any site has alerts, a coloured box per distinct alert: event, threat
   chip, issuing office (`sender` without a leading "NWS "), `area_desc` truncated to ~90
   characters at a "; " boundary with the full text in `title=`, the sites it is AT/NEAR,
   and the time span (below); otherwise "No NWS alerts for these sites". If the alerts feed
   is not ok, a visible warning line with the error and the age of the last good copy.
   Road line (R4): when any site lists a closure or water item, one monochrome box
   (`.abox.roadbox`) "Emergency road closures (NM): N closed, M water on road — <links to
   #roads-<slug>>" (`page.roads_anchor`), counting distinct ids across sites (storm reports alone do not trigger
   it).
5. Per site `<section id="site-<slug>">`: `h2` with name, coordinates, NWS office/grid
   ("NWS PPG, no forecast grid" when `meta["no_grid"]`), local
   time and an "NWS point forecast" link (`https://forecast.weather.gov/MapClick.php?lat=
   <lat>&lon=<lon>`); two-column grid: left = the map, right = Now tile grid (temp,
   feels-like/dew point, humidity, wind, pressure, visibility, sky, station + age) and the
   alert cards; below: sun/twilight row (if `site.sun`), hourly table (`cfg.hourly_hours`
   rows, PoP ≥ `cfg.pop_highlight_pct` highlighted), 7-day cards (icon, name, temp, PoP,
   wind, short; `<details>` with the detailed text).
   * Map: `<img class="radar-img radar-dark">` + `radar-light`, CSS shows one. Only the
     server-default theme's image (`run["night_default"]`) gets `src="<basename>?t=<mtime>"`;
     the other gets a 1×1 placeholder `src` plus `data-src`, and PAGE_JS `loadMaps()` swaps
     it in the first time that theme is shown (browsers download `display:none` images).
     `alt` describes the map. Caption: frame time, staleness and the **legend**:
     `Shaded:` swatch + event for every alert (AT or NEAR) whose id is in
     `alerts_drawn_ids`; `Listed, not shaded:` for alerts on the page that were not drawn
     (e.g. an AT alert without an outline); "No map rendered; the alerts are listed as
     text." when no theme map is ok. Without `alerts_drawn_ids` (older run dicts), NEAR
     alerts and AT alerts with a geometry count as drawn.
   * Alert card: left border in the alert colour; event + severity/urgency chips + AT/NEAR
     chip + threat chip (high contrast); headline, and `nws_headline` as a muted subtitle
     when it differs; time span; issuer + area; a link to the NWS hazard page
     `https://forecast.weather.gov/showsigwx.php?warnzone=<Z>&warncounty=<C>&firewxzone=<F>
     &local_place1=<site name>&product1=<event>` (URL-encoded; zone ids must match
     `^[A-Z]{2}[CZ]\d{3}$`). Verified live 2026-09-23: it answers 200 and shows the product
     when ANY of the three ids is covered by the alert, and "No Active Hazardous Weather
     Conditions" when none is. So an AT alert uses the site's own zones; a NEAR alert
     substitutes its own first forecast-zone (Z) and county (C) id; without a complete
     triple the link falls back to the MapClick point forecast page. The feed's `web` link
     is shown only when it is https and not the bare weather.gov home page. `<details>` with
     description and instruction (pre-wrap).
   * Time span (cards and banner): "from <onset> until <end>" when `onset` is later than
     `run["generated"]`, else "until <end>" (end = `end_dt`, in the site's tz); a future
     onset without an end reads "from <onset> (no end time given)".
   * Road block (R4), after the alert cards: `<div class="roads" id="roads-<slug>">`
     (scroll-margin-top like the sections) headed "Road closures (New Mexico, emergencies
     only)" with a count chip. `covers_nm` False → only the muted line "Road closures cover
     New Mexico state routes only; this map is outside New Mexico." (a map wholly in another
     state while some other site's map reaches New Mexico; items are never listed there).
     Otherwise one `<li>` per event in the given order: chip "ROAD
     CLOSED" (solid ink) / "WATER ON ROAD" (outlined ink) with a CSS symbol matching the
     map, route + "mile 17–19" (a 0 end of an "at mile marker" spot is dropped) + direction
     (a "null-null" route shows the title instead), the cause text or "cause not stated",
     NMDOT's title as a muted line, "NMDOT · updated <age> ago (<local time>)" (else
     "posted …"), a warn chip "not updated for N days, may have reopened" past
     `cfg.roads_old_days`, an "on map" chip when the id is in `drawn_ids`. Then "NWS storm
     reports (last <cfg.lsr_hours> h)": time, "place, X County", type, remark, "NWS <wfo>
     storm report · <age> ago · may have reopened". No events → "No emergency road closures
     reported on New Mexico state routes within this map." (never while NMDOT is down), or,
     when storm reports are listed, "NMDOT lists no emergency road closures within this map;
     see the NWS storm reports below." An `area_wide` event's chip reads "ROADS CLOSED IN
     AREA" / "WATER ON ROADS IN AREA" with "whole area, not drawn on the map" in place of
     "on map"; a `time_approx` event reads "NMDOT · posted about <age> ago (time
     approximate)".
     Feed lines: stale NMDOT → "NMDOT road feed: latest fetch failed (…); showing the last
     good copy from … ago."; down → "NMDOT road feed unavailable (…); road closures cannot
     be listed right now."; ok but `error` set (one file missing) → "NMDOT road feed
     incomplete (…)."; storm reports down (while enabled) → "NWS storm reports unavailable
     (…)". Last line: "NMRoads map" (https://nmroads.com/) · "NMDOT feed as of HH:MM MDT".
     Each row is guarded (one bad row cannot hide the others). Everything road-related uses
     only `var(--ink/--bg/--card/--line/--muted/--neutbg)`, never a literal colour. The map
     `alt` mentions road closures when the site's `drawn_ids` is not empty.
   * Sun row (C2): "Sun now <alt>° (<phase>)" (`sun["phase"]`, else derived from the
     altitude bands for older dicts), today's sunrise / solar noon / sunset, then a
     "Next 24 h" row with `sun["upcoming"]` in order, passed items muted. Times are rounded
     to the nearest minute at display (dt + 30 s, then `%H:%M`), with the weekday when the
     local date differs from today's; the `h2` names the time zone ("times in MDT").
6. Footer: per-source status line (alerts, radar frame, forecast, obs — fresh/stale/error
   with ages), the problems of the run in a `<details>`, fixed attribution HTML with links
   (OpenStreetMap copyright page, IEM, weather.gov — not `cfg.attribution`, which is only
   drawn into the PNGs), the page URL (only when `cfg.page_url` is set; linked when it is
   `https://`), "generated at … · refreshes every N s", and before them the page's own
   disclaimer `DISCLAIMER_HTML` (not an official product, not the only source for safety
   decisions, weather.gov for warnings) on every page; its short form
   `BANNER_DISCLAIMER_HTML` ends the alerts banner. A radar with `not_applicable` gives the
   pill "Radar: not applicable (no map in the MRMS grid)" and the map caption "No radar
   here: MRMS covers only the contiguous US"; a product with `not_provided` gives "not
   provided" in its pill and "<product>: not provided by NWS for this location (it has no
   forecast grid)." in its block, without an "unavailable" chip. With
   `run["roads"]`: pills "Roads (NMDOT): feed HH:MM MDT · fresh|stale …|incomplete · …|
   unavailable …" and (while `cfg.lsr_enabled`) "NWS storm reports: …"; `ROADS_CREDIT_HTML`
   ("Road information: NMDOT / NMRoads.com (public domain, CC0); storm reports: NWS via Iowa
   Environmental Mesonet." with links, then NMDOT's disclaimer "road information may not
   reflect all incidents or road conditions; it is not to be your only source."), a scope
   sentence that follows `cfg.roads_water` / `cfg.lsr_enabled` / `cfg.lsr_hours`, and
   "Left out right now: 47 Roadwork · 14 Alert · …" from `nmdot["excluded"]`.

All text HTML-escaped (`html.escape`); URLs from the feeds (`web`, `icon`) only after
checking they start with `https://`; the links built here use fixed hosts and validated,
URL-encoded parameters.

PAGE_JS (progressive enhancement):
* Day/night toggle: `sessionStorage["weather-mode"] = {mode, base}` where `base` is the
  page's `data-night-default` when the viewer chose `mode`; it is applied only while `base`
  equals the current default (otherwise removed), so the sun-driven default re-applies at
  the next change.
* Reload every `data-refresh` seconds instead of a bare meta refresh, preserving the open
  `<details>` (stable `data-k` keys: `"<slug>:<alert id>"`, `"<slug>:p<period number>"`,
  `"run:problems"`) and `scrollY` in `sessionStorage["weather-view"]` (written on the timer
  and on `pagehide`, restored on load and again on `window.load` if the viewer has not
  scrolled; a saved view older than 10 min is ignored).

`status.json`:
```
{"generated": iso, "page_written": bool,   # index.html is at least as new as this run
 "degraded": bool,                          # any source unavailable/stale, any map not
                                            # written, any network cause, or errors logged
 "ok": page_written and not degraded,       # the flag to alert on (with the age of generated)
 "problems": [str],                         # short: "radar: stale", "socorro light map: not
                                            # written", "network: api.weather.gov unreachable",
                                            # "network: run budget of 240 s used up",
                                            # "N error(s) logged in this run"
 "alerts": {"ok", "stale", "count"},
 "radar": {"ok", "stale", "ts"}|"not applicable",   # no site's map in the MRMS grid
 "network": {"budget_s", "left_s", "exhausted", "down_hosts"}|None,
 "roads": {"nmdot": {"ok", "stale", "events", "feed_time": iso|None},
           "lsr": {"ok", "stale", "reports"}}
          |"not applicable"                     # run["roads_not_applicable"]: no map in NM
          |None,                                # run without road data (roads off, older run)
 "sites": {slug: {"meta_ok", "obs_ok", "forecast_ok", "hourly_ok", "alerts_at",
                  "alerts_near", "maps_ok", "roads_events", "roads_reports"}},
 "errors": [str]}
```
`roads.nmdot.events` / `roads.lsr.reports` count the distinct items **listed on the page**
across all sites (an item on two maps counts once), not statewide: the run dict carries the
sources' statuses, not their item lists. Road problems (`_problems(run, cfg)`), only while
`cfg.roads_enabled` and `run["roads"]` is present: "NMDOT road feed: unavailable|stale|
missing|incomplete" and, while `cfg.lsr_enabled`, "NWS storm reports: unavailable|stale|
missing".
A deliberate `--skip-radar` / `WEATHER_RADAR=0` makes every run degraded ("radar:
unavailable"): a skip cannot be told apart from a failed fetch. A source dict with
`not_applicable` (the radar when no map reaches the MRMS grid) or `not_provided` (an NWS
product that does not exist for the location) is never a problem.

Units: `cfg.units == "us"` → °F/mph primary with °C/km/h in a `<span class="alt">`
(small, muted); `"metric"` → the reverse. Missing values render as "—". Never let one bad
site kill the page: wrap each site section in try/except and render an error box instead.
`index.html` and `status.json` are written tmp + flush + fsync + `os.replace`.

## make_weather_page.py (entry point, repo root)

```
usage: make_weather_page.py [--out DIR] [--cache DIR] [--verbose] [--skip-radar]
                            [--sites SPEC] [--once]
```
`--sites 'slug|Name|lat|lon[|tz];…'` (same format as `WEATHER_SITES`; given but listing no
site, e.g. `;`, is a configuration error, exit 2).
`run_once(cfg) -> run dict` does, with each step guarded so failures degrade per source:
1. `http.run_lock(cfg.lock_file)` (exit 0 with a log line if already running), then, inside
   the lock and before any fetch, `http.begin_run(cfg)`: a fresh network budget and
   circuit breaker for this run. Log `run started: N site(s) from <cfg.sites_source>, …`
   and `alert areas: <codes> (auto: the states, territories and marine areas on the sites'
   maps | auto: the areas on the sites' maps, plus <codes> from WEATHER_ALERT_AREAS |
   WEATHER_ALERT_AREAS)`, plus a WARNING per `cfg.sites_outside_alert_areas` entry.
2. Alerts: `alerts.fetch_active`, `alerts.resolve_geometries`.
3. Radar frame: when no site's frame intersects `geo.MRMS_BBOX` (`_maps_reach_mrms`), no
   request at all and `run["radar"]` = `{status keys (ok False, stale False, error
   RADAR_NOT_APPLICABLE), ts/url/image None, "not_applicable": True}`: not a problem, and
   status.json reports `"radar": "not applicable"`. Else `radar.load_frame` once (skipped when `--skip-radar` or `not
   cfg.radar_enabled` or `not radar.deps_available()`).
3a. Road closures (R5), only when `cfg.roads_enabled` (`_load_roads`): `roads.fetch_nm_roads`
   and, when `cfg.lsr_enabled`, `roads.fetch_storm_reports` once per run through the same
   guarded `_source` helper as the other feeds (a crash becomes a fallback status dict plus
   a `run["errors"]` line "road closures (NMDOT) …" / "storm reports (NWS) …"; an ok, not
   stale NMDOT result with `error` set adds "road closures (NMDOT) incomplete: …"). With
   `WEATHER_LSR=0` the storm-report status is `{"ok": True, "stale": False, "error": None,
   "age_s": None, "reports": [], "disabled": True}` and nothing is fetched. Item lists are
   sanitised to dicts. `run["roads"]` = both status dicts without their item lists. With
   `WEATHER_ROADS=0` nothing is fetched and neither the run nor the sites get a `roads` key.
   The same holds when no site's map reaches New Mexico (`_maps_reach_nm`: no
   `roads.bbox_covers_nm(b)` for `b` in `cfg.frame_bboxes`; if the boxes cannot be
   computed the roads are fetched as usual): nothing is fetched, `run["roads_not_applicable"]
   = ROADS_NOT_APPLICABLE` (the reason), the page shows nothing road-related, status.json
   reports `"roads": "not applicable"`, and the per-site and "done:" log lines say
   "roads not applicable (no map in NM)".
4. Per site: `nws.site_metadata`; the site's time zone is `meta["tz"]` when the metadata
   is ok, else `site["tz"]` (static, from the config), else "UTC" — when the metadata is
   not ok that zone is also written into `meta["tz"]`, because the page formats every
   local time with it (the placeholder entry of a crashed site does the same). When the
   alert query (`cfg.alert_area_codes`) leaves out the state of the site's own NWS zone
   (the first two letters of `forecast_zone`, else `county_zone`), `_alert_area_gap` adds
   a `run["errors"]` line "<slug> alerts not fetched: its NWS zone … is in XX, which the
   alert query (…) leaves out; …" (and logs it). Then
   `nws.observation`, `nws.forecast`, `nws.hourly`, `sun.sun_summary(…, tz)` (if
   `cfg.show_sun`), `alerts.site_alerts`, then for each theme `radar.SiteMap(...).render(...)`
   (notes: `_undrawn_notes(at)` plus, while a radar frame is loaded and the map lies wholly
   outside `geo.MRMS_BBOX`, `MRMS_OUTSIDE_NOTE` "no radar here: MRMS covers only the
   contiguous US"; both are drawn on the map and listed in `map_notes`)
   (with `site["tz"]` set to that zone, for the caption) into
   `os.path.join(cfg.out_dir, cfg.map_basename(...))`. Only renders that returned ok count:
   `alerts_drawn_ids` = sorted union of their `drawn_ids`; `alerts_near` = candidates whose
   id is in it (if no map was written at all, `alerts_near` = all candidates and
   `alerts_drawn_ids` = []). Roads (`_run_site(..., road_src=None)`): `roads.site_roads(nmdot,
   lsr, frame)` via `_step` ("<slug> road closures failed" on a crash or a non-dict; the
   entry then has **no** `roads` key, so the page shows no block rather than a false
   "none"); `roads=` is passed to both theme renders only when a road view exists (with
   roads off the renderer is called exactly as before). A render that raises with roads is
   drawn again without them ("<slug> <theme> map drawn without road closures: …"); a
   "road layer failed: …" part of an ok render's `error` is copied to `run["errors"]`.
   `entry["roads"] = {covers_nm, events, reports, drawn_ids}` with `drawn_ids` = sorted
   union of `roads_drawn_ids` of the ok renders (read with `.get`, so a fallback result
   without the key is fine). The per-site log line gains "roads N closed / N water / N
   report(s)" (or "outside NM", "unavailable", "off"); the final "done:" line gains "roads
   NM x closed / y water / z report(s)" or "roads off".
5. `run["network"] = http.run_status()`; `night_default` = first site's `sun.is_night`
   (False if unavailable).
6. `page.write_page`, `page.write_status_json`.
Exit code 0 even when sources fail (the scheduler — cron on tau.kirx.net, a systemd timer
elsewhere — must keep running); non-zero only for
config errors (2) or an unwritable output or cache directory (1). `--verbose` → DEBUG
logging to stderr. Logging default INFO to stderr in the format
`%(asctime)s %(levelname)s %(name)s: %(message)s`.

## Tests (pytest, no network)

Each module gets `weather/tests/test_<module>.py` using `cfg`, `cache`, `fake_http`.
Fixture JSON should be realistic but small (copy the field structure from api.weather.gov;
a `/points` sample, a 2-period forecast, a few hourly periods, one observation, an alert
feed with one polygon Flash Flood Warning, one zone-based Flood Watch and one expired
alert, one zone with geometry). Radar tests build a synthetic 7000×3500 "P" frame is too
big — instead monkeypatch `geo.MRMS_*`? No: keep the grid, but create the frame image with
`Image.new("P", (7000, 3500), 255)` and paint a small block of index 124 (30 dBZ) at the
site's cell; that is cheap. Basemap tiles in tests: `fake_http.add("openstreetmap", <256×256
PNG bytes>)`.

Road closures (2026-09-23): `test_roads.py` embeds trimmed real NMRoads (`nmroads.json` +
`rss.xml`) and IEM storm-report payloads of 2026-09-23 (roadwork, a construction Closure,
flood closures, standing water, alerts, a duplicate pair, "null-null" routes, the delimiter
token, every RSS date format); `test_radar.py`, `test_page.py` and `test_main.py` reuse real
items (NM 41 crash closure, NM 333 / Socorro-area standing water, NM 94 JSON-only closure,
the NM 304 Las Nutrias storm report). Conditional GETs are faked with
`fake_http.validators(...)` (see conftest above). `test_deploy.py` keeps
`weather.env.example` in step with every `WEATHER_*` variable, including the road ones.

Configurable sites: `test_sites.py` (sites file, precedence, the startup site checks,
the explicit-area gap report, the marine table and `--marine` generator, derived title, auto alert
areas for the example sites, Denver + Flagstaff, the Four Corners, Hawaii, Puerto Rico,
Guam, American Samoa, Chicago and the Aleutians, the state and marine tables and
`tools/state_bboxes.py` on synthetic shapefiles, and an `ast` scan that no string constant
in the code names an example site); `test_nws.py` a site moved under the same slug and a
point without a forecast grid; `test_roads.py` the New Mexico outline (Van Horn, Olton:
no; Farwell, Pecos: yes); `test_page.py` the disclaimer, the optional page URL, anchors
that cannot collide and "not provided" products; `test_main.py` runs whole generations for
Denver + Flagstaff (no road request or block, `"roads": "not applicable"`), Denver +
Socorro (roads fetched, Denver's one-line note), Honolulu + Denver (the MRMS note),
Honolulu alone (no MRMS request, `"radar": "not applicable"`), Pago Pago (no forecast
grid, a healthy run) and a site outside an explicit `WEATHER_ALERT_AREAS`.
