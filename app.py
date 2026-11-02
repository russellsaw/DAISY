"""DAISY — entry point. Run: streamlit run app.py

Sets the page config, logo and top navigation once; each page lives in app_pages/.
"""
import streamlit as st

from src import config

st.set_page_config(page_title=config.ASSISTANT_NAME, page_icon="static/daisy-mark.svg", layout="wide")
st.logo("static/daisy-logo-light.svg", icon_image="static/daisy-mark.svg", size="large")  # light theme only

pages = {
    "": [st.Page("app_pages/chat.py", title="Assistant", icon=":material/forum:", default=True)],
    "Bank staff": [
        st.Page("app_pages/banker.py", title="Banker view", icon=":material/support_agent:"),
        st.Page("app_pages/evals.py", title="Trust scorecard", icon=":material/fact_check:"),
        st.Page("app_pages/knowledge.py", title="Knowledge", icon=":material/library_books:"),
        st.Page("app_pages/replay.py", title="Flight recorder", icon=":material/flight:"),
    ],
}
st.navigation(pages, position="top").run()
