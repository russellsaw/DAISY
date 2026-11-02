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
from .rules.criteria import DBS_PLANNER_RATE, MAS
from .state import ConversationState


_POOL = ThreadPoolExecutor(max_workers=4)

HANDOFF_REQUEST_REPLY = (
    "I've put together a summary of our conversation for a DBS banker so you won't need to repeat yourself. "
    "I can't promise an outcome, approval or timing."
)
# What really happens to a handoff. Written by code and appended after review, so no model can promise more.
HANDOFF_NOTE = ("This is a prototype, so the summary stays on the Banker view page: nothing is sent to DBS and no one "
                "will contact you automatically. To speak to someone now, please contact DBS directly.")

# Two medium-term rates a customer can come across: DBS's planning tools' current 4.65%, and the MAS floor the
# eligibility check uses. The maths is the same, so the rate is what moves the answer.
_FLOOR = f"{MAS['stress_rate']['value']:.0%}"
_PLANNER = f"{DBS_PLANNER_RATE['value']:.2%}"
_TOOLS = f"[planning tools]({DBS_PLANNER_RATE['source']})"  # a link, so the customer can check the figure
_STRESS_MENTION = re.compile(r"4\.65\s*%")
STRESS_NOTE = (f"A note on rates: {_PLANNER} is what DBS's {_TOOLS} currently assume. MAS sets a floor of {_FLOOR} for "
               "residential property loans, and the eligibility check here uses that floor, so the two can give "
               "different figures.")
# After a shortlist or comparison, the natural next step.
DISCOVERY_NOTE = ("Next step: if you tell me your age, residency and monthly income, I can run an indicative check of "
                  "whether you meet the general entry criteria.")
HOME_CHECK_NOTE = (f"A note on this check: it uses the {_FLOOR} floor MAS sets for residential property loans. DBS's own "
                   f"{_TOOLS} currently assume {_PLANNER}, which can give a lower maximum loan, so a banker's "
                   "figure may differ.")


def _code_notes(intent: str, reply: str, home_check: bool = False, details_known: bool = False) -> str:
    """Code-written lines that follow a reply: what the customer should know whatever the model wrote."""
    notes = []
    if intent == "handoff_request":
        notes.append(HANDOFF_NOTE)
    if home_check:
        notes.append(HOME_CHECK_NOTE)
    elif _STRESS_MENTION.search(reply):
        notes.append(STRESS_NOTE)
    if intent == "comparison" and not details_known:  # don't ask for what they've already given
        notes.append(DISCOVERY_NOTE)
    return "\n\n".join(notes)


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
    max_loan: dict | None = None  # the "how much can I borrow?" result, when this turn ran it
    readback: bool = False      # this turn read the parameters back (no engine call)
    engine_ran: bool = False    # the rules engine ran this turn
    confirmed: bool = False     # the profile equals what the customer said yes to (after this turn)
    corrections: list = field(default_factory=list)  # values the customer corrected this turn
    defence: dict = field(default_factory=dict)  # which defence layer acted (src/defence.py)
    draft: str = ""             # the reply as drafted, before review (flight recorder)
    guard: dict = field(default_factory=dict)  # input guardrail decision
    log_path: str = ""          # where this conversation's flight-recorder log is


_DRAFT_AGENT = {"product_qa": "Product Knowledge", "comparison": "Product Knowledge", "eligibility": "Eligibility",
                "advice_seeking": "Boundary", "handoff_request": "Handoff", "out_of_scope": "Out of scope",
                "chit_chat": "Chit-chat", "refused": "Fallback",
                "blocked": "Input guardrail"}


def _wants_max_loan(state: ConversationState, decision: dict, products: list[str], changed: list[str]) -> bool:
    """A new "how much can I borrow?", or one already waiting on details, unless they've moved on: they've now named
    a loan amount to check, or they're asking about a different product."""
    if decision.get("asks_max_loan"):
        return not products or "home_loan" in products
    if state.pending_max_loan and ("home_loan_amount" in changed or (products and "home_loan" not in products)):
        state.pending_max_loan = False
    return state.pending_max_loan


