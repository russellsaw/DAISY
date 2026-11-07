"""Small helpers and wording shared by the Streamlit pages and the code-written replies."""

import re
import string

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


# Markdown may read any ASCII punctuation mark as syntax, and CommonMark lets every one of them be backslash-escaped:
# an escaped mark always renders as itself, so escaping all of them shows the text exactly as it was written.
_MD_PUNCTUATION = re.compile(f"([{re.escape(string.punctuation)}])")
_MD_INDENT = re.compile(r"^[ \t]+", re.MULTILINE)  # four leading spaces would make a code block


def md_escape(text) -> str:
    """Untrusted text (a customer's message, a field a model wrote from the transcript) for st.markdown, shown as
    written: no images that load from elsewhere, links, badges, icons, headings, HTML or LaTeX. It still reads
    naturally, e.g. "S$7,000 a month." Bare web addresses may still show as links; their text is the address."""
    return _MD_PUNCTUATION.sub(r"\\\1", _MD_INDENT.sub("", "" if text is None else str(text)))
