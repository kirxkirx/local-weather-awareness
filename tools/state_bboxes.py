#!/usr/bin/env python3
"""Print ``weather.states.STATE_BOXES`` or ``MARINE_BOXES`` from a shapefile (stdlib only).

``STATE_BOXES`` was made with this script from the U.S. Census Bureau's 2024 TIGER/Line
"States and Equivalent Entities" file:

    curl -O https://www2.census.gov/geo/tiger/TIGER2024/STATE/tl_2024_us_state.zip
    unzip tl_2024_us_state.zip
    python3 tools/state_bboxes.py tl_2024_us_state

It reads the ``.shp`` polygons and the ``.dbf`` attributes (``STUSPS``: the two-letter code
that api.weather.gov's ``/alerts/active?area=`` also uses; ``NAME``). Every entity's box is
the minimum and maximum of all its vertices, rounded outward to 0.01 degrees. The vertices
west and east of the prime meridian are boxed separately, so Alaska, whose western
Aleutians lie beyond 180 degrees (at positive longitudes), gets two boxes instead of one
spanning the globe; every other entity lies in one hemisphere and gets one box.

``MARINE_BOXES`` was made from the National Weather Service's coastal and offshore marine
zone files (https://www.weather.gov/gis/MarineZones, valid from 16 April 2026):

    curl -O https://www.weather.gov/source/gis/Shapefiles/WSOM/mz16ap26.zip
    curl -O https://www.weather.gov/source/gis/Shapefiles/WSOM/oz16ap26.zip
    unzip mz16ap26.zip && unzip oz16ap26.zip
    python3 tools/state_bboxes.py --marine mz16ap26 oz16ap26

A marine zone's ``ID`` (``LMZ740``) starts with its NWS marine area code (``LM``, Lake
Michigan), which ``/alerts/active?area=`` accepts like a state code. One box per area would
be far too large (the Atlantic area ``AM`` reaches from North Carolina to the Caribbean, so
its box would cover Georgia and Florida inland), so every zone is boxed on its own and the
boxes of one area are then merged, closest pair first, only while a merge adds at most
``MERGE_MAX_ADDED`` square degrees of area that neither box covered. Boxes on the two sides
of the prime meridian are never merged (they would span the globe).
"""
import math
import struct
import sys

# A merged box may cover at most this much more area (square degrees) than its two parts
# together: 1.0 keeps the table at about 190 boxes, and over a 0.5-degree grid of 100-mile
# maps from Hawaii to Maine it adds an area that no zone box touches for 0.6 % of the maps.
MERGE_MAX_ADDED = 1.0

# The NWS marine area codes (api.weather.gov MarineAreaCode), shortened for the comments.
MARINE_NAMES = {
    "AM": "Atlantic south of Currituck Beach Light NC, Florida Keys, Caribbean",
    "AN": "Atlantic from the Canadian border to Currituck Beach Light NC",
    "GM": "Gulf of Mexico",
    "LC": "Lake St. Clair",
    "LE": "Lake Erie",
    "LH": "Lake Huron",
    "LM": "Lake Michigan",
    "LO": "Lake Ontario",
    "LS": "Lake Superior",
    "PH": "Central Pacific, Hawaiian waters",
    "PK": "North Pacific, Alaskan waters",
    "PM": "Western Pacific, Mariana Islands waters",
    "PS": "South Central Pacific, American Samoa waters",
    "PZ": "Eastern North Pacific, US West Coast",
    "SL": "St. Lawrence River",
}


def read_dbf(path):
    """The records of a dBASE III file as a list of {field name: stripped text}."""
    with open(path, "rb") as f:
        data = f.read()
    nrec, hlen, rlen = struct.unpack("<IHH", data[4:12])
    fields = []
    pos = 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0", 1)[0].decode("ascii")
        fields.append((name, data[pos + 16]))
        pos += 32
    rows = []
    for i in range(nrec):
        rec = data[hlen + i * rlen:hlen + (i + 1) * rlen]
        off, row = 1, {}                    # byte 0 is the deletion flag
        for name, flen in fields:
            row[name] = rec[off:off + flen].decode("utf-8", "replace").strip()
            off += flen
        rows.append(row)
    return rows


