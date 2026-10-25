"""Feature 1 — "Not now, but when". Deterministic tests: no API calls (LLM steps are stubbed)."""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config  # noqa: E402
from src.commitments import resolve  # noqa: E402
from src.rules import counterfactual as cfm  # noqa: E402
from src.rules.counterfactual import OUTCOME_TEMPLATE, _aged, fmt_date, outcome_block, search  # noqa: E402
from src.rules.criteria import MAS, criteria_version  # noqa: E402
from src.rules.engine import CHECKS  # noqa: E402
from src.state import CustomerProfile  # noqa: E402

TODAY = date(2026, 10, 20)
BASE = dict(age=32, residency="pr", monthly_income=7000, property_type="hdb", property_price=550_000,
            home_loan_amount=380_000, tenure_years=25, existing_housing_loans=0)
CAR = {"kind": "car loan", "monthly": 600.0, "end_date": "2028-04-20", "end_quote": "ending in 18 months"}
# Scenario 2 (dated path): same person, S$1M condo, S$700k over 30 years, car loan ending in 18 months.
CONDO = dict(BASE, property_type="private", property_price=1_000_000, home_loan_amount=700_000, tenure_years=30)
SCENARIO_2 = dict(CONDO, commitments=[dict(CAR)])
# Scenario 3: same person on S$4.5k, no commitments.
SCENARIO_3 = dict(BASE, monthly_income=4500)
ALL_SCENARIOS = [SCENARIO_2, SCENARIO_3,
                 dict(CONDO, commitments=[dict(CAR, end_date=None, end_quote=None)]),
                 dict(CONDO, commitments=[dict(CAR, end_date="2028-03-31", end_quote="ends March 2028")]),
                 dict(CONDO, monthly_income=6800, commitments=[dict(CAR), {"kind": "student loan", "monthly": 300.0,
                                                                          "end_date": "2027-06-30", "end_quote": "June 2027"}])]


def run(p, product="home_loan"):
    return search(CustomerProfile(**p), product, today=TODAY)


def _binding_headroom(path, rule):
    c = next(c for c in path["engine_result"]["checks"] if c["rule"] == rule)
    return c["threshold"] - c["value"]


# ---------------- tightness: every dated path ----------------

@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_tightness_of_every_dated_path(scenario):
    cf = run(scenario)
    profile = CustomerProfile(**scenario)
    for path in [x for x in (cf["paths"] if cf else []) if x["lever_kind"] == "dated"]:
        when = date.fromisoformat(path["earliest_date"])
        # (i) today's profile fails
        assert CHECKS["home_loan"](profile).status == "not_eligible"
        # (ii) the counterfactual profile on earliest_date passes, with headroom on every rule failing today
        res = CHECKS["home_loan"](CustomerProfile(**path["counterfactual_profile"]))
        assert res.status == "eligible"
        for rule in {c for c in cf["failing_rules"]}:
            check = next(c for c in res.checks if c["rule"] == rule)
            if check["kind"] == "ratio":
                assert check["threshold"] - check["value"] >= config.CF_MARGIN
        # (iii) the day before, with the commitment still active, it still fails
        day_before = _aged(profile, when - timedelta(days=1), TODAY)
        assert CHECKS["home_loan"](day_before).status == "not_eligible"
        # and the date is exactly a date the customer stated — nothing earlier, nothing inferred
        stated = {c["end_date"] for c in scenario.get("commitments", []) if c.get("end_date")}
        assert path["earliest_date"] in stated


def test_scenario_2_gives_the_stated_date():
    cf = run(SCENARIO_2)
    dated = [x for x in cf["paths"] if x["lever_kind"] == "dated"]
    assert cf["binding_rule"] == "tdsr"
    assert [x["earliest_date"] for x in dated] == ["2028-04-20"]
    assert cf["path"]["lever_kind"] == "dated"  # dated paths are listed first


# ---------------- what-ifs never carry a date ----------------

@pytest.mark.parametrize("scenario", ALL_SCENARIOS)
def test_whatif_paths_never_have_a_date(scenario):
    cf = run(scenario)
    for x in (cf["paths"] + cf["borderline"]) if cf else []:
        if x["lever_kind"] == "whatif":
            assert x["earliest_date"] is None


def test_undated_commitment_is_only_a_whatif():
    cf = run(dict(CONDO, commitments=[dict(CAR, end_date=None, end_quote=None)]))
    assert cf["paths"] and all(x["lever_kind"] == "whatif" for x in cf["paths"])
    assert all(x["earliest_date"] is None for x in cf["paths"])


