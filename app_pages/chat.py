"""Assistant page: the customer conversation, the live profile and the agent timeline."""
import copy
import sys
import threading
import time

import streamlit as st

from src import config, defence
from src.knowledge import retriever
from src.pipeline import run_turn
from src.state import PROFILE_FIELDS, ConversationState
from src.rules.counterfactual import fmt_date
from src.ui import md_safe

AVATAR = "static/daisy-mark.svg"
# Plain names for each step of the pipeline, as the customer sees them (trace data keeps the internal names).
STEP_NAMES = {
    "PII guard": "Privacy check",
    "Input guardrail": "Safety check",
    "Orchestrator": "Understanding your question",
    "Readback": "Checking your details",
    "Product Knowledge": "Finding the answer",
    "Eligibility": "Eligibility check",
    "Counterfactual": "What could change the result",
    "Boundary": "Advice check",
    "Handoff": "Banker handoff",
    "Drafting": "Writing the reply",
    "Compliance": "Compliance review",
    "Defence layers": "Safety summary",
    "Flight recorder": "Saving the conversation",
}
VERDICT_BADGE = {
    "approve": ":green-badge[:material/verified_user: Checked before sending]",
    "revise": ":orange-badge[:material/edit: Edited before sending]",
    "handoff": ":red-badge[:material/support_agent: Referred to a banker]",
}
TEMPLATE_BADGE = ":gray-badge[:material/description: Standard wording, checked by code]"
# What each safety check did, in plain words. The status values themselves come from src/defence.py.
CHECK_WORDS = {
    "input_guard": {"clear": "Message checked", "pii removed": "Personal details removed",
                    "blocked": "Message blocked", "escalated": "Flagged for a banker"},
    "retrieval": {"grounded": "Matches DBS pages", "ungrounded": "Not matched to DBS pages",
                  "uncited": "No source cited", "no_source": "No DBS page found"},
    "engine": {"computed": "Eligibility calculated", "withheld": "Waiting for your details"},
    "compliance": {"approved": "Reviewed", "rewritten": "Edited in review", "replaced": "Referred to a banker",
                   "skipped": "Standard reply"},
    "output_filter": {"clean": "Wording checked", "acted": "Wording corrected"},
}
# Coded profile values (the orchestrator's enums) in the words a customer would use.
PLAIN_VALUES = {
    "residency": {"citizen": "Singapore Citizen", "pr": "Permanent Resident", "foreigner": "Foreigner"},
    "property_type": {"hdb": "HDB flat", "ec": "Executive condo", "private": "Private property"},
    "employment_type": {"salaried": "Salaried", "self_employed": "Self-employed", "variable": "Variable income",
                        "unemployed": "Not working"},
}
STATUS_BADGE = {"eligible": ("Meets today's criteria", "green", ":material/check_circle:"),
                "not_eligible": ("Doesn't meet today's criteria", "red", ":material/cancel:"),
                "need_more_info": ("More details needed", "gray", ":material/help:")}
SUGGESTIONS = {
    ":material/home: Can I get an HDB loan?":
        "I'm 32, a PR earning $7k a month. Can I get a home loan for a $550k HDB flat? "
        "I'd borrow $380k over 25 years and have no other loans.",
    ":material/calculate: How much can I borrow?": "How much can I borrow for a home?",
    ":material/search: Find a card for my spending":
        "I'm looking for a DBS credit card. What options are there for different kinds of spending?",
    ":material/payments: Cashline or personal loan?": "How does Cashline differ from a personal loan?",
    ":material/credit_card: Credit card annual fees": "Is there an annual fee on DBS credit cards?",
}


def step_name(agent: str) -> str:
    return STEP_NAMES.get(agent, agent)

# A session that predates a code change may hold an older ConversationState (e.g. without a conversation id
# for the flight recorder). A type check isn't enough — after a hot reload the object can be the "same" class
# but missing newer fields — so require every current field, else start that session afresh rather than crash.
_saved = st.session_state.get("state")
if not isinstance(_saved, ConversationState) or not all(
        hasattr(_saved, f) for f in ConversationState.__dataclass_fields__):
    st.session_state.state = ConversationState()
    st.session_state.turns = []
