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
    flagged = unsupported_numbers(
        "Rates start from 3% p.a.", allowed_text="", computations={}, profile={}, user_text=""
    )
    assert "3%" in flagged


def test_small_plain_integers_are_not_flagged():
    assert unsupported_numbers("Over 25 years, see [2]", "", {}, {}, "") == []


def test_numbers_from_sources_and_engine_pass():
    draft = "Rates from 1.48% p.a. Your TDSR is 30.2% against the 55% cap; stressed instalment S$2,111."
    flagged = unsupported_numbers(
        draft,
        "as low as 1.48% p.a.",
        {"tdsr": 0.3017, "tdsr_cap": 0.55, "stressed_monthly_instalment": 2111.27},
        {},
        "",
    )
    assert flagged == []


def test_percentage_needs_a_percentage_in_the_source():
    # "3 years" on a page doesn't support an invented "3% p.a."; a real "3%" does, including inside a range.
    assert unsupported_numbers("The rate is 3% p.a.", "Tenure is 3 years. See [1].", {}, {}, "") == ["3%"]
    assert unsupported_numbers("The rate is 3% p.a.", "A rate of 3% p.a. applies", {}, {}, "") == []
    assert unsupported_numbers("From 3% p.a.", "Rebates of 3-5% on dining", {}, {}, "") == []
    assert unsupported_numbers("The rate is 3 percent.", "Tenure is 3 years.", {}, {}, "") == ["3%"]


def test_plain_number_may_trace_to_a_percentage():
    assert unsupported_numbers("The floor is 4 per cent, or 4.", "a 4% floor", {}, {}, "") == []


def test_invented_small_amount_of_money_is_flagged():
    assert unsupported_numbers("The late fee is S$25.", "", {}, {}, "") == ["25"]
    assert unsupported_numbers("The late fee is S$25.", "A fee of S$25 applies", {}, {}, "") == []


def test_engine_ratio_counts_as_a_percentage():
    assert unsupported_numbers("MSR is 28.7%.", "", {"msr": 0.28654}, {}, "") == []


def test_profile_values_do_not_launder_invented_figures():
    assert unsupported_numbers("Rates start from 3% p.a.", "", {}, {"age": 32}, "") == ["3%"]
    assert unsupported_numbers("Rates start from 25% p.a.", "", {}, {"tenure_years": 25}, "") == ["25%"]
    assert unsupported_numbers("The fee is S$120.", "", {}, {"tenure_years": 10}, "") == ["120"]


def test_income_may_be_quoted_monthly_or_yearly():
    assert unsupported_numbers("That's S$84,000 a year.", "", {}, {"monthly_income": 7000.0}, "") == []
    assert unsupported_numbers("A gap of 3.3 points, or 3.3%.", "", {"cf.headroom_pp": 3.3}, {}, "") == []


# ---------------- engine and profile inputs ----------------


def test_only_real_property_loans_count_as_mortgages():
    from src.state import is_property_loan

    for kind in ("home loan", "mortgage", "HDB loan", "housing loan", "investment property loan"):
        assert is_property_loan(kind), kind
    for kind in (
        "flat rate personal loan",
        "home appliance instalment",
        "inflation loan",
        "home renovation loan",
        "car loan",
    ):
        assert not is_property_loan(kind), kind


def test_impossible_values_are_refused():
    from src.state import CustomerProfile

    p = CustomerProfile()
    assert p.update({"tenure_years": 0, "age": 0, "home_loan_amount": -5, "existing_housing_loans": 0}) == [
        "existing_housing_loans"
    ]


def test_conversation_ids_do_not_look_like_card_numbers():
    from src.state import ConversationState

    for _ in range(50):
        cid = ConversationState().conversation_id
        assert pii.redact(cid) == (cid, [])


def test_redacting_a_log_event_keeps_it_valid_and_leaves_numbers_alone():
    event = {"n": 4111111111111111, "text": "card 4111 1111 1111 1111", "nested": [{"nric": "S1234567A"}]}
    out = pii.redact_values(event)
    assert out["n"] == 4111111111111111 and "4111" not in out["text"] and "S1234567A" not in str(out)


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
    html = (
        "<html><head><meta charset='utf-8'><title>t</title></head><body><nav>Menu Home Loans</nav>"
        "<main><h2>Eligibility</h2><p>Age 21 – 70 years old</p><p>SG/PR: $20,000 p.a</p>"
        + "<p>filler text</p>" * 60
        + "</main><footer>©2026 DBS</footer></body></html>"
    ).encode("utf-8")
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
    assert (
        doc_id({"title": "Eligibility for a DBS Credit Card", "publisher": "DBS"})
        == "dbs-eligibility-for-a-dbs-credit-card"
    )


# ---------------- model layer ----------------


def test_strict_schema_closes_objects_and_rewrites_unions():
    from src.llm import strict_schema

    out = strict_schema(
        {
            "type": "object",
            "properties": {
                "a": {"type": ["number", "null"]},
                "b": {"type": "array", "items": {"type": "object", "properties": {"c": {"type": "string"}}}},
            },
        }
    )
    assert out["additionalProperties"] is False
    assert out["properties"]["a"]["anyOf"] == [{"type": "number"}, {"type": "null"}]
    assert out["properties"]["b"]["items"]["additionalProperties"] is False


def test_recent_history_starts_with_user():
    from src.state import ConversationState

    st = ConversationState()
    st.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(11)]
    hist = st.recent_history(4)
    assert hist[0]["role"] == "user" and hist[-1]["content"] == "10"


def test_transcript_leaves_out_the_message_being_handled():
    from src.state import ConversationState

    st = ConversationState()
    st.messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert "second" in st.transcript()
    assert "second" not in st.transcript(before="second") and "reply" in st.transcript(before="second")
    assert "second" in st.transcript(before="something else")


def test_schemas_stay_within_structured_output_limits():
    """The API rejects schemas with too many union-typed (nullable) parameters (17 failed every request)."""
    from evals.judge import SCHEMA as JUDGE
    from src.agents.compliance import REVIEW_SCHEMA
    from src.agents.handoff import CARD_SCHEMA
    from src.agents.orchestrator import ROUTE_SCHEMA
    from src.llm import strict_schema

    def unions(node):
        if isinstance(node, dict):
            return ("anyOf" in node) + sum(unions(v) for v in node.values())
        if isinstance(node, list):
            return sum(unions(v) for v in node)
        return 0

    for schema in (ROUTE_SCHEMA, REVIEW_SCHEMA, CARD_SCHEMA, JUDGE):
        assert unions(strict_schema(schema)) <= 8


def test_eval_cases_are_read_as_text():
    """Regression: pandas turned "1" into 1.0 in sparse columns and silently skipped expect_readback."""
    import pandas as pd

    from src import config

    df = pd.read_csv(config.EVAL_CASES, dtype=str, keep_default_na=False)
    flags = set(df["expect_readback"]) | set(df["adversarial"]) | set(df["expect_corrections"])
    assert flags <= {"", "0", "1"}
