"""'How much can I borrow?': the same forward engine, searched. No API calls."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents import compliance, eligibility, handoff, orchestrator  # noqa: E402
from src.agents.compliance import banned_phrases, unsupported_numbers  # noqa: E402
from src.pipeline import run_turn  # noqa: E402
from src.rules import max_loan  # noqa: E402
from src.state import ConversationState, CustomerProfile  # noqa: E402

PR_7K = dict(age=32, residency="pr", monthly_income=7000, existing_housing_loans=0, existing_monthly_debt=0)
HDB = {**PR_7K, "property_type": "hdb"}


@pytest.mark.parametrize("fields,tenure,dbs", [
    (HDB, 25, 372_089),                                                                      # P1 and P2
    ({**PR_7K, "property_type": "private", "commitments": [{"kind": "car loan", "monthly": 600.0}]}, 30, 630_288),  # P3
    ({**HDB, "monthly_income": 4500}, 25, 239_200),                                          # P4
    (dict(age=50, residency="citizen", monthly_income=12000, property_type="private", existing_housing_loans=0,
          existing_monthly_debt=0), 15, 854_168),                                            # P5
])
def test_reproduces_dbs_planner_to_the_dollar(fields, tenure, dbs):
    """Figures read from DBS's planner on 2026-11-02 (docs/CALCULATOR_CHECK.md)."""
    r = max_loan.find(CustomerProfile(**fields))
    assert r["tenure_years"] == tenure and not r["tenure_stated"]
    assert r["at_planner"]["max_loan"] == dbs


def test_the_floor_gives_more_than_dbs_planning_rate():
    r = max_loan.find(CustomerProfile(**HDB))
    assert r["at_floor"]["max_loan"] > r["at_planner"]["max_loan"]
    assert r["at_floor"]["limit_rule"] == "msr"


def test_maximum_passes_and_one_dollar_more_fails():
    p = CustomerProfile(**HDB, property_price=550_000, tenure_years=25)
    top = max_loan.find(p)["at_floor"]["max_loan"]
    from src.rules.engine import check_home_loan
    p.home_loan_amount = float(top)
    assert check_home_loan(p).status == "eligible"
    p.home_loan_amount = float(top + 1)
    assert check_home_loan(p).status == "not_eligible"


def test_ltv_caps_the_answer_when_the_price_is_known():
    r = max_loan.find(CustomerProfile(**{**HDB, "monthly_income": 30_000}, property_price=400_000))
    assert r["at_floor"]["max_loan"] == 300_000 and r["at_floor"]["limit_rule"] == "ltv"


def test_stated_tenure_is_used():
    r = max_loan.find(CustomerProfile(**HDB, tenure_years=20))
    assert r["tenure_years"] == 20 and r["tenure_stated"]
    assert r["at_floor"]["max_loan"] < max_loan.find(CustomerProfile(**HDB))["at_floor"]["max_loan"]


def test_existing_mortgage_lowers_the_maximum():
    p = CustomerProfile(**{**HDB, "existing_housing_loans": 1}, commitments=[{"kind": "home loan", "monthly": 1800.0}])
    assert max_loan.find(p)["at_floor"]["max_loan"] < max_loan.find(CustomerProfile(**HDB))["at_floor"]["max_loan"] / 5


def test_foreigner_buying_hdb_gets_no_figure_and_a_reason():
    r = max_loan.find(CustomerProfile(**{**HDB, "residency": "foreigner"}))
    assert r["at_floor"]["max_loan"] is None
    text = max_loan.text(r)
    assert "can't show a maximum" in text and "S$" not in text.split("Singapore")[0].replace("can't", "")


@pytest.mark.parametrize("fields", [HDB, {**HDB, "property_price": 550_000}, {**HDB, "tenure_years": 20},
                                    {**HDB, "employment_type": "variable"}, {**HDB, "residency": "foreigner"},
                                    dict(age=32, residency="pr", monthly_income=7000, property_type="private")])
def test_reply_is_clean_and_every_number_traces(fields):
    p = CustomerProfile(**fields)
    r = max_loan.find(p)
    text = max_loan.text(r)
    assert banned_phrases(text) == []
    assert unsupported_numbers(text, "", max_loan.numbers_in(r), p.to_dict(), "") == []
    assert "not an approval" in text


def test_unstated_debts_are_repeated_with_the_result():
    text = max_loan.text(max_loan.find(CustomerProfile(age=32, residency="pr", monthly_income=7000,
                                                       property_type="hdb")))
    assert "Things you didn't mention" in text and "housing loans" in text


def test_sources_include_the_rules_and_dbs_planner():
    sources = max_loan.find(CustomerProfile(**HDB))["sources"]
    assert any("mas.gov.sg" in s for s in sources) and any("plan/selection" in s for s in sources)


@pytest.mark.parametrize("msg,expected", [
    ("How much can I borrow for a condo?", {"property_type": "private"}),
    ("How much home loan can I get on a $550k HDB flat over 20 years?",
     {"property_type": "hdb", "property_price": 550_000.0, "tenure_years": 20}),
    ("What's the most for a S$1.2m condo, 30-year loan?",
     {"property_type": "private", "property_price": 1_200_000.0, "tenure_years": 30}),
    ("How much can I borrow for an EC?", {"property_type": "ec"}),
    ("How much can I borrow for my first home? I earn $7k a month.", {}),
    ("I've worked for 20 years. How much can I borrow?", {}),
])
def test_property_mentioned_in_the_question_is_read_in_code(msg, expected):
    assert orchestrator._apply_stated_property({}, msg, CustomerProfile()) == expected


