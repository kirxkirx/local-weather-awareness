"""Tests for weather.sun: NOAA solar equations, twilight events and the page summary.

Reference times come from the NOAA solar calculator (https://gml.noaa.gov/grad/solcalc/)
for Albuquerque; the tolerance is 3 minutes, comfortably above the contract's 2-minute
accuracy target and the calculator's own 1-minute display rounding. No network, no fixtures
from conftest are needed: the module is pure arithmetic.

The ``upcoming`` list is checked against an independent oracle: scanning the altitude
minute by minute over the window and counting threshold crossings / local maxima, so a
missing or duplicated event (midnight, DST, odd zone offsets) cannot hide behind the
implementation's own use of ``day_events``."""
from datetime import date, datetime, timedelta, timezone

import pytest

from weather import sun, util

ABQ = (35.0844, -106.6504)
DENVER = "America/Denver"


def _local(y, m, d, hh, mm, tzname=DENVER):
    return datetime(y, m, d, hh, mm, tzinfo=util.tzinfo_for(tzname))


def _minutes_apart(a, b):
    return abs((a - b).total_seconds()) / 60.0


# ---- day_events against NOAA -------------------------------------------------------
@pytest.mark.parametrize("day, sunrise, sunset", [
    (date(2026, 9, 23), (6, 55), (19, 3)),      # near the autumn equinox (task's example)
    (date(2026, 6, 21), (5, 52), (20, 24)),     # summer solstice, MDT
    (date(2026, 12, 21), (7, 11), (16, 58)),    # winter solstice, MST
])
def test_albuquerque_sunrise_sunset_match_noaa(day, sunrise, sunset):
    ev = sun.day_events(*ABQ, day, DENVER)
    assert _minutes_apart(ev["sunrise"], _local(day.year, day.month, day.day, *sunrise)) < 3
    assert _minutes_apart(ev["sunset"], _local(day.year, day.month, day.day, *sunset)) < 3
    # solar noon sits midway between sunrise and sunset (to well under a minute here)
    mid = ev["sunrise"] + (ev["sunset"] - ev["sunrise"]) / 2
    assert _minutes_apart(ev["solar_noon"], mid) < 1


def test_day_events_shape_and_local_zone():
    ev = sun.day_events(*ABQ, date(2026, 9, 23), DENVER)
    assert set(ev) == {"sunrise", "solar_noon", "sunset", "civil_dawn", "civil_dusk",
                       "nautical_dawn", "nautical_dusk", "astro_dawn", "astro_dusk"}
    for k, v in ev.items():
        assert isinstance(v, datetime) and v.tzinfo is not None, k
        assert v.utcoffset() == timedelta(hours=-6), k        # MDT on 2026-09-23
        assert v.date() == date(2026, 9, 23), k


def test_twilight_order_and_spacing():
    ev = sun.day_events(*ABQ, date(2026, 9, 23), DENVER)
    assert (ev["astro_dawn"] < ev["nautical_dawn"] < ev["civil_dawn"] < ev["sunrise"]
            < ev["solar_noon"]
            < ev["sunset"] < ev["civil_dusk"] < ev["nautical_dusk"] < ev["astro_dusk"])
    # each 6-degree twilight band takes ~25-30 min at 35 N near the equinox
    for a, b in (("sunset", "civil_dusk"), ("civil_dusk", "nautical_dusk"),
                 ("nautical_dusk", "astro_dusk")):
        assert 20 < _minutes_apart(ev[a], ev[b]) < 40, (a, b)


def test_events_land_on_their_defining_altitude():
    """The event solver and the altitude function must agree with each other: evaluating
    the altitude at each computed event time returns that event's threshold."""
    ev = sun.day_events(*ABQ, date(2026, 6, 21), DENVER)
    for key, alt in (("sunrise", -0.833), ("sunset", -0.833), ("civil_dawn", -6.0),
                     ("civil_dusk", -6.0), ("nautical_dawn", -12.0), ("nautical_dusk", -12.0),
                     ("astro_dawn", -18.0), ("astro_dusk", -18.0)):
        assert sun.sun_altitude_deg(*ABQ, ev[key]) == pytest.approx(alt, abs=0.02), key


