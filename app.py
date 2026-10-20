"""Level — entry point. Run: streamlit run app.py

Sets the page config, logo and top navigation once; each page lives in app_pages/.
"""
import streamlit as st

from src import config

st.set_page_config(page_title=config.ASSISTANT_NAME, page_icon="static/level-mark.svg", layout="wide")
dark = st.context.theme.type == "dark"
st.logo(f"static/level-logo-{'dark' if dark else 'light'}.svg", icon_image="static/level-mark.svg", size="large")

pages = [
    st.Page("app_pages/chat.py", title="Assistant", icon=":material/forum:", default=True),
    st.Page("app_pages/evals.py", title="Trust scorecard", icon=":material/fact_check:"),
    st.Page("app_pages/knowledge.py", title="Knowledge", icon=":material/library_books:"),
]
st.navigation(pages, position="top").run()
