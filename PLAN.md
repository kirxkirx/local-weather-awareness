# local-weather-awareness — implementation plan

> **Note.** This is the original plan, kept with the decisions taken since. It was written
> for one deployment; tau.kirx.net is now only the **example deployment** (Gentoo with
> OpenRC and cron, see the README), and the sites are configuration (§15, §16). Where this
> plan and the README disagree, the README describes the code.

Multi-location weather page for human viewers, derived from the `ttustatus` status-page
template (its page style, radar/basemap machinery, NWS client, systemd/deploy layout) with
every Raspberry-Pi sensor, camera, GPS/NTP, Weather-Underground and Alpaca safety-monitor
piece removed. Output is a single static `index.html` plus one radar PNG pair per location,
regenerated every 5 minutes and served by Apache on **tau.kirx.net**.

Status: **confirmed 2026-09-23** — URL fixed to https://tau.kirx.net/myweather/; twilight times included; °F/mph primary with metric in small print.

---

## 1. Scope

**In**
- Five locations, each with: a **100 × 100 statute-mile radar map centred on the site**,
  current conditions, an hourly forecast table, a 7-day forecast, and the **active NWS
  alerts** that apply to it.
- NWS alerts (flash floods, floods, severe thunderstorm, tornado, winter, wind, fire, dust,
  heat, air quality, … everything the API returns) shown **as text on the page** and
  **as shaded areas on the radar maps**.
- Day / night colour themes (same toggle and CSS family as the template).
- Local run mode, unit tests, CI, and an Apache + systemd deployment for tau.kirx.net.

**Out** (explicitly): local sensors, camera, GPS/NTP, Weather Underground, GLM lightning,
Alpaca safety monitor, any "safe/unsafe" logic. Nothing on this page is a control input.

## 2. Locations (verified against api.weather.gov today)

| # | Site | Lat, Lon | NWS grid | Time zone | Nearest radar | Forecast / county / fire zones |
|---|------|----------|----------|-----------|---------------|--------------------------------|
| 1 | Lubbock, TX | 33.5779, −101.8552 | LUB 49,33 | America/Chicago | KLBB | TXZ035 / TXC303 / TXZ035 |
| 2 | Clovis, NM | 34.4048, −103.2052 | ABQ 222,82 | America/Denver | KFDX | NMZ235 / NMC009 / NMZ126 |
| 3 | Fort Sumner, NM | 34.4717, −104.2456 | ABQ 184,87 | America/Denver | KFDX | NMZ237 / NMC011 / NMZ126 |
| 4 | Socorro, NM | 34.0584, −106.8914 | ABQ 86,76 | America/Denver | KABX | NMZ220 / NMC053 / NMZ106 |
| 5 | Albuquerque, NM | 35.0844, −106.6504 | ABQ 98,121 | America/Denver | KABX | NMZ219 / NMC001 / NMZ106 |

Assumption: "Fort Sumner MN" in the request means Fort Sumner, **New Mexico** (De Baca
County); there is no Fort Sumner in Minnesota and NWS resolves the coordinates above to it.
Coordinates are town centres; all of them live in a config file and can be nudged (e.g. to
an observatory site) without code changes. The grid/zone metadata is resolved from
`/points/{lat},{lon}` at run time and cached, not hard-coded. Since §15 this list is only
the example deployment's configuration: the sites are set with `WEATHER_SITES` or
`WEATHER_SITES_FILE`, and nothing else depends on them.

## 3. Data sources (all free, no API keys; all probed successfully on 2026-09-23)

