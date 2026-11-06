"""Entry point for the road forecast web app (run: streamlit run app/streamlit_app.py)."""
import streamlit as st

st.set_page_config(page_title="Road forecast: Columbus, OH", page_icon="\u2744\ufe0f", layout="wide")

pages = [
    st.Page("forecast.py", title="Forecast", icon=":material/thermostat:", default=True),
    st.Page("verification.py", title="Verification", icon=":material/fact_check:"),
]
st.navigation(pages).run()
