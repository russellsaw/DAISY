"""DAISY — entry point. Run: streamlit run app.py

Sets the page config, logo and top navigation once; each page lives in app_pages/.
"""
import streamlit as st

from src import config

st.set_page_config(page_title=config.ASSISTANT_NAME, page_icon="static/daisy-mark.svg", layout="wide")
dark = st.context.theme.type == "dark"
st.logo(f"static/daisy-logo-{'dark' if dark else 'light'}.svg", icon_image="static/daisy-mark.svg", size="large")

pages = [
    st.Page("app_pages/chat.py", title="Assistant", icon=":material/forum:", default=True),
    st.Page("app_pages/evals.py", title="Trust scorecard", icon=":material/fact_check:"),
    st.Page("app_pages/knowledge.py", title="Knowledge", icon=":material/library_books:"),
    st.Page("app_pages/replay.py", title="Flight recorder", icon=":material/flight:"),
]
st.navigation(pages, position="top").run()