def read_boxes(path):
    """One list of boxes per polygon record of a .shp file: [west box, east box], each
    present only when the record has vertices in that hemisphere."""
    with open(path, "rb") as f:
        data = f.read()
    pos, out = 100, []
    while pos < len(data):
        clen = struct.unpack(">i", data[pos + 4:pos + 8])[0] * 2
        content = data[pos + 8:pos + 8 + clen]
        pos += 8 + clen
        if struct.unpack("<i", content[:4])[0] != 5:
            raise SystemExit("not a polygon shapefile")
        nparts, npoints = struct.unpack("<ii", content[36:44])
        off = 44 + 4 * nparts
        xy = struct.unpack("<%dd" % (2 * npoints), content[off:off + 16 * npoints])
        boxes = {}
        for k in range(npoints):
            x, y = xy[2 * k], xy[2 * k + 1]
            b = boxes.setdefault(x >= 0, [x, y, x, y])
            b[0], b[1], b[2], b[3] = min(b[0], x), min(b[1], y), max(b[2], x), max(b[3], y)
        out.append([boxes[h] for h in (False, True) if h in boxes])
    return out


def outward(b):
    """The box rounded outward to 0.01 degrees, but never past +-180 / +-90 (zones on the
    antimeridian end at 180.0000001 in the NWS files)."""
    return (max(-180.0, math.floor(b[0] * 100) / 100), max(-90.0, math.floor(b[1] * 100) / 100),
            min(180.0, math.ceil(b[2] * 100) / 100), min(90.0, math.ceil(b[3] * 100) / 100))


def _area(b):
    return (b[2] - b[0]) * (b[3] - b[1])


def _added(a, b):
    """(area the box around a and b covers beyond a and b, that box)."""
    m = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    both = w * h if w > 0 and h > 0 else 0.0
    return _area(m) - (_area(a) + _area(b) - both), m


def merge_boxes(boxes, max_added=MERGE_MAX_ADDED):
    """Merge boxes, the pair whose common box adds the least area first, while that added
    area is at most ``max_added``. Returns the boxes sorted."""
    boxes = [tuple(b) for b in boxes]
    while len(boxes) > 1:
        best = None
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                added, m = _added(boxes[i], boxes[j])
                if added <= max_added and (best is None or added < best[0]):
                    best = (added, i, j, m)
        if best is None:
            break
        _, i, j, m = best
        boxes = [b for k, b in enumerate(boxes) if k not in (i, j)] + [m]
    return sorted(boxes)


def _load(prefix):
    rows = read_dbf(prefix + ".dbf")
    shapes = read_boxes(prefix + ".shp")
    if len(rows) != len(shapes):
        raise SystemExit("%s: .dbf and .shp disagree on the number of records" % prefix)
    return rows, shapes


def _print_entry(code, boxes, name):
    text = [("(%.2f, %.2f, %.2f, %.2f)" % outward(b)) for b in boxes]
    if len(text) == 1:
        print('    "%s": (%s,),  # %s' % (code, text[0], name))
        return
    lines = [", ".join(text[i:i + 2]) for i in range(0, len(text), 2)]
    print('    "%s": (  # %s' % (code, name))
    for n, line in enumerate(lines):
        print("        %s%s" % (line, "," if n < len(lines) - 1 else "),"))


def states_main(prefix):
    rows, shapes = _load(prefix)
    table = {row["STUSPS"]: (row["NAME"], [outward(b) for b in boxes])
             for row, boxes in zip(rows, shapes)}
    for code in sorted(table):
        name, boxes = table[code]
        text = ", ".join("(%.2f, %.2f, %.2f, %.2f)" % b for b in boxes)
        print('    "%s": (%s%s),  # %s' % (code, text, "," if len(boxes) == 1 else "", name))


def marine_main(prefixes):
    zones = {}                              # area code -> {hemisphere: [zone boxes]}
    for prefix in prefixes:
        rows, shapes = _load(prefix)
        for row, boxes in zip(rows, shapes):
            zid = (row.get("ID") or "").strip().upper()
            if len(zid) < 3 or not zid[:2].isalpha():
                raise SystemExit("%s: a record without a marine zone ID: %r" % (prefix, row))
            for b in boxes:
                zones.setdefault(zid[:2], {}).setdefault(b[0] >= 0, []).append(b)
    for code in sorted(zones):
        merged = []
        for hemisphere in (False, True):
            merged += merge_boxes(zones[code].get(hemisphere, []))
        _print_entry(code, merged, MARINE_NAMES.get(code, "marine area " + code))


def main(argv):
    if len(argv) >= 3 and argv[1] == "--marine":
        marine_main(argv[2:])
    elif len(argv) == 2 and not argv[1].startswith("-"):
        states_main(argv[1])
    else:
        raise SystemExit("usage: state_bboxes.py <state shapefile path without .shp>\n"
                         "       state_bboxes.py --marine <marine zone shapefile> ...")


if __name__ == "__main__":
    main(sys.argv)