def test_equator_equinox_day_is_about_twelve_hours():
    ev = sun.day_events(0.0, 0.0, date(2026, 3, 20), "UTC")
    day_len_min = (ev["sunset"] - ev["sunrise"]).total_seconds() / 60.0
    # 12 h plus ~7 min because sunrise/sunset use -0.833 deg (refraction + semi-diameter)
    assert abs(day_len_min - 12 * 60) < 10
    assert _minutes_apart(ev["solar_noon"], datetime(2026, 3, 20, 12, 7, tzinfo=timezone.utc)) < 3


def test_dst_transition_keeps_correct_offsets():
    before = sun.day_events(*ABQ, date(2026, 10, 31), DENVER)     # last day of MDT
    after = sun.day_events(*ABQ, date(2026, 11, 1), DENVER)       # fall-back day, MST
    assert before["sunset"].utcoffset() == timedelta(hours=-6)
    assert after["sunset"].utcoffset() == timedelta(hours=-7)
    # the clock jumps back an hour while the sun does not: local sunset ~1 h "earlier"

    def clock_min(dt):
        return dt.hour * 60 + dt.minute + dt.second / 60.0
    assert 55 < clock_min(before["sunset"]) - clock_min(after["sunset"]) < 65
    # ...but in absolute terms consecutive sunsets are ~1 min apart plus one day
    assert 22 * 60 < (after["sunset"] - before["sunset"]).total_seconds() / 60 < 26 * 60


@pytest.mark.parametrize("lat, lon, day, tzname, sunrise, sunset", [
    (-33.87, 151.21, date(2026, 9, 23), "Australia/Sydney", (5, 44), (17, 52)),
    (35.6762, 139.6503, date(2026, 9, 23), "Asia/Tokyo", (5, 30), (17, 38)),
    (21.31, -157.86, date(2026, 9, 23), "Pacific/Honolulu", (6, 21), (18, 26)),
])
def test_other_longitudes_stay_on_the_local_date(lat, lon, day, tzname, sunrise, sunset):
    """Far from Greenwich the UTC date of solar noon differs from the local date; the
    'nearest solar noon to local noon' anchor must still land on the requested day."""
    ev = sun.day_events(lat, lon, day, tzname)
    for k in ("sunrise", "sunset", "solar_noon"):
        assert ev[k].date() == day, k
    assert _minutes_apart(ev["sunrise"], _local(day.year, day.month, day.day, *sunrise, tzname=tzname)) < 3
    assert _minutes_apart(ev["sunset"], _local(day.year, day.month, day.day, *sunset, tzname=tzname)) < 3


def test_polar_midnight_sun_and_polar_night():
    tromso = (69.65, 18.96)
    june = sun.day_events(*tromso, date(2026, 6, 21), "Europe/Oslo")
    assert june["sunrise"] is None and june["sunset"] is None          # midnight sun
    assert june["civil_dusk"] is None and june["astro_dusk"] is None   # never gets dark
    assert june["solar_noon"] is not None
    assert sun.sun_altitude_deg(*tromso, june["solar_noon"] + timedelta(hours=12)) > 0
    dec = sun.day_events(*tromso, date(2026, 12, 21), "Europe/Oslo")
    assert dec["sunrise"] is None and dec["sunset"] is None            # polar night
    assert dec["civil_dawn"] < dec["solar_noon"] < dec["civil_dusk"]   # but twilight exists
    assert sun.sun_altitude_deg(*tromso, dec["solar_noon"]) < 0
    # the pole itself must not raise either
    pole = sun.day_events(90.0, 0.0, date(2026, 6, 21), "UTC")
    assert pole["sunrise"] is None and pole["solar_noon"] is not None


