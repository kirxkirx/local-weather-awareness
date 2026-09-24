# Road closures: research memo (2026-09-23)

Status: **version 1 implemented (2026-09-23)**, following the decisions below. Everything
after the Decisions section is the original research memo, kept as written. Where the memo
recommends something the owner then decided against (Texas sources, lane closures,
roadwork counts, ABQRoads), the Decisions win. How it works now is in README ("How road
closures are shown") and DESIGN.md (`weather/roads.py`).

## Decisions

Answers to the open questions at the end of this memo. Questions 1, 2, 4 and 6 were
decided by the owner. The lead settled 3, 5, 7 and 8, which the owner left open.

1. **Texas: none.** New Mexico only. No DriveTexas API key, no TDEM mirror, no legacy HCRS
   site, and no DriveTexas link-out. The Lubbock block says "Road closures cover New Mexico
   state routes only; this map is outside New Mexico." The Texas side of the Clovis map
   shows no road items.
2. **What is listed: emergencies only.** That means closures caused by flooding, washouts,
   debris/rock/mud slides, fire, crashes, hazmat, snow/ice, wind/dust, storm damage or law
   enforcement. Left out: roadwork, construction and lane closures, maintenance,
   scheduled/nightly and seasonal closures, special events, convoys and oversize loads, White
   Sands missile-range alerts, and rest-area closures. There is no "N roadwork items" link.
   The footer only counts what was left out.
3. **Which roads** (lead): everything NMDOT posts inside each map. There is no corridor list.
4. **Map lines: yes, drawn on the maps.** They are monochrome: the theme's ink with a
   contrasting halo, white on black on the dark map and near-black on white on the light
   map. Hazards are never green, flood alert areas are already red, and the radar uses
   cyan, blue, green, yellow, orange, red and magenta. The kinds differ by line style and
   symbol: a solid line with a "no entry" disc for a closure, a dashed line with a wave disc
   for water on the road, and a triangle for a storm report.
5. **Storm reports** (lead): 24 h. Only New Mexico reports count, and only those that
   mention a road, crossing or arroyo being closed, flooded or impassable (see below).
6. **ABQRoads: no.**
7. **Config switches** (lead): **yes.** They are `WEATHER_ROADS`, `WEATHER_ROADS_WATER`, `WEATHER_LSR`,
   `WEATHER_LSR_HOURS`, `WEATHER_ROADS_MAX_STALE`, `WEATHER_ROADS_OLD_DAYS` and the three
   source URLs.
8. **If-Modified-Since** (lead): **yes.** `http.cached_bytes` sends a conditional GET, and nmroads.com
   answers `304` while nothing changed.

The lead made three scoping calls, stated on the page. Each can be switched off:

- **Water on the road** is listed and drawn (`WEATHER_ROADS_WATER=1`). These are NMDOT
  driving-condition reports of flooding, standing water, water over the road or an
  impassable road. Flooding is the owner's main example, and NMDOT often files it this way
  rather than as a Closure.
- **NWS Local Storm Reports** are listed and drawn (`WEATHER_LSR=1`,
  `WEATHER_LSR_HOURS=24`). Only New Mexico reports count, and only those whose remark names
  a closed, flooded or impassable road, crossing or arroyo. Each is shown with its date and
  "may have reopened". These catch closures NMDOT never posts, such as "NM 304 closed at
  MM 11" on 2026-09-22.
- **NMDOT Closures with no stated cause** and no construction wording are listed as "cause
  not stated".

The implementation departs from this memo in a few places:

- **Storm-report URL.** It uses `…/lsr.geojson?hours={hours}&states=NM`. IEM silently
  ignores `wfos=`: checked live on 2026-09-23, that query returned the whole country's
  reports.
- **Muddy-road advisories** ("4x4 recommended", NM 107/52/163 on the Socorro map) are
  excluded. They are weather-caused, but they are neither a closure nor water on the road.
- **Lane Closure with standing water.** A Lane Closure caused by standing water is listed as
  water on road. Example: I-25 northbound, mm 276–277, "left lane is closed due to standing
  water".

## Research memo (as written before the decisions)

Re-checked 2026-09-23, 20:35 to 20:42 UTC (14:35 MDT). Read-only: no project file was changed. The raw responses were kept locally and are not part of this repository.

SHORT ANSWER
- New Mexico is easy. NMDOT publishes everything shown on nmroads.com as an open feed. It needs no key, declares a CC0 licence, and has line geometry that follows the road. The work is about 1 to 1.5 days including tests.
- Texas is harder, and it affects two maps:
  - It covers all of the Lubbock map, plus the eastern ~40% of the Clovis map. The Clovis map runs east to -102.33, i.e. into Cochran, Bailey and Parmer counties.
  - The only proper source is TxDOT's DriveTexas API. It needs a key that TxDOT approves, and its own "about" text says it is meant for emergency management and "should not be ... redistributed".
  - The keyless alternatives are either up to an hour behind or fragile HTML scraping.
- Some things have no machine-readable source anywhere in the region:
  - county roads
  - low-water and arroyo crossings
  - flooded city streets and playas
  The best partial substitute is NWS Local Storm Reports, e.g. "NMDOT reports NM 304 closed at MM 11 due to standing water". They must be shown as dated reports, because a report never says when a road reopens.
- Commercial traffic APIs are ruled out: TomTom, HERE, Azure, Mapbox, Google, INRIX, Waze and Bing. All need keys or billing. Their terms forbid, or leave unclear, server-rendered images or cached results served to many viewers. HERE's §6.4(l) and Mapbox's §1.9/§2.8.1 forbid it outright; TomTom's §11.4/§11.6.1 are a grey area. Their coverage of rural NM is unverified.

WHAT I RE-VERIFIED MYSELF
1. https://nmroads.com/nmroads.json
   - Response: HTTP 200, application/json, 596,189 bytes, no gzip even when asked. Last-Modified 20:16:00Z. If-Modified-Since returns 304.
   - feed_info.license is CC0 1.0; publisher NMDOT. robots.txt returns 404.
   - 93 events statewide, 46 in the region box. By map: Lubbock 0 (the map does not reach NM), Clovis 5, Fort Sumner 5, Socorro 8, Albuquerque 19.
   - Types in the box: work-zone 31, Alert 8, Difficult Driving Conditions 6, Closure 1. That one Closure is stale: the I-25 NB mm 290 ramp at Santa Fe, scheduled for Sep 21-22 and still listed.
2. https://nmroads.com/rss.xml
   - Response: HTTP 200, 130,875 bytes, same Last-Modified. 93 items; all 93 guids match the JSON ids.
   - The cause of each event ("closed due to flooding", "Standing water on roadway", "Roadway very muddy ... 4x4 recommended") is only in the RSS description. The JSON only carries a templated title.
   - NEW FINDING: the JSON timestamps are unreliable. Many update_date and creation_date values are 6 h too early:
     - The Socorro-area event says 08:15:38-06:00 in the JSON but 14:15:38 local in the RSS. The feed was rebuilt at 14:16:00 MDT (20:16:00Z), which matches the RSS time.
     - The I-25 mm 290 event has update 08:01:55-06:00, which is earlier than its creation time of 12:55:51-06:00.
     - Two of 93 events have update < creation.
     To show an event's age, use the RSS "Update Date" read as America/Denver time, or our own first-seen time.
   - Other quality issues:
     - 86 of 93 events have end_date 2099.
     - Two duplicate groups (US-60 mm 331 and NM 128).
     - 16 junk road names ('null-null' or '-').
     - "Lane Closure" events are filed under event_type work-zone, so classify by the title prefix instead.
     - Titles can overstate. US-60 Clovis is titled "Roadway closed", but the text says one lane each way stays open.
     - The "Alert" items are mostly long-running: wind-turbine convoys on NM 42 and NM 247 posted in 2024, the permanent WSMR missile-range alert on US-380, and rest-area closures.
3. IEM Local Storm Reports, https://mesonet.agron.iastate.edu/geojson/lsr.geojson?hours=72&wfos=ABQ,LUB,MAF,EPZ,AMA
   - Response: HTTP 200, GeoJSON, 380 KB, 0.2 s, no key.
   - 11 reports in the box, 6 of them FLASH FLOOD. The road-related ones:
     - NM 304 closed at MM 11, Las Nutrias. Reported by NMDOT at 2026-09-22 23:19Z; it is no longer in NMRoads.
     - Water over NM 252 north of House, 09-23 00:50Z.
     - Range Routes 8, 9 and 12 impassable, 13 mi W of Oscuro.
     - Water over FM168 and CR 262, 3 N Spade, Lamb Co. TX, 09-21.
4. DriveTexas API, api.drivetexas.org/api/conditions.geojson
   - Still returns 401 "Unauthorized" without a key.
   - about.json and tos.json re-read; the quotes below are confirmed.
5. TDEM ArcGIS mirror of DriveTexas
   - Response: HTTP 200, 96 KB for the region box: 39 features (27 Construction, 8 Flooding, 2 Closure, 2 Damage). dataLastEditDate 20:01Z.
   - It lags. It still gave FM1780, FM1779, FM1169 and FM303 flood end times of 16:00-17:00 CDT, while the legacy text site already showed them extended to 20:00 CDT. It also still listed the FM2901 and FM168 floods, which ended at 15:00 CDT.
   - Lubbock map: 16 items, 8 of them not construction: floods on FM1780, FM303, FM2901 and FM168, a damage closure on FM2008 in Garza Co., and roadway damage on FM786 and FM97.
   - Clovis map: 11 items, 6 not construction, including the SH-214 bridge closure in Parmer Co. and the Cochran/Hockley floods.
6. Legacy HCRS text site, http://conditions.drivetexas.org/current/conditions.asp (POST)
   - The Flood query answered at 20:36Z: 13.7 KB of HTML, 7+ records, including duplicate FM1780 records with different end times.
   - The next statewide "Closed" query hung for more than 4 minutes. From about 20:40Z every connection to the host timed out (3 tries), while drivetexas.org itself still answered.
   - Whether that is rate-limiting or an outage is unknown. Either way, it is fragile.

OPTIONS (details in the options list)
A. NMRoads: nmroads.json plus rss.xml. Recommended.
B. NWS Local Storm Reports via IEM. Recommended, as a supplement.
C. DriveTexas API, which needs a key. Recommended only if you want Texas and accept the approval process.
D. Keyless Texas stop-gaps: the TDEM mirror, the legacy text site, or just a link. Only a link-out is recommended for the public page.

RECOMMENDED MINIMAL FIRST VERSION (no keys): A + B, plus link-outs for Texas
Page. Add a "Roads" block per site, below the alert cards:
- Up to about 8 NMRoads items whose geometry touches that site's map, sorted in this order:
  1. full closures
  2. water or flooding on the road (regex on the RSS text: flood|standing water|water (running )?over|washed|impassab)
  3. crash or severe/difficult driving conditions
  4. lane closures on I-, US- and NM-corridor routes
  Each item shows route, mile markers, the cleaned cause text and "NMDOT, updated N h ago". Routine roadwork and long-running alerts are not listed individually; instead show a count and a link, e.g. "12 roadwork items: nmroads.com".
- A "Storm reports (last 48 h)" sub-list with the road-related LSRs, labelled with their time and "NWS report, may have reopened".
- On Lubbock, and on the Texas side of Clovis: "Texas road conditions: see DriveTexas" (link) plus the Texas LSRs.

Maps:
- Draw NMRoads segments as a thick line with a halo: black with a white halo on the light theme, reversed on the dark theme. Add a symbol for a full closure and a different one for water on the road.
- Draw LSRs as small triangles.
- Add one legend line.
- Do not use red: flood alert areas are now red (FLOOD_COLORS in alerts.py already uses #E53935 for Flood Watch). Avoid cyan, green and yellow as well, because the radar palette uses them.

Footer:
- NMRoads freshness, e.g. "as of 14:16 MDT".
- Credits: "Road information: NMDOT / NMRoads.com; storm reports: NWS via Iowa Environmental Mesonet".
- NMDOT's disclaimer: may not reflect all incidents, should not be your only source.

What v1 would show today:
- Clovis: US-60 reconstruction in Clovis (westbound lanes closed, one lane each way); NM 252 mm 24-25 standing water / roadway flooding; LSR of water over NM 252.
- Fort Sumner: NM 252 flooding; US-60/84 mm 331-342 single-lane closures.
- Socorro: "Difficult driving conditions throughout Socorro area: standing water"; NM 107, NM 52 and NM 163 muddy (4x4 recommended); LSR of NM 304 closed at Las Nutrias.
- Albuquerque: NM 333 (Tijeras) standing water; I-25 Los Lunas down to 1 lane each way; nightly lane closures at the Big I; the stale I-25 ramp closure, flagged by its age; LSR of NM 304.
- Lubbock: only the Spade LSR and the DriveTexas link.

Effort for v1, in concrete terms. The existing code is ~9,800 lines, test-heavy.
- New weather/roads.py, about 220-300 lines:
  - NMRoads: fetch both files, join on id == guid, strip HTML and the '<autodescriptiondelimiter>' token, parse the mixed RSS date formats as America/Denver, classify, dedupe on title+geometry, filter per site with MapFrame.bbox.
  - LSR: bbox, type and remark filter, and age.
- http.py: +20-30 lines for If-Modified-Since. Optional, but it keeps upstream traffic at ~2 conditional requests (usually 304) per run instead of ~730 KB every 5 min (~210 MB/day).
- radar.py: +60-90 lines (lines, halos, symbols, legend, both themes).
- page.py: +80-120 lines (Roads block, footer credit and freshness).
- config.py: +15-25 lines (WEATHER_ROADS on/off, LSR window, optional list of corridors).
- make_weather_page.py: +10-20 lines (call it with a fallback to the last good copy).
- Tests: new test_roads.py of 200-300 lines using trimmed fixtures from today's files (including the 6 h timestamp bug and the mixed date formats), plus small additions to test_radar and test_page.
- Total about 600-850 lines including tests. No secrets. Upstream use: 2 NMRoads requests (mostly 304) and 1 IEM request per run.

Phase 2, only if you apply and TxDOT approves: the DriveTexas API for the Lubbock and Clovis maps.
- About 80-120 more lines plus ~100 lines of tests. It uses the same drawing code.
- The key goes in weather.env as WEATHER_DRIVETEXAS_KEY and must never appear in index.html, status.json or the logs.
- Keys expire every March 31 and must be renewed.

Not recommended:
- The TDEM mirror or the legacy HCRS site on the public page.
- The internal APIs behind the NMRoads and DriveTexas maps. They are undocumented, and robots.txt disallows the DriveTexas one.
- Commercial APIs.
- rss.nmroads.com, the site's own "RSS Feed" button. It is stale, with the newest item from 2025-01.

Optional later: Albuquerque city streets via abqroads.json.
- It carries crashes and lane closures and updates every few minutes, so it suits a text list on the Albuquerque site only.
- The file needs a lenient JSON parse because of a trailing comma.
- The CABQ notice is required.
- I did not re-check it myself (a researcher verified it).

## Options

### A. NMRoads open feed: https://nmroads.com/nmroads.json joined with https://nmroads.com/rss.xml

- **Verified now:** yes
- **Access:** Open: no key, no registration. The feed declares CC0 1.0 (feed_info.license); publisher NMDOT. robots.txt returns 404. I found no terms forbidding automated use or redistribution. Credit NMDOT/NMRoads and repeat its disclaimer ('may not reflect all incidents ... should not be your only source').
- **Gives:** Every event NMDOT posts on NM state routes (I-25, I-40, US-60/84, US-380, US-70, US-285, NM-xxx):
  - Event types: Closure, Lane Closure, Construction Closure, Roadwork, Alert, and Difficult/Severe/Fair Driving Conditions. Crash is a legend category but none is live.
  - Geometry: LineStrings that follow the road (WGS84), or Points.
  - Cause text (flooding, standing water, mud) comes only from the RSS description, joined on id == guid (93/93 match).
  - Right now 46 of 93 events fall in the region. Per map: Lubbock 0, Clovis 5, Fort Sumner 5, Socorro 8, Albuquerque 19.
  - Flood-related now: NM 252 mm 24-25 (Clovis and Fort Sumner maps), NM 333 Tijeras (Albuquerque), 'throughout Socorro area: standing water' (Socorro). Just outside the region, closed due to flooding: NM 94 at Ledoux and NM 273 at Santa Teresa.
- **Effort:** About 1 to 1.5 days.
  - weather/roads.py NMRoads part: ~150-200 lines. Fetch, join, strip HTML and '<autodescriptiondelimiter>', parse RSS dates as America/Denver, classify by title prefix plus a flood regex, dedupe, filter per site via MapFrame.bbox.
  - If-Modified-Since in http.py: +20-30 lines.
  - Map drawing in radar.py: +60-90 lines.
  - Roads block and footer in page.py: +80-120 lines.
  - config.py: +15-25 lines.
  - Tests: ~150-250 lines with fixtures.
  - No secrets. 2 requests per run, usually answered 304.
- **Caveats:** - JSON timestamps are wrong by 6 h for many events (verified), so take ages from the RSS dates.
  - 86 of 93 events have a placeholder end_date of 2099.
  - Stale items stay listed: the I-25 mm 290 ramp closure was scheduled for Sep 21-22 and is still up.
  - Two duplicate groups; 16 junk road names.
  - Templated titles can overstate: US-60 Clovis says 'Roadway closed' but one lane stays open each way.
  - Area-wide conditions are a single point.
  - Long-running Alerts (wind-turbine convoys since 2024, WSMR missile firings) need filtering.
  - Covers NMDOT routes only.
  - Short closures can be missing: NM 304 at Las Nutrias appeared only in an NWS report.
  - 596 KB uncompressed when changed.
  - On a CloudFront/IIS outage, keep the last good copy and show 'as of'.
  - NMDOT's update interval is unverified. The feed appears to be regenerated within ~30 s of an edit.

### B. NWS Local Storm Reports via Iowa Environmental Mesonet: https://mesonet.agron.iastate.edu/geojson/lsr.geojson?hours=48&wfos=ABQ,LUB,MAF,EPZ,AMA

- **Verified now:** yes
- **Access:** Open, no key. NWS data is public domain. IEM asks for attribution and polite use (cache; one request per run is fine).
- **Gives:** Dated point reports (type, place, county, free-text remark) from spotters, emergency managers and NMDOT/TxDOT relays, in NM and TX. These are the only machine-readable mentions of county or local road flooding.
  
  In the last 72 h, 11 reports in the region (6 FLASH FLOOD). The road-related ones:
  - NM 304 closed at MM 11, Las Nutrias (NMDOT, 09-22 23:19Z)
  - Water over NM 252 N of House
  - Range Routes 8/9/12 impassable W of Oscuro
  - Water over FM168 and CR 262 near Spade, TX (Lubbock map)
- **Effort:** About 0.5 day.
  - ~60-100 lines in roads.py: bbox, typetext in {FLASH FLOOD, FLOOD, DEBRIS FLOW} or a remark matching closed|impassable|water over, and an age window.
  - Point drawing reuses option A's code.
  - Tests: ~80-120 lines.
  - Can use the existing cached_json helper. 1 request per run (0.2 s, a few hundred KB).
- **Caveats:** - These are reports, not closure status. No message ever says a road reopened, so show the report time and 'may have reopened' and drop items after 24-48 h.
  - Free text only.
  - Coverage depends on someone reporting to the NWS.
  - Can duplicate NMRoads items (NM 252).
  - Depends on IEM being up. The raw api.weather.gov LSR text is a fallback but needs parsing.
  - Gives nothing on crashes or construction.

### C. TxDOT DriveTexas API (full conditions feed, not the WZDx-only one): https://api.drivetexas.org/api/conditions.geojson?key=...

- **Verified now:** no
- **Access:** Key required: HTTP 401 'Unauthorized' without one (re-verified).
  - Apply with an email address and a justification on api.drivetexas.org. Approval takes up to 5 business days and may be suspended during critical weather.
  - Keys expire every March 31 and are renewed through an emailed link.
  - about.json (re-read): 'This API is intended for emergency management organizations ... The data should not be sold, rebranded, redistributed, or used for anything other than these purposes.'
  - tos.json (re-read) says 'User may use, copy, and distribute the Data in compliance with this Data Sharing and Usage Agreement'. It also says the user 'shall not use TxDOT's name, logo, trademark, or other marks without TxDOT's prior written consent', and forbids key sharing.
  - Access 'may be temporarily disabled during an emergency event'.
- **Gives:** Texas state-maintained roads (IH, US, SH, FM/RM):
  - Types: Closed, Flood (water over roadway), Damage, Accident, Snow/Ice, Construction.
  - LineString geometry with start and end times; refreshed every 5 minutes.
  - Needed for the Lubbock map and the eastern ~40% of the Clovis map.
  
  The same data seen today through the mirror:
  - Lubbock map: floods on FM1780, FM303, FM2901 and FM168; FM2008 closed (damage); damage on FM786 and FM97.
  - Clovis map: SH-214 bridge closed (Parmer Co.); Cochran/Hockley floods.
- **Effort:** Once a key exists: ~80-120 lines added to roads.py (fetch, bbox, type filter, drop events whose endTime has passed). Drawing is shared with option A.
  - Secret: WEATHER_DRIVETEXAS_KEY in weather.env, kept out of index.html, status.json and logs.
  - Tests: ~100 lines.
  - Also the application itself and a yearly renewal.
- **Caveats:** - Approval for a public hobby page is uncertain because of the 'emergency management' and 'not redistributed' wording. Ask TxDOT explicitly about public display and a source credit.
  - A revoked or expired key gives 401. Access can be switched off during emergencies, which is when flooding matters most.
  - Quality depends on TxDOT staff entering events by hand.
  - Content not verified directly (no key); only through the mirror and the legacy text site.

### D. Keyless Texas stop-gaps: TDEM ArcGIS mirror of DriveTexas (services5.arcgis.com/Rvw11bGpzJNE7apK/.../DriveTexas_API/FeatureServer/0), legacy HCRS text site (http://conditions.drivetexas.org/current/conditions.asp, POST), or a link-out to drivetexas.org

- **Verified now:** yes
- **Access:** All open, no key.
  - The mirror carries no licence of its own. It is TxDOT data held under TDEM's agreement, and its item description exposes TDEM's own key, which must not be used.
  - The legacy site's footer says 'Copyright 2023 Texas Department of Transportation, All Rights Reserved'.
  - A plain link carries no risk.
- **Gives:** - Mirror: the same TxDOT events with geometry through a bbox query (96 KB for the region; 39 features now).
  - Legacy site: current events as HTML text with no coordinates.
  - Link-out: nothing on the page itself.
- **Effort:** - Mirror: ~60 lines (ArcGIS returns GeoJSON).
  - Legacy site: ~100-150 lines of HTML scraping plus a county list; text only.
  - Link-out: ~5 lines.
- **Caveats:** - The mirror updates about hourly and was behind today. It showed FM1780/FM1779/FM1169/FM303 ending 16:00-17:00 CDT while the legacy site had them extended to 20:00 CDT. It still listed the FM2901 and FM168 floods after they ended. It can vanish without notice.
  - The legacy site answered one POST at 20:36Z, then hung. From ~20:40Z every connection timed out, while drivetexas.org itself still answered. It is HTTP only and returns duplicate records.
  - Use the mirror only for private prototyping, and on the public page use only the link-out.

## Recommendation

Build a keyless first version now:
- NMRoads (nmroads.json joined with rss.xml) for New Mexico.
- NWS Local Storm Reports via IEM for flood-related road reports in both states, including county roads, shown as dated reports.
- A DriveTexas link on the Lubbock and Clovis sections.

On the page, each site gets a Roads block listing, in order: closures, water/flooding on roads, crash/severe conditions, and lane closures on the main corridors. Routine roadwork and long-running alerts become a count plus a link. The block ends with a 'Storm reports, last 48 h' sub-list.

On the maps, draw closed or flooded segments as haloed black/white lines with symbols, and storm reports as small triangles. Do not use red (flood alerts are now red), and avoid the radar colours.

Credit NMDOT/NMRoads and NWS/IEM, and repeat the NMDOT disclaimer. Take event ages from the RSS dates in America/Denver time, not from the JSON timestamps.

Rough size: 600-850 lines including tests, no secrets, 3 small upstream requests per run.

For Texas, apply for a DriveTexas full-conditions key only if you want Texas closures on the Lubbock and Clovis maps. State the use plainly and ask TxDOT whether public display and a source credit are allowed. Do not put the TDEM mirror, the legacy HCRS site or any commercial traffic API on the public page.

## Gaps (no source exists)

- County roads, low-water crossings and arroyo crossings: no machine-readable source exists for Bernalillo, Socorro, De Baca, Curry or Lubbock counties. The Bernalillo and Curry county sites return 403 to automated clients (researcher-verified). De Baca County and the Village of Fort Sumner have no working website. Storm reports are the only partial substitute.
- Flooded city streets and playas in Lubbock, Clovis and Socorro: no feed. Lubbock's ArcGIS layer holds only planned barricade permits, with no street names and no stated licence.
- Short-lived NMDOT closures may never be posted, or may be removed quickly. The NM 304 closure at Las Nutrias (2026-09-22) shows up only in an NWS storm report.
- Reopening times: NMRoads end dates are the 2099 placeholder for 86 of 93 events, and storm reports never report a reopening. The page can only show how old an item is.
- NMRoads JSON timestamps are shifted 6 h early for many events (verified today). Event ages must come from the RSS dates.
- Crashes on rural NM highways: NMRoads has a Crash category, but none was live today, so whether crashes get posted promptly is unverified. TxDOT ITS incident lists for the Lubbock and Amarillo districts were empty (researcher).
- Texas without a DriveTexas key: no source that is sanctioned, current and has geometry. The TDEM mirror lags about an hour and has no licence of its own. The legacy text site went unreachable while I was testing it.
- White Sands missile-range closures of US-380 and US-70: the schedule is available only by phone (575-678-1178), not as data.
- Whether commercial traffic APIs cover rural NM is unverified (no keys), and their terms rule them out anyway.
- NMDOT's own update interval for nmroads.json is unverified. The feed appears to be regenerated within ~30 s of an edit.

## Open questions

1. Texas: are you willing to apply for a DriveTexas API key? That means an email address plus a justification, TxDOT approval taking up to 5 business days, a renewal every March 31, and a possible refusal or a 'no public display' answer. Or is a DriveTexas link plus NWS storm reports enough for the Lubbock map and the Texas side of the Clovis map?
2. What should be listed: only full closures and flood/weather conditions, or also lane closures and roadwork? Roadwork is about two-thirds of NMRoads events and would clutter the list.
3. Which roads matter: everything inside each 100 x 100 mi map, or a corridor list (I-25, I-40, US-60, US-84, US-380, US-70, US-285, NM-1, others)? Local roads are not available from any feed.
4. Map lines and markers in the first version, or a text list only? If lines: is a black/white haloed line with symbols acceptable, given that flood areas are now red?
5. Storm reports: show them for 24 h or 48 h? Only reports that mention a road, or every flash-flood report?
6. For Albuquerque: also list city-street crashes and closures from ABQRoads (updated every few minutes; requires the City of Albuquerque notice)?
7. Should the feature sit behind a config switch (for example WEATHER_ROADS=0/1) so it can be turned off if a source misbehaves?
8. Is it OK to add If-Modified-Since support to http.py? Without it, the NMRoads files alone add about 210 MB of downloads a day.