@pytest.mark.parametrize("msg,expected", [
    ("I'm over 30 years old", {}),
    ("I've been working for over 10 years, can I get a home loan?", {}),
    ("I want to borrow $500k for a condo", {"property_type": "private"}),
    ("I owe $3,000 on my condo", {"property_type": "private"}),
    ("What if I bought a $1m condo?", {}),
])
def test_things_that_are_not_property_details_are_left_alone(msg, expected):
    assert orchestrator._apply_stated_property({}, msg, CustomerProfile()) == expected


def test_the_model_s_own_values_win():
    assert orchestrator._apply_stated_property({"tenure_years": 25}, "over 20 years", CustomerProfile())[
        "tenure_years"] == 25


def test_a_stored_price_is_never_overwritten():
    p = CustomerProfile(property_price=1_000_000.0, property_type="private")
    assert orchestrator._apply_stated_property({}, "and a $3,000 condo", p) == {}


def test_tenure_wording_is_honest_past_age_60():
    r = max_loan.find(CustomerProfile(**{**HDB, "age": 62}))
    assert r["tenure_years"] == 5 and not r["keeps_full_ltv"]
    assert "the longest that keeps" not in max_loan.text(r) and "lower loan-to-value limit applies" in max_loan.text(r)
    readback = eligibility.readback_text(CustomerProfile(**{**HDB, "age": 62}), ["home_loan"], max_loan_check=True)
    assert "the longest that keeps" not in readback


def test_price_range_reads_low_to_high_across_a_million():
    text = max_loan.text(max_loan.find(CustomerProfile(**{**PR_7K, "monthly_income": 6600, "property_type": "private"})))
    low, high = [int(x.replace(",", "")) for x in text.split("so S$")[1].split(" is the most")[0].split(" to S$")]
    assert low < high


# ---------------- through the pipeline: ask, read back, then run ----------------

@pytest.fixture
def bot(monkeypatch):
    state = ConversationState()
    reviews = []
    script = {}

    def fake_route(st, msg):
        updates, asks, reply = script.pop("next")
        changed = st.profile.update(updates or {})
        return {"intent": "eligibility", "products": [], "injection_suspected": False, "asks_max_loan": asks,
                "readback_reply": reply, "profile_changed": changed}

    def structured(system, text, schema, **k):
        if "verdict" in schema["properties"]:  # the compliance reviewer, not the input guard's classifier
            reviews.append(1)
            return {"verdict": "approve", "issues": []}
        return {"category": "none", "evidence": ""}

    monkeypatch.setattr(orchestrator, "route", fake_route)
    monkeypatch.setattr(eligibility.llm, "chat", lambda *a, **k: "Could you share your age?")
    monkeypatch.setattr(compliance.llm, "structured", structured)
    monkeypatch.setattr(handoff, "build_card", lambda state, trigger: {"trigger": trigger})

    def send(msg, updates=None, asks=False, reply="not_a_reply"):
        script["next"] = (updates, asks, reply)
        return run_turn(state, msg)

    return send, state, reviews


def test_asks_for_details_then_reads_back_then_runs(bot):
    send, state, reviews = bot
    r = send("How much can I borrow?", asks=True)
    assert r.response == "Could you share your age?" and not r.engine_ran and state.pending_max_loan
    r = send("I'm 32, a PR on $7k, HDB", dict(age=32, residency="pr", monthly_income=7000, property_type="hdb"))
    assert r.readback and not r.engine_ran and "the longest that keeps the full loan-to-value limit" in r.response
    n = len(reviews)
    r = send("yes")
    assert r.engine_ran and r.confirmed and r.max_loan["at_planner"]["max_loan"] == 372_089
    assert "S$372,089" in r.response and not state.pending_max_loan
    assert len(reviews) == n and r.compliance["reviewer"].startswith("code template")  # written by code, no LLM review
    assert r.eligibility == []


def test_naming_a_loan_amount_switches_to_the_normal_check(bot):
    send, state, reviews = bot
    send("How much can I borrow?", dict(age=32, residency="pr", monthly_income=7000, property_type="hdb"), asks=True)
    assert state.pending_max_loan
    r = send("Actually, check $380k over 25 years on a $550k flat",
             dict(home_loan_amount=380000, tenure_years=25, property_price=550000))
    assert not state.pending_max_loan and r.readback and "New home loan: S$380,000" in r.response


def test_changed_details_hide_the_old_maximum_and_read_back_again(bot):
    send, state, reviews = bot
    send("How much?", dict(age=32, residency="pr", monthly_income=7000, property_type="hdb"), asks=True)
    send("yes")
    assert state.is_confirmed()
    r = send("What if I earn 8k?", dict(monthly_income=8000), asks=True)
    assert r.readback and not r.engine_ran and not state.is_confirmed()


def test_old_maximum_is_not_passed_to_the_banker_after_details_change(bot, monkeypatch):
    send, state, reviews = bot
    monkeypatch.undo()
    card = max_loan.find(CustomerProfile(**HDB))
    state.last_max_loan = card
    assert card["profile"] == CustomerProfile(**HDB).to_dict()
    assert state.last_max_loan["profile"] != state.profile.to_dict()