def test_day_events_accepts_datetime_and_unknown_tz():
    d1 = sun.day_events(*ABQ, date(2026, 9, 23), DENVER)
    d2 = sun.day_events(*ABQ, datetime(2026, 9, 23, 15, 30), DENVER)
    assert d1 == d2
    utc = sun.day_events(*ABQ, date(2026, 9, 23), "Not/AZone")       # util falls back to UTC
    assert utc["sunset"].utcoffset() == timedelta(0)
    assert _minutes_apart(utc["sunset"], d1["sunset"]) < 0.01           # same instant
    with pytest.raises(TypeError):
        sun.day_events(*ABQ, "2026-09-23", DENVER)


# ---- sun_altitude_deg ---------------------------------------------------------------
def test_altitude_at_solar_noon_is_ninety_minus_lat_minus_decl():
    for day in (date(2026, 6, 21), date(2026, 9, 23), date(2026, 12, 21)):
        ev = sun.day_events(*ABQ, day, DENVER)
        noon_utc = ev["solar_noon"].astimezone(timezone.utc)
        decl, _eot = sun._sun_position(sun._julian_century(noon_utc))
        expect = 90.0 - abs(ABQ[0] - decl)
        assert sun.sun_altitude_deg(*ABQ, ev["solar_noon"]) == pytest.approx(expect, abs=0.05)
    # solstice declination is the obliquity of the ecliptic, ~23.44 deg
    ev = sun.day_events(*ABQ, date(2026, 6, 21), DENVER)
    assert sun.sun_altitude_deg(*ABQ, ev["solar_noon"]) == pytest.approx(90 - (35.0844 - 23.44), abs=0.1)


def test_altitude_naive_datetime_is_utc_and_night_is_negative():
    aware = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)        # 03:00 MDT: deep night
    naive = datetime(2026, 9, 23, 9, 0)
    assert sun.sun_altitude_deg(*ABQ, aware) == sun.sun_altitude_deg(*ABQ, naive)
    assert sun.sun_altitude_deg(*ABQ, aware) < -30
    # a non-UTC aware datetime is converted, not misread
    mdt = datetime(2026, 9, 23, 3, 0, tzinfo=util.tzinfo_for(DENVER))
    assert sun.sun_altitude_deg(*ABQ, mdt) == pytest.approx(sun.sun_altitude_deg(*ABQ, aware))
    assert -90 <= sun.sun_altitude_deg(*ABQ, aware) <= 90


# ---- sun_summary ----------------------------------------------------------------------
def test_sun_summary_structure_evening_then_next_morning():
    """Backward-compatible keys (the page now renders ``upcoming``, tested below)."""
    now = datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)        # 14:00 MDT, afternoon
    s = sun.sun_summary(*ABQ, DENVER, now_utc=now)
    assert s["tz"] == DENVER
    assert s["now_local"] == now.astimezone(util.tzinfo_for(DENVER))
    assert s["sun_alt_deg"] > 40 and s["is_night"] is False
    assert s["today"] == sun.day_events(*ABQ, date(2026, 9, 23), DENVER)
    assert s["tomorrow"] == sun.day_events(*ABQ, date(2026, 9, 24), DENVER)
    assert [n for n, _ in s["evening"]] == ["Sunset", "Civil dusk", "Nautical dusk", "Astro dusk"]
    assert [n for n, _ in s["morning"]] == ["Astro dawn", "Nautical dawn", "Civil dawn", "Sunrise"]
    ev_times = [t for _, t in s["evening"]]
    mo_times = [t for _, t in s["morning"]]
    assert ev_times == sorted(ev_times) and mo_times == sorted(mo_times)
    assert ev_times[-1] < mo_times[0]                                 # morning follows evening
    assert all(t.date() == date(2026, 9, 23) for t in ev_times)
    assert all(t.date() == date(2026, 9, 24) for t in mo_times)
    assert s["evening"][0][1] == s["today"]["sunset"]
    assert s["morning"][-1][1] == s["tomorrow"]["sunrise"]