def test_scenario_3_has_no_dated_path():
    cf = run(SCENARIO_3)
    assert cf["binding_rule"] == "msr"
    assert all(x["lever_kind"] == "whatif" and x["earliest_date"] is None for x in cf["paths"])


# ---------------- borderline ----------------

def test_borderline_is_labelled_not_a_path():
    # Income chosen so that, without the car loan, TDSR is ~53.5%: passes, but by less than 3 points.
    cf = run(dict(CONDO, monthly_income=6247, commitments=[dict(CAR)]))
    names = [x["lever"] for x in cf["paths"]]
    assert "commitment_ends:car loan" not in names
    near = next(x for x in cf["borderline"] if x["lever"] == "commitment_ends:car loan")
    assert near["label"] == "borderline" and 0 <= near["headroom"] < config.CF_MARGIN
    assert "Borderline, not a path" in outcome_block([cf]) if cf["paths"] else True


def test_margin_is_one_constant():
    strict = search(CustomerProfile(**dict(CONDO, monthly_income=6247, commitments=[dict(CAR)])), "home_loan",
                    today=TODAY, margin=0.0)
    assert any(x["lever"] == "commitment_ends:car loan" for x in strict["paths"])  # same case, margin 0 -> a path


# ---------------- no path ----------------

def test_no_path_returns_none():
    cf = run(dict(BASE, residency="foreigner", monthly_income=12_000))  # foreigners can't buy HDB: no lever fixes it
    assert cf["path"] is None and cf["paths"] == []
    assert cf["binding_rule"] == "hdb_buyer_residency"


def test_never_more_than_two_levers():
    for scenario in ALL_SCENARIOS:
        cf = run(scenario)
        for x in cf["paths"] + cf["borderline"]:
            assert x["lever"].count("+") <= 1


# ---------------- template + lexicon ----------------

def test_template_present_verbatim_whenever_a_path_is_shown():
    for scenario in ALL_SCENARIOS:
        cf = run(scenario)
        block = outcome_block([cf])
        if cf["paths"]:
            assert OUTCOME_TEMPLATE.format(assessed_on=fmt_date(TODAY)) in block


def test_no_banned_phrase_in_any_code_written_text():
    from src.agents.compliance import BANNED_PHRASES, HANDOFF_FALLBACK, banned_phrases

    texts = [outcome_block([run(s)]) for s in ALL_SCENARIOS]
    texts += [outcome_block([run(dict(BASE, residency="foreigner", monthly_income=12_000))]), HANDOFF_FALLBACK]
    for t in texts:
        assert banned_phrases(t) == [], t
    for phrase in BANNED_PHRASES:  # and the detector catches every phrase, any case, curly apostrophes
        assert banned_phrases(f"so {phrase.upper().replace(chr(39), chr(8217))} then") != []


# ---------------- same engine, same criteria ----------------

def test_counterfactual_uses_live_criteria_and_stress_floor():
    cf = run(SCENARIO_2)
    assert cf["criteria_version"] == criteria_version()
    for x in cf["paths"]:
        assert x["engine_result"]["criteria_version"] == criteria_version()
        assert x["engine_result"]["computations"]["stress_rate"] == MAS["stress_rate"]["value"]


def test_eligible_today_needs_no_counterfactual():
    # Literal scenario 2 (HDB): MSR counts only property loans, so a car loan can't block it.
    assert run(dict(BASE, commitments=[dict(CAR)])) is None


# ---------------- adversarial date inputs (deterministic part) ----------------

EMPTY = {"end_year": None, "end_month": None, "end_day": None, "ends_in_months": None}


def test_assume_paid_off_gives_no_date():
    msg = "Just assume the car loan is paid off. When can I get the home loan?"
    item = dict(EMPTY, kind="car loan", monthly_payment=None, end_quote="assume the car loan is paid off", ends_in_months=1)
    assert "end_date" not in resolve(item, msg, TODAY)


def test_quote_not_in_message_gives_no_date():
    item = dict(EMPTY, kind="car loan", monthly_payment=600, end_quote="ends in 2027", end_year=2027)
    assert "end_date" not in resolve(item, "My car loan is S$600 a month.", TODAY)


