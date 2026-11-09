"""Verification page: how well did past forecasts match what the road stations measured?

Reads data/verification/summary.json from the `data` branch (written daily by
scripts/verify.py). This page never writes anything.
"""
import pandas as pd
import streamlit as st

import charts
import road_data as rd
from ui import fmt_et, is_phone, theme_name


@st.cache_data(ttl=300, show_spinner=False)
def get_summary():
    return rd.load_json("data/verification/summary.json")


def pct(x) -> str:
    return "–" if x is None else f"{x:.0%}"



st.title("Verification")
st.caption("How well did past forecasts match what the road stations measured? "
           "**Experimental.** Early results rest on few observations, so treat them as a rough guide.")

try:
    summary = get_summary()
except rd.DataError as exc:
    st.error(f"Could not load the verification data. {exc}")
    st.stop()
if summary is None:
    st.info("No verification results have been published yet. They appear once forecasts have been made "
            "and later station observations have been collected (it runs once a day).")
    st.stop()

generated = rd.to_eastern(pd.Series([summary["generated_utc"]])).iloc[0]
st.markdown(f"**Last {summary['window_days']} days** · {summary['cycles_verified']} forecast runs checked · "
            f"{summary['matched_pairs']:,} forecast/observation matches · updated {fmt_et(generated)} Eastern.")
if summary["matched_pairs"] == 0:
    st.info("There are no matches yet. Verification needs forecasts and station observations from the hours after "
            "them. Check back after the pipeline has run for a day or so.")
    st.stop()

theme, compact = theme_name(), is_phone()

# ---- one site (or all) for both sections ----
road_sites = summary["temperature"]["road_temp_f"]["by_site"]
choice = st.selectbox("Site", ["__all__"] + list(road_sites),
                      format_func=lambda s: "All sites" if s == "__all__" else road_sites[s]["name"])

# ---- temperature error by lead time ----
st.subheader("Temperature error by lead time")
var = st.radio("Measurement", list(summary["temperature"]), horizontal=True,
               format_func=lambda v: summary["temperature"][v]["label"])
block = summary["temperature"][var]
if choice == "__all__":
    rows = block["overall_by_lead"]
else:
    rows = block["by_site"].get(choice, {}).get("by_lead", [])
if not rows:
    st.info("No matched observations for this selection yet.")
else:
    df = pd.DataFrame(rows)
    st.plotly_chart(charts.error_by_lead(df, theme, compact), theme=None, width="stretch",
                    alt=f"Line chart of bias and RMSE in degrees Fahrenheit for {block['label'].lower()} by forecast lead time.")
    st.caption("**Bias** is the average of (forecast minus observed): above zero means the forecast ran too warm, "
               "below zero too cold. **RMSE** is the typical size of the error, ignoring direction. "
               "Lead time is counted from the weather-model run (the 12Z run is hour 0 at 12:00 UTC), "
               "and the road forecast itself only starts a few hours later.")
    with st.expander("Table view"):
        st.dataframe(df.rename(columns={"lead": "Lead (hours)", "n": "Matches", "bias": "Bias °F",
                                        "rmse": "RMSE °F", "mae": "Mean abs. error °F"}),
                     hide_index=True, width="stretch")

st.markdown("**By site**")
site_rows = [{"Site": s["name"], "Type": s["station_type"], "Matches": f"{s['overall']['n']:,}",
              "Bias \u00b0F": f"{s['overall']['bias']:.2f}", "RMSE \u00b0F": f"{s['overall']['rmse']:.2f}"}
             for s in block["by_site"].values()]
st.table(pd.DataFrame(site_rows).set_index("Site"))

# ---- hazard flags ----
st.subheader("Hazard flags")
cd = summary["flags"]["condition_data"]
if cd["with_surface_condition"] == 0:
    st.warning("No station surface-condition reports (dry, wet, frost, ice…) have been collected yet, so the flags "
               "that depend on them cannot be scored. “Bridges first” uses observed road temperatures and can be.")
else:
    st.caption(f"Station surface condition was reported for {cd['with_surface_condition']:,} of "
               f"{cd['observations']:,} observations in this window. Observations with no report are skipped, not counted as “no hazard”.")

flag_rows, any_low = [], False
for key, f in summary["flags"]["by_flag"].items():
    if choice == "__all__":
        r = f["overall"]
    else:
        r = f["by_site"].get(choice)
    if r is None:
        flag_rows.append({"Flag": f["name"], "Hazard seen": "–",
                          "Hit rate": "–", "False-alarm ratio": "–", "False-alarm rate": "–",
                          "Hits": "–", "Misses": "–", "False alarms": "–"})
        continue
    any_low |= r["low_sample"]
    flag_rows.append({"Flag": f["name"] + (" *" if r["low_sample"] else ""),
                      "Hazard seen": r["observed_events"], "Hit rate": pct(r["hit_rate"]),
                      "False-alarm ratio": pct(r["false_alarm_ratio"]), "False-alarm rate": pct(r["false_alarm_rate"]),
                      "Hits": r["hits"], "Misses": r["misses"], "False alarms": r["false_alarms"]})
st.table(pd.DataFrame(flag_rows).set_index("Flag"))
if any_low:
    st.caption("\\* Fewer than 10 times the hazard was observed, so these rates could change a lot with more data.")
for name, why in summary["flags"]["not_verified"].items():
    st.caption(f"**{name}** is not scored. {why}")

with st.expander("What each flag is checked against"):
    for f in summary["flags"]["by_flag"].values():
        st.markdown(f"- **{f['name']}**: {f['verified_against']}")

with st.expander("How to read the flag scores"):
    d = summary["flags"]["definitions"]
    st.markdown(
        f"- **Hit rate:** {d['hit_rate']}\n"
        f"- **False-alarm ratio:** {d['false_alarm_ratio']}\n"
        f"- **False-alarm rate:** {d['false_alarm_rate']}\n\n"
        "Each station observation is compared with every forecast run that covered it, so the same hazard can be "
        "counted several times (once per run). That is normal for forecast checking, but it means the counts "
        "overstate how many separate events there were. The rules and the observed-condition words are described in "
        "`docs/verification.md` in the repo.")