def test_sun_summary_is_night_follows_civil_twilight():
    ev = sun.day_events(*ABQ, date(2026, 9, 23), DENVER)
    just_before = ev["civil_dusk"] - timedelta(minutes=3)
    just_after = ev["civil_dusk"] + timedelta(minutes=3)
    assert sun.sun_summary(*ABQ, DENVER, now_utc=just_before)["is_night"] is False
    assert sun.sun_summary(*ABQ, DENVER, now_utc=just_after)["is_night"] is True
    midnight = datetime(2026, 9, 24, 6, 0, tzinfo=timezone.utc)      # 00:00 MDT on the 24th
    s = sun.sun_summary(*ABQ, DENVER, now_utc=midnight)
    assert s["is_night"] is True and s["now_local"].date() == date(2026, 9, 24)
    assert s["today"]["sunrise"].date() == date(2026, 9, 24)


def test_sun_summary_defaults_to_now_and_tolerates_missing_events():
    s = sun.sun_summary(*ABQ, DENVER)                                 # now_utc defaults
    assert isinstance(s["now_local"], datetime) and s["now_local"].tzinfo is not None
    assert isinstance(s["is_night"], bool)
    assert s["phase"] in ("day", "civil twilight", "nautical twilight",
                          "astronomical twilight", "night")
    assert isinstance(s["upcoming"], list) and s["upcoming"]
    polar = sun.sun_summary(69.65, 18.96, "Europe/Oslo",
                            now_utc=datetime(2026, 6, 21, 12, tzinfo=timezone.utc))
    assert len(polar["evening"]) == 4 and len(polar["morning"]) == 4
    assert all(t is None for _, t in polar["evening"])                # midnight sun
    assert polar["is_night"] is False and polar["phase"] == "day"
    # midnight sun: the only event left in the next 24 h is a solar noon
    assert polar["upcoming"] and {it["label"] for it in polar["upcoming"]} == {"Solar noon"}


# ---- phase --------------------------------------------------------------------------------
@pytest.mark.parametrize("alt, phase", [
    (45.0, "day"), (0.0, "day"), (-0.833, "day"), (-0.84, "civil twilight"),
    (-2.5, "civil twilight"), (-6.0, "civil twilight"), (-6.01, "nautical twilight"),
    (-12.0, "nautical twilight"), (-12.01, "astronomical twilight"),
    (-18.0, "astronomical twilight"), (-18.01, "night"), (-80.0, "night"),
])
def test_sun_phase_bands(alt, phase):
    assert sun.sun_phase(alt) == phase


def test_sun_summary_phase_just_after_sunset_is_twilight_not_day():
    """Review case: 9 min after sunset the sun is -2.5 deg; that is civil twilight (the
    palette's is_night is still False, which is what the page's default follows)."""
    now = datetime(2026, 9, 24, 1, 10, tzinfo=timezone.utc)          # 19:10 MDT on the 23rd
    s = sun.sun_summary(*ABQ, DENVER, now_utc=now)
    assert -3.5 < s["sun_alt_deg"] < -1.5
    assert s["phase"] == "civil twilight" and s["is_night"] is False
    ev = sun.day_events(*ABQ, date(2026, 9, 23), DENVER)
    for key, phase in (("solar_noon", "day"),
                       ("sunset", "civil twilight"), ("civil_dusk", "nautical twilight"),
                       ("nautical_dusk", "astronomical twilight"), ("astro_dusk", "night")):
        # a few seconds after each event the sun is in the next band down
        assert sun.sun_summary(*ABQ, DENVER, now_utc=ev[key] + timedelta(seconds=20))["phase"] == phase, key


# ---- upcoming -------------------------------------------------------------------------------
CYCLE = ["Astro dawn", "Nautical dawn", "Civil dawn", "Sunrise", "Solar noon",
         "Sunset", "Civil dusk", "Nautical dusk", "Astro dusk"]
# threshold events: (label, altitude, rising?)
_CROSSINGS = [("Astro dawn", -18.0, True), ("Nautical dawn", -12.0, True),
              ("Civil dawn", -6.0, True), ("Sunrise", -0.833, True),
              ("Sunset", -0.833, False), ("Civil dusk", -6.0, False),
              ("Nautical dusk", -12.0, False), ("Astro dusk", -18.0, False)]


