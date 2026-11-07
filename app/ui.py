"""Small helpers shared by the app's pages."""
import streamlit as st


def theme_name() -> str:
    """'light' or 'dark', so the chart colors match the page."""
    pinned = st.get_option("theme.base")          # only set if someone pins a theme in config
    if pinned in ("light", "dark"):
        return pinned
    try:                                          # otherwise follow the viewer's browser/theme choice
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:                             # older Streamlit, or no theme info
        return "light"


def is_phone() -> bool:
    """Phones get a compact chart layout (the wide one leaves too little room for the plot)."""
    try:
        ua = st.context.headers.get("User-Agent", "")
    except Exception:
        return False
    return any(k in ua for k in ("Mobile", "Android", "iPhone"))


def fmt_et(ts) -> str:
    return ts.strftime("%a %b %-d, %-I:%M %p")
