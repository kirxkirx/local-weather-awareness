# local-weather-awareness

**This repository is a test of how public weather data can be used to create personalized
local weather situation awareness pages.**

- **Example.** An example is deployed at https://tau.kirx.net/myweather/. It covers five
  sites in New Mexico and West Texas: radar, NWS alerts, forecasts, sun times and, for New
  Mexico, road closures caused by emergencies such as flooding. The sites are
  configuration: any places in the US work (see [Sites](#sites)).
- **Not an official product.** This page is not from, or endorsed by, NOAA, the National
  Weather Service, NMDOT or any other agency. **It must not be the only source for safety
  decisions.** It can be late, incomplete or wrong whenever an upstream feed is. For
  official forecasts, watches and warnings use [weather.gov](https://www.weather.gov/). For
  New Mexico road conditions use [nmroads.com](https://nmroads.com/).
- **Crucial: put your real e-mail address in `WEATHER_USER_AGENT`.** The deployment
  examples below show the placeholder `you@example.org`. It is absolutely crucial to
  replace it with a real e-mail address before the first run: with the placeholder left
  in, OpenStreetMap blocks every map tile request (the maps show an "Access blocked" tile)
  and the generator refuses to start. See
  [step 5](#5-configuration-etclocal-weather-awarenessenv).
- **Origin.** The code is derived from
  [github.com/kirxkirx/ttustatus](https://github.com/kirxkirx/ttustatus), an observatory
  status page. It keeps that page's style, the MRMS radar and basemap machinery, the NWS
  client and the deploy layout. Every Raspberry-Pi sensor, camera, GPS/NTP,
  Weather-Underground and safety-monitor piece was removed.

A Python 3.9+ script (standard library + Pillow) runs every 5 minutes. It fetches public
NOAA data, renders one radar map per site and theme, and writes a single self-contained
`index.html`, which a web server serves as plain files. On the example host, tau.kirx.net
(Gentoo Linux with OpenRC, no systemd), cron runs the script and Apache serves the page (see
[Deploy](#deploy-on-a-gentooopenrc-host-with-cron-example-taukirxnet)). A systemd timer is
included for [other hosts](#other-hosts-systemd). Nothing on the page is a control input;
it is for people to read.

## What the page shows

Per site, top to bottom:

- **Radar map**, 100 × 100 statute miles centred on the site (Web Mercator, so the 10 mi and
  25 mi scale bars are valid in every direction): OpenStreetMap basemap, NOAA MRMS
  composite reflectivity (NWS-style dBZ palette, ≥ 5 dBZ), **active NWS alert areas** shaded
  in the NWS watch/warning colours, except flood products, which are drawn in reds (see
  [Colours](#how-alerts-are-shown)), each outline with a black/white casing, plus the site
  marker, frame time (UTC + local) and attribution. **Road closures** in New Mexico that
  were caused by an emergency, **water on the road** and NWS storm reports of closed or
  flooded roads are drawn on top in black and white (see
  [How road closures are shown](#how-road-closures-are-shown)). Both a dark and a light map
  are rendered. The page shows the one matching the theme and downloads the other only when the
  viewer switches themes.
- **Now**: the newest report from the nearest reporting NWS station (temperature, dew point,
  humidity, wind/gust, pressure, visibility, sky, station and report age).
- **Alert cards** for every alert AT or NEAR the site (see [How alerts are shown](#how-alerts-are-shown)).
- **Road closures (New Mexico, emergencies only)**: NMDOT closures caused by flooding,
  washouts, slides, fire, crashes, hazardous materials, snow and ice, wind and dust, storm
  damage or law enforcement, water on the road, and recent NWS storm reports of closed or
  flooded roads, each dated. Roadwork is left out. A map outside New Mexico says so in one
  line (in the example deployment, Lubbock's); when no site's map reaches New Mexico, the
  block is not shown at all (see [How road closures are shown](#how-road-closures-are-shown)).
- **Sun & twilight** (pure-Python NOAA equations), in the site's time zone, which the
  heading names: "Sun now <altitude>° (<phase>)", where the phase is day, civil, nautical or
  astronomical twilight, or night. Then today's sunrise, solar noon and sunset, and a
  "Next 24 h" row listing every dawn, sunrise, noon, sunset and dusk from 30 min ago to 24 h
  ahead in time order, with past ones greyed. So after midnight the coming dawns are always
  listed. Times are rounded to the nearest minute, as in the NOAA and USNO tables.
- **Next 24 hours** as a table and the **7-day forecast** as day/night cards starting with
  the current period (periods that have already ended are dropped), from NWS.

Page-wide:

- A masthead with the page title, the generation time and a day/night toggle. The title is
  made from the site names ("Local weather: Lubbock · Clovis · …", see
  [Sites](#sites)) unless `WEATHER_TITLE` sets one. The clock shows the first site's time
  zone, and the palette follows the sun at the first site (night below −6°); a viewer's
  choice holds until that default next changes.
- A sticky site navigation with alert-count badges. On phones under 600 px it is one
  horizontally scrollable row.
- An alerts banner across all sites, with one extra line counting the emergency road
  closures and water-on-road items in New Mexico when there are any on the maps, and a
  short line saying that the page is not an official NWS product (for warnings, use
  weather.gov).
- A footer with per-source freshness, linked credits, the page's disclaimer (not an
  official product, not the only source for safety decisions) and the page's address when
  `WEATHER_PAGE_URL` is set.

The page reloads itself every 5 minutes (`WEATHER_REFRESH_SECONDS`) with a little JavaScript
that keeps open sections and the scroll position. Without JavaScript, a `<noscript>` meta
refresh does the reload. The only external assets are the NWS forecast icons. Units: °F and
mph primary, °C and km/h in small print (`WEATHER_UNITS`).

## Sites

The site list is configuration: a deployment for other places changes its settings, never
the code. The five sites below are the **example deployment's configuration**
(tau.kirx.net). They are also the built-in list the generator uses when no other is
configured, and `sites.example` holds the same list as a sites file.

### Example configuration (tau.kirx.net)

Town centres. The NWS grid, zones, time zone and station list are not configured: they
come from `api.weather.gov/points` at run time (cached 7 days). The time zone column is
also each site's optional static `tz`, used only when `/points` is unavailable, so the
sun row and map captions never fall back to UTC.

| # | Site | Lat, Lon | NWS grid | Time zone | Nearest radar | Forecast / county / fire zones |
|---|------|----------|----------|-----------|---------------|--------------------------------|
| 1 | Lubbock, TX | 33.5779, −101.8552 | LUB 49,33 | America/Chicago | KLBB | TXZ035 / TXC303 / TXZ035 |
| 2 | Clovis, NM | 34.4048, −103.2052 | ABQ 222,82 | America/Denver | KFDX | NMZ235 / NMC009 / NMZ126 |
| 3 | Fort Sumner, NM | 34.4717, −104.2456 | ABQ 184,87 | America/Denver | KFDX | NMZ237 / NMC011 / NMZ126 |
| 4 | Socorro, NM | 34.0584, −106.8914 | ABQ 86,76 | America/Denver | KABX | NMZ220 / NMC053 / NMZ106 |
| 5 | Albuquerque, NM | 35.0844, −106.6504 | ABQ 98,121 | America/Denver | KABX | NMZ219 / NMC001 / NMZ106 |

(Verified against api.weather.gov on 2026-09-23.) For these five maps the alert query
resolves to NM, OK and TX, and the title is "Local weather: Lubbock · Clovis · Fort
Sumner · Socorro · Albuquerque".

### Changing the sites

Each site is one entry `slug|Name|lat|lon[|tz]`:

- `slug`: letters, digits, `_` and `-`, unique without regard to case (`a` and `A` would
  name the same map file on macOS). It names the map files (`radar_<slug>_dark.png`) and
  the page anchors (`#site-<slug>`, and `#roads-<slug>` for a road block).
- `Name`: shown on the page. The part before the first comma goes into the title.
- `lat`, `lon`: WGS84 decimal degrees, north and east positive (US longitudes are
  negative). The map is centred there and the NWS forecast is the one for that point. In
  OpenStreetMap, right-click the spot and choose "Show address" to see its coordinates. A
  site whose map reaches no US state or territory (typically a longitude without its minus
  sign) is refused, and so is a map across the 180th meridian (the western Aleutians near
  Amchitka; a smaller `WEATHER_MAP_MILES` or a site a little further east avoids it).
- `tz` (optional): an IANA time zone such as `America/Denver`, a fallback for when NWS
  `/points` is unavailable. An unknown name is a startup error.

Put the list in `/etc/local-weather-awareness.env` in one of two ways:

- **A sites file**, one entry per line, blank lines and `#` comment lines allowed.
  `sites.example` explains the format in its header:

  ```bash
  mkdir -p /etc/local-weather-awareness
  cp <checkout>/sites.example /etc/local-weather-awareness/sites
  chmod 0644 /etc/local-weather-awareness/sites    # then edit it
  echo 'WEATHER_SITES_FILE=/etc/local-weather-awareness/sites' >> /etc/local-weather-awareness.env
  ```

- **One variable**, entries separated by `;`:

  ```
  WEATHER_SITES=denver|Denver, CO|39.7392|-104.9903;flagstaff|Flagstaff, AZ|35.1983|-111.6513
  ```

The first of these that is set wins: `--sites` on the command line, `WEATHER_SITES`,
`WEATHER_SITES_FILE`, the built-in list. Only the one used is read, so `--sites` also works
while `WEATHER_SITES` is malformed. An unreadable or malformed file or entry, a repeated
slug, a site outside the US or an empty list (also a `WEATHER_SITES` or `--sites` that
holds only `;`) stops the run with exit status 2 and a log line that names the file and
line, or the entry (`WEATHER_SITES entry 2: site 'oops' (32.7026, 103.136): its map reaches
no US state or territory …`); the generator never falls back to other sites. The first
log line of a run says where the list came from, e.g.
`run started: 2 site(s) from WEATHER_SITES_FILE /etc/local-weather-awareness/sites, …`.

The next run uses the new list; nothing has to be reinstalled or restarted. Its first run
fetches the basemap tiles of the new maps, which takes a minute or two. The NWS caches are
keyed by the site's coordinates, not its slug, so a site that keeps its slug but moves gets
the forecast, zones and time zone of its new place at once. Maps of removed sites stay in
the output directory, no longer linked, until you delete them.

**What adapts automatically:**

- **NWS grid, zones, station list and time zone**: from `/points` for each site.
- **Alert areas**: `WEATHER_ALERT_AREAS=auto` (the default) asks
  `api.weather.gov/alerts/active` for every state, territory and NWS marine area whose
  bounding box touches one of the maps. The state boxes are those of the US Census
  Bureau's 2024 TIGER/Line state boundaries (50 states, DC, Puerto Rico and the four
  Island Areas). The marine areas matter on a coast or a Great Lake: a state's feed has no
  marine zones, so a Small Craft Advisory, Gale Warning or Special Marine Warning on Lake
  Michigan is only in the area `LM`, off the West Coast only in `PZ`. Their boxes come from
  the NWS coastal and offshore marine zone files, boxed per zone and merged per area while
  that adds little, because one box per area would reach far inland. Both tables are in
  `weather/states.py`, made with `tools/state_bboxes.py`. The run log names the result,
  e.g. `alert areas: AZ,CO,NE (auto: …)`; a Chicago map gives `IL,IN,LM,MI,WI`, Honolulu's
  `HI,PH`. A box is larger than its state, so a neighbour can be included whose land is
  not on any map: Oklahoma's box (panhandle to Red River) covers the Lubbock and Clovis
  maps, and Nebraska's (its panhandle reaches 104.05° W) touches the edge of a Denver map.
  That only adds a few alerts to the download, because each alert is kept for a site by
  its own geometry. Codes listed with `auto` are added (`auto,PK`). An explicit list such
  as `NM,TX` still works, but when a site lies in a state the list leaves out (the site
  list changed, the area list did not), the run log warns at startup, and every run
  reports it as a problem in the page footer and `status.json`, naming the site's NWS zone.
- **Maps**: each centred on its site, basemap tiles fetched on the first run.
- **Page title**: "Local weather: " and the names up to their first comma, joined with
  " · "; beyond 80 characters as many names as fit, then "+N more". Two sites with the same
  short name keep their full names. `WEATHER_TITLE` sets a title of your own.
- **Navigation and order**: the page lists the sites in the configured order. The first
  site's time zone is the masthead clock, and the sun there picks the day or night palette.

**What is region-specific:**

- **US only.** Forecasts, observations and alerts come from the US National Weather
  Service, which covers the 50 states, DC, Puerto Rico and the US territories. A site whose
  map touches none of them is refused at startup (exit status 2). NWS has no forecast grid
  for American Samoa: there the page shows alerts, sun times and local times, and says
  that NWS provides no forecast or observations for the location (not an error).
- **Radar: contiguous US.** The NOAA MRMS composite spans 130° W–60° W and 20° N–55° N. A
  map wholly outside it (Alaska, Hawaii, Puerto Rico, the territories) is drawn without
  radar and says "no radar here: MRMS covers only the contiguous US". When no site's map
  reaches it, the frame is not fetched at all, and `status.json` reports
  `"radar": "not applicable"` rather than a problem.
- **Road closures: New Mexico only** (the owner's decision, see
  [How road closures are shown](#how-road-closures-are-shown)). Whether a map reaches New
  Mexico is decided by the state's outline, not its bounding box, so a Texas map south of
  32° N (Van Horn) or just east of the state line (Olton) does not. A map outside New Mexico
  says so in one line while another site's map reaches New Mexico. When no site's map does,
  NMDOT and the storm reports are not fetched at all, no road block, banner line, footer
  pill or credit appears, and `status.json` reports `"roads": "not applicable"`.

## Data sources and credits

All sources are free and need no API key. `api.weather.gov` requires a `User-Agent` that
identifies the application, and NWS asks for a contact in it: every deployment adds its
operator's e-mail address to `WEATHER_USER_AGENT` (see
[step 5](#5-configuration-etclocal-weather-awarenessenv)). That address must be real:
OpenStreetMap's tile servers block any User-Agent with a placeholder such as
`you@example.org`, and no basemap tile can then be fetched.

| Data | Source | Fetched / cached |
|------|--------|------------------|
| Alerts | NOAA/NWS `api.weather.gov/alerts/active?area=…` for the states, territories and marine areas on the maps (`NM,OK,TX` for the example sites; see [Sites](#changing-the-sites)) (CAP fields, polygon geometry for polygon-based products) | every run; last good copy reused for 20 min |
| Alert zone shapes | `api.weather.gov/zones/{forecast,county,fire}/{id}` (zone-based products carry no polygon) | on first sight of a zone id, then cached for a year; new ones only while half of the run's network budget is left, the rest in the next runs |
| Site metadata | `api.weather.gov/points/{lat},{lon}`: grid, time zone, zones, station list | cached 7 days, per location |
| 7-day forecast | `…/gridpoints/{grid}/forecast` | cached 30 min (last good kept 2 h) |
| Hourly forecast | `…/gridpoints/{grid}/forecast/hourly` | cached 30 min (last good kept 2 h) |
| Current conditions | `…/stations/{id}/observations/latest` for the first ≤ 3 stations of the grid | cached 10 min (last good kept 1 h) |
| Radar | NOAA/NSSL **MRMS composite reflectivity**, CONUS PNG from the Iowa Environmental Mesonet archive (`GIS/mrms/lcref_YYYYMMDDHHMM.png`, 7000 × 3500 px, 0.01° grid, 0.5 dBZ steps) | one frame per run, shared by all sites (none when no map reaches the grid); last frame reused up to 30 min |
| Road closures (NM) | NMDOT's open NMRoads feed: `nmroads.com/nmroads.json` (GeoJSON, road-following lines) joined with `nmroads.com/rss.xml` (the cause text and the update times) | every run in which a site's map reaches New Mexico, with a conditional GET (`If-None-Match` / `If-Modified-Since`), usually answered `304 Not Modified` without a body; last good copy reused for 1 h |
| Storm reports (NM) | NWS Local Storm Reports via the Iowa Environmental Mesonet, `mesonet.agron.iastate.edu/geojson/lsr.geojson?hours=24&states=NM` | every run in which a site's map reaches New Mexico (~10 KB); last good copy reused for 1 h |
| Basemap | OpenStreetMap standard raster tiles (`tile.openstreetmap.org`), zoom 9; the dark theme inverts their lightness (hue kept) because no key-free dark style with labels exists (CARTO's free rasters now return "API KEY REQUIRED") | once per site and theme, composited and cached on disk indefinitely |

Credits and attribution requirements. Every PNG carries the attribution text from
`weather/config.py`: *© OpenStreetMap contributors · Radar: NOAA/NSSL MRMS via Iowa
Environmental Mesonet · Forecast & alerts: NOAA/NWS*. The page footer carries the same
credits as fixed HTML with links to the OpenStreetMap copyright page, IEM and weather.gov,
as the OSM attribution guidelines ask for on web pages.

- **NOAA / National Weather Service** (api.weather.gov): US government data, public domain.
  Keep the `User-Agent` and the request rate modest (with the five example sites this page
  makes at most ~20 requests per 5-minute run, most of them served from the local cache;
  each site adds up to 4).
- **NMDOT / NMRoads.com** road information: the feed declares itself public domain
  ([CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)), publisher NMDOT. The page
  credits NMDOT / NMRoads.com and repeats NMDOT's disclaimer: the information may not reflect
  all incidents or road conditions and is not to be your only source. Two conditional
  requests per run; NMDOT's server answers `304` while nothing changed.
- **NWS Local Storm Reports** are public-domain NWS data, fetched from the **Iowa
  Environmental Mesonet**'s GeoJSON service (credited on the page), one small request per
  run.
- **NOAA/NSSL MRMS** radar via the **Iowa Environmental Mesonet** (Iowa State University),
  which republishes the composite as a georeferenced PNG archive as a courtesy service.
  Credit IEM, fetch one frame per run, never poll faster than the 2-minute product cadence.
- **OpenStreetMap** data is © OpenStreetMap contributors under the ODbL; the credit line is
  mandatory wherever the map is shown. The standard tiles from `tile.openstreetmap.org` are
  run by volunteers under the [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/):
  a User-Agent that identifies the application (`WEATHER_USER_AGENT`) and light use. Each
  tile is **downloaded once and kept in the disk cache for good** (both themes and every
  rebuild reuse it), so the five example sites cost about 45 tile requests in total, ever,
  not per run. OpenStreetMap **blocks** a User-Agent that is a library default, imitates a
  browser, or carries a placeholder contact such as `example.org` (checked 2026-09-24: the
  same string with a real address gets real tiles); its "Access blocked" tile comes with
  HTTP 200 and `Cache-Control: no-cache`. That reply, and any 403/418/429, is never
  cached: the build stops at the first refusal, logs an error, draws the maps on a plain
  background with a note, and makes no tile request for 6 hours unless the User-Agent or
  tile URL changes. A placeholder contact in `WEATHER_USER_AGENT` stops the run at start-up
  with a configuration error. Any other
  `{z}/{x}/{y}` raster source can be configured (`WEATHER_TILE_URL_DARK` / `_LIGHT`); a
  genuinely dark tile set is left as served (`WEATHER_TILE_DARK_INVERT=0` forces that).

## Repository layout

```
make_weather_page.py       entry point: fetch → render maps → write HTML (one run)
weather/
  config.py                sites (the example list, WEATHER_SITES, WEATHER_SITES_FILE), paths,
                           intervals, URLs, thresholds, derived title; WEATHER_* overridable
  states.py                US state/territory (Census) and NWS marine-area bounding boxes for
                           WEATHER_ALERT_AREAS=auto
  http.py                  GET with timeout/retry, disk cache with TTL + last-good fallback
  geo.py                   Mercator/tile maths, MRMS grid, point-in-polygon, bounding boxes
  util.py                  time parsing/formatting, unit conversion
  alerts.py                fetch/filter alerts, zone-shape cache, AT/NEAR classification, colours
  roads.py                 New Mexico emergency road closures (NMRoads) and NWS storm reports
  nws.py                   points metadata, forecast, hourly, observations
  sun.py                   sunrise/sunset/twilight (NOAA solar equations)
  radar.py                 MRMS frame discovery/fetch, basemap cache, per-site map renderer
  page.py                  HTML/CSS builders (template's style family), all values escaped
  tests/                   pytest, network blocked: fixtures replace weather.http
deploy/
  local-weather-awareness-cron.sh      cron wrapper (POSIX sh): env files, nice/timeout,
                                       syslog or log file
  install-cron.sh                      idempotent cron installer (Gentoo/OpenRC or any cron host)
  install.sh                           idempotent systemd installer: dirs, SELinux label,
                                       unit + timer
  local-weather-awareness.service      oneshot generator (template; placeholders filled by
                                       install.sh)
  local-weather-awareness.timer        every 5 min, jittered
  apache-local-weather-awareness.conf  Alias /myweather → output dir, no listings or foreign
                                       symlinks, short Cache-Control
weather.env.example        every WEATHER_* variable with its default
sites.example              the example deployment's sites as a WEATHER_SITES_FILE, format explained
tools/state_bboxes.py      regenerates weather/states.py's tables from the Census and NWS shapefiles
run_local.sh               local preview: regenerate every 5 min and serve ./out on port 8080
DESIGN.md                  module contract (signatures and dict shapes)
PLAN.md                    the original plan and the decisions taken
ROAD_CLOSURES.md           research memo on road-closure data sources
.github/workflows/ci.yml   flake8 + pytest on Python 3.9 and 3.12
.flake8                    flake8 settings (the same as CI's command line)
```

## Run locally

```bash
git clone https://github.com/kirxkirx/local-weather-awareness.git
cd local-weather-awareness
python3 make_weather_page.py --out ./out --verbose      # one run against live data
python3 -m http.server -d out 8000                      # then open http://localhost:8000/
```

Needs Python ≥ 3.9 and Pillow. On Gentoo: `emerge dev-python/pillow media-fonts/dejavu`
(see [Packages](#1-packages)). On Debian/Ubuntu: `apt install python3-pil`. On RHEL-family
hosts: `dnf install python3-pillow` from EPEL (see [Other hosts](#other-hosts-systemd)).
Anywhere: `python3 -m pip install --user pillow`. The map labels also need a TrueType font,
such as DejaVu Sans (`media-fonts/dejavu`, `fonts-dejavu-core` or `dejavu-sans-fonts`).
Without Pillow the page is still generated, but without maps. The cache lives in
`~/.cache/local-weather-awareness` (`--cache DIR`).

Useful options:

- `--skip-radar` saves the MRMS download.
- `--sites 'slug|Name|lat|lon[|tz];…'` overrides the site list for one run. For a lasting
  list set `WEATHER_SITES` or `WEATHER_SITES_FILE` (see [Changing the sites](#changing-the-sites)).

The first run fetches the basemap tiles for every site and theme and takes a minute or two.
Later runs take seconds.

### Local preview (`run_local.sh`)

`run_local.sh` works like the production cron job and Apache together. It regenerates the
page every 5 minutes in a background loop and serves it with Python's built-in HTTP server:

```bash
./run_local.sh                  # port 8080, output ./out, cache ./.cache-dev
./run_local.sh 8090 /tmp/wx     # another port and output directory
BIND=127.0.0.1 ./run_local.sh   # reachable from this machine only
```

- Arguments: `[port] [out_dir]`, defaults `8080` and `./out`.
- `INTERVAL`: seconds between runs (default 300). The first run starts at once.
- `BIND`: listen address (default `0.0.0.0`, every interface), so the page also opens from
  other machines as `http://<host>:8080/`.
- Any `WEATHER_*` variable is passed to the generator. The cache is `./.cache-dev` unless
  `WEATHER_CACHE_DIR` is set; the User-Agent defaults to
  `local-weather-awareness-dev (+https://github.com/kirxkirx/local-weather-awareness)`.
- It prints the URL, then the generator's log lines. Ctrl-C stops the server and the loop,
  including a generator run in progress.
- Exit status: the server's own status when it stops by itself (e.g. `1` when the port is
  already in use), or 128 + signal when interrupted (130 for Ctrl-C).

## Tests

```bash
pip install pytest flake8 pillow          # or the distro packages
python3 -m pytest weather/tests -q
python3 -m flake8 --max-line-length=100 --extend-ignore=E203,E501,W503,E402 weather make_weather_page.py tools
sh -n deploy/local-weather-awareness-cron.sh
for f in deploy/install-cron.sh deploy/install.sh run_local.sh; do bash -n "$f"; done
shellcheck deploy/*.sh run_local.sh       # optional; the suite runs it when it is installed
```

The test suite blocks `urllib` outright. Every module fetches through `weather.http`, which
the `fake_http` fixture replaces with an in-memory registry.

`weather/tests/test_roads.py` classifies trimmed real NMRoads and storm-report payloads
from 2026-09-23 (roadwork, construction closures, flood closures, standing water, alerts,
duplicates, junk route names and every RSS date format), so a change to the keyword lists
shows at once which real events move in or out. It also checks which maps reach New
Mexico's outline (Van Horn and Olton, Texas, do not; Farwell and Pecos do).

`weather/tests/test_sites.py` covers the configurable site list: the sites file and its
error messages, the precedence of `--sites`, `WEATHER_SITES` and `WEATHER_SITES_FILE`, the
startup checks (a site outside the US, a map across the 180th meridian, slugs repeated in
any case, a list of only `;`), the derived title, the auto alert areas for the example
sites (NM, OK, TX), for Denver + Flagstaff (AZ, CO, NE), the Four Corners (AZ, CO, NM,
UT), Chicago (with Lake Michigan), Hawaii, Puerto Rico, Guam, American Samoa and the
Aleutians (with their marine areas), `auto` plus codes, the report of an explicit list
that leaves out a site's state, the Census and marine tables and their generator, and that
no string in the code names one of the five example sites. `test_nws.py` moves a site
without changing its slug (its new metadata is fetched at once) and covers a point with no
forecast grid. `test_main.py` runs the whole generator for Denver + Flagstaff (no road
request, no road block, `"roads": "not applicable"`), Honolulu alone (no MRMS request,
`"radar": "not applicable"`), Honolulu next to Denver (the MRMS note), Pago Pago (no
forecast grid, still a healthy run) and a site outside an explicit area list.

`weather/tests/test_deploy.py` covers the operational files:

- `weather.env.example` lists every `WEATHER_*` variable with the default that `config.py`
  really uses.
- The unit templates and both installers agree with `config.py` and with each other.
- `DRY_RUN=1` renders of both installers: the crontab line (an older
  `# local-weather-awareness` line is replaced, other lines kept), the Gentoo DocumentRoot
  default, the refusal of a path that an env file overrides, and the append to
  `/etc/local-weather-awareness.env` that leaves existing lines alone.
- The cron wrapper runs against a stub generator:
  - An env file line such as `X=$(touch /tmp/pwned)` is exported as text and never runs.
  - Comments and malformed lines are skipped.
  - Output goes to syslog through `logger`, or to a log file with the 1 MB rotation.
  - `nice` and the timeout are applied.
  - The exit status is passed through.
- The installers' check commands use `WEATHER_PAGE_URL` (else the host name), never the
  example deployment's host, and CI runs on a pinned Ubuntu image that has Python 3.9.
- The published files contain no internal host names, personal paths or e-mail addresses.

CI (GitHub Actions) runs the same on every push.

## Deploy on a Gentoo/OpenRC host with cron (example: tau.kirx.net)

The example deployment, tau.kirx.net, runs Gentoo Linux with OpenRC and no systemd, so
**cron** runs the generator. The checkout lives in the home of an ordinary admin account
(`kirx` there) and the generator runs as `apache`, which owns Gentoo's web root, so it can
write the output directory without any hand-over. `deploy/install-cron.sh` puts one line
into `apache`'s crontab:

```
*/5 * * * * /home/kirx/local-weather-awareness/deploy/local-weather-awareness-cron.sh # local-weather-awareness
```

Apache then serves the output directory as https://tau.kirx.net/myweather/. The same steps
work on any host with a cron daemon; use your own host name wherever tau.kirx.net appears.

| What | Where |
|------|-------|
| checkout | `~kirx/local-weather-awareness`: your own account owns it and runs `git pull`; the run user only reads it |
| run user | `apache`, the owner of the web root (a dedicated account is stricter, see step 3) |
| output | `<DocumentRoot>/myweather`, on a stock Gentoo Apache `/var/www/localhost/htdocs/myweather` |
| cache | `~apache/.cache/local-weather-awareness` (`apache`'s home is `/var/www`, outside `htdocs`) |
| configuration | `/etc/local-weather-awareness.env`, optionally overridden by `~apache/local-weather-awareness.env` |
| logs | syslog, tag `local-weather-awareness` (a log file when there is no syslog, see step 6) |

**Run the commands below in a root shell** (`su -`). A Gentoo stage3 has no `sudo`; if you
installed `app-admin/sudo`, prefix the commands with `sudo` instead. Commands that must run
as the run user are written with `su -s /bin/sh apache -c '…'`, which works from a root
shell without sudo. Steps marked *(verify on the host)* follow Gentoo's documentation and
defaults but have not been tried on tau.kirx.net itself; check them there the first time.

### 1. Packages

```bash
# Pillow's FreeType (map labels) and zlib (PNG) support are USE flags; only the desktop
# profiles turn "truetype" on, so a server profile would build Pillow without it
echo 'dev-python/pillow truetype zlib' >> /etc/portage/package.use/local-weather-awareness
emerge --ask --noreplace dev-python/pillow media-fonts/dejavu www-servers/apache \
     sys-process/cronie dev-vcs/git
rc-update add cronie default && rc-service cronie start
```

**Python.** `dev-lang/python` is already installed, because Portage needs it. The generator
needs Python ≥ 3.9 (`python3 --version`); current Portage builds Pillow for Python 3.12 and
newer only (`PYTHON_COMPAT` of `dev-python/pillow`).

**Pillow.** Pillow must be built for the Python that `/usr/bin/python3` runs, which is the
one cron uses. On Gentoo, `python3` is a python-exec wrapper that picks a version
(`eselect python`), and `dev-python/pillow` is built only for the versions listed in
`PYTHON_TARGETS`. Check as the run user, once it exists (step 3):

```bash
# as the run user, with cron's bare environment (PATH=/usr/bin:/bin)
su -s /bin/sh apache -c 'cd / && env -i HOME=/var/www PATH=/usr/bin:/bin python3 --version'
su -s /bin/sh apache -c 'cd / && env -i HOME=/var/www PATH=/usr/bin:/bin python3 -c "import PIL; print(PIL.__version__)"'
su -s /bin/sh apache -c 'cd / && env -i HOME=/var/www PATH=/usr/bin:/bin python3 -c "from PIL import features; print(features.check(\"zlib\"), features.check(\"freetype2\"))"'
```

(Replace `/home/weather` with the run user's home if it is elsewhere. The installer in step
6 runs the same checks the same way.)

- `No module named 'PIL'` means Pillow is not built for that Python version. See which
  versions have it with `ls -d /usr/lib*/python3*/site-packages/PIL`. Then do one of these:
  - Rebuild Pillow for this version. Add the target to the file of step 1,
    `echo 'dev-python/pillow python_targets_python3_13' >> /etc/portage/package.use/local-weather-awareness`
    (with the version you need), then run
    `emerge --ask --oneshot --newuse dev-python/pillow`. Portage says so if a dependency
    needs the same target.
  - Switch `python3` to a version Pillow has (`eselect python list`, then
    `eselect python set …`).
  - Leave the system default alone and point the cron wrapper at a version Pillow has:
    `WEATHER_PYTHON=python3.12` in `/etc/local-weather-awareness.env`.

  *(verify on the host)*
- The two flags must print `True True`: PNG maps need zlib, and the map labels need
  FreeType. The `package.use` line above enables both USE flags (`zlib`, `truetype`); if one
  is still `False`, check it with `emerge -pv dev-python/pillow` and re-emerge Pillow
  (`emerge --ask --oneshot --newuse dev-python/pillow`) *(verify on the host)*.

**Fonts.** `media-fonts/dejavu` installs `/usr/share/fonts/dejavu/DejaVuSans.ttf`, the first
font the map renderer looks for.

**Apache.** `www-servers/apache` needs `mod_alias` for the snippet's `Alias`, and
`mod_headers` for its short `Cache-Control` headers. Gentoo builds modules from
`APACHE2_MODULES`: both are in the default set, but a custom list in
`/etc/portage/make.conf` must contain `alias` and `headers`. Check what is built and what is
loaded *(verify on the host)*:

```bash
emerge -pv www-servers/apache | tr ' ' '\n' | grep -E 'apache2_modules_(alias|headers)'
apache2ctl -M 2>/dev/null | grep -E 'alias_module|headers_module'
grep -nE 'LoadModule (alias|headers)_module' /etc/apache2/httpd.conf
```

Without `mod_headers` the page still works: the snippet's `Header` lines sit inside
`<IfModule mod_headers.c>`. Browsers then just get no short cache lifetime.

**Cron.** Any cron daemon works: cronie, dcron, fcron or busybox crond. The installer warns
when none is running, or when none is in an OpenRC runlevel (so it would not come back after
a reboot).

**Syslog.** The wrapper logs through `logger` (sys-apps/util-linux) to the syslog daemon.
For example, `app-admin/sysklogd` (`rc-update add sysklogd default`) writes
`/var/log/messages` *(verify on the host)*. When no syslog daemon is running, `/dev/log` is
missing, and the wrapper writes `~apache/.cache/local-weather-awareness/cron.log` instead.

### 2. Find the DocumentRoot

```bash
grep -rn DocumentRoot /etc/apache2/vhosts.d/
```

Gentoo's default is `/var/www/localhost/htdocs` (in `default_vhost.include`, which both the
port-80 and the TLS default virtual hosts use). The output directory is then
`/var/www/localhost/htdocs/myweather`, and the page appears at
https://tau.kirx.net/myweather/. If tau.kirx.net has its own `<VirtualHost>` with a different
DocumentRoot, use `<that DocumentRoot>/myweather` in step 5 instead *(verify on the host)*.
Any other directory works too; the snippet's `Alias` then publishes it as `/myweather/`.

### 3. Run user

Use the account that owns the web root. On a stock Gentoo Apache that is `apache`:

```bash
ls -ld /var/www/localhost/htdocs     # drwxr-xr-x apache apache ... on tau.kirx.net
getent passwd apache                 # home /var/www: the cache goes to /var/www/.cache/...
```

No login shell is needed: cron runs the line with `/bin/sh`. Running as `apache` means the
generator and the web server share one identity. A dedicated account
(`useradd --system --create-home --shell /sbin/nologin weather`) keeps them apart; the
output directory then has to be handed to it first (see step 6).

### 4. Copy the repository

Clone it as your own account (`kirx` on tau.kirx.net), not as root:

```bash
su - kirx -c 'git clone https://github.com/kirxkirx/local-weather-awareness.git ~/local-weather-awareness'
ls -ld /home/kirx     # apache must be able to enter it: drwxr-xr-x or drwx--x--x
# if it is drwx------:  chmod 711 /home/kirx   (others may pass through, not list it)
```

Or from a workstation with rsync, run from the top of your checkout:

```bash
rsync -a --exclude out --exclude '.cache*' --exclude .pytest_cache --exclude __pycache__ \
      --exclude '*.env' --exclude '.nfs*' --exclude .git --exclude .private-patterns \
      ./ tau.kirx.net:local-weather-awareness/     # as kirx: ~kirx/local-weather-awareness
```

The excludes keep development output, caches and any local `*.env` override out of the
production copy. `weather.env.example` is still copied. The run user only reads the
checkout. It needs read access (a umask of 022 gives that) and the executable bit on
`deploy/local-weather-awareness-cron.sh` (git and `rsync -a` keep it).

### 5. Configuration: `/etc/local-weather-awareness.env`

```bash
cat > /etc/local-weather-awareness.env <<'EOF'
WEATHER_OUT_DIR=/var/www/localhost/htdocs/myweather
WEATHER_PAGE_URL=https://tau.kirx.net/myweather/
WEATHER_USER_AGENT=local-weather-awareness (+https://github.com/kirxkirx/local-weather-awareness; you@example.org)
EOF
chmod 0644 /etc/local-weather-awareness.env
```

> **Absolutely crucial: replace `you@example.org` with your real e-mail address.** Without
> a real address the OpenStreetMap map tiles cannot be fetched: OpenStreetMap blocks every
> tile request whose User-Agent carries a placeholder such as `you@example.org` (the maps
> then show an "Access blocked" tile), and the generator refuses to start with one. Check
> with `grep USER_AGENT /etc/local-weather-awareness.env` before step 6.

Edit three things in that file:

- Replace `you@example.org` with a real e-mail address where NWS and OpenStreetMap can reach
  you (see the box above). api.weather.gov requires a User-Agent and asks for a contact in
  it. The built-in default,
  `local-weather-awareness (+https://github.com/kirxkirx/local-weather-awareness)`, names
  only the project, so every deployment should add its own address this way.
- Set `WEATHER_OUT_DIR` to the DocumentRoot found in step 2, plus `/myweather`.
- Set `WEATHER_PAGE_URL` to your page's public address; the line above is the example
  deployment's. The page footer shows it, and the installer prints it in its check
  commands. Without it the footer shows no address.

Without further settings the page shows the example deployment's five sites. For your own
places add `WEATHER_SITES_FILE` or `WEATHER_SITES` here (see
[Changing the sites](#changing-the-sites)); the rest (alert areas, title, road closures)
follows from the sites.

`weather.env.example` lists every variable with its default, all commented out, so copying
it here changes nothing until you uncomment a line.

Set `WEATHER_OUT_DIR` and `WEATHER_CACHE_DIR` in `/etc/local-weather-awareness.env` only.
The installer runs as root and hands those directories to the run user, so it refuses to
take them from `~apache/local-weather-awareness.env`, a file the run user can change.

How the cron wrapper reads the files:

- It reads `/etc/local-weather-awareness.env`, then `~apache/local-weather-awareness.env`,
  before every run; the later file wins.
- The format is `KEY=value` lines and `#` comments. The files are parsed, never sourced.
  Blanks around the `=` and at the ends of the value are removed (`KEY = value` is
  `KEY=value`); the installers read the files the same way.
- Values are taken literally: `$(…)`, backquotes and `$VAR` stay plain text. One pair of
  surrounding quotes is removed. So `WEATHER_LSR_URL=…?hours={hours}&states=NM` needs no
  quotes (in a file that a shell sources, the `&` would have to be quoted).
- `export …` lines are ignored, and an empty value does not clear an earlier file's value.
- The run user must be able to read the files (mode 0644). Nothing in them is secret.

### 6. Install the cron job

```bash
RUN_USER=apache /home/kirx/local-weather-awareness/deploy/install-cron.sh
# preview only, nothing changed, no root needed:
#   DRY_RUN=1 RUN_USER=apache /home/kirx/local-weather-awareness/deploy/install-cron.sh
```

With `RUN_USER=apache` the installer creates the output directory as `apache`, which owns
`htdocs`. With a dedicated run user it cannot: the installer never creates a directory as
root inside a parent that root does not own alone, and it stops with an error that names
that parent. Create the directory yourself, hand it to that user and rerun the installer:

```bash
mkdir -p /var/www/localhost/htdocs/myweather
chown weather:weather /var/www/localhost/htdocs/myweather
chmod 0755 /var/www/localhost/htdocs/myweather
RUN_USER=weather /home/kirx/local-weather-awareness/deploy/install-cron.sh
```

Always name `RUN_USER`. Without it the installer picks the sudo caller, else the owner of the
checkout (`kirx` here), which cannot write the web root. It refuses root. It is idempotent: rerun it whenever you
like. It:

1. **Resolves the output directory**: the `OUT_DIR` argument or variable, else
   `WEATHER_OUT_DIR` from the env files, else `/var/www/localhost/htdocs/myweather` when
   `/var/www/localhost/htdocs` exists, else `/var/www/html/myweather`.
2. **Resolves the cache directory**: `CACHE_DIR`, else `WEATHER_CACHE_DIR`, else
   `~apache/.cache/local-weather-awareness`. It refuses an `OUT_DIR` or `CACHE_DIR` that
   contradicts an env file, and names that file: the wrapper loads the env files before
   every run, so they would win. It also refuses either path when it comes from an env file
   that anybody but root can change (`~apache/local-weather-awareness.env` belongs to the
   run user), and a `WEATHER_PYTHON` or `PATH` value the wrapper could not use
   (`PATH=$PATH:…` is not expanded in an env file).
3. **Checks the run user's Python**, as that user and with cron's bare environment
   (`PATH=/usr/bin:/bin`): that `python3` (or `WEATHER_PYTHON`) is ≥ 3.9 and imports
   Pillow, that Pillow has zlib and FreeType, and that a TrueType font is found. For each
   problem it prints the Gentoo fix. Every interpreter it starts runs as the run user
   (`su` + `env -i`), never as root: `WEATHER_PYTHON` may come from the run user's own file.
4. **Creates the output and cache directories**, owned by the run user, without ever giving
   the run user something it should not have:
   - symlinks are resolved first, and a system location (`/etc`, `/usr`, `/root`, `/boot`,
     `/var/log`, …, or a bare `/var/www/html`) is refused;
   - under a root-owned parent (`/var/www/localhost/htdocs/myweather`) root creates the
     directory; an existing one there is taken over only if it is the default output
     directory or already belongs to the run user;
   - in space the run user controls (`~apache/.cache/local-weather-awareness`) the run user
     creates it itself, so no root action follows a path the run user could swap for a
     symlink.
5. **Completes `/etc/local-weather-awareness.env`**: it appends the resolved
   `WEATHER_OUT_DIR` and `WEATHER_CACHE_DIR` when those lines are missing and sets mode
   0644. It never rewrites any other line.
6. **Installs the crontab line** for the run user. An older `# local-weather-awareness`
   line is replaced; every other line of that crontab is kept.
7. **Checks that a cron daemon is running**, and that it is in an OpenRC runlevel. If not,
   it prints
   `emerge --ask sys-process/cronie && rc-update add cronie default && rc-service cronie start`.
8. **Renders the Apache snippet** with the real output directory to
   `/etc/local-weather-awareness/apache-local-weather-awareness.conf`.
9. **Runs the generator once** through the wrapper as the run user, with the output on the
   terminal. The first run fetches the basemap tiles for every site and takes a minute or
   two. A failure is reported, not fatal.
10. **Prints the next steps**, with check commands for the page's address:
    `WEATHER_PAGE_URL` from the env files, else a guess from the host name.

On every cron run, `deploy/local-weather-awareness-cron.sh` does this:

1. `cd` to the checkout.
2. Load the env files as described in step 5.
3. Run `timeout 600 nice -n 10 python3 make_weather_page.py` with umask 022, so Apache can
   read the files.
4. Log the output.
5. Exit with the generator's status.

The generator takes its own lock (`<cache>/run.lock`), so a run that starts while the
previous one is still busy exits at once. The wrapper refuses to run as root. Its own
settings go in an env file:

| Variable | Default | Meaning |
|----------|---------|---------|
| `WEATHER_LOG` | unset | Unset: syslog via `logger -t local-weather-awareness` when `logger` and `/dev/log` exist, else `<cache>/cron.log`. A path: append to that file. `-`: stdout. A log file larger than 1 MB is renamed to `<file>.1` before the next run (one old copy is kept). |
| `WEATHER_PYTHON` | `python3` | interpreter, e.g. `python3.12` when Pillow is built only for that version |
| `WEATHER_TIMEOUT` | `600` | seconds before a wedged run is stopped (exit status 124); `0` = no limit |
| `WEATHER_GENERATOR` | `make_weather_page.py` | script to run, relative to the checkout (the tests use it) |
| `PATH` | cron's, `/usr/bin:/bin` with cronie | where `python3`, `nice`, `timeout` and `logger` are found |

### 7. Apache

```bash
cp /etc/local-weather-awareness/apache-local-weather-awareness.conf /etc/apache2/vhosts.d/local-weather-awareness.conf
/etc/init.d/apache2 configtest          # or: apache2ctl configtest
rc-service apache2 reload
```

Gentoo's `httpd.conf` includes `/etc/apache2/vhosts.d/*.conf` at server level. The snippet
therefore applies to every virtual host, the TLS one included, and its file name must end in
`.conf`. Check with `grep -n vhosts.d /etc/apache2/httpd.conf` *(verify on the host)*.
Alternatively, paste its lines inside the tau.kirx.net `<VirtualHost *:443>` block (on a
stock install, `00_default_ssl_vhost.conf`).

The snippet contains:

- **`Alias /myweather <output dir>`.** Harmless when the output directory is
  `<DocumentRoot>/myweather`: the same URL maps to the same directory. It is what publishes
  the directory when it lives anywhere else.
- **A `<Directory>` block** with:
  - `Options -Indexes -FollowSymLinks +SymLinksIfOwnerMatch`: no directory listings, and a
    symlink is served only when it and its target have the same owner. The run user owns
    the directory and the generator never makes links, so a link planted there to a file
    the run user does not own is refused;
  - `Require all granted`;
  - `DirectoryIndex index.html`;
  - UTF-8 as the default charset;
  - inside `<IfModule mod_headers.c>`, `Cache-Control: max-age=60` for `index.html` and
    `status.json`, and `max-age=120` for the PNGs, so viewers get fresh maps without a hard
    reload.

`deploy/apache-local-weather-awareness.conf` in the repository is the template with
`__OUT_DIR__`.

### 8. Verify

```bash
crontab -u apache -l                        # the */5 line ending in "# local-weather-awareness"
rc-service cronie status                    # started
# the runs' log lines (wherever your syslog writes):
grep local-weather-awareness /var/log/messages | tail -n 20
ls -l /var/www/localhost/htdocs/myweather/  # index.html, status.json, radar_<site>_<theme>.png
curl -I https://tau.kirx.net/myweather/     # 200, Cache-Control: max-age=60
curl -s https://tau.kirx.net/myweather/status.json
```

A normal run logs one line per site plus about seven more: it starts with
`run started: 5 site(s) from …` (where the site list came from) and `alert areas: NM,OK,TX
(auto: …)`, and ends with `done: sites ok 5/5, …` (for the five example sites). To run it
by hand with the output on the terminal (an env file that sets `WEATHER_LOG` wins over the
`-`):

```bash
su -s /bin/sh apache \
   -c 'WEATHER_LOG=- /home/kirx/local-weather-awareness/deploy/local-weather-awareness-cron.sh'
```

`status.json` is a small health summary for monitoring:

| Key | Meaning |
|-----|---------|
| `generated` | ISO time of the run |
| `page_written` | `index.html` was written by this run |
| `degraded` | a source is unavailable or serving a stale last-good copy, a map was not written, or errors were logged |
| `ok` | `page_written` and not `degraded`: the flag to alert on, together with the age of `generated` |
| `problems` | short strings naming what is degraded, e.g. `radar: stale`, `socorro light map: not written`, `network: api.weather.gov unreachable`, `NMDOT road feed: unavailable` (or `stale`, `incomplete`), `NWS storm reports: stale`, `3 error(s) logged in this run` |
| `alerts`, `radar` | `ok` and `stale`, plus the alert count or the frame time; `radar` is `"not applicable"` when no site's map reaches the MRMS grid (nothing was fetched) |
| `network` | the run's network budget after all fetches: `budget_s`, `left_s`, `exhausted`, `down_hosts` |
| `roads` | `{"nmdot": {ok, stale, events, feed_time}, "lsr": {ok, stale, reports}}`, where `events` / `reports` count the distinct items listed on the page (an item on two maps counts once) and `feed_time` is the NMDOT feed's Last-Modified time; `"not applicable"` when no site's map reaches New Mexico (nothing road-related was fetched); `null` when road closures are off (`WEATHER_ROADS=0`) |
| `sites` | per site: `meta_ok`, `obs_ok`, `forecast_ok`, `hourly_ok`, `maps_ok`, `alerts_at`, `alerts_near`, `roads_events`, `roads_reports` (`obs_ok`, `forecast_ok` and `hourly_ok` are also false where NWS provides no such product, e.g. American Samoa; that is not a problem) |
| `errors` | the run's full error lines |

A run with `--skip-radar` or `WEATHER_RADAR=0` counts as degraded (`radar: unavailable`),
except when no site's map reaches the MRMS grid: the radar is then `"not applicable"`.

### Updating

```bash
su - kirx -c 'git -C ~/local-weather-awareness pull'   # or the rsync from step 4
```

There is nothing to restart: the next cron run, within 5 minutes, uses the new code. When
anything under `deploy/` changed, rerun
`RUN_USER=apache /home/kirx/local-weather-awareness/deploy/install-cron.sh`. It only changes
what differs. If the Apache snippet changed, copy it again and reload Apache.

### Uninstall

```bash
crontab -u apache -l | grep -v '# local-weather-awareness$' | crontab -u apache -
rm -f /etc/apache2/vhosts.d/local-weather-awareness.conf && rc-service apache2 reload
rm -rf /etc/local-weather-awareness /etc/local-weather-awareness.env /home/kirx/local-weather-awareness
rm -rf /var/www/localhost/htdocs/myweather
rm -rf ~apache/.cache/local-weather-awareness ~apache/local-weather-awareness.env
# only if nothing else needs these Pillow flags:
rm -f /etc/portage/package.use/local-weather-awareness
```

`crontab -u apache -r` removes the user's whole crontab instead of just the
local-weather-awareness line.

### Troubleshooting

- **No log lines 5 minutes after the install.**
  - Is cron running (`rc-service cronie status`), and is it in the default runlevel
    (`rc-update show default`)?
  - cronie logs every job start to syslog as `CROND[…]: (apache) CMD (…)`.
  - If `/etc/cron.allow` exists, the run user must be listed in it.

  *(verify on the host)*
- **`python interpreter not found` or `No module named 'PIL'` in the log.** Cron's PATH is
  `/usr/bin:/bin`, and Pillow must be built for that `python3` (see [Packages](#1-packages)).
  Set `WEATHER_PYTHON` (and `PATH` if needed) in `/etc/local-weather-awareness.env`.
- **Nothing in `/var/log/messages`.** No syslog daemon is running (`/dev/log` is missing).
  The wrapper then writes `~apache/.cache/local-weather-awareness/cron.log` instead.
  `WEATHER_LOG=/path/file` forces a log file.
- **`generator stopped by the timeout after 600 s (exit status 124)`.** A run hung beyond
  its network budget (`WEATHER_RUN_BUDGET`, 240 s). The next run starts fresh.
- **Apache 403.** The output directory must be readable by the Apache user (0755, files
  0644; the wrapper sets umask 022). Check that the snippet is loaded (`configtest`, and
  `grep -rn myweather /etc/apache2/vhosts.d/`).
- **Apache 404 for `/myweather/`.** The snippet is not included (in `vhosts.d` its name must
  end in `.conf`), or `WEATHER_OUT_DIR` is not `<DocumentRoot>/myweather` and the `Alias` is
  missing.
- **`cannot write the output or cache directory` (exit status 1).** A directory was deleted
  or its owner changed. Rerun the installer: it recreates both, owned by the run user
  (it refuses, and prints the command to run, when a directory exists but belongs to
  someone else).
- **Tiny, unscalable map labels, with `no TrueType font found (tried …)` in the log.**
  Install `media-fonts/dejavu`, and check Pillow's FreeType support (see
  [Packages](#1-packages)).
- **Maps show an "Access blocked" tile, or the log says "tile server refused the map
  tiles".** OpenStreetMap refused the User-Agent. Put a real contact (or none) in
  `WEATHER_USER_AGENT`; the pause lifts as soon as the User-Agent changes. Tiles cached by
  versions before 2026-09-24 could hold the blocked image; they are fetched once more
  automatically, or clear them with `rm -f ~apache/.cache/local-weather-awareness/{tile,basemap}_*`.
- **`HTTP 403` from api.weather.gov.** The User-Agent was rejected. Set
  `WEATHER_USER_AGENT` with a contact e-mail address (see
  [step 5](#5-configuration-etclocal-weather-awarenessenv)).
- **Many sources labelled STALE at once.** Look in the log for
  `<host> unreachable (…): skipping it for the rest of this run`: that host timed out or
  refused connections after its retries, so the rest of that run skipped it and used last
  good copies. `run budget of 240 s exhausted: skipping all further network requests` means
  the run's network time (`WEATHER_RUN_BUDGET`) was used up. Both reset with the next run.
  `status.json` lists them under `problems` as `network: …`.
- **"NMDOT road feed unavailable" in a road block.** nmroads.com did not answer and the
  last good copy is older than `WEATHER_ROADS_MAX_STALE` (1 h). The block then says the
  closures cannot be listed rather than "no closures". "NMDOT road feed incomplete" means
  one of its two files failed: closures are listed without their causes.
- **The page shows "STALE" or "unavailable" labels.** A source is down. The last good data
  is shown for the `*_MAX_STALE` window, then the label turns into an error. The run still
  exits 0, so cron keeps going.

## Other hosts: systemd

On a host with systemd (Debian, Ubuntu, RHEL, Alma, Rocky), `deploy/install.sh` installs a
oneshot service and a 5-minute timer instead of the crontab line. Use one or the other, not
both. `install.sh` stops on a host without `systemctl` and points to `install-cron.sh`.

1. **Packages.**
   - Debian/Ubuntu:
     `sudo apt install -y python3 python3-pil fonts-dejavu-core apache2 rsync && sudo a2enmod headers`.
   - RHEL family: `python3-pillow` is in EPEL, not in BaseOS, AppStream or CRB. Enable EPEL
     first:
     - AlmaLinux/Rocky: `sudo dnf install -y epel-release`.
     - RHEL: `sudo subscription-manager repos --enable "codeready-builder-for-rhel-9-$(arch)-rpms"`,
       then install `https://dl.fedoraproject.org/pub/epel/epel-release-latest-9.noarch.rpm`
       with `dnf`.

     Then:
     `sudo dnf install -y python3 python3-pillow dejavu-sans-fonts httpd rsync policycoreutils-python-utils`.
     `mod_headers` is loaded by default there. Without EPEL, install Pillow for the run user
     only: `sudo -u <run user> -H python3 -m pip install --user pillow`. The unit runs
     `/usr/bin/python3` as that user, which sees `~/.local`.
   - Python must be ≥ 3.9: Debian 11+, Ubuntu 22.04+ and RHEL 9+ qualify.
2. **Copy the repository** to `/opt/local-weather-awareness` as in
   [step 4](#4-copy-the-repository). Create `/etc/local-weather-awareness.env` as in
   [step 5](#5-configuration-etclocal-weather-awarenessenv), at least for the contact
   e-mail address in `WEATHER_USER_AGENT` (**crucial**: replace `you@example.org` with a
   real address, or OpenStreetMap blocks the map tiles) and for `WEATHER_PAGE_URL`, this
   host's address
   (`https://<host>/myweather/`). There, the default output directory is
   `/var/www/html/myweather`, under the usual DocumentRoot `/var/www/html`.
3. **Install:** `sudo RUN_USER=weather /opt/local-weather-awareness/deploy/install.sh`
   (preview with `DRY_RUN=1`). It:
   - resolves the user and the paths like `install-cron.sh`, with the same env-file conflict
     refusal;
   - warns about a missing Pillow or font, and about a `WEATHER_LOCK_FILE` outside the
     writable paths;
   - creates the directories;
   - on SELinux hosts, labels the output directory `httpd_sys_content_t` (`semanage
     fcontext` + `restorecon`, else `chcon`);
   - renders `local-weather-awareness.service` and `.timer` into `/etc/systemd/system/` and
     the Apache snippet into `/etc/local-weather-awareness/`;
   - enables the timer and runs the service once.

   The unit reads `/etc/local-weather-awareness.env`, then
   `~<run user>/local-weather-awareness.env`, as `EnvironmentFile=` (later wins; they
   override the paths written into the unit). Its `ExecStartPre=` line recreates deleted
   directories as the run user (`setpriv`, outside the sandbox, never as root); an output
   directory under a root-owned parent such as `/var/www/html` cannot be recreated that
   way, so after deleting it rerun `install.sh`. Paths and directories follow the same
   rules as in `install-cron.sh` (step 6), and the Python checks run as the run user.
4. **Apache.**
   - Debian:
     `sudo cp /etc/local-weather-awareness/apache-local-weather-awareness.conf /etc/apache2/conf-available/local-weather-awareness.conf && sudo a2enconf local-weather-awareness && sudo apache2ctl configtest && sudo systemctl reload apache2`.
   - RHEL:
     `sudo cp /etc/local-weather-awareness/apache-local-weather-awareness.conf /etc/httpd/conf.d/local-weather-awareness.conf && sudo apachectl configtest && sudo systemctl reload httpd`.
5. **Verify.** Check `systemctl list-timers 'local-weather-awareness*'` and
   `journalctl -u local-weather-awareness -n 50`, then `curl -I https://<host>/myweather/`
   and `status.json` as in [step 8](#8-verify).
6. **Update.** `git pull` or rsync, then
   `sudo systemctl start local-weather-awareness.service` to run at once. Rerun
   `install.sh` when `deploy/` changed.
7. **Uninstall.**
   - `sudo systemctl disable --now local-weather-awareness.timer`
   - `sudo rm -f /etc/systemd/system/local-weather-awareness.service /etc/systemd/system/local-weather-awareness.timer && sudo systemctl daemon-reload`
   - Remove the Apache snippet and reload Apache.
   - `sudo rm -rf /etc/local-weather-awareness /etc/local-weather-awareness.env /var/www/html/myweather /opt/local-weather-awareness ~<run user>/.cache/local-weather-awareness`
   - If the installer added an SELinux rule:
     `sudo semanage fcontext -d '/var/www/html/myweather(/.*)?'`
8. **Troubleshooting (systemd only).**
   - `Failed to set up mount namespacing` in the journal: some containers cannot apply the
     unit's sandboxing. Comment out its `Protect*`/`Private*` lines.
   - `Read-only file system` for the cache, or `cannot write the output or cache
     directory`: the `ExecStartPre=` line could not recreate it (e.g. the output directory
     under `/var/www/html`). Rerun the installer.
   - Apache 403 with the files present: an SELinux label. `ls -Z` should show
     `httpd_sys_content_t`, and `ausearch -m avc -ts recent` shows the denials. Rerun the
     installer.

## Configuration reference

Set these in `/etc/local-weather-awareness.env` or `~<run user>/local-weather-awareness.env`,
which the cron wrapper reads before every run and systemd reads as `EnvironmentFile=` (see
`weather.env.example`). They also work as plain environment variables. Command-line flags
win over both. Durations are in seconds. The cron wrapper's own variables (`WEATHER_LOG`,
`WEATHER_PYTHON`, `WEATHER_TIMEOUT`, `WEATHER_GENERATOR`) are listed in
[step 6](#6-install-the-cron-job).

| Variable | Default | Meaning |
|----------|---------|---------|
| `WEATHER_SITES` | unset: the sites file, else the built-in list (the [example configuration](#example-configuration-taukirxnet)) | `slug\|Name\|lat\|lon[\|tz];…` sets the site list; `tz` is an IANA name (e.g. `America/Denver`) used for local times when NWS `/points` is unavailable; an unknown zone, a site outside the US, a repeated slug or a value of only `;` is a startup error (see [Changing the sites](#changing-the-sites)) |
| `WEATHER_SITES_FILE` | unset | a file with the same entries, one per line, `#` comment lines allowed (format: `sites.example`); used when `WEATHER_SITES` is unset. Unreadable or malformed: startup error (exit status 2) naming the file and line, never a fall-back |
| `WEATHER_OUT_DIR` | `./out`, relative to the working directory (the checkout, under the cron wrapper); `install-cron.sh` writes the real path into `/etc/local-weather-awareness.env`, and `weather.env.example` has `/var/www/html/myweather` | output directory for `index.html`, `status.json`, PNGs |
| `WEATHER_CACHE_DIR` | `~/.cache/local-weather-awareness` | disk cache (JSON, zone shapes, basemaps, last radar frame, lock) |
| `WEATHER_LOCK_FILE` | `<cache>/run.lock` | single-instance lock |
| `WEATHER_TITLE` | unset: derived from the site names, e.g. `Local weather: Lubbock · Clovis · Fort Sumner · Socorro · Albuquerque` | page title (`<title>` and masthead); the derived one lists the names up to their first comma, "+N more" beyond 80 characters |
| `WEATHER_PAGE_URL` | empty: no address shown | the page's public address, shown (and linked when it is `https://`) in the footer and printed by the installers, e.g. `https://tau.kirx.net/myweather/` for the example deployment |
| `WEATHER_REFRESH_SECONDS` | `300` | page auto-reload interval (min 30): JavaScript reload that keeps open sections and scroll position, `<noscript>` meta refresh as fallback |
| `WEATHER_UNITS` | `us` | `us` = °F/mph primary; `metric` = °C/km/h primary |
| `WEATHER_HOURLY_HOURS` | `24` | rows in the hourly table |
| `WEATHER_FORECAST_PERIODS` | `14` | day/night periods shown (NWS returns 14) |
| `WEATHER_POP_HIGHLIGHT_PCT` | `30` | precipitation probability at/above which cells are highlighted |
| `WEATHER_SHOW_SUN` | `1` | sun/twilight row |
| `WEATHER_USER_AGENT` | `local-weather-awareness (+https://github.com/kirxkirx/local-weather-awareness)` | sent with every request; required by NWS, which asks for a contact in it: add your e-mail address after the URL, separated by a semicolon (see [step 5](#5-configuration-etclocal-weather-awarenessenv)). **It must be a real address**: with a placeholder such as `you@example.org` OpenStreetMap blocks every tile request and the generator refuses to start |
| `WEATHER_HTTP_TIMEOUT` | `25` | per-request timeout |
| `WEATHER_HTTP_RETRIES` | `2` | retries on network errors / 5xx / 429 |
| `WEATHER_RUN_BUDGET` | `240` | network time budget per run (min 10); then every further fetch is skipped and last-good copies are used (see [Operational notes](#operational-notes)) |
| `WEATHER_POINTS_TTL` | `604800` | re-fetch `/points` metadata after (7 d) |
| `WEATHER_FORECAST_TTL` / `_MAX_STALE` | `1800` / `7200` | 7-day forecast: re-fetch after / keep last good until |
| `WEATHER_HOURLY_TTL` / `_MAX_STALE` | `1800` / `7200` | hourly forecast, same |
| `WEATHER_OBS_TTL` / `_MAX_STALE` | `600` / `3600` | observations, same |
| `WEATHER_ALERT_AREAS` | `auto` | states, territories and marine areas in the alerts query: `auto` = every one whose bounding box touches a site's map (`NM,OK,TX` for the example sites, `IL,IN,LM,MI,WI` for Chicago; the run log names them); codes listed with `auto` are added (`auto,PK`); without `auto`, two-letter codes such as `NM,TX` that cover the maps (a site in a state the list leaves out is reported in the log, the footer and `status.json`); a malformed value is a startup error |
| `WEATHER_ALERTS_MAX_STALE` | `1200` | keep the last good alert feed this long on failure |
| `WEATHER_ALERT_FILL_ALPHA` | `55` | 0..255 fill opacity of alert areas on the maps |
| `WEATHER_ALERT_COLORS` | empty | per-event colour overrides, `Event Name=#rrggbb;Other Event=#rrggbb`; names as NWS sends them, case-insensitive; a malformed entry is a startup error |
| `WEATHER_MAP_MILES` | `100` | map width and height in statute miles (10..1000) |
| `WEATHER_MAP_PX` | `600` | map width in pixels (200..2000) |
| `WEATHER_TILE_ZOOM` | `9` | basemap tile zoom (5..12) |
| `WEATHER_TILE_URL_DARK` / `_LIGHT` | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` (both) | `{z}/{x}/{y}` tile URL templates |
| `WEATHER_TILE_DARK_INVERT` | `1` | dark theme: invert the lightness (hue kept) of each tile that comes out light (`0` = use the tiles as served) |
| `WEATHER_RADAR` | `1` | `0` skips the MRMS layer |
| `WEATHER_RADAR_DBZ_MIN` | `5` | echoes below this are not drawn |
| `WEATHER_RADAR_ALPHA` | `170` | 0..255 opacity of the radar layer |
| `WEATHER_RADAR_MAX_BACK` | `10` | 2-minute frames to look back for the newest MRMS image |
| `WEATHER_RADAR_MAX_STALE` | `1800` | reuse the last frame (labelled STALE) this long |
| `WEATHER_MRMS_ARCHIVE` | IEM `…/GIS/mrms/lcref_%Y%m%d%H%M.png` | strftime pattern of the archive URL |
| `WEATHER_ROADS` | `1` | New Mexico emergency road closures, listed per site and drawn on the maps; fetched only when a site's map reaches New Mexico; `0` = no road fetches, no road blocks, maps as before |
| `WEATHER_ROADS_WATER` | `1` | also list and draw "water on road" (NMDOT reports of flooding, standing water, water over the road or an impassable road) |
| `WEATHER_LSR` | `1` | also list and draw NWS Local Storm Reports from New Mexico that name a closed, flooded or impassable road, crossing or arroyo |
| `WEATHER_LSR_HOURS` | `24` | storm-report window in hours (1..168) |
| `WEATHER_ROADS_MAX_STALE` | `3600` | keep showing a road source's last good copy this long when it is down (0..604800) |
| `WEATHER_ROADS_OLD_DAYS` | `3` | flag an NMDOT item "not updated for N days, may have reopened" after this many days (> 0, ≤ 365) |
| `WEATHER_NMROADS_JSON_URL` | `https://nmroads.com/nmroads.json` | NMRoads GeoJSON (http or https) |
| `WEATHER_NMROADS_RSS_URL` | `https://nmroads.com/rss.xml` | NMRoads RSS with the cause texts and update times |
| `WEATHER_LSR_URL` | `https://mesonet.agron.iastate.edu/geojson/lsr.geojson?hours={hours}&states=NM` | storm-report URL; `{hours}` becomes `WEATHER_LSR_HOURS`, and no other `{…}` field is allowed. IEM filters by state with `states=NM` and silently ignores a `wfos=` list |
| `WEATHER_VERBOSE` | `0` | DEBUG logging (same as `--verbose`) |

Fixed in `weather/config.py` (no env var): `/points` last-good window 60 days, observation
reports older than 180 min are ignored, alert outline width 3 px, the attribution text drawn
into the PNGs (the page footer has its own linked credits).

## How alerts are shown

**Selection.** Every run fetches all active alerts of the states, territories and marine
areas on the maps (`WEATHER_ALERT_AREAS=auto`; NM, OK and TX for the example sites) and
keeps those with `status == Actual`, `messageType` `Alert` or `Update` (no cancellations,
tests or exercises) whose `ends` (else `expires`) time has not passed. When the feed is
unreachable the last good copy is used for up to `WEATHER_ALERTS_MAX_STALE` (20 min) and
the banner says so with the age of that copy; after that the page shows the fetch error
rather than a silent "no alerts".

**Shape: polygon vs zone-based.** Polygon-based products (Flash Flood, Severe Thunderstorm
and Tornado Warnings, Special Weather Statements, …) carry their own polygon in the feed.
Zone-based products (watches, advisories, most winter, wind, fire-weather, heat and air
quality products) carry `geometry: null` and a list of affected forecast/county/fire zones;
their shape is the union of those zone polygons, fetched from `api.weather.gov/zones/…`
once and cached for a year. If none of an alert's zones can be fetched it has no shape:
it can still be listed AT a site by zone id, but cannot be shaded. New outlines are fetched
only while half of the run's network budget is left (a first run during a statewide event
can meet hundreds of zones); an alert still missing one is listed but not shaded, and the
next runs fetch the rest.

**AT vs NEAR.** For each site:

- **AT**, for a polygon-based product (a warning that carries its own polygon): the site
  coordinate lies inside that polygon (ray casting). Nothing else counts. The alert's
  `affectedZones` list every county the polygon merely touches, and Socorro and De Baca
  counties are larger than the whole 100-mile map, so a listed county does not make the
  site AT. NWS's own point query behaves the same way.
- **AT**, for a zone-based product (watches, advisories, most statements): the site's own
  forecast, county or fire zone id is among the affected zones, **or** the site lies inside
  the merged zone outline. The zone-id test works even when no outline could be fetched.
- **NEAR**: not AT, but the alert's shading leaves at least one pixel on a map of the site
  that was actually written (up to ~50 mi from the site, ~70 mi at the corners). This is
  decided from the rendered mask, so the list of NEAR alerts and the shaded areas can never
  disagree. If no map could be rendered at all (Pillow missing), every alert whose bounding
  box touches the map frame is listed NEAR instead.
- Anything else is not shown for that site.

Order everywhere (banner, cards, legend): **warnings → watches → advisories → statements
(statements, outlooks, alerts, messages) → other**; within a kind by severity Extreme →
Severe → Moderate → Minor → Unknown, then by event name, then by end time.

**Colours.** Each event name maps to its colour on the NWS watch/warning/advisory map (the
chart at [weather.gov/help-map](https://www.weather.gov/help-map), copied row by row into
`NWS_COLORS` in `weather/alerts.py`), with one deliberate exception: **inland flood products
are drawn in reds** rather than the chart's greens. The chart has Flood Watch sea green,
Flood Warning lime and Flood Advisory spring green. On this page green reads as "good",
which a flood is not, and a green fill also disappears into the green 15–35 dBZ radar echoes
of the rain behind the flood. So the owner asked for red (`FLOOD_COLORS`, laid over the
chart). Darker means more serious. Flash Flood Warning keeps its official `#8B0000`, and
Coastal and Lakeshore flood products keep their NWS colours. Tornado Warning is red as well;
the legend and the cards always name the event. Examples (* = this page's flood palette,
not the NWS chart):

| Event | Colour | Event | Colour |
|-------|--------|-------|--------|
| Tornado Warning | `#FF0000` red | Tornado Watch | `#FFFF00` yellow |
| Severe Thunderstorm Warning | `#FFA500` orange | Severe Thunderstorm Watch | `#DB7093` pale violet red |
| Flash Flood Warning | `#8B0000` dark red | Flood Warning | `#C62828` dark red * |
| Flood Watch, Flash Flood Watch | `#E53935` red * | Flood Advisory, Flood Statement | `#FA8072` salmon * |
| Hydrologic Outlook | `#F4A6A6` light red * | Hazardous Weather Outlook | `#EEE8AA` pale goldenrod |
| Winter Storm Warning | `#FF69B4` hot pink | Winter Weather Advisory | `#7B68EE` medium slate blue |
| High Wind Warning | `#DAA520` goldenrod | Wind Advisory | `#D2B48C` tan |
| Red Flag Warning | `#FF1493` deep pink | Fire Weather Watch | `#FFDEAD` navajo white |
| Dust Storm Warning | `#FFE4C4` bisque | Blowing Dust Advisory | `#BDB76B` dark khaki |
| Extreme Heat Warning | `#C71585` medium violet red | Heat Advisory | `#FF7F50` coral |
| Freeze Warning | `#483D8B` dark slate blue | Dense Fog Advisory | `#708090` slate grey |
| Special Weather Statement | `#FFE4B5` moccasin | Air Quality Alert | `#808080` grey |

Any event's colour can be changed with `WEATHER_ALERT_COLORS`, e.g.
`WEATHER_ALERT_COLORS=Flood Watch=#2E8B57` restores the chart's sea green for Flood Watches.
Events missing from the table fall back to their kind: warning `#d00000`, watch `#e6b800`,
advisory `#7b68ee`, statement `#ffe4b5`, other `#808080`. The kind comes from the event
name's last word (Warning / Watch / Advisory; Statement, Outlook, Alert, Message, Forecast
and Emergency count as statements).

**On the maps.** Every AT or NEAR alert with a shape is drawn as a translucent fill
(`WEATHER_ALERT_FILL_ALPHA`, default 55/255) and a solid 3 px outline in the same colour
over a 5 px casing, black on the dark map and white on the light one, so a red flood outline
stays visible over red radar echoes. Polygons with holes go through a mask, so an excluded
inner area stays clear. The draw order is the **reverse** of the listing order: statements
first, then advisories, watches, and warnings last, so a warning is never hidden under a
watch that covers the same area. The caption under each map is the legend:

- **Shaded:** a colour swatch and event name for every alert (AT or NEAR) actually drawn on
  a map that was written.
- **Listed, not shaded:** alerts on the page that are not on the map, e.g. an AT watch whose
  zone outline could not be fetched. The map itself notes "not shaded (no outline
  available)" for alerts that have no outline.
- **No map rendered** when neither theme's map was written.

**On the page.** The banner at the top lists each distinct alert once, colour-coded,
linking to the site sections (or "No NWS alerts for these sites"). Each box shows:

- the issuing office, so Flood Watches from two offices can be told apart;
- the area, shortened to about 90 characters with the full list on hover;
- the sites the alert is AT or NEAR;
- its time span.

The navigation badge per site counts the alerts AT it, in the colour of the top-ranked one.
Each site's alert cards show:

- the event with severity/urgency chips and an AT/NEAR chip;
- the NWS headline, with the product's own short `NWSheadline` under it when that differs;
- the time span in the site's local time, the issuing office and the area description;
- a **Full text on weather.gov** link to the NWS hazard page
  (`forecast.weather.gov/showsigwx.php` for the site's zones; "NWS forecast for this site",
  the point forecast page, when the zones are unknown);
- the full description and instruction in a collapsible `<details>`.

The time span reads "from <onset> until <end>" while an alert has not started yet, and
"until <end>" once it is in effect. Each site heading also links the site's
**NWS point forecast**.

Impact-based warnings get a high-contrast **threat chip** on the card and in the banner,
taken from the CAP damage-threat tags. Most serious first:

- TORNADO EMERGENCY
- FLASH FLOOD EMERGENCY
- PDS: a particularly dangerous situation, i.e. a tornado warning tagged CONSIDERABLE
- DESTRUCTIVE: a severe thunderstorm
- CONSIDERABLE FLASH FLOODING
- CONSIDERABLE DAMAGE: a severe thunderstorm tagged CONSIDERABLE, which NWS does not call
  PDS

All text is HTML-escaped; links taken from the feeds are used only when they start with
`https://`.

## How road closures are shown

**Scope (the owner's decisions).**

- **New Mexico only.** No other state's source is used at all. A map that does not reach
  New Mexico (in the example deployment, Lubbock's, in Texas; decided by the state's
  outline, padded by about a kilometre, not by its bounding box) has a block that
  reads "Road closures cover New Mexico state routes only; this map is outside New
  Mexico.", and the part of a map beyond the state line (the Texas side of the Clovis map)
  shows no road items. When no site's map reaches New Mexico, nothing road-related is
  fetched or shown and `status.json` reports `"roads": "not applicable"`. City streets
  (for example ABQRoads for Albuquerque) are not used either.
- **Only closures caused by an emergency**: flooding, washouts, debris, rock or mud slides,
  fire, crashes, hazardous materials, snow and ice, wind and dust, storm damage, and
  law-enforcement closures. Left out: roadwork, construction and lane closures,
  maintenance, scheduled or nightly closures, seasonal closures, special events, convoys and
  oversize loads, White Sands missile-range alerts and rest-area closures.
- **Drawn on the maps** as well as listed under each site.

**Sources.** Both are free, keyless and fetched once per run for all maps, in every run in
which at least one site's map reaches New Mexico.

- NMDOT's open **NMRoads** feed, the data behind nmroads.com. `nmroads.json` (GeoJSON)
  gives the geometry, lines that follow the road, but only a templated title. `rss.xml`
  gives the free-text cause ("closed due to flooding", "Standing water on roadway") and the
  update times. The two files are joined on feature id = RSS guid. Both are fetched with a
  conditional GET, so NMDOT answers `304 Not Modified` without a body until an event changes.
  The event ages come from the RSS "Update Date", read as New Mexico time. NMDOT also
  re-saves many events at once with one shared stamp (19, 14, 12 and 7 events shared four
  such stamps on 2026-09-23, with post dates from 2021 on); a stamp with a fraction of a
  second shared by 3 or more events is ignored, and those events are aged from their "Post
  Date". The JSON's own dates run 0 to 7 hours behind the RSS ones (6 h for most events on
  2026-09-23), so they are used only when `rss.xml` is down, marked "time approximate".
- **NWS Local Storm Reports** from New Mexico, via the Iowa Environmental Mesonet.

**Exactly what is listed.** The NMDOT event type and the free text decide, in this order:

1. **Closed by an emergency.** One sentence of the text (or of the title) says the road is
   closed or impassable **and** names an emergency in that same sentence: listed as closed,
   whatever the event type and whatever else the text mentions. "Roadway closed due to a
   crash. Traffic is being diverted at the Anton Chico rest area." is a crash closure, and
   "Roadway closed due to blowing dust near White Sands Missile Range" is a dust closure.
   A sentence about a rest area that names no road ("Anton Chico Rest Area is closed due to
   flooding") or about missile-range firings does not count.
2. **Roadwork-type events** are otherwise left out: Roadwork, Construction Closure, Lane
   Closure, Seasonal Closure, Alert, Fair Driving Conditions, Maintenance, Special Event and
   Work Zone. The exception: a sentence reports water on the road without construction
   wording, e.g. a Lane Closure on I-25 northbound reading "The left lane is closed due to
   standing water on the roadway". It is listed as water on road.
3. **Always left out** otherwise: rest areas and welcome centres, missile firings and White
   Sands Missile Range activity, wind-turbine convoys and oversize or wide loads, special
   events, parades, races, fiestas, festivals and film shoots, seasonal and winter closures,
   and NMDOT's permanent alerts.
4. **"Closure" events** are listed as closed when a sentence names an emergency, with the
   cause shown. When the text names neither an emergency nor construction or scheduling,
   they are listed as closed with "**cause not stated**". A Closure whose text is about
   planned work (resurfacing, paving, a project, girders, blasting, culverts, night work, a
   clock-time window such as "7 a.m. to 5 p.m.", a date range such as "Sept 21-22",
   "daily", "nightly", "scheduled", …) is left out.
5. **Driving conditions and any other type** are listed as closed when the text says the
   road itself is closed. A lane or a shoulder does not count. If such a text names no
   emergency but does use construction wording, the event is left out. They are listed as
   **water on road** when the text reports flooding, standing water, high water, water over
   or across the road, ponding, a washout, or an impassable road. Otherwise they are left
   out. That includes muddy-road advisories ("Roadway very muddy, 4x4 recommended"): they are caused
   by the weather, but they are neither a closure nor water on the road.

An **emergency** is named in a sentence by:

- A strong hazard word: flood or flash flood, standing or high water, water over the road,
  washout, rock, mud or land slide, sinkhole, fire or smoke, crash, accident, collision or
  rollover, hazmat or spill, snow, ice, blizzard or freezing rain, high or strong winds,
  dust storm or blowing dust, low visibility, tornado or thunderstorm, downed power lines
  or trees, emergency services, law enforcement, police or sheriff, evacuation, impassable,
  or heavy rain.
- A weak one: damage, emergency, storm, wind, debris, mud, dust, visibility, incident,
  collapse, erosion, rain, boulders, or a stalled or disabled vehicle.

A sentence about planned work names no emergency, whatever hazard word it holds: "NM 14
will be closed for construction of a new flood control channel", "Road closed for
installation of a new snow fence" and "The ramp will be closed nightly to replace the crash
cushion damaged in an earlier accident" are roadwork. With construction wording in the
sentence, only a strong hazard named as the cause counts ("Road closed due to flooding in
the construction zone"); with scheduling wording (scheduled, nightly, daily, a project, a
time window or date range), nothing does. Harmless phrases that contain a hazard word are
ignored: wind turbine, storm drain, fire hydrant, crash attenuator or cushion, dust control,
snow fence, Ice Caves Road, "to reduce crashes" and "weather permitting". Sentences end at
". ", "; " and the like, but not after an abbreviation ("3 a.m. Tuesday", "St. Francis
Dr.", "approx. 6 p.m.", "U. S. 70").
Duplicates (the same title at the same place) are listed once. Only events inside New
Mexico's box (longitude −109.1 … −103.0, latitude 31.3 … 37.0) count, and a site lists
those whose line or point touches its 100-mile map.

**The lead's scoping calls.** Each one can be switched off:

- **Water on road** (`WEATHER_ROADS_WATER=1`, the default). Flooding is the owner's main
  example, and NMDOT often files a flooded road as "Difficult Driving Conditions: Standing
  water on roadway" rather than as a Closure. `0` lists full closures only.
- **NWS storm reports** (`WEATHER_LSR=1`, `WEATHER_LSR_HOURS=24`). A report is listed when
  it comes from New Mexico and one sentence of its remark names a road, route, highway,
  street, crossing, arroyo or bridge (also "U.S. 491", "NM-143") together with closed,
  impassable, flooded, flood waters, water over, across, on or in the road, overtopping,
  inundated, washed out, standing water, barricaded, blocked, mud, rocks or debris on or
  across the road, stranded motorists, or a damaged road. Parking lots, homes and yards
  that flooded "near US 491", and a "road grader", do not make a road report. Roads on
  White Sands Missile Range ("Range Route 8, 9 and 12 impassable", "roads ... on WSMR") are
  military range routes and are left out, like NMDOT's missile-range alerts; "WSMR Met
  reports flooding along Dripping Springs Rd." is a public road and stays. Such reports
  catch closures that NMDOT never posts: on 2026-09-22, "NMDOT reports NM State Highway 304
  closed at Mile Marker 11 (Las Nutrias) due to standing water" appeared only as a storm
  report. A report never says when the road reopens, so each one is shown with its time
  and "may have reopened". Re-issues are folded in: "Corrected ...", "Corrects previous
  <type> report from <place>." and "Corrects time on previous ..." replace the report they
  correct (a correction that only says what was wrong, "NM-149 should read NM-143", keeps
  the corrected report's text with the correction appended), and a neighbouring office's
  "Report duplicated with WFO ABQ." copy is dropped.
- **Cause not stated.** An NMDOT Closure whose text gives no cause and no construction
  wording is listed as "cause not stated" rather than dropped.

**On the maps.** Hazards are never green here. Flood alert areas are already red (Flood
Watch `#E53935`), and the radar spans cyan, blue, green, yellow, orange, red and magenta.
So road features are **monochrome**: the map's ink colour over a halo in the contrasting
colour (white on black on the dark map, near-black on white on the light map). Line style
and symbol tell them apart:

| Item | Line | Symbol |
|------|------|--------|
| Road closed | solid 4 px line on a 9 px halo, round joins | "no entry": a disc (radius 8 px) with a bar across it, halfway along the visible part of the line |
| Water on road | dashed line (8 px dash, 6 px gap) on a continuous 9 px halo | a ringed disc with two wave strokes |
| Storm report (NWS) | none | a small upward triangle (12 px side) with a halo |

Road items are drawn over the alert areas and under the site marker and labels. Closures
go on top of water, and discs on top of triangles. A storm-report triangle that a disc
would hide moves just above that disc. The NWS report of water over NM 252 sits on NMDOT's
own NM 252 item, for example. When at least one road item is on the map, a small key shows
the symbols present: above the scale bars, or, when a road symbol lies there, above the
dBZ bar or in a top corner under the caption, wherever it hides no road symbol and keeps
the site marker clear. When no such place exists (a small map, or symbols in every
corner), the key is left out rather than hide a symbol; the page lists every item with the
same symbols. Area-wide NMDOT conditions ("Difficult Driving Conditions exist throughout
the Socorro - 41-57 area.") are one point for a whole patrol area, often far from any road,
so they are listed but not drawn. Emergency closures are often short (NM 41 at McIntosh: 2
miles, about 12 px on the 600 px map), so the symbol is often all that shows. A map with no
road items is byte-for-byte the same as before this feature existed.

**On the page.** Each site has a block **"Road closures (New Mexico, emergencies only)"**
after its alert cards:

- One row per NMDOT item, with:
  - a black-and-white chip, **ROAD CLOSED** or **WATER ON ROAD** (for an area-wide
    condition **ROADS CLOSED IN AREA** or **WATER ON ROADS IN AREA**, with "whole area, not
    drawn on the map" instead of the "on map" chip);
  - the route, mile markers and direction;
  - the cause text, or "cause not stated";
  - NMDOT's own title, with the place names;
  - "NMDOT · updated 33 min ago (15:30 MDT)";
  - an **on map** chip when the item is drawn;
  - a "**not updated for N days, may have reopened**" chip once it is older than
    `WEATHER_ROADS_OLD_DAYS` (3). NMDOT gives no reopening times: most events (86 of 93
    on 2026-09-23) carry a placeholder end date of 2099.
- "NWS storm reports (last 24 h)": time, place and county, report type, remark and "NWS ABQ
  storm report · 21 h ago · may have reopened".
- With nothing to list: "No emergency road closures reported on New Mexico state routes
  within this map." When NMDOT lists nothing but NWS storm reports are listed below, the
  line reads "NMDOT lists no emergency road closures within this map; see the NWS storm
  reports below." instead, because a report may well be of a closure NMDOT never posted.
- A link to the NMRoads map and "NMDOT feed as of 15:34 MDT".

When any site lists a closure or water item, the banner at the top adds one line,
"Emergency road closures (NM): N closed, M water on road", linking to those blocks. The
footer adds:

- source pills for NMDOT and the storm reports;
- the credits "Road information: NMDOT / NMRoads.com (public domain, CC0); storm reports:
  NWS via Iowa Environmental Mesonet";
- NMDOT's disclaimer (the information may not reflect all incidents or road conditions and
  is not to be your only source);
- a sentence saying what is and is not listed;
- how many NMDOT events are left out right now, by type (e.g. "47 Roadwork · 14 Alert ·
  9 Lane Closure · …").

**When a source fails.** A road source that is down is shown from its last good copy for
up to `WEATHER_ROADS_MAX_STALE` (1 h), and the block says so. After that the block reads
"NMDOT road feed unavailable (…); road closures cannot be listed right now". It never shows
a false "no closures". There are two partial failures:

- `rss.xml` down while `nmroads.json` answers: Closure events are listed, all with "cause
  not stated" (a construction closure cannot be told apart from an emergency without the
  text, and hiding a possible emergency would be worse), no water-on-road items or closures
  filed as driving conditions can be found, and the block says "NMDOT road feed incomplete
  (… closures listed may include planned work …)". Each item is dated from the JSON's
  creation date, "posted about 5 d ago (time approximate)", so an old scheduled closure
  still gets its "may have reopened" chip.
- `nmroads.json` down while `rss.xml` answers: items are drawn as points instead of lines.

`status.json` reports each case under `problems`. The road overlay can never cost a site
its map: if drawing the roads fails, the map is drawn again without them and the run
records the error. `WEATHER_ROADS=0` switches the whole feature off, with no requests, no
blocks, and maps as before.

**Known data-quality caveats** (details in [ROAD_CLOSURES.md](ROAD_CLOSURES.md)):

- Only NMDOT-maintained routes (I-, US- and NM- routes). County roads, city streets, and
  low-water and arroyo crossings have no feed anywhere in the region. Storm reports are the
  only partial substitute.
- Short closures may never be posted, or may be removed quickly (NM 304, above).
- Stale items stay listed until NMDOT removes them, hence the age and the "may have
  reopened" flag.
- Templated titles can overstate. US 60 in Clovis is titled "Roadway closed" while its text
  says one lane each way stays open. Classification reads the free text, not the title.
- Area-wide conditions ("throughout the Socorro area") are a single point: listed, not
  drawn.
- NMRoads has a Crash category, but whether crashes are posted promptly is unverified.
- Storm reports depend on someone reporting to the NWS, are free text, and can repeat an
  NMDOT item.
- The keyword lists were tuned against the live feed of 2026-09-23. Every excluded event
  on the example deployment's four New Mexico maps was read by hand; no emergency closure
  was dropped, and no roadwork got through.

## Operational notes

- Every source degrades independently. A dead IEM keeps the previous radar frame with a
  "STALE" label (30 min) and then a "radar unavailable" label. When IEM does not answer at
  all, the run spends a single 8 s HEAD probe, not up to 11 × 15 s, before it falls back:
  the label then reads "MRMS archive unreachable (no answer to HEAD for HH:MMZ)". A dead NWS
  keeps the last good forecast/observation for the `*_MAX_STALE` window and then shows
  "unavailable". The map is always produced, even without radar or tiles. The run exits 0
  in all these cases, so cron (or the timer) keeps going. It exits non-zero only for a
  configuration error or an unwritable output or cache directory.
- **Network budget and circuit breaker.** A run's network traffic gets `WEATHER_RUN_BUDGET`
  seconds (default 240). Every request's timeout is capped by what is left, never below 3 s.
  Once the budget is spent, every further request is skipped without touching the network,
  and each source serves its last good copy, labelled stale. A host that times out, refuses
  or resets connections, or sends a truncated response after its retries is marked down.
  Requests to it are then skipped for the rest of that run, so one dead API costs one
  timeout, not one per URL. HTTP error answers (4xx/5xx) never mark a host down. The journal
  says so once per cause: `<host> unreachable (…): skipping it for the rest of this run` or
  `run budget of 240 s exhausted: skipping all further network requests`. `status.json` has
  the same under `network` and `problems`. The next run starts fresh.
- Output files are written atomically (temp file, flushed and fsynced, then `os.replace`),
  so Apache never serves a half-written or, after a crash, empty page. A `flock` on
  `<cache>/run.lock` prevents overlapping runs.
- Load per run: 1 MRMS frame (~0.6 MB; none when no map reaches the MRMS grid), 1 alerts
  request, per site up to 3–4 NWS requests when their caches have expired, and, while a
  site's map reaches New Mexico, 2 conditional NMRoads requests (answered `304` without a
  body unless NMDOT changed something; ~730 KB when it did) and 1 storm-report request
  (~10 KB). Basemap tiles are fetched once per map.
- **Cron (tau.kirx.net).** `deploy/local-weather-awareness-cron.sh` runs the generator as an
  unprivileged user (it refuses root) with `nice -n 10`, umask 022 and `timeout 600`, which
  kills a wedged run while leaving ample room for the 240 s network budget plus a few
  seconds of rendering. Keep `WEATHER_RUN_BUDGET` well below `WEATHER_TIMEOUT`. It reads
  the env files without evaluating them. Logs go to syslog
  (`logger -t local-weather-awareness`) or to `<cache>/cron.log`, which is rotated at 1 MB.
  Its exit status is the generator's.
- **systemd (other hosts).** The service unit runs the generator as an unprivileged user
  with `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, `ProtectSystem=full`,
  `ProtectHome=read-only` and `ReadWritePaths=` for only the output and cache directories.
  An `ExecStartPre=` line recreates those two directories, as the run user, if they were
  deleted.
  `TimeoutStartSec=10min` kills a wedged run. Logs go to journald
  (`journalctl -u local-weather-awareness`).

## License

No license has been chosen for this code yet. Until the owner adds a `LICENSE` file, the
usual copyright rules apply: you may read the code here, but no permission to copy, modify
or redistribute it is granted. The data shown on the page are not covered by this: they come
from public sources under their own terms (see [Data sources and
credits](#data-sources-and-credits)).