def _utc(dt):
    """Absolute-time arithmetic needs UTC: ``aware_local + timedelta`` is wall-clock
    arithmetic in Python and is an hour off across a DST change."""
    return dt.astimezone(timezone.utc)


def _oracle_counts(lat, lon, now):
    """Label -> how many times that event happens in [now - 30 min, now + 24 h], found by
    sampling the altitude every minute (threshold crossings, and local maxima for noon)."""
    lo = _utc(now) - timedelta(minutes=30)
    alts = [sun.sun_altitude_deg(lat, lon, lo + timedelta(minutes=i)) for i in range(24 * 60 + 31)]
    counts = {label: 0 for label in CYCLE}
    for a, b in zip(alts, alts[1:]):
        for label, thr, rising in _CROSSINGS:
            if (rising and a < thr <= b) or (not rising and a >= thr > b):
                counts[label] += 1
    for a, b, c in zip(alts, alts[1:], alts[2:]):
        if b > a and b >= c:
            counts["Solar noon"] += 1
    return counts


def _check_upcoming(lat, lon, tzname, now):
    """The C1 invariants every ``upcoming`` list must satisfy; returns the list."""
    items = sun.sun_summary(lat, lon, tzname, now_utc=now)["upcoming"]
    tz = util.tzinfo_for(tzname)
    lo, hi = _utc(now) - timedelta(minutes=30), _utc(now) + timedelta(hours=24)
    for it in items:
        assert set(it) == {"label", "dt", "passed"}
        assert it["label"] in CYCLE
        dt = it["dt"]
        assert dt.tzinfo is not None and dt.utcoffset() == dt.astimezone(tz).utcoffset(), it
        assert lo <= dt <= hi, it                                     # inside the window
        assert it["passed"] is (dt < now), it
    # strictly chronological in absolute time (same-tzinfo comparison would ignore fold)
    times = [it["dt"].timestamp() for it in items]
    assert all(a < b for a, b in zip(times, times[1:]))
    labels = [it["label"] for it in items]
    cycle = [label for label in CYCLE if label in labels]            # polar: some never occur
    for a, b in zip(labels, labels[1:]):                              # nothing skipped between
        assert cycle[(cycle.index(a) + 1) % len(cycle)] == b, labels
    got = {label: labels.count(label) for label in CYCLE}
    assert got == _oracle_counts(lat, lon, now), labels               # nothing missing at the ends
    return items


def _abq_local(y, m, d, hh, mm, fold=0):
    return datetime(y, m, d, hh, mm, fold=fold, tzinfo=util.tzinfo_for(DENVER))


@pytest.mark.parametrize("now", [
    _abq_local(2026, 9, 23, 1, 0), _abq_local(2026, 9, 23, 12, 0),
    _abq_local(2026, 9, 23, 21, 0), _abq_local(2026, 9, 23, 19, 30),
    _abq_local(2026, 10, 31, 12, 0), _abq_local(2026, 10, 31, 21, 0),
    _abq_local(2026, 11, 1, 1, 0, fold=0), _abq_local(2026, 11, 1, 1, 0, fold=1),
    _abq_local(2026, 11, 1, 12, 0), _abq_local(2026, 11, 1, 21, 0),
    _abq_local(2026, 3, 8, 1, 30), _abq_local(2026, 3, 8, 21, 0),   # spring-forward day
], ids=lambda d: d.strftime("%Y%m%d-%H%M") + ("-fold1" if d.fold else ""))
def test_upcoming_invariants_albuquerque(now):
    items = _check_upcoming(*ABQ, DENVER, now)
    # at 35 N every event happens once a day: 9 in 24 h, plus any from the 30-min look-back
    # that recur (a minute or two earlier) before the window closes
    assert 9 <= len(items) <= 11
    assert len(items) == 9 + sum(it["passed"] for it in items)


