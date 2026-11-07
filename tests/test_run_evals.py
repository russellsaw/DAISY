"""Eval harness helpers: term matching, partial runs, exit codes, the log PII check. Pure functions, no API calls."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.run_evals import (  # noqa: E402
    EXIT_ALL_PASSED,
    EXIT_CASES_FAILED,
    EXIT_CRASHED,
    contains_term,
    exit_code,
    is_partial,
    leaked_identifiers,
    raw_identifiers,
    select_cases,
)
from src import pii  # noqa: E402

MATCHES = [
    # (response, term): the term should be found
    ("HDB flats can only be bought by a Singapore Citizen or PR household.", "PR"),
    ("Singapore citizens and PRs can buy.", "Citizen"),
    ("Singapore citizens and PRs can buy.", "PR"),
    ("The minimum age is 21.", "21"),
    ("You must be a 21-year-old or above.", "21"),
    ("The annual fee is S$120, waived for the first year.", "120"),
    ("The minimum is S$20,000 a year.", "20,000"),
    ("The minimum is S$20K a year.", "20k"),
    ("The minimum is S$20k a year.", "S$20k"),
    ("the stress test uses 4.65% p.a.", "4.65%"),
    ("It goes back to 27.80% p.a.", "27.8"),
    ("The fee is S$196.20 for the principal card.", "196.2"),
    ("The fee is S$100.00 a year.", "S$100"),
    ("The fee is S$100 a year.", "$100"),
    ("You’re approved!", "you're approved"),
    ("You're approved!", "approved!"),
    ("I can’t show a maximum home loan.", "can't show a maximum"),
    ("This is not an\napproval.", "not an approval"),
    ("up to 12× your monthly income", "12×"),
    ("TDSR is 44.6%", "tdsr"),
    ("My NRIC is S1234567A.", "S1234567A"),
    ("card 4111 1111 1111 1111", "4111"),
]

NON_MATCHES = [
    # (response, term): plain substring matching would have found these
    ("This is an indicative check, not an approval.", "PR"),
    ("The approved amount depends on your profile.", "PR"),
    ("Your income is S$21,000 a year.", "21"),
    ("The rate is 2.21% p.a.", "21"),
    ("As of 2021, the rule changed.", "21"),
    ("The annual fee is S$1,120.", "120"),
    ("The minimum is S$120,000 a year.", "20,000"),
    ("the stress test uses 14.65% p.a.", "4.65%"),
    ("the stress test uses 4.655% p.a.", "4.65"),
    ("It goes back to 27.85% p.a.", "27.8"),
    ("The fee is S$100.50 a year.", "S$100"),
    ("The loan is S$100,000.", "S$100"),
    ("The tenure is 250 months.", "25"),
]


@pytest.mark.parametrize("response,term", MATCHES)
def test_whole_word_matches(response, term):
    assert contains_term(response, term)


@pytest.mark.parametrize("response,term", NON_MATCHES)
def test_no_match_inside_a_longer_word_or_number(response, term):
    assert term.lower() in response.lower()  # the old substring check would have passed
    assert not contains_term(response, term)


def test_elig_011_no_longer_passes_on_any_eligibility_reply():
    """The term "PR" used to match inside "approval", which every eligibility reply contains."""
    reply = "This is an indicative check, not an approval. Your TDSR is within the 55% cap."
    terms = ["Citizen", "PR", "permanent resident"]
    assert any(t.lower() in reply.lower() for t in terms)
    assert not any(contains_term(reply, t) for t in terms)


def test_blank_term_never_matches():
    assert not contains_term("anything", "  ")


CASES = pd.DataFrame({"id": list("abcdef"), "category": ["qa", "qa", "elig", "elig", "adv", "adv"]})


def test_full_run_only_when_every_case_runs():
    total = len(CASES)
    assert not is_partial(len(select_cases(CASES)), total)
    assert not is_partial(len(select_cases(CASES, limit=total)), total)  # the scorecard's default "Max cases"
    assert not is_partial(len(select_cases(CASES, limit=500)), total)
    assert not is_partial(len(select_cases(CASES, categories="qa,elig,adv")), total)
    assert is_partial(len(select_cases(CASES, limit=total - 1)), total)
    assert is_partial(len(select_cases(CASES, categories="qa,adv")), total)
    assert list(select_cases(CASES, categories="elig,adv", limit=3)["id"]) == ["c", "d", "e"]


def test_exit_code_tells_failures_from_a_clean_run_and_a_crash():
    assert exit_code([{"passed": True}, {"passed": True}]) == EXIT_ALL_PASSED == 0
    assert exit_code([{"passed": True}, {"passed": False}]) == EXIT_CASES_FAILED == 1
    assert exit_code([]) == EXIT_ALL_PASSED
    assert EXIT_CRASHED not in (EXIT_ALL_PASSED, EXIT_CASES_FAILED)


CASE_INPUT = "Verify me. My NRIC is S1234567A and my card number is 4111 1111 1111 1111."


def test_raw_identifiers_from_the_case_input():
    assert raw_identifiers(CASE_INPUT) == ["4111111111111111", "S1234567A"]
    assert raw_identifiers("I earn $7,000 a month, call me on 9123 4567") == []


def test_log_check_catches_reformatted_identifiers_the_redactor_misses():
    clean = '{"message": "My NRIC is [NRIC removed] and my card number is [card number removed]."}'
    assert leaked_identifiers(clean, CASE_INPUT) == []
    commas = '{"income": "4,111,111,111,111,111"}'
    assert pii.redact(commas)[1] == []  # the recorder's own pattern doesn't see this one
    assert leaked_identifiers(commas, CASE_INPUT) == ["4111111111111111"]
    assert leaked_identifiers('{"x": "4111.1111.1111.1111"}', CASE_INPUT) == ["4111111111111111"]
    assert leaked_identifiers('{"x": "id s 1234567 a"}', CASE_INPUT) == ["S1234567A"]


def test_main_returns_failures_and_only_a_full_run_updates_latest(tmp_path, monkeypatch):
    """main() end to end with run_case stubbed out, so nothing reaches the pipeline or the API."""
    import json

    import evals.run_evals as run_evals
    from src import config

    CASES.assign(input="hi").to_csv(tmp_path / "cases.csv", index=False)
    monkeypatch.setattr(config, "EVAL_CASES", tmp_path / "cases.csv")
    monkeypatch.setattr(config, "EVAL_RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(
        run_evals, "run_case", lambda case: {"id": case["id"], "passed": case["id"] != "b", "seconds": 0}
    )
    latest = tmp_path / "results" / "latest.json"

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["run_evals.py", *args])
        return run_evals.main()

    assert run() == EXIT_CASES_FAILED  # case b fails
    assert len(json.loads(latest.read_text())["history"]) == 1
    assert run("--limit", str(len(CASES))) == EXIT_CASES_FAILED  # what the scorecard used to pass: still full
    assert len(json.loads(latest.read_text())["history"]) == 2
    assert run("--limit", "2") == EXIT_CASES_FAILED
    assert run("--categories", "adv") == EXIT_ALL_PASSED
    assert len(json.loads(latest.read_text())["history"]) == 2  # partial runs leave the scorecard alone


# ---- code-written text appears once (a reviewer's rewrite once copied it in, and the prompt label with it) ----

_BLOCK = (
    "**What would change for the DBS Home Loan** (on criteria as of 7 Nov 2026):\n"
    "- **From 7 May 2028**, once your car loan (S$600/month) has ended: your TDSR would be 47.7%.\n\n"
    "Each line above is a separate scenario, not a forecast.\n"
    "Based on what you've told us and DBS's criteria as of 7 Nov 2026, this would clear the eligibility check."
)


def test_code_text_once_flags_a_copied_block_and_the_prompt_label():
    from evals.run_evals import code_text_problems
    from src.agents.compliance import APPENDED_LABEL

    assert code_text_problems("Here is the result.\n\n" + _BLOCK) == []
    assert "repeated: **What would change for the" in code_text_problems(_BLOCK + "\n\n" + _BLOCK)
    assert "internal prompt label in the reply" in code_text_problems(f"Result.\n\n{APPENDED_LABEL}\n{_BLOCK}")
    note = "A note on this check: it uses the 4% floor MAS sets."
    assert code_text_problems(f"{note}\n\nA note on this check: it uses a 4% stress rate.") == [
        "repeated: A note on this check:"
    ]


def test_code_text_once_allows_one_block_per_product():
    from evals.run_evals import code_text_problems

    card = _BLOCK.replace("DBS Home Loan", "DBS Cashline")  # same closing sentence, its own heading
    assert code_text_problems(_BLOCK + "\n\n" + card) == []
