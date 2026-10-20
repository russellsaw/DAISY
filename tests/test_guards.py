"""Unit tests for the deterministic guards and the knowledge pipeline. No API calls."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import pii  # noqa: E402
from src.agents.compliance import unsupported_numbers  # noqa: E402
from src.knowledge.ingest import chunk, doc_id, html_to_text  # noqa: E402
from src.ui import md_safe  # noqa: E402


# ---------------- compliance number check ----------------

def test_invented_small_percentage_is_flagged():
    flagged = unsupported_numbers("Rates start from 3% p.a.", allowed_text="", computations={}, profile={}, user_text="")
    assert "3%" in flagged


def test_small_plain_integers_are_not_flagged():
    assert unsupported_numbers("Over 25 years, see [2]", "", {}, {}, "") == []


def test_numbers_from_sources_and_engine_pass():
    draft = "Rates from 1.48% p.a. Your TDSR is 30.2% against the 55% cap; stressed instalment S$2,111."
    flagged = unsupported_numbers(draft, "as low as 1.48% p.a.",
                                  {"tdsr": 0.3017, "tdsr_cap": 0.55, "stressed_monthly_instalment": 2111.27}, {}, "")
    assert flagged == []


# ---------------- PII guard ----------------

def test_nric_and_card_removed():
    text, kinds = pii.redact("NRIC S1234567A, card 4111 1111 1111 1111")
    assert "S1234567A" not in text and "4111" not in text
    assert kinds == ["card_number", "nric"]


def test_amounts_and_phone_numbers_kept():
    msg = "Call me on 91234567 about a S$550,000 flat, loan 380000"
    assert pii.redact(msg) == (msg, [])


def test_non_luhn_long_number_kept():
    msg = "reference 1234 5678 9012 3456"
    assert pii.redact(msg)[0] == msg


# ---------------- UI ----------------

def test_md_safe_escapes_dollars():
    assert md_safe("S$7,000 and S$2,111") == r"S\$7,000 and S\$2,111"


# ---------------- knowledge pipeline ----------------

def test_html_to_text_keeps_structure_and_drops_chrome():
    html = ("<html><head><meta charset='utf-8'><title>t</title></head><body><nav>Menu Home Loans</nav>"
            "<main><h2>Eligibility</h2><p>Age 21 – 70 years old</p><p>SG/PR: $20,000 p.a</p>"
            + "<p>filler text</p>" * 60 + "</main><footer>©2026 DBS</footer></body></html>").encode("utf-8")
    text = html_to_text(html)
    assert "## Eligibility" in text
    assert "Age 21 – 70 years old" in text
    assert "Menu" not in text and "©2026" not in text


def test_chunk_sizes_and_overlap():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(60))
    chunks = chunk(text)
    assert len(chunks) > 3
    assert all(len(c) <= 1000 for c in chunks)
    assert chunks[1].splitlines()[0] in chunks[0]  # overlap carried forward


def test_doc_id_is_stable_and_readable():
    assert doc_id({"title": "DBS Woman's Card T&Cs", "publisher": "DBS"}) == "dbs-womans-card-tncs"
    assert doc_id({"title": "MAS: Calculating TDSR", "publisher": "MAS"}) == "mas-calculating-tdsr"
    assert doc_id({"title": "Eligibility for a DBS Credit Card", "publisher": "DBS"}) == \
        "dbs-eligibility-for-a-dbs-credit-card"
