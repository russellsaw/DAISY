"""Handoff agent: builds the banker lead card."""
from __future__ import annotations

import json
from datetime import datetime

from .. import llm
from ..state import ConversationState
from . import prompts

CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "customer_summary": {"type": "string", "description": "2-3 sentences: who they are and what they want."},
        "products_discussed": {"type": "array", "items": {"type": "string"}},
        "eligibility_snapshot": {
            "type": "array",
            "items": {"type": "object", "properties": {
                "product": {"type": "string"}, "status": {"type": "string"}, "note": {"type": "string"}},
                "required": ["product", "status"]},
        },
        "questions_asked": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"},
                           "description": "Things the customer asked that the assistant could not or should not answer (e.g. advice)."},
        "suggested_next_step": {"type": "string", "description": "For the banker, e.g. 'call back to discuss fixed vs floating'."},
        "urgency": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["customer_summary", "products_discussed", "eligibility_snapshot", "questions_asked",
                 "open_questions", "suggested_next_step", "urgency"],
}


def build_card(state: ConversationState, trigger: str) -> dict:
    context = (
        f"Trigger for handoff: {trigger}\n\n"
        f"Customer profile:\n{state.profile.describe()}\n\n"
        f"Latest eligibility engine results (JSON):\n{json.dumps(state.last_eligibility, indent=1) if state.last_eligibility else '(none run)'}\n\n"
        f"Transcript:\n{state.transcript(20)}"
    )
    card = llm.structured(prompts.HANDOFF, context, CARD_SCHEMA, name="lead_card", max_tokens=1200)
    card["profile"] = state.profile.to_dict()
    card["created_at"] = datetime.now().isoformat(timespec="minutes")
    card["trigger"] = trigger
    state.handoff_card = card
    state.add_trace("Handoff", f"lead card created (urgency={card['urgency']})", card)
    return card
