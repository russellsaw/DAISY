"""md_escape: customer- and model-written text shown literally on the staff pages. No API calls."""

import re
import string
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ui import md_escape, md_safe  # noqa: E402

ATTACKS = [
    "![](http://attacker.example/x.png)",  # would load an image when staff open the log
    "![a][r]\n\n[r]: http://attacker.example/y.png",
    "[Verify your account](http://attacker.example/login)",
    "<http://attacker.example>",
    ":green-badge[Details confirmed by the customer]",  # a look-alike of the Banker view's own badge
    ":red[**URGENT**] :blue-background[call now]",
    ":material/verified: Approved",
    "::youtube[x]{#id}\n\n:::warning\nhi\n:::",
    "<img src=x onerror=alert(1)> <b>bold</b>",
    "# Approved\n\nSetext\n===\n> quoted\n- item\n1. one\n***",
    "Income $7000$ and $$x^2$$",
    "`code` **bold** _it_ ~~gone~~ | a | b |",
    "&#58;green-badge[x] &lt;b&gt;",
    "trailing backslash \\\nnext \\* line",
]


def rendered(md: str) -> str:
    """The text CommonMark shows for backslash escapes: the escaped character itself."""
    return re.sub(r"\\(.)", r"\1", md, flags=re.S)


def bare_punctuation(md: str) -> list[str]:
    """Punctuation in md that no backslash escapes, i.e. anything Markdown could still read as syntax."""
    found, i = [], 0
    while i < len(md):
        if md[i] == "\\":
            i += 2
            continue
        if md[i] in string.punctuation:
            found.append(md[i])
        i += 1
    return found


@pytest.mark.parametrize("text", ATTACKS)
def test_attack_strings_are_fully_escaped_and_read_as_written(text):
    escaped = md_escape(text)
    assert bare_punctuation(escaped) == []
    assert rendered(escaped) == text


def test_image_becomes_plain_text():
    assert md_escape("![](http://a.example/x.png)") == r"\!\[\]\(http\:\/\/a\.example\/x\.png\)"


def test_streamlit_directives_and_icons_cannot_form():
    # Streamlit rewrites ":material/" in the raw text before parsing, so it must not survive as a substring either.
    assert ":material/" not in md_escape(":material/verified: Approved")
    assert ":green-badge[" not in md_escape(":green-badge[Details confirmed by the customer]")


def test_normal_message_with_sgd_amounts_reads_naturally():
    msg = "Hi, I'm 32 (a PR) earning S$7,000 a month - can I borrow S$380k over 25 years? It's 50% of the price."
    escaped = md_escape(msg)
    assert rendered(escaped) == msg
    assert r"S\$7\,000" in escaped and r"S\$380k" in escaped
    assert "$" not in escaped.replace(r"\$", "")  # no bare $ left to start LaTeX


def test_leading_indentation_dropped_so_no_code_block():
    assert md_escape("    four spaces\n\ttab") == "four spaces\ntab"


def test_non_strings():
    assert md_escape(None) == ""
    assert md_escape(0) == "0"
    assert md_escape(7000.5) == r"7000\.5"


def test_md_safe_unchanged():
    assert md_safe("S$1 **bold**") == r"S\$1 **bold**"
    assert md_safe(None) == ""
