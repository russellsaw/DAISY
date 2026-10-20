"""run_turn(): one customer message in, one reviewed response out.

    Orchestrator -> (Product Knowledge | Eligibility | boundary | handoff | ...)
                 -> Compliance (veto) -> response

Everything the UI and the eval runner need comes back in TurnResult.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config, llm, pii
from .agents import compliance, eligibility, handoff, orchestrator, product_knowledge, prompts
from .state import ConversationState


@dataclass
class TurnResult:
    response: str
    intent: str
    sources: list[dict] = field(default_factory=list)
    eligibility: list[dict] = field(default_factory=list)
    handoff_card: dict | None = None
    compliance: dict = field(default_factory=dict)
    trace: list = field(default_factory=list)
    user_msg: str = ""  # the customer message after PII redaction — what the UI should display


def run_turn(state: ConversationState, user_msg: str) -> TurnResult:
    state.trace = []
    state.last_sources = []
    user_msg, pii_found = pii.redact(user_msg)
    if pii_found:
        state.add_trace("PII guard", f"removed {', '.join(pii_found)} before any agent saw the message")
    state.messages.append({"role": "user", "content": user_msg})

    decision = orchestrator.route(state, user_msg)
    intent = decision["intent"]
    products = decision.get("products") or []
    injection = bool(decision.get("injection_suspected"))

    sources: list[dict] = []
    sources_text = ""
    computations: dict = {}
    card = None

    if intent in ("product_qa", "comparison"):
        out = product_knowledge.answer(state, user_msg, products)
        draft, sources = out["draft"], out["sources"]
        sources_text = "\n\n".join(c["text"] for c in out["chunks"])

    elif intent == "eligibility":
        out = eligibility.assess(state, user_msg, products)
        draft, computations = out["draft"], out["computations"]
        sources = out.get("sources", [])
        sources_text = "\n".join(str(r) for r in out["results"])

    elif intent == "advice_seeking":
        draft = llm.chat(prompts.BOUNDARY, [{"role": "user", "content":
                         f"Customer profile:\n{state.profile.describe()}\n\nCustomer message:\n{user_msg}"}],
                         model=config.MODEL_MAIN, max_tokens=400)
        state.add_trace("Boundary", "advice-seeking: declined to advise, offered banker")
        card = handoff.build_card(state, "customer sought personal financial advice")

    elif intent == "handoff_request":
        card = handoff.build_card(state, "customer asked to apply / speak to a banker")
        draft = ("I've put together a summary of our conversation for a DBS banker so you won't need to repeat "
                 "yourself. A banker can take it from here, walk you through the application and answer anything "
                 "I couldn't. Is there anything else you'd like me to add to the summary?")

    elif intent == "out_of_scope":
        draft = llm.chat(prompts.OUT_OF_SCOPE, [{"role": "user", "content": user_msg}],
                         model=config.MODEL_FAST, max_tokens=300)

    else:  # chit_chat
        draft = llm.chat(prompts.CHIT_CHAT, state.recent_history(4), model=config.MODEL_FAST, max_tokens=200)

    reviewed = compliance.review(state, user_msg, intent, draft, sources_text=sources_text,
                                 computations=computations, injection_suspected=injection)
    final = reviewed["final"]

    if reviewed["record"]["verdict"] == "handoff" and card is None:
        card = handoff.build_card(state, "compliance reviewer escalated")

    state.messages.append({"role": "assistant", "content": final})
    state.last_sources = sources
    return TurnResult(
        response=final,
        intent=intent,
        sources=sources,
        eligibility=state.last_eligibility if intent == "eligibility" else [],
        handoff_card=card,
        compliance=reviewed["record"],
        trace=list(state.trace),
        user_msg=user_msg,
    )
