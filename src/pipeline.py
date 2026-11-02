"""run_turn(): one customer message in, one reviewed response out.

    Orchestrator -> (Product Knowledge | Eligibility | boundary | handoff | ...)
                 -> Compliance (veto) -> response

Everything the UI and the eval runner need comes back in TurnResult.
"""
from __future__ import annotations

import copy
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from . import config, defence, llm, pii, recorder
from .agents import compliance, eligibility, handoff, input_guard, orchestrator, product_knowledge, prompts
from .rules import counterfactual
from .state import ConversationState


_POOL = ThreadPoolExecutor(max_workers=4)

HANDOFF_REQUEST_REPLY = (
    "I've put together a summary of our conversation for a DBS banker so you won't need to repeat yourself. "
    "A banker can take it from here, walk you through the application and answer anything I couldn't. "
    "Is there anything else you'd like me to add to the summary?"
)


# A bare "yes" to a readback. Anything longer is left to the orchestrator's readback_reply.
_AFFIRM = r"(yes|yep|yeah|yup|correct|right|ok|okay|sure|that'?s (all )?(right|correct)|all (good|correct|right)|looks (good|right))"
_YES = re.compile(rf"^\s*{_AFFIRM}([\s.!,]+({_AFFIRM}|thanks?|thank you|please))*[\s.!]*$", re.IGNORECASE)
WHAT_TO_CHANGE_REPLY = ("No problem. Tell me which detail is wrong and the right value, and I'll read everything "
                        "back again before running the check.")


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
    counterfactuals: list = field(default_factory=list)  # "not now, but when" results, one per failing product
    sources_text: str = ""  # what the drafting agent was grounded on (for the traceability eval)
    computations: dict = field(default_factory=dict)  # engine numbers the reply may use
    readback: bool = False      # this turn read the parameters back (no engine call)
    engine_ran: bool = False    # the rules engine ran this turn
    confirmed: bool = False     # the profile equals what the customer said yes to (after this turn)
    corrections: list = field(default_factory=list)  # values the customer corrected this turn
    defence: dict = field(default_factory=dict)  # which defence layer acted (src/defence.py)
    draft: str = ""             # the reply as drafted, before review (flight recorder)
    guard: dict = field(default_factory=dict)  # input guardrail decision (Feature 5)
    log_path: str = ""          # where this conversation's flight-recorder log is


_DRAFT_AGENT = {"product_qa": "Product Knowledge", "comparison": "Product Knowledge", "eligibility": "Eligibility",
                "advice_seeking": "Boundary", "handoff_request": "Handoff", "out_of_scope": "Out of scope",
                "chit_chat": "Chit-chat", "refused": "Fallback",
                "blocked": "Input guardrail"}


