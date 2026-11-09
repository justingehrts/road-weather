"""Forecast page: road surface temperature and hazard flags for RWIS sites near Columbus, Ohio.

Reads the newest published files from the `data` branch of the GitHub repo
(via raw.githubusercontent.com). This page never writes anything, and the
forecast files never live on `main`.
"""
import pandas as pd
import streamlit as st

import charts
import road_data as rd
from ui import fmt_et, is_phone, theme_name


# ---- cached loaders (the data changes a few times a day; 5 minutes is plenty) ----
@st.cache_data(ttl=300, show_spinner=False)
def get_latest():
    return rd.load_json("data/latest.json")


@st.cache_data(ttl=300, show_spinner=False)
def get_flags(cycle: str):
    return rd.load_json(f"data/flags/{cycle}.json")


@st.cache_data(ttl=300, show_spinner=False)
def get_roadcast(cycle: str, site: str):
    return rd.load_roadcast(cycle, site)


@st.cache_data(ttl=300, show_spinner=False)
def get_obs(site: str, since):
    return rd.load_obs(site, since)



st.title("Road surface temperature forecast")
st.caption("Automated METRo road-weather forecasts for RWIS stations near Columbus, Ohio. "
           "**Experimental.** Hazard thresholds are starting guesses still being tuned, "
           "so do not use this for operational or travel decisions.")

# ---- find the newest published forecast ----
try:
    latest = get_latest()
except rd.DataError as exc:
    st.error(f"Could not load the forecast data. {exc}")
    st.stop()
if latest is None:
    st.info("No forecast has been published yet. Once the pipeline has run, it will appear here.")
    st.stop()

cycle = latest["cycle"]
try:
    flags = get_flags(cycle)
except rd.DataError as exc:
    st.error(f"Could not load the hazard flags. {exc}")
    st.stop()
if flags is None:
    st.warning(f"The newest forecast ({cycle}) has no flag file yet. Try again in a few minutes.")
    st.stop()

cycle_et = rd.to_eastern(pd.Series([flags["cycle_utc"]])).iloc[0]
age = rd.age_hours(flags["generated_utc"])
st.markdown(f"**Forecast from the {flags['cycle_utc'][11:13]}Z HRRR weather model run** "
            f"({fmt_et(cycle_et)} Eastern) · published {age:.1f} hours ago. All times Eastern.")
if age > 9:
    st.warning("This forecast is more than 9 hours old. A newer run should have appeared by now, "
               "so the pipeline may have a problem.")

sites = flags["sites"]
if not sites:
    st.info("This forecast run has no sites with a roadcast.")
    st.stop()

# ---- overview of all sites ----
rows = []
for sid, s in sites.items():
    iv = rd.flag_intervals(s)
    first = iv["start_et"].min() if len(iv) else None
    rows.append({
        "Site": s["name"], "Type": s["station_type"], "Flag periods": len(iv),
        "Types of flag": ", ".join(sorted(iv["name"].unique())) if len(iv) else "–",
        "First flag starts": fmt_et(first) if first is not None else "–",
    })
st.subheader("All sites")
st.table(pd.DataFrame(rows).set_index("Site"))   # st.table wraps long text; dataframe truncates it
if flags.get("skipped_sites"):
    with st.expander(f"{len(flags['skipped_sites'])} site(s) have no forecast this run"):
        for sid, why in flags["skipped_sites"].items():
            st.write(f"**{sid}**: {why}")

# ---- one site in detail ----
st.subheader("Site detail")
site_id = st.selectbox("Site", list(sites), format_func=lambda s: f"{sites[s]['name']}  ({sites[s]['station_type']})")
site = sites[site_id]

road = get_roadcast(cycle, site_id)
if road is None:
    st.warning("The roadcast file for this site could not be found.")
    st.stop()

now = rd.now_eastern()
obs = get_obs(site_id, road["time_utc"].min() - pd.Timedelta(hours=24))
theme = theme_name()
compact = is_phone()

fig = charts.meteogram(road, obs, now, theme, compact)
st.plotly_chart(fig, theme=None, width="stretch",
                alt="Line chart of forecast road surface temperature, air temperature and dewpoint in "
                    "degrees Fahrenheit, with observed road temperature as dots and a 32 degree reference line.")

intervals = rd.flag_intervals(site)
x_range = fig.layout.xaxis.range
st.markdown("**Active hazard flags**")
if len(intervals) == 0:
    st.success("No hazard flags are forecast for this site in this run.")
st.plotly_chart(charts.flag_timeline(intervals, site["evaluated"], flags["flag_names"], x_range, now, theme, compact),
                theme=None, width="stretch",
                alt="Timeline with one row per hazard flag; bars mark the periods when that flag is active.")
if site["not_evaluated"]:
    st.caption("Not checked for this site: " + "; ".join(
        f"{flags['flag_names'].get(k, k)} ({v})" for k, v in site["not_evaluated"].items()))

# ---- table view (also the accessible alternative to the charts) ----
with st.expander("Table view"):
    st.markdown("**Flag periods**")
    if len(intervals):
        st.dataframe(
            intervals.assign(Start=intervals["start_et"].map(fmt_et), End=intervals["end_et"].map(fmt_et))
            [["name", "Start", "End", "hours", "score", "detail"]]
            .rename(columns={"name": "Flag", "hours": "Hours", "score": "Score", "detail": "Note"}),
            hide_index=True, width="stretch")
    else:
        st.write("None.")
    st.markdown("**Roadcast (every 20 minutes)**")
    table = road.assign(Time=rd.to_eastern(road["time_utc"]).map(fmt_et))[
        ["Time", "road_temp_f", "air_temp_f", "dewpoint_f", "wind_mph", "road_condition"]].round(1)
    st.dataframe(table.rename(columns={
        "road_temp_f": "Road surface °F", "air_temp_f": "Air °F", "dewpoint_f": "Dewpoint °F",
        "wind_mph": "Wind mph", "road_condition": "Road condition"}), hide_index=True, width="stretch")

with st.expander("How the flags work"):
    th = flags["thresholds"]
    st.markdown(
        "Each flag is a simple rule applied to the METRo road forecast. For example, **Pavement frost** needs "
        f"road temperature at or below {th['freezing_f']:g}°F with dewpoint above it for at least "
        f"{th['frost_min_hours']:g} hours. The full table is in `docs/flags.md` in the repo. "
        "All thresholds are starting guesses to be checked against real observations.")
