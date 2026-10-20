"""run_turn(): one customer message in, one reviewed response out.

    Orchestrator -> (Product Knowledge | Eligibility | boundary | handoff | ...)
                 -> Compliance (veto) -> response

Everything the UI and the eval runner need comes back in TurnResult.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from . import config, llm, pii
from .agents import compliance, eligibility, handoff, orchestrator, product_knowledge, prompts
from .state import ConversationState


_POOL = ThreadPoolExecutor(max_workers=4)

HANDOFF_REQUEST_REPLY = (
    "I've put together a summary of our conversation for a DBS banker so you won't need to repeat yourself. "
    "A banker can take it from here, walk you through the application and answer anything I couldn't. "
    "Is there anything else you'd like me to add to the summary?"
)


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

    sources: list[dict] = []
    sources_text = ""
    computations: dict = {}
    card_job: Future | None = None

    def start_card(trigger: str) -> Future:
        # The lead card only reads the transcript, so it is built while the reply is drafted and
        # reviewed instead of before it (saves one model round-trip of latency).
        return _POOL.submit(handoff.build_card, state, trigger)

    try:
        decision = orchestrator.route(state, user_msg)
    except llm.Refusal as exc:
        decision = {"intent": "refused", "products": [], "injection_suspected": True}
        state.add_trace("Orchestrator", f"model declined to route the message ({exc})")
    intent = decision["intent"]
    products = decision.get("products") or []
    injection = bool(decision.get("injection_suspected"))

    try:
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
            card_job = start_card("customer sought personal financial advice")
            draft = llm.chat(prompts.BOUNDARY, [{"role": "user", "content":
                             f"Customer profile:\n{state.profile.describe()}\n\nCustomer message:\n{user_msg}"}],
                             model=config.MODEL_MAIN, max_tokens=1500)
            state.add_trace("Boundary", "advice-seeking: declined to advise, offered banker")

        elif intent == "handoff_request":
            card_job = start_card("customer asked to apply / speak to a banker")
            draft = HANDOFF_REQUEST_REPLY

        elif intent == "out_of_scope":
            draft = llm.chat(prompts.OUT_OF_SCOPE, [{"role": "user", "content": user_msg}],
                             model=config.MODEL_FAST, max_tokens=1200)

        elif intent == "refused":
            draft = compliance.HANDOFF_FALLBACK

        else:  # chit_chat
            draft = llm.chat(prompts.CHIT_CHAT, state.recent_history(4), model=config.MODEL_FAST, max_tokens=800)
    except llm.Refusal as exc:
        draft = compliance.HANDOFF_FALLBACK
        state.add_trace("Drafting", f"model declined to draft a reply ({exc}); using the banker handoff")

    reviewed = compliance.review(state, user_msg, intent, draft, sources_text=sources_text,
                                 computations=computations, injection_suspected=injection)
    final = reviewed["final"]

    if card_job is None and (reviewed["record"]["verdict"] == "handoff" or intent == "refused"):
        card_job = start_card("compliance reviewer escalated")
    card = card_job.result() if card_job else None

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