def run_turn(state: ConversationState, user_msg: str) -> TurnResult:
    started_at = time.time()
    state.turn_count += 1
    state.trace = []
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
    # Input guardrail runs in parallel with the orchestrator, so it adds no latency. If it
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

    # ---- Readback before clearance ----
    changed = decision.get("profile_changed") or []
    corrections = state.log_corrections(reference) if changed else []
    if corrections:
        state.add_trace("Readback", "customer corrected " + ", ".join(c["field"] for c in corrections), corrections)
    deterministic_reply = None
    if intent == "blocked" or guard["action"] == "escalate":
        pass  # a blocked message can't confirm, correct or change anything; distress goes to a banker summary first
    elif was_pending:
        if changed and intent not in ("handoff_request", "advice_seeking"):
            intent, products = "eligibility", state.pending_products  # a correction: read everything back again
        elif changed:
            pass  # they also asked for a banker or for advice: answer that, and the readback lapses below
        elif decision.get("readback_reply") == "confirm" or _YES.match(user_msg):
            if state.profile.to_dict() == state.readback_profile:  # a yes only counts for what they actually saw
                state.confirm()
                state.add_trace("Readback", "customer said yes: parameters confirmed, engine may run",
                                state.confirmed_profile)
            intent, products = "eligibility", state.pending_products
        elif decision.get("readback_reply") == "correct":
            intent, deterministic_reply = "eligibility", WHAT_TO_CHANGE_REPLY
    elif (changed and state.confirmed_profile is not None and not state.is_confirmed()
          and intent not in ("handoff_request", "advice_seeking", "product_qa", "comparison")):
        # A confirmed fact changed later (e.g. a contradicted end date): re-confirm before any result.
        # A question or a banker request in the same message is still answered; no result is shown either way.
        intent, products = "eligibility", products or state.pending_products

    if intent not in ("eligibility", "blocked") and not state.pending_readback:
        state.pending_max_loan = False

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

        elif intent == "eligibility" and _wants_max_loan(state, decision, products, changed):
            out = eligibility.assess_max_loan(state, user_msg, search_query=decision.get("search_query", ""))
            draft, computations = out["draft"], out["computations"]
            sources = out.get("sources", [])
            sources_text = out.get("excerpts", "")

        elif intent == "eligibility":
            out = eligibility.assess(state, user_msg, products, search_query=decision.get("search_query", ""))
            draft, computations = out["draft"], out["computations"]
            sources = out.get("sources", [])
            cfs = out.get("counterfactuals", [])
            sources_text = "\n\n".join(x for x in (out.get("excerpts", ""), "\n".join(str(r) for r in out["results"])) if x)

        elif intent == "advice_seeking":
            card_job = start_card("customer sought personal financial advice")
            # No recommendation, but still the facts: the same retrieval and citations as a product answer.
            chunks, excerpts = product_knowledge.search(user_msg, products, decision.get("search_query", ""))
            draft = llm.chat(prompts.BOUNDARY, [{"role": "user", "content":
                             f"Customer profile:\n{state.profile.describe()}\n\n"
                             f"Source excerpts:\n{excerpts or '(none)'}\n\nCustomer message:\n{user_msg}"}],
                             model=config.MODEL_MAIN, max_tokens=2500)
            sources, cited = product_knowledge.cited_sources(draft, chunks)
            sources_text = excerpts
            out = {"chunks": chunks}
            state.add_trace("Boundary", f"advice-seeking: declined to advise, gave cited facts, summary prepared "
                                        f"(retrieved {len(chunks)} chunks, cited {len(cited)})",
                            product_knowledge.hits_detail(user_msg, decision.get("search_query", ""), products,
                                                          chunks, cited))

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
            draft = llm.chat(prompts.CHIT_CHAT, state.recent_history(4), model=config.MODEL_FAST, max_tokens=1500)
    except llm.Refusal as exc:
        draft = compliance.HANDOFF_FALLBACK
        state.add_trace("Drafting", f"model declined to draft a reply ({exc}); using the banker handoff")

    is_readback = intent == "eligibility" and bool(out.get("readback"))
    # "Not now, but when": the outcome lines and the fixed outcome sentence are written by code and
    # appended after review, so no model can alter or omit them. The reviewer sees them, so it doesn't
    # write them into the reply as well. Not shown if review replaced the reply.
    outcome = counterfactual.outcome_block(cfs) if cfs else ""
    home_check = bool(out.get("engine_ran")) and any(r["product"] == "home_loan" and r["status"] != "need_more_info"
                                                     for r in out.get("results", []))
    reviewed = compliance.review(state, user_msg, intent, draft, sources_text=sources_text,
                                 computations=computations, injection_suspected=injection,
                                 deterministic=(is_readback or draft == WHAT_TO_CHANGE_REPLY or intent == "blocked"
                                                or bool(out.get("code_written"))),
                                 appended="\n\n".join(x for x in (outcome, HANDOFF_NOTE if intent == "handoff_request"
                                                                   else "", HOME_CHECK_NOTE if home_check else "") if x))
    final = reviewed["final"]
    if outcome and final != compliance.HANDOFF_FALLBACK:
        final = final.rstrip() + "\n\n" + outcome
    known = not state.profile.missing(["age", "residency", "monthly_income"])
    notes = (_code_notes(intent, final, home_check, known)
             if final != compliance.HANDOFF_FALLBACK and not out.get("code_written") else "")
    if notes:
        final = final.rstrip() + "\n\n" + notes
        sources_text = "\n\n".join(x for x in (sources_text, notes) if x)  # so its numbers count as traced
    if any(not cf["paths"] for cf in cfs) and card_job is None:
        card_job = start_card(counterfactual.NO_PATH_TRIGGER)

    if card_job is None and (reviewed["record"]["verdict"] == "handoff" or intent == "refused"):
        card_job = start_card("compliance reviewer escalated")
    card = None
    if card_job:
        try:
            card = card_job.result()
        except Exception as exc:  # noqa: BLE001 — the reply is ready: a failed lead card must not throw it away
            state.add_trace("Handoff", f"could not prepare the lead card ({type(exc).__name__}: {exc})")
    if card is not None and guard["action"] == "escalate":
        card["urgency"] = "high"  # vulnerable customer: fair dealing says a human, soon

    layers = defence.assess(
        intent=intent, draft=draft, sources=sources, sources_text=sources_text,
        chunks_retrieved=len(out.get("chunks", [])) if intent in ("product_qa", "comparison", "advice_seeking") else 0,
        engine_ran=bool(intent == "eligibility" and out.get("engine_ran")), readback=is_readback,
        computations=computations, compliance_record=reviewed["record"], pii_found=pii_found,
        profile=state.profile.to_dict(), user_msg=user_msg, guard=guard)
    state.add_trace("Defence layers", ", ".join(f"{k}={v['status']}" for k, v in layers.items()), layers)

    # A readback only stands while it's the question on the table. After anything else, a later "ok thanks"
    # must not count as a yes: the customer gets a fresh readback when they come back to it.
    if (was_pending and state.pending_readback and intent != "blocked" and not is_readback
            and draft != WHAT_TO_CHANGE_REPLY):
        state.readback_profile = None
    state.messages.append({"role": "assistant", "content": final})
    result = TurnResult(
        response=final,
        intent=intent,
        sources=sources,
        eligibility=state.last_eligibility if intent == "eligibility" and out.get("engine_ran") else [],
        handoff_card=card,
        compliance=reviewed["record"],
        trace=list(state.trace),
        user_msg=user_msg,
        counterfactuals=cfs,
        sources_text=sources_text,
        computations=computations,
        max_loan=out.get("max_loan"),
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
