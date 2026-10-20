"""Small helpers shared by the Streamlit pages."""


def md_safe(text: str | None) -> str:
    """Escape `$` so st.markdown doesn't render "S$7,000 … S$2,111" as LaTeX maths."""
    return (text or "").replace("$", r"\$")