if "turns" not in st.session_state:
    st.session_state.turns = []  # list of {"user": str, "result": TurnResult, "seconds": float}

state: ConversationState = st.session_state.state


def render_assistant(result, seconds: float | None) -> None:
    st.markdown(md_safe(result.response))
    from_template = result.compliance.get("reviewer", "").startswith("code template")
    st.markdown(TEMPLATE_BADGE if from_template else VERDICT_BADGE.get(result.compliance.get("verdict"), ""))
    if result.defence:  # Swiss cheese: which safety check acted on this reply
        badges = []
        for key, _ in defence.LAYERS:
            layer = result.defence.get(key, {})
            status = layer.get("status", "n/a")
            if status == "n/a":
                continue
            color = "green" if layer.get("acted") else "gray"
            badges.append(f":{color}-badge[{CHECK_WORDS.get(key, {}).get(status, status)}]")
        st.caption(":material/layers: " + " ".join(badges))
    if result.sources:
        def label(src):
            ns = src.get("n")
            ns = ns if isinstance(ns, list) else ([ns] if ns else [])
            return ("".join(f"[{n}]" for n in ns) + " " if ns else "") + f"[{src['title']}]({src['url']})"
        st.caption(":material/link: Sources: " + ", ".join(label(src) for src in result.sources))
    if seconds:
        st.caption(f":material/schedule: Checked in {seconds:.0f} seconds")


# ---------------- Sidebar: live customer profile, eligibility, lead card ----------------
with st.sidebar:
    st.caption("DAISY is an AI prototype built for the DBS brief, not a live DBS service. It answers about DBS home "
               "loans, credit cards and personal loans. Product information only, not financial advice.")

    kb = retriever.load()
    if kb.ready:
        saved = min((c["fetched_at"][:10] for c in kb.chunks if c.get("fetched_at")), default=None)
        st.caption(f":material/library_books: Based on {len(kb.meta.get('documents', []))} DBS, POSB, MAS and HDB pages"
                   + (f", saved on or after {fmt_date(saved)}." if saved else "."))
    else:
        st.error("Knowledge base missing. Run `python scripts/ingest.py`.", icon=":material/error:")

    st.subheader("Your details", anchor=False)
    profile = state.profile.to_dict()
    if profile:
        if state.is_confirmed():
            st.badge("Confirmed by you", icon=":material/verified:", color="green")
        elif state.pending_readback:
            st.badge("Waiting for you to confirm", icon=":material/pending:", color="orange")
        else:
            st.badge("Not confirmed yet", icon=":material/help:", color="gray")
    with st.container(border=True):
        if profile:
            for key, value in profile.items():
                tick = " :green[:material/check_circle:]" if state.field_confirmed(key) else ""
                if key == "commitments":
                    for c in value:
                        ends = f"until {fmt_date(c['end_date'])}" if c.get("end_date") else "no end date given"
                        amount = md_safe(f"S${c['monthly']:,.0f} a month")
                        st.markdown(f":gray[{c['kind'].capitalize()}]{tick}  \n**{amount}**, {ends}")
                    continue
                if isinstance(value, float):
                    value = f"{value:,.0f}"
                value = PLAIN_VALUES.get(key, {}).get(value, value)
                st.markdown(f":gray[{PROFILE_FIELDS[key][0]}]{tick}  \n**{md_safe(str(value))}**")
        else:
            st.caption("Details you share will show here.")

    if state.corrections:
        n = len(state.corrections)
        with st.expander(f"{n} detail{'s' if n > 1 else ''} corrected", icon=":material/rule:"):
            for c in state.corrections:
                st.markdown(md_safe(f"- **{c['field']}**: {c['from']} → {c['to']}"))

    # Only while the details still match what was checked: a changed detail waits for a new readback.
    if state.last_eligibility and state.is_confirmed():
        st.subheader("Eligibility check", anchor=False)
        st.caption("Indicative only. This isn't a loan approval.")
        for r in state.last_eligibility:
            label, color, icon = STATUS_BADGE[r["status"]]
            st.markdown(f"**{r['product_name']}**")
            st.badge(label, color=color, icon=icon)

    # Only while the details still match what it was worked out from.
    if state.last_max_loan and state.last_max_loan["profile"] == state.profile.to_dict() and state.is_confirmed():
        st.subheader("Maximum home loan", anchor=False)
        st.caption("Indicative only. This isn't a loan approval.")
        for at, label in ((state.last_max_loan["at_floor"], "At the MAS floor"),
                          (state.last_max_loan["at_planner"], "At DBS's planning rate")):
            if at["max_loan"] is not None:
                st.markdown(md_safe(f":gray[{label} ({at['rate'] * 100:g}%)]  \n**S${at['max_loan']:,.0f}**"))

    if state.handoff_card:
        st.caption(":material/support_agent: A summary is ready for a DBS banker. In this prototype it stays on the "
                   "Banker view page and nothing is sent to DBS.")

    st.caption(":material/privacy_tip: Conversations are saved on this computer for review. NRIC/FIN and card numbers "
               "are removed. Phone numbers and emails aren't, so please leave them out.")
    if st.button("New conversation", icon=":material/restart_alt:", width="stretch"):
        st.session_state.state = ConversationState()
        st.session_state.turns = []
        st.rerun()

