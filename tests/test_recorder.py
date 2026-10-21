"""Feature 4 — flight recorder. Deterministic: LLM steps stubbed; logs go to a temp dir (conftest)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import recorder  # noqa: E402
from src.agents import compliance, eligibility, handoff, orchestrator  # noqa: E402
from src.pipeline import run_turn  # noqa: E402
from src.state import ConversationState  # noqa: E402

CONDO = {"age": 32, "residency": "pr", "monthly_income": 7000, "property_type": "private",
         "property_price": 1_000_000, "home_loan_amount": 700_000, "tenure_years": 30,
         "commitments": [{"kind": "car loan", "monthly": 600, "end_date": "2028-04-20", "end_quote": "in 18 months"}]}


@pytest.fixture
def stubbed(monkeypatch):
    script = {}

    def fake_route(st, msg):
        updates, reply = script.pop("next")
        changed = st.profile.update(updates or {})
        decision = {"intent": "eligibility", "products": ["home_loan"], "injection_suspected": False,
                    "readback_reply": reply, "profile_changed": changed, "profile_after": st.profile.to_dict()}
        st.add_trace("Orchestrator", "intent=eligibility (stub)", decision)  # as the real route() does
        return decision

    monkeypatch.setattr(orchestrator, "route", fake_route)
    monkeypatch.setattr(eligibility.llm, "chat", lambda *a, **k: "Your TDSR is above the cap.")
    monkeypatch.setattr(compliance.llm, "structured", lambda *a, **k: {"verdict": "approve", "issues": []})
    monkeypatch.setattr(handoff, "build_card", lambda state, trigger: {"trigger": trigger})

    def send(state, msg, updates=None, reply="not_a_reply"):
        script["next"] = (updates, reply)
        return run_turn(state, msg)

    return send


def test_every_turn_is_logged_with_what_each_agent_saw(stubbed):
    state = ConversationState()
    r1 = stubbed(state, "details", CONDO)
    r2 = stubbed(state, "yes")
    events = recorder.load(r2.log_path)
    assert r1.log_path == r2.log_path  # one file per conversation
    t1 = [e["type"] for e in events if e["turn"] == 1]
    t2 = [e["type"] for e in events if e["turn"] == 2]
    assert t1[0] == "conversation_start" and "readback" in t1 and "engine" not in t1
    for needed in ("user_turn", "routing", "readback", "engine", "counterfactual", "draft", "compliance", "defence",
                   "final"):
        assert needed in t2, needed
    assert t2.index("draft") < t2.index("compliance") < t2.index("final")
    engine = next(e for e in events if e["type"] == "engine")
    assert engine["data"]["input_profile"]["monthly_income"] == 7000 and engine["data"]["criteria_version"]
    final = next(e for e in events if e["type"] == "final" and e["turn"] == 2)
    assert final["data"]["counterfactual_dates"] == ["2028-04-20"]


def test_every_counterfactual_run_is_logged(stubbed):
    state = ConversationState()
    stubbed(state, "details", CONDO)
    r = stubbed(state, "yes")
    cf = next(e for e in recorder.load(r.log_path) if e["type"] == "counterfactual")
    runs = cf["data"]["runs"]
    assert len(runs) > 10 and {"lever", "candidate", "verdict", "headroom"} <= set(runs[0])
    assert any(x["lever"] == "commitment_ends:car loan" and x["verdict"] == "path" for x in runs)
    # every path shown has a matching logged run
    for path in cf["data"]["counterfactuals"][0]["paths"]:
        assert any(x["lever"] == path["lever"] and x["verdict"] == "path" for x in runs)


def test_log_never_holds_identifiers(stubbed, monkeypatch):
    """Adversarial: identifiers in the message — and an agent detail that (wrongly) carries one."""
    state = ConversationState()
    real_add = state.add_trace

    def leaky_trace(agent, summary, detail=None):
        if agent == "Eligibility":
            detail = {"debug": "raw S7654321Z / 5500 0000 0000 0004"}  # simulated bug in an agent
        real_add(agent, summary, detail)

    monkeypatch.setattr(state, "add_trace", leaky_trace)
    r = stubbed(state, "Log this: NRIC S7654321Z, card 5500 0000 0000 0004", {"age": 32})
    text = Path(r.log_path).read_text()
    assert "S7654321Z" not in text and "5500 0000 0000 0004" not in text
    assert "[NRIC removed]" in text


def test_recording_can_be_switched_off(stubbed):
    state = ConversationState(record=False)
    r = stubbed(state, "details", {"age": 32})
    assert r.log_path == "" and not list(recorder.CONVERSATIONS_DIR.glob("*.jsonl"))


def test_listing_and_replay_page(stubbed):
    from streamlit.testing.v1 import AppTest

    state = ConversationState()
    stubbed(state, "details", CONDO)
    stubbed(state, "yes")
    convs = recorder.list_conversations(recorder.CONVERSATIONS_DIR)
    assert convs[0]["id"] == state.conversation_id and convs[0]["turns"] == 2
    page = Path(__file__).resolve().parents[1] / "app_pages" / "replay.py"
    at = AppTest.from_file(str(page), default_timeout=60).run()
    assert not at.exception
    at.segmented_control[1].set_value("Whole turn").run()
    assert not at.exception
    assert any("Counterfactual" in e.label for e in at.expander)