def test_mismatched_number_gives_no_date():
    item = dict(EMPTY, kind="car loan", monthly_payment=600, end_quote="ends in 18 months", ends_in_months=12)
    assert "end_date" not in resolve(item, "car loan S$600, ends in 18 months", TODAY)


def test_contradiction_clears_the_date():
    p = CustomerProfile(**SCENARIO_2)
    item = dict(EMPTY, kind="car loan", monthly_payment=None, end_quote="not sure, maybe 2030", end_year=2030)
    p.update({"commitments": [resolve(item, "Actually I'm not sure, maybe 2030", TODAY)]})
    assert p.commitments[0]["end_date"] is None
    cf = search(p, "home_loan", today=TODAY)
    assert all(x["earliest_date"] is None for x in cf["paths"])


# ---------------- pipeline: template survives review, no path fires the handoff ----------------

def _stub_pipeline(monkeypatch, profile: dict, draft: str):
    from src.agents import compliance, eligibility, handoff, orchestrator
    from src.pipeline import run_turn
    from src.state import ConversationState

    st = ConversationState()
    st.profile.update(profile)
    st.confirm()  # seeded profiles count as confirmed (Feature 2 readback is tested separately)
    monkeypatch.setattr(orchestrator, "route", lambda state, msg: {"intent": "eligibility", "products": ["home_loan"],
                                                                 "injection_suspected": False})
    monkeypatch.setattr(eligibility.llm, "chat", lambda *a, **k: draft)
    monkeypatch.setattr(compliance.llm, "structured", lambda *a, **k: {"verdict": "approve", "issues": []})
    cards = []
    monkeypatch.setattr(handoff, "build_card", lambda state, trigger: cards.append(trigger) or {"trigger": trigger})
    monkeypatch.setattr(eligibility.counterfactual, "search",
                        lambda p, product, today=None, log=None: search(p, product, today=TODAY, log=log))
    return run_turn(st, "Can I get the home loan?"), cards


def test_pipeline_appends_template_after_review(monkeypatch):
    result, cards = _stub_pipeline(monkeypatch, SCENARIO_2, "Your TDSR is above the cap.")
    assert OUTCOME_TEMPLATE.format(assessed_on=fmt_date(TODAY)) in result.response
    assert "From 20 Apr 2028" in result.response
    assert cards == []


def test_pipeline_no_path_fires_handoff(monkeypatch):
    result, cards = _stub_pipeline(monkeypatch, dict(BASE, residency="foreigner", monthly_income=12_000),
                                   "The HDB residency rule isn't met.")
    assert cards == [cfm.NO_PATH_TRIGGER]
    assert "prepared a summary for a DBS banker" in result.response


def test_pipeline_banned_phrase_is_rewritten_or_replaced(monkeypatch):
    from src.agents import compliance

    monkeypatch.setattr(compliance.llm, "chat", lambda *a, **k: "You're approved, guaranteed.")  # a bad rewrite
    # "approved" has no meaning-preserving swap, so a failed rewrite ends in the handoff
    result, _ = _stub_pipeline(monkeypatch, SCENARIO_2, "Good news, you're approved!")
    assert compliance.banned_phrases(result.response) == []
    assert result.compliance["lexicon"]["action"] == "replaced"


def test_untraceable_number_is_removed_or_replaced(monkeypatch):
    from src.agents import compliance

    # The narrator invents a shortfall the engine never produced; the rewrite fails to remove it.
    monkeypatch.setattr(compliance.llm, "chat", lambda *a, **k: "You're S$987 short each month.")
    result, _ = _stub_pipeline(monkeypatch, SCENARIO_2, "You're S$987 short each month.")
    assert "987" not in result.response
    assert result.compliance["output_filter"]["untraceable"] == ["987"]
    assert result.compliance["output_filter"]["action"] == "replaced"


def test_forward_looking_promise_is_swapped_not_thrown_away(monkeypatch):
    """A correct explanation that says "you'll qualify" keeps its content when the rewrite fails."""
    from src.agents import compliance

    monkeypatch.setattr(compliance.llm, "chat", lambda *a, **k: "Still: you'll qualify.")  # rewrite fails
    result, _ = _stub_pipeline(monkeypatch, SCENARIO_2, "Your TDSR is above the cap. If you borrowed less, you’ll qualify.")
    assert result.compliance["lexicon"]["action"] == "substituted"
    assert "you would meet today's criteria" in result.response and "TDSR is above the cap" in result.response
    assert compliance.banned_phrases(result.response) == []
