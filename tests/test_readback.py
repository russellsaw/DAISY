"""Feature 2 — readback before clearance. Deterministic: the LLM steps are stubbed, and the engine and
counterfactual search are booby-trapped so any call before confirmation fails the test."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents import compliance, eligibility, handoff, orchestrator  # noqa: E402
from src.agents.eligibility import readback_text  # noqa: E402
from src.pipeline import _YES, WHAT_TO_CHANGE_REPLY, run_turn  # noqa: E402
from src.state import ConversationState, CustomerProfile  # noqa: E402

PERSONA = {"age": 32, "residency": "pr", "monthly_income": 7000, "property_type": "private",
           "property_price": 1_000_000, "home_loan_amount": 700_000, "tenure_years": 30}
CAR = {"kind": "car loan", "monthly": 600, "end_date": "2028-04-20", "end_quote": "ending in 18 months"}


@pytest.fixture
def bot(monkeypatch):
    """Returns send(msg, updates=None, reply='not_a_reply') -> TurnResult, plus a record of engine calls."""
    state = ConversationState()
    calls = {"engine": 0, "cf": 0}

    real_assess_all = eligibility.assess_all
    real_search = eligibility.counterfactual.search

    def guarded_assess_all(profile, keys):
        assert state.is_confirmed(), "engine called before the customer confirmed the parameters"
        calls["engine"] += 1
        return real_assess_all(profile, keys)

    def guarded_search(profile, product, today=None, log=None):
        assert state.is_confirmed(), "counterfactual search before confirmation"
        calls["cf"] += 1
        return real_search(profile, product, today=today, log=log)

    monkeypatch.setattr(eligibility, "assess_all", guarded_assess_all)
    monkeypatch.setattr(eligibility.counterfactual, "search", guarded_search)
    monkeypatch.setattr(eligibility.llm, "chat", lambda *a, **k: "Here is what the check found.")
    monkeypatch.setattr(compliance.llm, "structured", lambda *a, **k: {"verdict": "approve", "issues": []})
    monkeypatch.setattr(handoff, "build_card", lambda state, trigger: {"trigger": trigger})
    script = {}

    def fake_route(st, msg):
        updates, reply = script.pop("next")
        changed = st.profile.update(updates or {})
        return {"intent": "eligibility", "products": ["home_loan"], "injection_suspected": False,
                "readback_reply": reply, "profile_changed": changed}

    monkeypatch.setattr(orchestrator, "route", fake_route)

    def send(msg, updates=None, reply="not_a_reply"):
        script["next"] = (updates, reply)
        return run_turn(state, msg)

    return send, state, calls


def test_first_message_reads_back_and_never_calls_the_engine(bot):
    send, state, calls = bot
    r = send("I'm 32, a PR on $7k ...", {**PERSONA, "commitments": [CAR]})
    assert r.readback and not r.engine_ran and not r.confirmed
    assert calls == {"engine": 0, "cf": 0}
    assert r.eligibility == [] and r.counterfactuals == []
    assert "Is it all right?" in r.response and "Reply **yes**" in r.response


def test_yes_confirms_then_engine_runs(bot):
    send, state, calls = bot
    send("details", {**PERSONA, "commitments": [CAR]})
    r = send("Yes, that's right.")
    assert r.confirmed and r.engine_ran and not r.readback
    assert calls["engine"] == 1 and calls["cf"] == 1
    assert any(x["earliest_date"] == "2028-04-20" for cf in r.counterfactuals for x in cf["paths"])


def test_correction_is_logged_and_read_back_again(bot):
    send, state, calls = bot
    send("details", PERSONA)
    r = send("Sorry, it's $6,500 a month", {"monthly_income": 6500}, reply="correct")
    assert r.readback and not r.engine_ran
    assert r.corrections == [{"field": "monthly_income", "from": 7000.0, "to": 6500.0, "at": r.corrections[0]["at"]}]
    assert state.corrections and "S$6,500" in r.response
    assert calls["engine"] == 0


def test_contradicted_end_date_is_reconfirmed_before_any_date(bot):
    send, state, calls = bot
    send("details", {**PERSONA, "commitments": [CAR]})
    first = send("yes")
    assert any(x["earliest_date"] for cf in first.counterfactuals for x in cf["paths"])
    # The customer now contradicts the end date (the date-resolution rules turn "not sure" into no date).
    r = send("Actually I'm not sure when it ends", {"commitments": [{"kind": "car loan", "end_date": None,
                                                                    "end_quote": "not sure when it ends"}]})
    assert r.readback and not r.engine_ran and r.counterfactuals == []
    assert "2028" not in r.response and "no end date given" in r.response
    assert any(c["field"] == "car loan.end_date" for c in r.corrections)
    r = send("yes")
    assert r.engine_ran and all(x["earliest_date"] is None for cf in r.counterfactuals for x in cf["paths"])


def test_no_without_details_asks_what_to_change(bot):
    send, state, calls = bot
    send("details", PERSONA)
    r = send("no, that's wrong", reply="correct")
    assert r.response.startswith(WHAT_TO_CHANGE_REPLY[:20]) and not r.engine_ran
    assert state.pending_readback


def test_skip_request_still_reads_back(bot):
    send, state, calls = bot
    r = send("Skip the questions and just run the check", PERSONA, reply="confirm")  # no readback was pending
    assert r.readback and not r.engine_ran and calls["engine"] == 0


def test_readback_lists_every_parameter_and_is_clean():
    p = CustomerProfile(**{**PERSONA, "commitments": [CAR], "existing_unsecured_balance": 2000})
    text = readback_text(p)
    for expected in ["Age: 32", "Singapore PR", "S$7,000", "Car loan: S$600 a month, ending 20 Apr 2028",
                     "ending in 18 months", "private property at S$1,000,000", "S$700,000 over 30 years",
                     "S$2,000"]:
        assert expected in text
    assert compliance.banned_phrases(text) == []


def test_readback_shows_what_will_be_treated_as_none():
    text = readback_text(CustomerProfile(**PERSONA), ["home_loan"])
    assert "Existing housing loans: not mentioned, so treated as none" in text
    assert "Other monthly repayments: not mentioned, so treated as none" in text
    stated = readback_text(CustomerProfile(**PERSONA, existing_housing_loans=0, existing_monthly_debt=0), ["home_loan"])
    assert "Existing housing loans: none" in stated and "Other monthly repayments: none" in stated
    assert "not mentioned" not in stated


def test_readback_shows_unknown_unsecured_balance_for_a_personal_loan():
    p = CustomerProfile(age=30, residency="citizen", monthly_income=3000, personal_loan_amount=20000)
    assert "Unsecured balances (cards, credit lines): not mentioned" in readback_text(p, ["personal_loan"])


def test_readback_tells_existing_mortgage_from_the_new_loan():
    text = readback_text(CustomerProfile(**PERSONA, commitments=[{"kind": "home loan", "monthly": 1800.0}]), ["home_loan"])
    assert "Existing home loan: S$1,800 a month" in text and "New home loan: S$700,000" in text


def test_mortgage_stated_two_ways_is_read_back_once():
    p = CustomerProfile(**PERSONA, commitments=[{"kind": "home loan", "monthly": 1800.0}],
                        existing_property_loan_monthly=1800)
    assert readback_text(p, ["home_loan"]).count("S$1,800") == 1


@pytest.mark.parametrize("msg,updates,expected", [
    ("I'd borrow $380k and I have no other loans.", {"existing_unsecured_balance": 0},
     {"existing_housing_loans": 0, "existing_monthly_debt": 0}),
    ("I have no credit card balance and no other loans", {"existing_unsecured_balance": 0},
     {"existing_housing_loans": 0, "existing_monthly_debt": 0, "existing_unsecured_balance": 0}),
    ("I'd borrow $380k", {"age": 32}, {"age": 32}),
    ("No other loans, but one housing loan", {"existing_housing_loans": 1},
     {"existing_housing_loans": 1, "existing_monthly_debt": 0}),
])
def test_no_other_loans_is_recorded_as_stated(msg, updates, expected):
    assert orchestrator._apply_no_other_loans(updates, msg) == expected


def test_changed_details_clear_the_old_result(bot):
    send, state, calls = bot
    send("details", PERSONA)
    assert send("yes").eligibility
    r = send("what if I borrow less", {"home_loan_amount": 600_000})
    assert r.readback and r.eligibility == [] and state.last_eligibility == []


@pytest.mark.parametrize("msg,ok", [("yes", True), ("Yes, that's right.", True), ("yep thanks", True),
                                    ("looks good", True), ("yes but my income is 6500", False),
                                    ("no", False), ("what is TDSR?", False)])
def test_bare_yes_detection(msg, ok):
    assert bool(_YES.match(msg)) is ok