def test_upcoming_before_dawn_starts_with_todays_dawn():
    """Review case: at 01:00 the next dawn is today's (hours away), not tomorrow's."""
    now = _abq_local(2026, 9, 23, 1, 0)
    items = _check_upcoming(*ABQ, DENVER, now)
    today = sun.day_events(*ABQ, date(2026, 9, 23), DENVER)
    assert [it["label"] for it in items] == CYCLE
    assert items[0]["dt"] == today["astro_dawn"]
    assert all(it["dt"].date() == date(2026, 9, 23) and not it["passed"] for it in items)
    assert _minutes_apart(items[0]["dt"], _local(2026, 9, 23, 5, 31)) < 3


def test_upcoming_midday_runs_into_next_morning():
    now = _abq_local(2026, 9, 23, 12, 0)
    items = _check_upcoming(*ABQ, DENVER, now)
    labels = [it["label"] for it in items]
    assert labels == CYCLE[4:] + CYCLE[:4]                            # noon .. next sunrise
    assert items[0]["dt"].date() == date(2026, 9, 23)
    assert items[-1]["dt"] == sun.day_events(*ABQ, date(2026, 9, 24), DENVER)["sunrise"]


def test_upcoming_evening_after_astro_dusk_is_tomorrows_day():
    now = _abq_local(2026, 9, 23, 21, 0)                              # astro dusk was ~20:26
    items = _check_upcoming(*ABQ, DENVER, now)
    assert [it["label"] for it in items] == CYCLE
    assert all(it["dt"].date() == date(2026, 9, 24) for it in items)


def test_upcoming_keeps_the_last_half_hour_as_passed():
    now = _abq_local(2026, 9, 23, 19, 30)                             # sunset 19:01, civil dusk 19:27
    items = _check_upcoming(*ABQ, DENVER, now)
    assert [(it["label"], it["passed"]) for it in items[:3]] == [
        ("Sunset", True), ("Civil dusk", True), ("Nautical dusk", False)]
    # and 24 h later the same events of the next evening close the window, not passed
    assert [it["label"] for it in items[-2:]] == ["Sunset", "Civil dusk"]
    assert items[-1]["dt"].date() == date(2026, 9, 24) and not items[-1]["passed"]


def test_upcoming_across_fall_back():
    """On 2026-11-01 02:00 MDT clocks go back to 01:00 MST. A window opened on the evening
    before holds only MST times; one opened at noon the day before mixes both offsets; the
    two 01:00s of that morning are distinct instants but see the same nine events."""
    eve = _check_upcoming(*ABQ, DENVER, _abq_local(2026, 10, 31, 21, 0))
    assert [it["label"] for it in eve] == CYCLE
    assert all(it["dt"].utcoffset() == timedelta(hours=-7) for it in eve)
    assert all(it["dt"].date() == date(2026, 11, 1) for it in eve)

    noon = _check_upcoming(*ABQ, DENVER, _abq_local(2026, 10, 31, 12, 0))
    offsets = {it["dt"].date(): it["dt"].utcoffset() for it in noon}
    assert offsets == {date(2026, 10, 31): timedelta(hours=-6), date(2026, 11, 1): timedelta(hours=-7)}

    first = _abq_local(2026, 11, 1, 1, 0, fold=0)                    # 01:00 MDT = 07:00Z
    second = _abq_local(2026, 11, 1, 1, 0, fold=1)                   # 01:00 MST = 08:00Z
    assert (second - first) == timedelta(0)                          # same wall time...
    assert second.astimezone(timezone.utc) - first.astimezone(timezone.utc) == timedelta(hours=1)
    a = _check_upcoming(*ABQ, DENVER, first)
    b = _check_upcoming(*ABQ, DENVER, second)
    assert [it["dt"] for it in a] == [it["dt"] for it in b]
    assert [it["label"] for it in a] == CYCLE
    assert a[3]["dt"].strftime("%H:%M") == "06:27"                   # sunrise, MST (NOAA 06:28)