| Data | Source | Cadence / cache |
|------|--------|-----------------|
| Radar | NOAA/NSSL **MRMS composite reflectivity**, CONUS PNG from the Iowa Environmental Mesonet archive (`GIS/mrms/lcref_YYYYMMDDHHMM.png`, 7000×3500 px, 0.01° plate-carrée grid, 0.5 dBZ steps) — same feed the template uses | new frame every 2 min; fetched once per run and cropped/reprojected for all 5 sites |
| Basemap | OpenStreetMap standard raster tiles; the dark theme inverts their lightness (Carto's free `dark_all`/`light_all` rasters now require an API key — found during integration on 2026-09-23) | fetched **once per site and theme**, composited and cached on disk forever |
| Alerts | `api.weather.gov/alerts/active?area=NM,TX` (GeoJSON, CAP fields, polygon geometry when the product is polygon-based) | every run (5 min) |
| Alert zone shapes | `api.weather.gov/zones/{forecast,county,fire}/{id}` — needed because watches/advisories are zone-based and carry `geometry: null` | fetched on first sight of a zone id, cached on disk indefinitely |
| Site metadata | `api.weather.gov/points/{lat},{lon}` → grid, time zone, zones, station list | cached 7 days |
| 7-day forecast | `…/gridpoints/{grid}/forecast` (14 day/night periods, icons, PoP, wind, text) | cached 30 min |
| Hourly forecast | `…/gridpoints/{grid}/forecast/hourly` (temp, PoP, RH, dew point, wind, sky text) | cached 30 min |
| Current conditions | first reporting station from `…/gridpoints/{grid}/stations` → `/stations/{id}/observations/latest` (KLBB, KCVN/KCVS, KFSU-class AWOS, KONM, KABQ) | cached 10 min |

`api.weather.gov` requires a `User-Agent`; it is a config value (`WEATHER_USER_AGENT`,
default `local-weather-awareness (+https://github.com/kirxkirx/local-weather-awareness)`),
so a contact can be added in `weather.env`.

## 4. Radar map specification (per site, two themes)

- **Extent:** exactly 100 mi (160.9 km) east–west and north–south, centred on the site,
  drawn in Web Mercator (tile-aligned, so a circle is a circle and one scale bar is valid in
  every direction). At 34° N that is ≈ ±0.73° lat, ±0.87° lon.
- **Size:** ~600 px wide (config), height follows the Mercator aspect (≈ square).
  Basemap tiles at zoom 9 (≈ 1.5 km/px before downsampling) so towns and highways are
  readable.
- **Layers, bottom → top:** basemap → MRMS reflectivity (NWS-style dBZ palette, ≥ 5 dBZ,
  ~67 % opacity, alpha 170) → **alert areas** (translucent fill + solid outline over a
  black/white casing, NWS watch/warning/advisory colours, e.g. Flash Flood Warning dark
  red, Flood Watch red (flood products deliberately red, not the NWS chart's greens),
  Severe Thunderstorm Warning orange, Tornado Warning red, Winter Storm Warning hot pink,
  Red Flag Warning deep pink, Wind Advisory tan, Dust Storm Warning bisque, unknown events →
  a kind-based fallback) → centre marker + site name → 10 mi / 25 mi scale bars →
  frame timestamp (UTC + local) → attribution line.
- **Draw order of alerts:** watches/advisories first, warnings on top, so a warning is never
  hidden under a watch. Polygons with holes are rendered through a mask, so a zone with an
  excluded inner area is drawn correctly.
- **Reprojection:** per-pixel lookup table from map pixel → MRMS grid cell, built once per
  site and cached in memory (same approach as the template, ~0.4 M lookups per image).
- **Legend:** the page caption under each map lists the shaded alerts with their colour
  swatches, so a viewer never has to guess what a colour means. The dBZ scale bar is drawn
  in the image.

## 5. Alerts: selection, text, and shading

1. Fetch all active NM + TX alerts once per run. Keep `status == Actual`, message types
   `Alert`/`Update` (drop `Cancel`, tests, exercises), and drop anything whose `ends`
   (else `expires`) is already past.
2. For each alert, resolve its area: the CAP polygon if present, otherwise the union of its
   `affectedZones` shapes (fetched/cached from the zones endpoints).
3. For each site classify the alert as
   - **AT the site** — for a polygon warning, only the point-in-polygon test (ray casting,
     pure Python) counts: its `affectedZones` list every county the polygon touches. For a
     zone-based product, the site's own forecast/county/fire zone id is in `affectedZones`,
     **or** the point is inside the merged zone outline;
   - **NEAR** — its shape leaves at least one pixel on the 100-mile map (decided from the
     rendered mask, so "listed" and "shaded" can never disagree);
   - otherwise ignored for that site.
4. Page text: a **banner at the top** ("3 active alerts: Flash Flood Warning at Socorro,
   Flood Watch at Albuquerque and Socorro, …", colour-coded, links to the sections) and,
   under each site's map, a card per alert with: event name + severity/urgency chips, the
   NWS headline, onset → ends **in the site's local time**, issuing office, area
   description, and the full description + instruction in a collapsible `<details>`.
   Alerts are ordered warnings → watches → advisories → statements, then by end time.
5. Shading uses exactly the alert set from step 3 (both AT and NEAR).
6. When the alert feed is unreachable, the page says so plainly (timestamp of the last good
   fetch) instead of silently showing "no alerts"; last-good data is reused for up to 20 min.

## 6. Forecast and current conditions (per site)

- **Now tile:** station id + name, observation age, sky/text, temperature, dew point,
  humidity, wind (speed/gust/direction), pressure, visibility. Units: °F and mph primary,
  °C / km/h in small print (configurable; see open question 2).
- **Next 24 hours:** hourly table — local hour, temperature, precipitation probability,
  humidity, wind, short sky text. Precipitation probabilities above a threshold are
  highlighted.
- **7 days:** the 14 NWS day/night periods as compact cards: name, icon (NWS icon URL,
  with a text fallback), high/low, PoP, wind, short forecast; detailed text in a `<details>`.
- **Optional (open question 1):** sunset, civil/nautical/astronomical twilight and sunrise
  for each site, computed with a pure-Python NOAA solar algorithm (no astropy). Useful for
  observers; not weather.

## 7. Page layout and behaviour

- One static `index.html`, auto-refreshes every 5 min (a small inline script that keeps
  open sections and the scroll position; `<noscript>` meta refresh as fallback), no external
  JavaScript or CSS, images are local PNGs with cache-busting query strings.
- Masthead (title, generation time, day/night toggle stored in `sessionStorage`), sticky
  **site navigation** (Lubbock · Clovis · Fort Sumner · Socorro · Albuquerque), the
  **alerts banner**, then one section per site: map (dark or light per theme) beside the
  Now tile and the alert cards, then the hourly table and 7-day cards. Footer: data
  timestamps per source, attributions (OpenStreetMap, NOAA MRMS via IEM, NWS).
- Responsive: two columns ≥ 900 px, one column on phones; tables scroll horizontally.
- Day/night default follows the sun at the first site (as the template does), user toggle
  overrides. Both map themes are always generated; CSS shows the matching one.

## 8. Code layout (mirrors the template)

(As planned. Added since: `weather/roads.py` for road closures (§14), and
`deploy/local-weather-awareness-cron.sh` + `deploy/install-cron.sh` for cron hosts such as
tau.kirx.net; README's "Repository layout" is current.)

```
make_weather_page.py       entry point: fetch → render maps → write HTML (one run)
weather/
  config.py                sites list, paths, intervals, UA, thresholds — env-overridable
  http.py                  GET with timeout/retry, disk cache with TTL + last-good fallback
  geo.py                   Mercator/tile maths, MRMS grid maths, point-in-polygon, bbox
  alerts.py                fetch/filter alerts, zone-shape cache, per-site classification,
                           NWS colour table
  nws.py                   points metadata, forecast, hourly, observations (unit conversion)
  radar.py                 MRMS frame discovery/fetch, basemap cache, per-site renderer
                           (radar + alert shading + decorations)
  page.py                  HTML/CSS builders (template's style family), all values escaped
  tests/                   pytest: geo maths, alert filtering/classification, colour map,
                           renderer on a synthetic frame, page render on fixture JSON,
                           end-to-end run with HTTP mocked
deploy/
  install.sh                           idempotent installer: venv-free (system python3 +
                                       Pillow), systemd service + timer, output dir,
                                       Apache snippet hint
  local-weather-awareness.service      oneshot generator (runs make_weather_page.py)
  local-weather-awareness.timer        every 5 min (`OnUnitActiveSec=5min`, jittered)
  apache-local-weather-awareness.conf  Alias /myweather → output dir, -Indexes, short
                                       Cache-Control
weather.env.example        WEATHER_OUT_DIR, WEATHER_USER_AGENT, intervals, sites override
README.md                  overview, local run, deploy, config reference, data credits
.github/workflows/ci.yml   flake8 + pytest (Pillow installed, network mocked)
```

Runtime behaviour: every fetch has a timeout; every source degrades independently (a dead
IEM leaves the previous map with a "stale since …" label, a dead NWS keeps last-good
forecast for 2 h then shows "unavailable"); output files are written atomically via
`os.replace`; a lock file prevents overlapping runs; the script exits 0 even when sources
fail so the timer keeps going; logs go to journald.

Dependencies: Python ≥ 3.9 standard library + **Pillow** only (numpy optional, not
required). No shapely/pyproj — the geometry needed here is small and is implemented in
`geo.py` with tests.

## 9. Refresh cadence and load

- Timer: every **5 min** → 1 MRMS frame (~0.6 MB) + 1 alerts request + (per site) up to
  3 NWS requests when their caches expire. Well inside NWS/IEM courtesy limits.
- Basemap tiles: ~12 tiles per site per theme, once ever (cached PNG composites).

## 10. Local development and verification

```bash
cd local-weather-awareness
python3 make_weather_page.py --out ./out --verbose      # one run, live data
python3 -m http.server -d ./out 8000                    # open http://localhost:8000/
python3 -m pytest weather/tests -q
python3 -m flake8 --max-line-length=100 --extend-ignore=E203,E501,W503,E402 weather make_weather_page.py tools
```
(`.flake8` holds the same settings, so a bare `flake8` agrees with CI.)
Verification before hand-off: real run against live data on this machine, visual check of
all ten PNGs (map extent, marker, scale bars, alert shading vs. NWS's own maps for today's
Flash Flood Warnings), HTML validated, tests green, then a multi-agent review pass
(correctness, robustness, security/escaping) with fixes applied.

## 11. Deployment on tau.kirx.net (Apache)

> Superseded (2026-09-23): tau.kirx.net runs Gentoo with OpenRC and **no systemd**, so it
> uses cron (`deploy/local-weather-awareness-cron.sh`, installed by
> `deploy/install-cron.sh`) and Gentoo's DocumentRoot (`/var/www/localhost/htdocs/myweather`
> on a stock install). See README, "Deploy on a Gentoo/OpenRC host with cron (example:
> tau.kirx.net)". The systemd steps below remain for other hosts ("Other hosts: systemd").
> This section is kept as the original plan.

Written as step-by-step instructions in `README.md` and encoded in `deploy/install.sh`:
1. `git clone` (or `rsync`) the repo to e.g. `/opt/local-weather-awareness` as an
   unprivileged user (`weather`); `apt install python3-pil fonts-dejavu-core` / `dnf install
   python3-pillow dejavu-sans-fonts` (python3-pillow is in EPEL on RHEL-family hosts).
2. Output directory `/var/www/html/myweather/` (owned by that user; SELinux label
   `httpd_sys_content_t` on RHEL-family hosts).
3. `sudo deploy/install.sh` → installs `local-weather-awareness.service` + `.timer`, runs
   once, enables the timer.
4. Apache: either nothing (if `/var/www/html` is the DocumentRoot the page is live at
   `https://tau.kirx.net/myweather/`) or drop `deploy/apache-local-weather-awareness.conf`
   into `conf.d/`/`sites-enabled` for an `Alias`, then reload.
5. Checks: `systemctl list-timers local-weather-awareness*`,
   `journalctl -u local-weather-awareness`, `curl -I`.

## 12. Decisions

1. Sun / twilight times per site: **included** (pure Python, no astropy).
2. Units: **°F + mph primary, metric in small print**.
3. URL: **https://tau.kirx.net/myweather/** → output directory `/var/www/html/myweather/`
   (on Gentoo/cron: `<DocumentRoot>/myweather`, see §11's note and README).
4. Extras (precipitation accumulation map, forecast discussion): **not now**.

## 13. Changes after review (2026-09-23)

A multi-agent review of the first complete version found bugs and gaps; all were fixed the
same day. Contract details are in DESIGN.md, operator-facing ones in README.md.

- **Flood colours (owner's request).** Inland flood products are drawn in reds instead of
  the NWS chart's greens, because green reads as "good": Flood Watch `#E53935`, Flood
  Warning `#C62828`, Flash Flood Warning `#8B0000` (official), advisories/statements
  `#FA8072`, Hydrologic Outlook `#F4A6A6`. `WEATHER_ALERT_COLORS` overrides any event.
  Three chart mismatches were fixed as well (Storm Surge Warning, Rip Current Statement,
  Hazardous Weather Outlook).
- **Alert outlines get a black/white casing**, so a red outline stays visible over red
  echoes. Default opacities lowered: alert fill 80 → 55, radar 200 → 170.
- **AT rule for polygon warnings:** only the polygon counts (§5.3). A listed county no
  longer makes a far-away site AT.
- **Legend tells the truth:** "Shaded:" lists only alerts actually drawn, "Listed, not
  shaded:" the others.
- **Impact-based threat tags** (Tornado/Flash Flood Emergency, PDS, destructive storms) are
  shown as chips. Alerts that have not started yet read "from … until …". Banner boxes name
  the issuing office and area. Cards link to the NWS hazard page.
- **Sun row** shows the current phase and every event of the next 24 h, so the dawns after
  midnight are no longer missing; times are rounded to the minute.
- **Robustness:** a per-run network budget (`WEATHER_RUN_BUDGET`, 240 s) plus a per-host
  circuit breaker bound a run when an upstream hangs. The radar frame search stops at the
  first unanswered probe. Sites carry a static time zone for when `/points` is down.
  Forecasts drop periods that have already ended, and a calm wind no longer reads "N".
  Atomic writes are fsynced.
- **Page behaviour:** a JavaScript reload keeps open sections and the scroll position.
  Only the visible theme's maps are downloaded. The day/night choice no longer pins the
  palette for good. Phone navigation is one scrollable row. Credits are linked.
- **Operations:** `status.json` separates `page_written` from `degraded`/`ok` and lists
  `problems`, including the network cause. The installer refuses paths that an env file
  would override at run time, and warns about missing Pillow or fonts. The unit recreates
  deleted output/cache directories. The README covers EPEL for Pillow, fonts, the rsync
  excludes and the local preview. `run_local.sh` stops cleanly and reports the server's
  exit status.

## 14. Road closures (2026-09-23)

The research memo (ROAD_CLOSURES.md) was followed by the owner's decisions, and version 1
was built the same day. It was tested against the live feeds but is not yet running on
tau.kirx.net. Contract details are in DESIGN.md (`weather/roads.py`, the radar road layer,
the page road block, the new config fields); viewer- and operator-facing ones are in README
("How road closures are shown").

**Owner's decisions.**
- **New Mexico only.** No Texas source at all (no DriveTexas API, no TDEM mirror, no legacy
  HCRS site). The Lubbock block says its map is outside New Mexico.
- **No ABQRoads** (Albuquerque city streets).
- **Emergencies only**: flooding, washouts, debris/rock/mud slides, fire, crashes, hazmat,
  snow/ice, wind/dust, storm damage, law-enforcement closures. Roadwork, construction and
  lane closures, maintenance, scheduled/nightly and seasonal closures, special events,
  convoys and oversize loads, White Sands missile-range alerts and rest-area closures are
  left out.
- **Drawn on the maps**, in monochrome. Hazards are never green, flood alert areas are
  already red, and the radar uses cyan..magenta. So roads use the theme's ink over a
  contrasting halo (white on black on the dark map, near-black on white on the light map),
  told apart by line style and symbol.
- tau.kirx.net is Gentoo without systemd: cron deployment, a public GitHub repository
  whose README states that this is a test of personalised local weather awareness pages
  built from public data, with the example at https://tau.kirx.net/myweather/.

**Lead's scoping calls** (each switchable):
- Water on the road is listed and drawn (`WEATHER_ROADS_WATER=1`). NMDOT files flooded
  roads as "Difficult Driving Conditions: standing water" more often than as a Closure.
- NWS Local Storm Reports from New Mexico that name a closed, flooded or impassable road,
  crossing or arroyo, from the last 24 h (`WEATHER_LSR=1`, `WEATHER_LSR_HOURS=24`). They are
  shown dated and marked "may have reopened". They catch closures NMDOT never posts, such
  as NM 304 at MM 11 on 2026-09-22.
- NMDOT Closures with no stated cause and no construction wording are shown as "cause not
  stated".

**What was built.**
- `weather/roads.py`:
  - The NMRoads JSON and RSS are joined on id == guid. Classification uses the NMDOT type
    plus strong and weak hazard words, harmless phrases, the owner's hard exclusions and
    same-sentence re-admission.
  - Ages come from the RSS dates as America/Denver time, because the JSON dates run 0 to
    7 h behind them (6 h for most events). Batch re-save stamps in the RSS "Update Date"
    are ignored (the event is aged from its post date); the JSON creation date is used,
    marked approximate, only when `rss.xml` is down.
  - After review: a sentence that says the road is closed by an emergency wins over the
    owner's hard exclusions elsewhere in the text; planned-work sentences (construction,
    scheduled, nightly, a time window, a project) never count as an emergency; sentences
    are not split after abbreviations; area-wide conditions are listed, not drawn; storm
    reports fold in corrections and duplicates and leave out White Sands Missile Range
    roads.
  - Duplicates are dropped, and only features inside the New Mexico box are kept.
  - Either file alone still gives a degraded answer.
  - Storm reports come from IEM with `states=NM`. IEM ignores `wfos=`, so the contract's
    URL returned the whole country.
- `weather/http.py`: `fetch_conditional` and `cached_bytes`. The two NMRoads files are
  fetched every run with `If-None-Match` / `If-Modified-Since` and are normally answered
  `304`. That avoids ~730 KB per run, about 210 MB a day.
- `weather/radar.py`:
  - Closures are a solid line with a "no entry" disc, water on the road a dashed line with
    a wave disc, and storm reports a triangle.
  - A key sits above the scale bars.
  - Maps without road items are byte-identical to before.
  - A triangle that a disc would hide moves just above it.
- `weather/page.py`:
  - A block per site after the alert cards, and a banner line.
  - Footer pills, credits, NMDOT's disclaimer, the scope and the excluded counts.
  - `status.json` gains `roads` and per-site counts.
  - A down, stale or incomplete feed is always said, never shown as "no closures".
- `make_weather_page.py`: a road step, then per-site selection. A road-layer failure
  redraws the map without roads.
- `weather/config.py`: nine `WEATHER_ROADS*` / `WEATHER_LSR*` / `WEATHER_NMROADS_*`
  variables, validated at startup.
- Deployment: `deploy/local-weather-awareness-cron.sh` (POSIX sh; env files parsed, never
  sourced) and `deploy/install-cron.sh` for Gentoo/OpenRC. The README now leads with the
  Gentoo chapter.

**Live check (2026-09-23, 22:00Z).** 94 NMDOT events statewide. Left out: 47 Roadwork,
14 Alert, 9 Lane Closure, 5 Difficult Driving Conditions (muddy roads), 2 Construction
Closure, 1 Fair Driving Conditions and 2 duplicates. Kept: 6 closed and 8 water on the road
statewide. Per map:
- Albuquerque: NM 41 closed (crash), I-25 NB mm 276–277 water on the road, NM 333 standing
  water, and the NM 304 storm report.
- Socorro: area-wide standing water and the NM 304 report.
- Clovis and Fort Sumner: NM 252 water on the road and the "water over NM 252" report.
- Lubbock: outside New Mexico.

Every excluded event on the four New Mexico maps was read by hand. No emergency closure was
dropped, and no roadwork got through.

**Not done / possible later.** A "mud" kind for "4x4 recommended" advisories; Texas (only
via a DriveTexas API key, see ROAD_CLOSURES.md option C); a corridor filter.

## 15. Configurable sites (2026-09-23)

**Owner's decisions.** The project is named for its content (local-weather-awareness);
tau.kirx.net is only the example deployment. The list of cities will change: nothing
outside the default-configuration table, the example sites file, the tests and the docs
examples may depend on the current five sites. Road closures stay New Mexico only,
emergencies only.

**What was built.**
- `WEATHER_SITES_FILE`: one `slug|Name|lat|lon[|tz]` entry per line, `#` comment lines,
  checked by `parse_sites`. Precedence `--sites` > `WEATHER_SITES` > `WEATHER_SITES_FILE`
  > the built-in list. An unreadable or malformed file is a configuration error (exit 2,
  naming the file and line), never a silent fall-back. `sites.example` holds the five
  sites with a header on the format, choosing coordinates, the optional `tz` and the
  limits. The installers need no change: the file is just another env-file setting.
- `DEFAULT_SITES` in `weather/config.py` is labelled as the example deployment's
  configuration; the README's site table likewise.
- Title: `WEATHER_TITLE` now defaults to empty, and the page uses `cfg.page_title`: "Local
  weather: " + the names up to their first comma, "+N more" beyond 80 characters, full
  names for a repeated short name. `<title>`, masthead and README agree.
- Alert areas: `WEATHER_ALERT_AREAS` defaults to `auto`, which queries every state or
  territory whose bounding box touches a site's map. The boxes, in the new
  `weather/states.py`, come from the Census Bureau's 2024 TIGER/Line state file (56
  entities, rounded outward to 0.01°, Alaska split at the antimeridian);
  `tools/state_bboxes.py` regenerates them, and the 2024 cartographic boundary file agrees
  within 0.1°. Explicit lists still work and are now validated (two-letter codes). The run
  log names the resolved list. `auto` for maps outside the US is a startup error.
  - Example sites: NM, OK, TX. Oklahoma's box (panhandle at 103.00° W, Red River at
    33.61° N) covers the Lubbock and Clovis maps although no Oklahoma land is on them;
    harmless, since alerts are kept by their geometry.
  - Denver + Flagstaff: AZ, CO, NE (the Denver map's east edge, 104.049° W, is just inside
    Nebraska's box, whose west edge is set by its panhandle further north).
  - Four Corners: AZ, CO, NM, UT.
- Road closures only when needed: when no site's map reaches New Mexico, NMDOT and the
  storm reports are not fetched, the page has no road block, banner line, pill or credit,
  and `status.json` reports `"roads": "not applicable"`. A map outside New Mexico next to
  one inside keeps its one-line note. The storm-report query stays `states=NM` and the
  NMDOT dates stay America/Denver: both sources are New Mexico's.
- Radar: a map wholly outside the MRMS grid (Alaska, Hawaii, Puerto Rico, territories)
  says "no radar here: MRMS covers only the contiguous US" instead of looking rain-free.
- The night default and the masthead clock still follow the first site, by design.
- Tests: `weather/tests/test_sites.py` (26 tests) and three whole-run tests in
  `test_main.py`, including an `ast` scan that no string constant in the code names an
  example site.

**Live check (2026-09-23, 23:47Z).** Denver + Flagstaff: exit 0, 4 maps, alert areas
AZ,CO,NE (9 active, a Flood Advisory near Denver shaded on its map), no request to
nmroads.com or to the storm-report feed, no road block, title "Local weather: Denver ·
Flagstaff", `status.json` ok with `"roads": "not applicable"`. The five example sites
right after: exit 0, sites ok 5/5, alert areas NM,OK,TX (16 active), NMDOT and storm
reports fetched, road blocks on all five maps (Lubbock's "outside New Mexico"), the
banner line "0 closed, 4 water on road", `status.json` ok.

## 16. Generality review (2026-09-23)

A review of what still assumed the example deployment or its five sites, with live runs for
other places (one site, eight sites, Houston, Honolulu + Anchorage, Pago Pago, the
Aleutians, a moved site, sign typos, Texas towns near New Mexico). Changes:

- **Nothing names the example host by default.** `WEATHER_PAGE_URL` defaults to empty (the
  footer then shows no address); the installers print `WEATHER_PAGE_URL` from the env
  files, else a guess from the host name, never tau.kirx.net. The README's tau.kirx.net
  env file sets it. The unit comment no longer counts the five sites' ten maps, and
  `weather.env.example` no longer repeats the five sites.
- **The page carries its own disclaimer** (not official, not the only source for safety
  decisions, use weather.gov): in the footer and under the alerts banner, whatever the
  configuration. NMDOT's disclaimer stays with the road data.
- **NWS caches are keyed by the point, not the slug** (`points/<lat>,<lon>`, likewise
  `forecast/`, `hourly/`, `stations/`): a site that keeps its slug but moves used to be
  served the old place's grid, zones, time zone and forecast for up to 7 days.
- **Marine alert areas.** api.weather.gov keeps marine zones out of the state feeds, so a
  lakefront or coastal map never showed a Small Craft Advisory or Special Marine Warning.
  `auto` now adds the NWS marine areas (LM, PZ, AM, ...) whose zones may lie on a map:
  `MARINE_BOXES`, made from the NWS coastal and offshore zone files by `tools/state_bboxes.py
  --marine` (zone boxes merged per area while that adds at most 1 square degree; 192 boxes,
  no inland map among the five example sites, Denver, Flagstaff, Tucson or Atlanta gets
  one). `WEATHER_ALERT_AREAS` also takes `auto` plus codes (`auto,PK`).
- **An explicit area list that misses a site** (the site list changed, the area list did
  not) is reported: a startup warning per site, and per run an error naming the site's
  NWS zone and state, so status.json is degraded instead of the page saying "No NWS
  alerts".
- **Site checks at startup**, each naming the file and line or the entry: a map that
  reaches no US state or territory (a longitude without its minus sign), a map across the
  180th meridian (the renderer cannot draw it; it came out 600 × 1 px), slugs that repeat
  in any case (map file names collide on macOS), `WEATHER_SITES` or `--sites` set to only
  `;` (was a silent fall-back). `--sites` now also works while `WEATHER_SITES` is
  malformed.
- **Page anchors are prefixed** (`#site-<slug>`, `#roads-<slug>`): slug `page` (Page, AZ),
  `alerts` or `modebtn`, or `x` next to `x-roads`, duplicated ids of the page.
- **Road closures follow New Mexico's outline**, not its box: a 12-vertex outline from the
  Census polygon, padded by 0.01° (every Census vertex inside). Texas maps inside the box
  (Van Horn, Fort Stockton, Olton) no longer fetch NMDOT or show a New Mexico block.
- **Radar "not applicable"**: when no map reaches the MRMS grid (Hawaii, Alaska, the
  territories) the frame is not fetched and neither the page nor status.json calls the
  radar a problem.
- **A point without a forecast grid** (American Samoa: zones and a time zone, no grid) is
  valid metadata; forecasts and observations are "not provided", not errors.
- **Zone outlines keep half the run budget**: new outlines are fetched only while half the
  network budget is left, so a first run during a statewide event still gets the
  forecasts; the next runs fetch the rest.
- CI runs on ubuntu-24.04 (setup-python has no 3.9 for 26.04) with the Node 24 actions.

Not changed: no `LICENSE` yet (the owner's decision; the README says so).
