"""Small helpers and wording shared by the Streamlit pages and the code-written replies."""

# One set of customer-facing words, so a fact reads the same in the reply, the sidebar and the Banker view.
RESIDENCY_LABELS = {"citizen": "Singapore Citizen", "pr": "Singapore PR", "foreigner": "Foreigner"}
PROPERTY_LABELS = {"hdb": "HDB flat", "ec": "Executive Condominium", "private": "private property"}
EMPLOYMENT_LABELS = {
    "salaried": "Salaried",
    "self_employed": "Self-employed",
    "variable": "Variable income",
    "unemployed": "Not working",
}
STATUS_WORDS = {
    "eligible": "Meets today's criteria",
    "not_eligible": "Doesn't meet today's criteria",
    "need_more_info": "More details needed",
}


def plain_value(key: str, value):
    """A coded profile value in the words a customer would use."""
    label = {"residency": RESIDENCY_LABELS, "property_type": PROPERTY_LABELS, "employment_type": EMPLOYMENT_LABELS}.get(
        key, {}
    ).get(value, value)
    return label[0].upper() + label[1:] if isinstance(label, str) and label else label


def md_safe(text: str | None) -> str:
    """Escape `$` so st.markdown doesn't render "S$7,000 … S$2,111" as LaTeX maths."""
    return (text or "").replace("$", r"\$")
