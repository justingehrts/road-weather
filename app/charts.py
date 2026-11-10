"""Plotly figures for the forecast page: a meteogram and a flag timeline.

Colors follow the data-viz palette (validated for color-blind safety in light
and dark). Identity is never color alone: every line is direct-labeled and in
the legend, the observed series uses markers instead of a line, and the page
offers a table view. Text uses text colors, not series colors.
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

PALETTES = {
    "light": dict(text="#0b0b0b", text2="#52514e", grid="#e6e5e1", surface="#fcfcfb",
                  rst="#2a78d6", air="#eb6834", dew="#1baf7a", flag="#6250d6"),
    "dark": dict(text="#f0efec", text2="#c3c2b7", grid="#343432", surface="#1a1a19",
                 rst="#3987e5", air="#d95926", dew="#199e70", flag="#9085e9"),
}
FREEZING_F = 32.0
# Margins are identical on both charts so their time axes line up. The compact set is for phones.
MARGIN = dict(l=140, r=118, t=36, b=40)
MARGIN_COMPACT = dict(l=84, r=14, t=36, b=40)


def _margin(compact: bool) -> dict:
    return dict(MARGIN_COMPACT if compact else MARGIN)


def _wrap(name: str) -> str:
    """Break a long flag name over two lines (phones)."""
    words = name.split()
    if len(name) <= 12 or len(words) < 2:
        return name
    cut = max(1, len(words) // 2) if len(words) > 2 else 1
    return " ".join(words[:cut]) + "<br>" + " ".join(words[cut:])


def _base_layout(p: dict, height: int, x_range, compact: bool = False) -> dict:
    return dict(
        height=height, margin=_margin(compact),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=p["text2"], size=13),
        xaxis=dict(type="date", range=x_range, showgrid=True, gridcolor=p["grid"], zeroline=False,
                   linecolor=p["grid"], tickformat="%a\n%b %-d" if compact else "%a %-I %p\n%b %-d", nticks=5 if compact else None,
                   showspikes=True, spikemode="across", spikecolor=p["text2"], spikethickness=1,
                   spikedash="dot", spikesnap="cursor"),
    )


def _now_line(fig: go.Figure, now, p: dict, label: bool):
    fig.add_shape(type="line", x0=now, x1=now, y0=0, y1=1, xref="x", yref="paper",
                  line=dict(color=p["text2"], width=1, dash="dot"))
    if label:
        fig.add_annotation(x=now, y=1, xref="x", yref="paper", text="now", showarrow=False,
                           xanchor="left", xshift=4, yanchor="top", font=dict(color=p["text2"], size=12))


def _spread(positions: list[float], min_gap: float) -> list[float]:
    """Nudge label positions apart (keeping their order) so labels never overlap."""
    order = sorted(range(len(positions)), key=lambda i: positions[i])
    out = list(positions)
    for a, b in zip(order, order[1:]):
        if out[b] - out[a] < min_gap:
            out[b] = out[a] + min_gap
    return out


def meteogram(road: pd.DataFrame, obs: pd.DataFrame, now, theme: str = "light", compact: bool = False) -> go.Figure:
    """Forecast road surface temp, air temp, dewpoint (lines) + observed road/air/dewpoint temps (dots)."""
    from road_data import to_eastern   # local import keeps this module light

    p = PALETTES[theme]
    t = to_eastern(road["time_utc"])
    x_range = [t.min(), t.max()]
    fig = go.Figure()
    series = [
        ("Road surface temp (forecast)", road["road_temp_f"], p["rst"], "solid"),
        ("Air temp (2 m)", road["air_temp_f"], p["air"], "solid"),
        ("Dewpoint (2 m)", road["dewpoint_f"], p["dew"], "solid"),
    ]
    ends = [float(y.iloc[-1]) for _, y, _, _ in series]
    all_y = [v for _, y, _, _ in series for v in y.dropna()]
    min_gap = 0.075 * (max(all_y) - min(all_y)) if all_y else 1.0
    label_y = _spread(ends, min_gap)
    for (name, y, color, dash), ly in zip(series, label_y):
        fig.add_trace(go.Scatter(x=t, y=y, mode="lines", name=name, line=dict(color=color, width=2, dash=dash),
                                 hovertemplate=f"{name}: %{{y:.1f}}\u00b0F<extra></extra>"))
        if not compact:
            fig.add_annotation(x=t.iloc[-1], y=ly, xref="x", yref="y", text=name.split(" (")[0],
                               showarrow=False, xanchor="left", xshift=6, font=dict(color=p["text2"], size=12))
    if len(obs):
        ot = to_eastern(obs["time_utc"])
        observed = [("road_temp_f", "Road surface temp (observed)", "Observed road temp", p["rst"]),
                    ("air_temp_f", "Air temp (observed)", "Observed air temp", p["air"]),
                    ("dewpoint_f", "Dewpoint (observed)", "Observed dewpoint", p["dew"])]
        for col, name, short, color in observed:
            if col not in obs or obs[col].notna().sum() == 0:
                continue
            fig.add_trace(go.Scatter(x=ot, y=obs[col], mode="markers", name=name,
                                     marker=dict(color=color, size=8, line=dict(color=p["surface"], width=2)),
                                     hovertemplate=f"{short}: %{{y:.1f}}\u00b0F<extra></extra>"))
        if not compact and obs["road_temp_f"].notna().any():
            first = obs["road_temp_f"].first_valid_index()
            fig.add_annotation(x=ot.loc[first], y=obs["road_temp_f"].loc[first], xref="x", yref="y",
                               text="Observed (dots)", showarrow=False, xanchor="left", yanchor="bottom",
                               yshift=8, font=dict(color=p["text2"], size=12))
        x_range[0] = min(x_range[0], ot.min())
    fig.add_hline(y=FREEZING_F, line=dict(color=p["text2"], width=1, dash="dash"),
                  annotation_text="32°F", annotation_position="bottom left",
                  annotation_font=dict(color=p["text2"], size=12))
    _now_line(fig, now, p, label=True)
    layout = _base_layout(p, 420 if compact else 360, x_range, compact)
    layout.update(
        yaxis=dict(title="Temperature (°F)", gridcolor=p["grid"], zeroline=False, linecolor=p["grid"]),
        hovermode="x unified", showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(color=p["text2"], size=11 if compact else 13)),
    )
    fig.update_layout(**layout)
    return fig


def flag_timeline(intervals: pd.DataFrame, evaluated: list[str], flag_names: dict, x_range, now,
                  theme: str = "light", compact: bool = False) -> go.Figure:
    """One row per evaluated flag; a bar wherever that flag is active."""
    p = PALETTES[theme]
    label = _wrap if compact else (lambda n: n)
    names = [label(flag_names.get(f, f)) for f in evaluated]
    fig = go.Figure()
    # An invisible point per row keeps rows with no bars visible on the axis.
    fig.add_trace(go.Scatter(x=[None] * len(names), y=names, mode="markers", showlegend=False, hoverinfo="skip"))
    if len(intervals):
        # Pad each bar by one roadcast step so a single active step is still visible.
        dur = (intervals["end_et"] - intervals["start_et"]).dt.total_seconds() * 1000 + 20 * 60 * 1000
        score = intervals["score"].apply(lambda s: "" if pd.isna(s) else f"<br>Score: {s:g}")
        hover = (intervals["name"] + "<br>" + intervals["start_et"].dt.strftime("%a %-I:%M %p") + " to "
                 + intervals["end_et"].dt.strftime("%a %-I:%M %p") + "<br>" + intervals["hours"].astype(str)
                 + " h" + score + "<br>" + intervals["detail"].fillna(""))
        fig.add_trace(go.Bar(base=intervals["start_et"], x=dur, y=intervals["name"].map(label), orientation="h",
                             marker=dict(color=p["flag"], cornerradius=4), showlegend=False,
                             hovertext=hover, hoverinfo="text", width=0.6))
    _now_line(fig, now, p, label=False)
    layout = _base_layout(p, 60 + (44 if compact else 34) * max(1, len(names)), x_range, compact)
    layout.update(
        yaxis=dict(categoryorder="array", categoryarray=names, autorange="reversed", gridcolor=p["grid"],
                   linecolor=p["grid"], automargin=False, tickfont=dict(color=p["text"], size=12)),
        bargap=0.3, hovermode="closest",
    )
    layout["margin"]["t"] = 8
    fig.update_layout(**layout)
    return fig


def error_by_lead(rows: pd.DataFrame, theme: str = "light", compact: bool = False) -> go.Figure:
    """Bias and RMSE (one temperature axis, degrees F) for each lead-time bin.

    `rows` has columns lead (e.g. "6-12"), n, bias, rmse. Bias can be negative
    (forecast too cold); RMSE is never negative. The dashed zero line is "no bias".
    """
    p = PALETTES[theme]
    leads = list(rows["lead"])
    fig = go.Figure()
    series = [("Bias (forecast minus observed)", "bias", p["rst"]), ("RMSE (typical error size)", "rmse", p["air"])]
    ends = [float(rows[col].iloc[-1]) for _, col, _ in series]
    spread = max(0.4, 0.12 * (float(rows[["bias", "rmse"]].max().max()) - float(rows[["bias", "rmse"]].min().min())))
    label_y = _spread(ends, spread)
    for (name, col, color), ly in zip(series, label_y):
        fig.add_trace(go.Scatter(
            x=leads, y=rows[col], mode="lines+markers", name=name, line=dict(color=color, width=2),
            marker=dict(color=color, size=8, line=dict(color=p["surface"], width=2)),
            customdata=rows["n"], hovertemplate=name.split(" (")[0] + ": %{y:.2f}°F  (n=%{customdata})<extra></extra>"))
        if not compact:
            fig.add_annotation(x=leads[-1], y=ly, xref="x", yref="y", text=name.split(" (")[0], showarrow=False,
                               xanchor="left", xshift=10, font=dict(color=p["text2"], size=12))
    fig.add_hline(y=0, line=dict(color=p["text2"], width=1, dash="dash"))
    fig.update_layout(
        height=380 if compact else 340, margin=dict(l=56, r=14 if compact else 96, t=60 if compact else 36, b=48),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(color=p["text2"], size=13),
        hovermode="x unified", showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
                    font=dict(color=p["text2"], size=11 if compact else 13)),
        xaxis=dict(title=dict(text="Lead time (hours after the weather-model run)", font=dict(size=12)), type="category", categoryorder="array",
                   categoryarray=leads, gridcolor=p["grid"], linecolor=p["grid"]),
        yaxis=dict(title="Error (°F)", gridcolor=p["grid"], zeroline=False, linecolor=p["grid"]),
    )
    return fig
