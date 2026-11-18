#!/usr/bin/env python3
"""Build app/map_layers.json: county lines and Interstate / US / state highway lines for the site map.

This is a ONE-TIME helper, not part of the hourly pipeline. The result is a small static file that is committed
next to the app (the app only reads it). Re-run it only if the map window or the road/county selection changes.

Sources (US Census Bureau, public domain; no key needed):
  * counties : cartographic boundary file cb_2023_us_county_500k (generalized, good for maps)
  * roads    : TIGER/Line 2023 "primary and secondary roads" for Ohio (Interstates, US routes, state routes)

    pip install pyshp
    python scripts/build_map_layers.py --out app/map_layers.json
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

COUNTY_URL = "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_500k.zip"
ROADS_URL = "https://www2.census.gov/geo/tiger/TIGER2023/PRISECROADS/tl_2023_39_prisecroads.zip"
OHIO = "39"
# Map window in degrees: a little bigger than the area covered by the sites in sites.yaml.
WEST, EAST, SOUTH, NORTH = -83.90, -82.10, 39.35, 40.65
ROAD_TOLERANCE = 0.0006      # degrees (about 65 m): drops points that do not change the line visibly
COUNTY_TOLERANCE = 0.0008

ROUTE_PATTERNS = (            # (class, regex on the TIGER FULLNAME, label)
    ("interstate", re.compile(r"^I-\s*(\d+)"), "I-{}"),
    ("us", re.compile(r"^US (?:Hwy|Rte)\s+(\d+)"), "US {}"),
    ("state", re.compile(r"^State Rte\s+(\d+)"), "SR {}"),
)


def classify(fullname: str) -> tuple[str, str] | None:
    """('interstate'|'us'|'state', label like 'I-70') for a TIGER road name, or None for anything else."""
    for kind, pattern, label in ROUTE_PATTERNS:
        m = pattern.match((fullname or "").strip())
        if m:
            return kind, label.format(m.group(1))
    return None


def inside(p) -> bool:
    return WEST <= p[0] <= EAST and SOUTH <= p[1] <= NORTH


def clip_segment(a, b):
    """Liang-Barsky: the part of segment a-b inside the window, or None."""
    (x0, y0), (x1, y1) = a, b
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - WEST), (dx, EAST - x0), (-dy, y0 - SOUTH), (dy, NORTH - y0)):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            if t > t1:
                return None
            t0 = max(t0, t)
        else:
            if t < t0:
                return None
            t1 = min(t1, t)
    return (x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy)


def clip_line(points) -> list[list[tuple]]:
    """Split a polyline into the pieces that lie inside the window."""
    pieces, current = [], []
    for a, b in zip(points, points[1:]):
        seg = clip_segment(a, b)
        if seg is None:
            if len(current) > 1:
                pieces.append(current)
            current = []
            continue
        s, e = seg
        if current and current[-1] == s:
            current.append(e)
        else:
            if len(current) > 1:
                pieces.append(current)
            current = [s, e]
    if len(current) > 1:
        pieces.append(current)
    return pieces


def simplify(points, tol):
    """Ramer-Douglas-Peucker (iterative)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        (x0, y0), (x1, y1) = points[i], points[j]
        dx, dy = x1 - x0, y1 - y0
        norm = (dx * dx + dy * dy) ** 0.5
        far, far_d = None, tol
        for k in range(i + 1, j):
            x, y = points[k]
            d = (abs(dy * (x - x0) - dx * (y - y0)) / norm) if norm else ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
            if d > far_d:
                far, far_d = k, d
        if far is not None:
            keep[far] = True
            stack += [(i, far), (far, j)]
    return [p for p, k in zip(points, keep) if k]


def rounded(points):
    out = []
    for x, y in points:
        p = [round(x, 4), round(y, 4)]
        if not out or out[-1] != p:
            out.append(p)
    return out


def ring_centroid(ring):
    """Area centroid of a polygon ring (lon, lat)."""
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:]):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if a == 0:
        return ring[0]
    return cx / (3 * a), cy / (3 * a)


def build(counties_shp: Path, roads_shp: Path) -> dict:
    import shapefile                      # pyshp; only needed to build the file, never by the app

    counties = []
    reader = shapefile.Reader(str(counties_shp))
    for sr in reader.iterShapeRecords():
        if sr.record["STATEFP"] != OHIO:
            continue
        x0, y0, x1, y1 = sr.shape.bbox
        if x1 < WEST or x0 > EAST or y1 < SOUTH or y0 > NORTH:
            continue
        parts = list(sr.shape.parts) + [len(sr.shape.points)]
        rings = [[tuple(p) for p in sr.shape.points[a:b]] for a, b in zip(parts, parts[1:])]
        lines = []
        for ring in rings:
            for piece in clip_line(ring):
                piece = rounded(simplify(piece, COUNTY_TOLERANCE))
                if len(piece) > 1:
                    lines.append(piece)
        biggest = max(rings, key=len)
        label = ring_centroid(biggest)
        if not inside(label):
            clipped = [p for line in lines for p in line]
            label = (sum(p[0] for p in clipped) / len(clipped), sum(p[1] for p in clipped) / len(clipped))
        counties.append({"name": sr.record["NAME"], "label": [round(label[0], 4), round(label[1], 4)], "lines": lines})

    roads: dict[str, dict[str, list]] = {"interstate": {}, "us": {}, "state": {}}
    reader = shapefile.Reader(str(roads_shp))
    for sr in reader.iterShapeRecords():
        kind_label = classify(sr.record["FULLNAME"])
        if kind_label is None:
            continue
        kind, label = kind_label
        parts = list(sr.shape.parts) + [len(sr.shape.points)]
        for a, b in zip(parts, parts[1:]):
            for piece in clip_line([tuple(p) for p in sr.shape.points[a:b]]):
                piece = rounded(simplify(piece, ROAD_TOLERANCE))
                if len(piece) > 1:
                    roads[kind].setdefault(label, []).append(piece)
    return {
        "source": "US Census Bureau: cb_2023_us_county_500k and TIGER/Line 2023 primary+secondary roads (Ohio)",
        "window": {"west": WEST, "east": EAST, "south": SOUTH, "north": NORTH},
        "counties": counties,
        "roads": {kind: [{"ref": ref, "lines": lines} for ref, lines in sorted(by_ref.items())]
                  for kind, by_ref in roads.items()},
    }


def fetch_zip(url: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    if not list(dest.glob("*.shp")):
        print(f"downloading {url}", file=sys.stderr)
        with urllib.request.urlopen(url, timeout=120) as resp:
            zipfile.ZipFile(io.BytesIO(resp.read())).extractall(dest)
    return next(dest.glob("*.shp"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=Path("app/map_layers.json"))
    ap.add_argument("--cache", type=Path, default=Path(".map_cache"), help="where the downloads are unpacked")
    args = ap.parse_args()
    data = build(fetch_zip(COUNTY_URL, args.cache / "counties"), fetch_zip(ROADS_URL, args.cache / "roads"))
    args.out.write_text(json.dumps(data, separators=(",", ":")))
    n_roads = {k: len(v) for k, v in data["roads"].items()}
    print(f"wrote {args.out} ({args.out.stat().st_size / 1024:.0f} KB): {len(data['counties'])} counties, routes {n_roads}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