# ---------------- Main: conversation ----------------
if not config.ANTHROPIC_API_KEY:
    st.error("DAISY needs an API key to answer. Copy `.env.example` to `.env`, add your `ANTHROPIC_API_KEY` and "
             "restart the app.", icon=":material/key:")

if not st.session_state.turns:
    st.title(f":primary[{config.ASSISTANT_NAME}]", anchor=False)
    st.markdown(f"##### {config.ASSISTANT_TAGLINE}")
    st.markdown("Ask me about DBS home loans, credit cards and personal loans.")
    st.caption("I answer from DBS's published information and can check whether you meet the basic criteria. "
               "Replies are checked before you see them, by a compliance review or by code. This isn't financial advice "
               "or a loan approval.")

for turn in st.session_state.turns:
    with st.chat_message("user"):
        st.markdown(md_safe(turn["user"]))
    with st.chat_message("assistant", avatar=AVATAR):
        render_assistant(turn["result"], turn.get("seconds"))

prompt = st.chat_input("Ask about home loans, cards or personal loans", submit_mode="disable")
if st.session_state.turns and state.pending_readback:
    if st.button("Yes, that's right", icon=":material/check:", type="primary"):
        prompt = "Yes, that's right."
if not st.session_state.turns:
    suggestions = st.empty()
    # A new key after a failed turn, so the old pick doesn't fire again and override what's typed next.
    picked = suggestions.pills("Try asking", list(SUGGESTIONS), label_visibility="collapsed",
                               key=f"suggestion_{st.session_state.get('failed_turns', 0)}")
    if picked:
        prompt = SUGGESTIONS[picked]
    if prompt:
        suggestions.empty()

if prompt:
    with st.chat_message("user"):
        st.markdown(md_safe(prompt))
    with st.chat_message("assistant", avatar=AVATAR):
        with st.status(":shimmer[Reading your message]", type="compact") as status:
            owner = threading.get_ident()

            def on_event(ev):
                if threading.get_ident() != owner:  # background threads can't draw
                    return
                status.update(label=f":shimmer[{step_name(ev.agent)}]")
                st.status(step_name(ev.agent), type="step", state="complete")

            before_turn = copy.deepcopy(state)  # a failed turn must leave nothing behind: details, readback, count
            state.listener = on_event
            started = time.time()
            try:
                result = run_turn(state, prompt)
            except Exception as exc:  # noqa: BLE001
                st.session_state.state = before_turn
                st.session_state.failed_turns = st.session_state.get("failed_turns", 0) + 1
                status.update(label="Something went wrong", state="error")
                st.error("Sorry, something went wrong. Please try again.", icon=":material/error:")
                print(f"run_turn failed: {exc!r}", file=sys.stderr)  # details for the operator, not the customer
                st.stop()
            finally:
                state.listener = None
            seconds = time.time() - started
            status.update(label=f"Checked in {seconds:.0f} seconds", state="complete")
        st.markdown(md_safe(result.response))
    # Store the redacted message, so an NRIC or card number typed in never stays in the session.
    st.session_state.turns.append({"user": result.user_msg or prompt, "result": result, "seconds": seconds})
    st.rerun()