def test_upcoming_depends_on_zone_only_for_display():
    """The window is absolute: the same instants come back whatever zone renders them
    (a UTC fallback shifts the wall clock, not which events are listed)."""
    now = datetime(2026, 9, 24, 2, 0, tzinfo=timezone.utc)           # 20:00 MDT, 02:00 UTC next day
    den = sun.sun_summary(*ABQ, DENVER, now_utc=now)["upcoming"]
    utc = sun.sun_summary(*ABQ, "UTC", now_utc=now)["upcoming"]
    assert [i["label"] for i in den] == [i["label"] for i in utc]
    # anchored on a different local noon, the solver lands within milliseconds
    assert all(_minutes_apart(a["dt"], b["dt"]) < 1 / 60 for a, b in zip(den, utc))
    assert all(i["dt"].utcoffset() == timedelta(0) for i in utc)


def test_upcoming_sorts_by_instant_inside_the_repeated_hour(monkeypatch):
    """01:30 MDT happens before 01:10 MST on 2026-11-01 although its wall clock is later;
    a plain sort on same-zone datetimes ignores fold and gets this backwards."""
    tz = util.tzinfo_for(DENVER)
    early = datetime(2026, 11, 1, 1, 30, fold=0, tzinfo=tz)          # 07:30Z
    late = datetime(2026, 11, 1, 1, 10, fold=1, tzinfo=tz)           # 08:10Z
    assert late < early and _utc(early) < _utc(late)                 # the Python gotcha

    def fake_day_events(lat, lon, day, tzname):
        ev = dict.fromkeys(sun.EVENT_LABELS)
        if day == date(2026, 11, 1):
            ev["nautical_dusk"], ev["astro_dusk"] = early, late
        return ev
    monkeypatch.setattr(sun, "day_events", fake_day_events)
    items = sun.upcoming_events(*ABQ, DENVER, datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc))
    assert [(i["label"], i["dt"]) for i in items] == [("Nautical dusk", early), ("Astro dusk", late)]


def test_upcoming_includes_dawn_that_precedes_its_own_local_midnight():
    """Fuyuan (48.4 N, 134.3 E) keeps Beijing time, so solar noon is ~11:04 and around the
    June solstice the astronomical dawn *of 22 June* happens at ~23:28 on 21 June. Seen at
    23:45 on 20 June, that event is inside the 24 h window although it belongs to the day
    after tomorrow; the oracle in _check_upcoming fails if it is dropped."""
    fuyuan, zone = (48.36, 134.3), "Asia/Shanghai"
    now = datetime(2026, 6, 20, 23, 45, tzinfo=util.tzinfo_for(zone))
    items = _check_upcoming(*fuyuan, zone, now)
    assert items[-1]["label"] == "Astro dawn"
    assert items[-1]["dt"] == sun.day_events(*fuyuan, date(2026, 6, 22), zone)["astro_dawn"]
    assert items[-1]["dt"].date() == date(2026, 6, 21)


@pytest.mark.parametrize("lat, lon, zone, now", [
    (69.65, 18.96, "Europe/Oslo", datetime(2026, 6, 21, 12, tzinfo=timezone.utc)),   # midnight sun
    (69.65, 18.96, "Europe/Oslo", datetime(2026, 12, 21, 12, tzinfo=timezone.utc)),  # polar night
    (-33.87, 151.21, "Australia/Sydney", datetime(2026, 10, 3, 15, 30, tzinfo=timezone.utc)),  # AEST->AEDT
    (33.5779, -101.8552, "America/Chicago", datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc)),  # Lubbock
])
def test_upcoming_invariants_elsewhere(lat, lon, zone, now):
    items = _check_upcoming(lat, lon, zone, now)
    labels = {it["label"] for it in items}
    if lat > 60 and now.month == 6:
        assert labels == {"Solar noon"}
    if lat > 60 and now.month == 12:
        assert "Sunrise" not in labels and "Civil dawn" in labels


def test_upcoming_at_the_pole_does_not_raise():
    s = sun.sun_summary(90.0, 0.0, "UTC", now_utc=datetime(2026, 6, 21, tzinfo=timezone.utc))
    assert [it["label"] for it in s["upcoming"]] in (["Solar noon"], ["Solar noon", "Solar noon"])
