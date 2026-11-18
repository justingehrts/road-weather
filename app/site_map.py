"""Clickable map of the RWIS sites for the Forecast page (no Streamlit calls in here, so it is easy to test).

The map is drawn with Plotly as plain x/y lines (no map tiles, no token, no outside service): county lines and
Interstate / US / state highway lines come from `map_layers.json` (built once by scripts/build_map_layers.py),
and each site is a marker. Identity is never color alone: every site status has its own marker SHAPE too, road
classes differ in width, tone and dash, and the legend names all of them.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import plotly.graph_objects as go

import road_data as rd
from charts import PALETTES

LAYERS_FILE = Path(__file__).with_name("map_layers.json")
LAT0 = 40.0
KX = math.cos(math.radians(LAT0))      # one degree of longitude is this much shorter than one of latitude

# Status of a site in the newest forecast run, in legend order: (key, legend text, marker shape).
STATUSES = [
    ("ok", "No hazard flags", "circle"),
    ("flag", "1 type of flag", "triangle-up"),
    ("multi", "2+ types of flags", "diamond"),
    ("none", "No forecast this run", "x"),
]

MAP_COLORS = {
    "light": dict(bg="#f5f4ef", county="#b9b8af", county_text="#8a897f", state="#c4ab7a", us="#5f7fb0",
                  interstate="#2f3a4f", ok="#2a78d6", flag="#d98a00", multi="#c8372d", none="#8a8984"),
    "dark": dict(bg="#1f1f1e", county="#4b4b46", county_text="#8d8d85", state="#8a7650", us="#7f9fd0",
                 interstate="#d6dbe6", ok="#3987e5", flag="#f0a928", multi="#f0685c", none="#9a998f"),
}


def _with_alpha(hex_color: str, alpha: float) -> str:
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r},{g},{b},{alpha})"


def project(lon: float, lat: float) -> tuple[float, float]:
    """Longitude/latitude to plot x/y, so that equal distances look equal on screen."""
    return lon * KX, lat


@lru_cache(maxsize=1)
def load_layers(path: Path = LAYERS_FILE) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- site statuses
def site_points(coords: dict[str, dict], forecast_sites: dict, skipped: dict | None = None) -> list[dict]:
    """One dict per site that has coordinates: id, name, type, x, y, status, details (list of text lines)."""
    skipped = skipped or {}
    points = []
    for sid, c in coords.items():
        x, y = project(c["lon"], c["lat"])
        p = {"id": sid, "name": c["name"], "type": c["station_type"], "x": x, "y": y}
        site = forecast_sites.get(sid)
        if site is None:
            p.update(status="none", details=["No forecast this run: " + str(skipped.get(sid, "not in the newest run"))])
        else:
            iv = rd.flag_intervals(site)
            kinds = []
            if len(iv):
                for name, g in iv.groupby("name", sort=False):
                    kinds.append(f"{name}: {_when(g['start_et'].min(), g['end_et'].max())}")
            p["status"] = "ok" if not kinds else ("flag" if len(kinds) == 1 else "multi")
            p["details"] = kinds or ["No hazard flags are forecast"]
        points.append(p)
    return points


def _when(start, end) -> str:
    fmt = "%a %-I %p"
    return f"{start.strftime(fmt)} to {end.strftime(fmt)}"


def click_to_site(selection, known: set[str] | list[str]) -> str | None:
    """The site id from a Streamlit plotly selection event, or None (nothing clicked, or not a site marker)."""
    try:
        points = selection["selection"]["points"]
    except (KeyError, TypeError):
        return None
    for pt in points or []:
        cd = pt.get("customdata")
        if isinstance(cd, (list, tuple)):
            cd = cd[0] if cd else None
        if isinstance(cd, str) and cd in known:
            return cd
    return None


# ---------------------------------------------------------------- label placement
def _line_length(line) -> float:
    return sum(math.dist(project(*a), project(*b)) for a, b in zip(line, line[1:]))


def _point_along(line, fraction: float) -> tuple[float, float]:
    total = _line_length(line)
    target, run = total * fraction, 0.0
    for a, b in zip(line, line[1:]):
        pa, pb = project(*a), project(*b)
        seg = math.dist(pa, pb)
        if seg and run + seg >= target:
            f = (target - run) / seg
            return pa[0] + f * (pb[0] - pa[0]), pa[1] + f * (pb[1] - pa[1])
        run += seg
    return project(*line[-1])


class _Placed:
    """Remembers what is already on the map so labels do not land on a site or on each other."""

    def __init__(self, window: dict, points: list[dict]):
        w = window
        self.xmin, self.xmax = project(w["west"], 0)[0], project(w["east"], 0)[0]
        self.ymin, self.ymax = w["south"], w["north"]
        self.items = [(p["x"], p["y"], 0.075) for p in points]       # keep clear of every site marker

    def free(self, x: float, y: float, clearance: float, pad: float = 0.07) -> bool:
        """Is there room for a label here? `pad` keeps it that far inside the map edge."""
        if not (self.xmin + pad < x < self.xmax - pad and self.ymin + pad < y < self.ymax - pad):
            return False
        return all(math.dist((x, y), (ox, oy)) >= max(clearance, oc) for ox, oy, oc in self.items)

    def add(self, x: float, y: float, clearance: float):
        self.items.append((x, y, clearance))


def route_label_spots(roads: list[dict], placed: _Placed, per_route: int, clearance: float) -> list[tuple[str, float, float]]:
    """Up to `per_route` label positions for each route, on its longest stretches inside the window."""
    spots = []
    for route in sorted(roads, key=lambda r: -sum(_line_length(l) for l in r["lines"])):
        found = 0
        for line in sorted(route["lines"], key=_line_length, reverse=True):
            if found >= per_route:
                break
            for frac in (0.5, 0.35, 0.65, 0.2, 0.8, 0.1, 0.9):
                x, y = _point_along(line, frac)
                if placed.free(x, y, clearance):
                    spots.append((route["ref"], x, y))
                    placed.add(x, y, clearance)
                    found += 1
                    break
    return spots


def road_points(roads: list[dict], step: float = 0.03) -> list[tuple[float, float]]:
    """Points spaced about `step` apart along the given routes (plot units), used to keep labels off the roads."""
    pts = []
    for route in roads:
        for line in route["lines"]:
            for a, b in zip(line, line[1:]):
                (x0, y0), (x1, y1) = project(*a), project(*b)
                n = max(1, int(math.dist((x0, y0), (x1, y1)) / step))
                pts += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n + 1)]
    return pts


def over_road(x: float, y: float, pts: list[tuple[float, float]], half_w: float = 0.15, half_h: float = 0.045) -> bool:
    """Would a county-name label centred here cover any of these road points?"""
    return any(abs(px - x) < half_w and abs(py - y) < half_h for px, py in pts)


# ---------------------------------------------------------------- the figure
def _lines_trace(lines, color, width, dash, name, showlegend=True) -> go.Scatter:
    xs, ys = [], []
    for line in lines:
        for lon, lat in line:
            x, y = project(lon, lat)
            xs.append(x)
            ys.append(y)
        xs.append(None)
        ys.append(None)
    return go.Scatter(x=xs, y=ys, mode="lines", name=name, showlegend=showlegend, hoverinfo="skip",
                      line=dict(color=color, width=width, dash=dash))


def build_map(points: list[dict], layers: dict | None, selected: str | None, theme: str = "light",
              compact: bool = False) -> go.Figure:
    p, m = PALETTES[theme], MAP_COLORS[theme]
    fig = go.Figure()
    window = (layers or {}).get("window") or {"west": -83.9, "east": -82.1, "south": 39.35, "north": 40.65}
    placed = _Placed(window, points)

    if layers:
        roads = layers["roads"]
        fig.add_trace(_lines_trace([l for c in layers["counties"] for l in c["lines"]], m["county"], 1.1, "dot", "County line"))
        fig.add_trace(_lines_trace([l for r in roads["state"] for l in r["lines"]], m["state"], 1.2, "solid", "State highway"))
        fig.add_trace(_lines_trace([l for r in roads["us"] for l in r["lines"]], m["us"], 2.6, "solid", "US highway"))
        interstate_lines = [l for r in roads["interstate"] for l in r["lines"]]
        fig.add_trace(_lines_trace(interstate_lines, p["surface"], 7.0, "solid", "", showlegend=False))   # casing
        fig.add_trace(_lines_trace(interstate_lines, m["interstate"], 4.2, "solid", "Interstate"))

        # Labels: Interstates first (they matter most), then US routes, then county names.
        for ref, x, y in route_label_spots(roads["interstate"], placed, per_route=2, clearance=0.2):
            fig.add_annotation(x=x, y=y, text=f"<b>{ref}</b>", showarrow=False, font=dict(size=11, color=p["surface"]),
                               bgcolor=m["interstate"], borderpad=2, opacity=0.95)
        if not compact:
            for ref, x, y in route_label_spots(roads["us"], placed, per_route=1, clearance=0.2):
                fig.add_annotation(x=x, y=y, text=ref, showarrow=False, font=dict(size=10, color=m["us"]),
                                   bgcolor=p["surface"], bordercolor=m["us"], borderwidth=1, borderpad=1, opacity=0.95)
            names = []
            interstates = road_points(roads["interstate"])      # a name plate must not hide the heavy Interstate lines
            for c in layers["counties"]:               # the centre first, then a few nudges if something is in the way
                cx, cy = project(*c["label"])
                for dx, dy in ((0, 0), (0.1, 0), (-0.1, 0), (0, 0.09), (0, -0.09), (0.1, 0.09), (-0.1, -0.09),
                               (0.1, -0.09), (-0.1, 0.09), (0, 0.16), (0, -0.16)):
                    x, y = cx + dx, cy + dy
                    if placed.free(x, y, 0.1, pad=0.1) and not over_road(x, y, interstates):
                        names.append((c["name"], x, y))
                        placed.add(x, y, 0.19)          # later names stay at least this far away from this one
                        break
            for n, x, y in names:                      # a soft plate behind each name keeps it readable over roads
                fig.add_annotation(x=x, y=y, text=f"<i>{n} Co.</i>", showarrow=False,
                                   font=dict(size=11, color=m["county_text"]), bgcolor=_with_alpha(m["bg"], 0.78), borderpad=1)

    size = 20 if compact else 15
    sel = next((q for q in points if q["id"] == selected), None)
    if sel:
        fig.add_trace(go.Scatter(x=[sel["x"]], y=[sel["y"]], mode="markers", hoverinfo="skip", showlegend=False,
                                 marker=dict(symbol="circle-open", size=size + 16, color=p["text"],
                                             line=dict(width=3, color=p["text"]))))
    for key, label, symbol in STATUSES:
        group = [q for q in points if q["status"] == key]
        selectable = key != "none"
        text = [f"<b>{q['name']}</b> ({q['type']})<br>" + "<br>".join(q["details"]) for q in group]
        fig.add_trace(go.Scatter(
            x=[q["x"] for q in group] or [None], y=[q["y"] for q in group] or [None], mode="markers",
            name=f"{label} ({len(group)})", text=text or None, hoverinfo="text" if group else "skip",
            customdata=[q["id"] for q in group] if selectable and group else None,
            marker=dict(symbol=symbol, size=size if key != "none" else size - 1, color=m[key],
                        line=dict(width=2.5 if key == "none" else 2, color=m[key] if key == "none" else p["surface"])),
            selected=dict(marker=dict(opacity=1)), unselected=dict(marker=dict(opacity=1)),
            hoverlabel=dict(bgcolor=p["surface"], font=dict(color=p["text"], size=13), bordercolor=m[key])))

    x0, x1 = project(window["west"], 0)[0], project(window["east"], 0)[0]
    fig.update_layout(
        height=380 if compact else 500, margin=dict(l=4, r=4, t=4, b=4),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor=m["bg"], font=dict(color=p["text2"], size=12),
        xaxis=dict(range=[x0, x1], visible=False, fixedrange=True, constrain="domain"),
        yaxis=dict(range=[window["south"], window["north"]], visible=False, fixedrange=True,
                   scaleanchor="x", scaleratio=1, constrain="domain"),
        dragmode=False, clickmode="event+select", hovermode="closest",
        legend=dict(orientation="h", yanchor="top", y=-0.01, xanchor="left", x=0, font=dict(color=p["text2"], size=11 if compact else 12),
                    itemclick=False, itemdoubleclick=False, tracegroupgap=0),
    )
    return fig