def run_turn(state: ConversationState, user_msg: str) -> TurnResult:
    started_at = time.time()
    state.turn_count += 1
    state.trace = []
    state.last_sources = []
    user_msg, pii_found = pii.redact(user_msg)
    if pii_found:
        state.add_trace("PII guard", f"removed {', '.join(pii_found)} before any agent saw the message")
    state.messages.append({"role": "user", "content": user_msg})

    sources: list[dict] = []
    sources_text = ""
    computations: dict = {}
    cfs: list[dict] = []
    out: dict = {}
    draft = ""
    card_job: Future | None = None

    def start_card(trigger: str) -> Future:
        # The lead card only reads the transcript, so it is built while the reply is drafted and
        # reviewed instead of before it (saves one model round-trip of latency).
        return _POOL.submit(handoff.build_card, state, trigger)

    was_pending = state.pending_readback
    reference = state.readback_profile if was_pending else state.confirmed_profile
    # Input guardrail (Feature 5) runs in parallel with the orchestrator, so it adds no latency. If it
    # blocks, everything the orchestrator extracted from the message is thrown away.
    profile_before = copy.deepcopy(state.profile)
    guard_job = _POOL.submit(input_guard.check, user_msg)
    try:
        decision = orchestrator.route(state, user_msg)
    except llm.Refusal as exc:
        decision = {"intent": "refused", "products": [], "injection_suspected": True}
        state.add_trace("Orchestrator", f"model declined to route the message ({exc})")
    guard = guard_job.result()
    intent = decision["intent"]
    products = decision.get("products") or []
    injection = bool(decision.get("injection_suspected"))
    if guard["action"] == "block":
        state.profile = profile_before
        decision["profile_changed"] = []
        intent, products = "blocked", []
        state.add_trace("Input guardrail", f"BLOCKED {guard['category']} ({guard['source']}); "
                                           "the orchestrator's extraction was discarded", guard)
    elif guard["action"] == "escalate":
        intent = "handoff_request"
        state.add_trace("Input guardrail", f"ESCALATED {guard['category']}: routed to a banker, priority high", guard)
    else:
        state.add_trace("Input guardrail", f"allowed ({guard['source']})", guard)

    # ---- Readback before clearance (Feature 2) ----
    changed = decision.get("profile_changed") or []
    corrections = state.log_corrections(reference) if changed else []
    if corrections:
        state.add_trace("Readback", "customer corrected " + ", ".join(c["field"] for c in corrections), corrections)
    deterministic_reply = None
    if intent == "blocked":
        pass  # a blocked message can't confirm, correct or change anything
    elif was_pending:
        if changed:  # a correction: read everything back again before anything runs
            intent, products = "eligibility", state.pending_products
        elif decision.get("readback_reply") == "confirm" or _YES.match(user_msg):
            state.confirm()
            state.add_trace("Readback", "customer said yes: parameters confirmed, engine may run",
                            state.confirmed_profile)
            intent, products = "eligibility", state.pending_products
        elif decision.get("readback_reply") == "correct":
            intent, deterministic_reply = "eligibility", WHAT_TO_CHANGE_REPLY
    elif changed and state.confirmed_profile is not None and not state.is_confirmed():
        # A confirmed fact changed later (e.g. a contradicted end date): re-confirm before any result.
        intent, products = "eligibility", products or state.pending_products

    try:
        if intent in ("product_qa", "comparison"):
            out = product_knowledge.answer(state, user_msg, products, decision.get("search_query", ""))
            draft, sources = out["draft"], out["sources"]
            # Exactly the excerpts the drafting agent saw ([n], title, as-of date, URL), so the reviewer can
            # check citations and a quoted "as of" date or link traces to its source like any other fact.
            sources_text = out.get("excerpts") or "\n\n".join(c["text"] for c in out["chunks"])

        elif intent == "blocked":
            draft = guard["reply"]

        elif intent == "eligibility" and deterministic_reply:
            draft = deterministic_reply
            out = {"engine_ran": False}

        elif intent == "eligibility":
            out = eligibility.assess(state, user_msg, products, search_query=decision.get("search_query", ""))
            draft, computations = out["draft"], out["computations"]
            sources = out.get("sources", [])
            cfs = out.get("counterfactuals", [])
            sources_text = "\n\n".join(x for x in (out.get("excerpts", ""), "\n".join(str(r) for r in out["results"])) if x)

        elif intent == "advice_seeking":
            card_job = start_card("customer sought personal financial advice")
            draft = llm.chat(prompts.BOUNDARY, [{"role": "user", "content":
                             f"Customer profile:\n{state.profile.describe()}\n\nCustomer message:\n{user_msg}"}],
                             model=config.MODEL_MAIN, max_tokens=1500)
            state.add_trace("Boundary", "advice-seeking: declined to advise, offered banker")

        elif intent == "handoff_request":
            card_job = start_card("customer may be in financial distress: prioritise a call back"
                                  if guard["action"] == "escalate" else "customer asked to apply / speak to a banker")
            # Drafted (not templated) so it can acknowledge what the customer actually said — urgency,
            # worry — while promising nothing; compliance reviews it like any other reply.
            draft = llm.chat(prompts.HANDOFF_REPLY, [{"role": "user", "content": user_msg}],
                             model=config.MODEL_MAIN, max_tokens=1200) or HANDOFF_REQUEST_REPLY

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

    is_readback = intent == "eligibility" and bool(out.get("readback"))
    reviewed = compliance.review(state, user_msg, intent, draft, sources_text=sources_text,
                                 computations=computations, injection_suspected=injection,
                                 deterministic=is_readback or draft == WHAT_TO_CHANGE_REPLY or intent == "blocked")
    final = reviewed["final"]

    # "Not now, but when": the outcome lines and the fixed outcome sentence are written by code and
    # appended after review, so no model can alter or omit them. Not shown if review replaced the reply.
    if cfs and final != compliance.HANDOFF_FALLBACK:
        final = final.rstrip() + "\n\n" + counterfactual.outcome_block(cfs)
    if any(not cf["paths"] for cf in cfs) and card_job is None:
        card_job = start_card(counterfactual.NO_PATH_TRIGGER)

    if card_job is None and (reviewed["record"]["verdict"] == "handoff" or intent == "refused"):
        card_job = start_card("compliance reviewer escalated")
    card = card_job.result() if card_job else None
    if card is not None and guard["action"] == "escalate":
        card["urgency"] = "high"  # vulnerable customer: fair dealing says a human, soon

    layers = defence.assess(
        intent=intent, draft=draft, sources=sources, sources_text=sources_text,
        chunks_retrieved=len(out.get("chunks", [])) if intent in ("product_qa", "comparison") else 0,
        engine_ran=bool(intent == "eligibility" and out.get("engine_ran")), readback=is_readback,
        computations=computations, compliance_record=reviewed["record"], pii_found=pii_found,
        profile=state.profile.to_dict(), user_msg=user_msg, guard=guard)
    state.add_trace("Defence layers", ", ".join(f"{k}={v['status']}" for k, v in layers.items()), layers)

    state.messages.append({"role": "assistant", "content": final})
    state.last_sources = sources
    result = TurnResult(
        response=final,
        intent=intent,
        sources=sources,
        eligibility=state.last_eligibility if intent == "eligibility" else [],
        handoff_card=card,
        compliance=reviewed["record"],
        trace=list(state.trace),
        user_msg=user_msg,
        counterfactuals=cfs,
        sources_text=sources_text,
        computations=computations,
        readback=is_readback,
        engine_ran=bool(intent == "eligibility" and out.get("engine_ran")),
        confirmed=state.is_confirmed(),
        corrections=corrections,
        defence=layers,
        draft=draft,
        guard=guard,
    )
    if state.record:
        try:
            result.log_path = str(recorder.record_turn(
                state, started_at=started_at, user_msg=user_msg, pii_found=pii_found, draft=draft,
                draft_agent=_DRAFT_AGENT.get(intent, intent), result=result, finished_at=time.time()))
        except Exception as exc:  # noqa: BLE001 — the recorder must never break a customer's turn
            state.add_trace("Flight recorder", f"could not write the log ({exc})")
    return result
