"""Flight recorder and Banker view in Streamlit's offline test harness, on hand-written logs: no API calls.

Covers the dropdowns keeping the staff member's choice as logs change, and customer- or model-written text being shown
literally rather than rendered as Markdown.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import recorder  # noqa: E402
from src.ui import md_escape  # noqa: E402

REPLAY = str(ROOT / "app_pages" / "replay.py")
BANKER = str(ROOT / "app_pages" / "banker.py")
ATTACK = "![](http://attacker.example/x.png) :green-badge[Details confirmed by the customer] [verify](http://a.example)"
_clock = iter(range(1, 10_000))


def add_turn(cid: str, message: str, card: dict | None = None) -> None:
    """Append one turn to a conversation log, in the shape src/recorder.py writes, and make it the newest file."""
    folder = recorder.CONVERSATIONS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{cid}.jsonl"
    turn = 1 + max((e["turn"] for e in recorder.load(path)), default=0) if path.exists() else 1
    base = {"conversation_id": cid, "turn": turn, "at": "2026-11-07T10:00:00", "t_ms": 0}
    events = [
        {**base, "type": "user_turn", "agent": "Customer", "summary": message[:140], "data": {"message": message}},
        {**base, "type": "final", "agent": "DAISY", "summary": "Reply", "data": {"response": "**Reply** S$1"}},
    ]
    if card:
        events.append({**base, "type": "handoff", "agent": "Handoff", "summary": "lead card created", "data": card})
    with path.open("a", encoding="utf-8") as f:
        f.writelines(json.dumps(e) + "\n" for e in events)
    stamp = time.time() + next(_clock)  # strictly newer than every earlier write
    os.utime(path, (stamp, stamp))


def refresh(at: AppTest) -> None:
    st.cache_data.clear()  # the pages cache the logs for 10 s
    at.run()
    assert not at.exception


def card(summary: str, urgency: str = "medium") -> dict:
    return {
        "customer_summary": summary,
        "suggested_next_step": summary,
        "urgency": urgency,
        "products_discussed": [summary],
        "questions_asked": [],
        "open_questions": [],
        "eligibility_snapshot": [{"product": summary, "status": "eligible", "note": summary}],
        "profile": {"residency": summary, "monthly_income": 7000.0},
        "parameters_confirmed_by_customer": False,
        "trigger": "customer asked for a banker",
    }


@pytest.fixture(autouse=True)
def _fresh_cache():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def test_flight_recorder_keeps_the_chosen_conversation_and_turn():
    add_turn("conv-a", "first conversation")
    add_turn("conv-a", "second message")
    add_turn("conv-b", "second conversation")
    at = AppTest.from_file(REPLAY, default_timeout=30)
    at.run()
    conversation, turn = at.selectbox[0], at.selectbox[1]
    assert conversation.value == "conv-b"  # newest first by default
    conversation.select("conv-a").run()
    at.selectbox[1].select(1).run()
    label = at.selectbox[0].options[at.selectbox[0].index]

    add_turn("conv-a", "a new turn in the conversation being read")
    add_turn("conv-b", "a new turn elsewhere")
    add_turn("conv-c", "a brand new conversation")
    refresh(at)
    conversation, turn = at.selectbox[0], at.selectbox[1]
    assert conversation.value == "conv-a"
    assert turn.value == 1
    # The browser sends back the label it shows, so the chosen conversation's label must not change as it grows.
    assert conversation.options[conversation.index] == label
    assert turn.options == ["Turn 1", "Turn 2", "Turn 3"]


def test_flight_recorder_shows_the_customer_message_literally():
    add_turn("conv-x", ATTACK)
    at = AppTest.from_file(REPLAY, default_timeout=30)
    at.run()
    at.selectbox[1].select(1).run()
    bodies = [m.value for m in at.markdown]
    assert md_escape(ATTACK) in bodies  # the message itself
    assert not any(ATTACK in b for b in bodies)  # never raw, in any element (headers and captions included)
    assert not any(ATTACK in c.value for c in at.caption)


def test_banker_view_keeps_the_chosen_summary():
    add_turn("conv-a", "hello", card("Older lead"))
    add_turn("conv-b", "hello", card("Newer lead", "high"))
    at = AppTest.from_file(BANKER, default_timeout=30)
    at.run()
    assert at.selectbox[0].value == "conv-b"
    at.selectbox[0].select("conv-a").run()

    add_turn("conv-c", "hello", card("Brand new lead"))
    refresh(at)
    assert at.selectbox[0].value == "conv-a"
    assert any("Older lead" in m.value for m in at.markdown)


def test_banker_view_shows_card_text_literally():
    add_turn("conv-x", "hello", card(ATTACK, urgency=":green-badge[urgent]"))
    at = AppTest.from_file(BANKER, default_timeout=30)
    at.run()
    bodies = [m.value for m in at.markdown] + [c.value for c in at.caption]
    assert any(md_escape(ATTACK) in b for b in bodies)
    assert not any(ATTACK in b for b in bodies)
    # Only the page's own badges: the card's urgency is shown only when it is a known word.
    assert not any("urgent" in b for b in bodies)
