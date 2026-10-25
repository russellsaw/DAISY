"""Assistant page: the customer conversation, the live profile and the agent timeline."""
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
AGENT_ICONS = {
    "PII guard": ":material/shield_lock:",
    "Orchestrator": ":material/alt_route:",
    "Product Knowledge": ":material/menu_book:",
    "Eligibility": ":material/calculate:",
    "Boundary": ":material/front_hand:",
    "Handoff": ":material/support_agent:",
    "Compliance": ":material/verified_user:",
    "Drafting": ":material/edit_note:",
}
VERDICT_BADGE = {
    "approve": ":green-badge[:material/verified_user: Passed compliance review]",
    "revise": ":orange-badge[:material/edit: Revised by compliance]",
    "handoff": ":red-badge[:material/support_agent: Replaced with a banker handoff]",
}
STATUS_BADGE = {"eligible": ("Meets today's criteria", "green", ":material/check_circle:"),
                "not_eligible": ("Doesn't meet today's criteria", "red", ":material/cancel:"),
                "need_more_info": ("Needs more info", "gray", ":material/help:")}
SUGGESTIONS = {
    ":material/home: Can I finance an HDB flat?":
        "I'm 32, a PR earning $7k a month. Can I get a home loan for a $550k HDB flat? "
        "I'd borrow $380k over 25 years and have no other loans.",
    ":material/credit_card: Card annual fees": "Is there an annual fee on DBS credit cards?",
    ":material/payments: Cashline vs personal loan": "How does Cashline differ from a personal loan?",
    ":material/event_busy: Early repayment fees": "Is there a fee if I repay my DBS personal loan early?",
}

# A session that predates a code change may hold an older ConversationState (e.g. without a conversation id
# for the flight recorder): start that session afresh rather than crash.
if not isinstance(st.session_state.get("state"), ConversationState):
    st.session_state.state = ConversationState()
    st.session_state.turns = []
if "turns" not in st.session_state:
    st.session_state.turns = []  # list of {"user": str, "result": TurnResult, "seconds": float}

state: ConversationState = st.session_state.state


def render_trace(result, label: str) -> None:
    with st.expander(label, type="compact"):
        for ev in result.trace:
            with st.expander(f"{ev.agent} · {md_safe(ev.summary)}", type="step", icon=AGENT_ICONS.get(ev.agent)):
                if ev.detail:
                    st.json(ev.detail, expanded=False)
                else:
                    st.caption("No further detail.")
        if result.compliance.get("original_draft"):
            st.caption("Draft before compliance review")
            st.text(result.compliance["original_draft"])


def render_assistant(result, seconds: float | None) -> None:
    st.markdown(md_safe(result.response))
    st.markdown(VERDICT_BADGE.get(result.compliance.get("verdict"), ""))
    if result.defence:  # Swiss cheese: which layer acted on this reply
        badges = []
        for key, name in defence.LAYERS:
            layer = result.defence.get(key, {})
            if layer.get("status", "n/a") == "n/a":
                continue
            color = "green" if layer.get("acted") else "gray"
            badges.append(f":{color}-badge[{name}: {layer['status']}]")
        st.caption(":material/layers: " + " ".join(badges))
    if result.sources:
        def label(src):
            ns = src.get("n")
            ns = ns if isinstance(ns, list) else ([ns] if ns else [])
            return ("".join(f"[{n}]" for n in ns) + " " if ns else "") + f"[{src['title']}]({src['url']})"
        st.caption(":material/link: Sources · " + " · ".join(label(src) for src in result.sources))
    took = f" in {seconds:.0f}s" if seconds else ""
    render_trace(result, f"Checked by {len({ev.agent for ev in result.trace})} agents{took} · intent: {result.intent}")


# ---------------- Sidebar: live customer profile, eligibility, lead card ----------------
with st.sidebar:
    st.caption(f"{config.ASSISTANT_NAME} · {config.ASSISTANT_TAGLINE} · home loans, cards and personal loans · "
               "product information only")

    kb = retriever.load()
    if kb.ready:
        st.caption(f":material/library_books: {len(kb.meta.get('documents', []))} sources · "
                   f"{len(kb.chunks)} passages · built {kb.meta.get('built_on')}")
    else:
        st.error("Knowledge base missing. Run `python scripts/ingest.py`.", icon=":material/error:")

    st.subheader("Customer profile", anchor=False)
    profile = state.profile.to_dict()
    if profile:
        if state.is_confirmed():
            st.badge("Checked with the customer", icon=":material/verified:", color="green")
        elif state.pending_readback:
            st.badge("Read back, waiting for a yes", icon=":material/pending:", color="orange")
        else:
            st.badge("Not yet checked with the customer", icon=":material/help:", color="gray")
    with st.container(border=True):
        if profile:
            for key, value in profile.items():
                tick = " :green[:material/check_circle:]" if state.field_confirmed(key) else ""
                if key == "commitments":
                    for c in value:
                        ends = f"ends {fmt_date(c['end_date'])}" if c.get("end_date") else "no end date given"
                        amount = md_safe(f"S${c['monthly']:,.0f}/month")
                        st.markdown(f":gray[{c['kind'].capitalize()}]{tick}  \n**{amount}** · {ends}")
                    continue
                if isinstance(value, float):
                    value = f"{value:,.0f}"
                st.markdown(f":gray[{PROFILE_FIELDS[key][0]}]{tick}  \n**{md_safe(str(value))}**")
        else:
            st.caption("Fills in as the customer shares details.")

    if state.corrections:
        with st.expander(f"Readback caught {len(state.corrections)} correction(s)", icon=":material/rule:"):
            for c in state.corrections:
                st.markdown(md_safe(f"- **{c['field']}**: {c['from']} → {c['to']}"))

    if state.last_eligibility:
        st.subheader("Indicative eligibility", anchor=False)
        for r in state.last_eligibility:
            label, color, icon = STATUS_BADGE[r["status"]]
            st.markdown(f"**{r['product_name']}**")
            st.badge(label, color=color, icon=icon)

    if state.handoff_card:
        card = state.handoff_card
        st.subheader("Banker lead card", anchor=False)
        with st.container(border=True):
            st.markdown(md_safe(card["customer_summary"]))
            st.markdown(f":gray[Next step]  \n{md_safe(card['suggested_next_step'])}")
            st.badge(f"Urgency: {card['urgency']}", color={"high": "red", "medium": "orange"}.get(card["urgency"], "gray"))
            if card.get("paths") or card.get("no_clear_path_for"):
                st.markdown(":gray[Not now, but when]")
                for path in card.get("paths", []):
                    when = f"from {fmt_date(path['earliest_date'])}" if path["earliest_date"] else "what-if, no date"
                    st.markdown(f"- {md_safe(path['description'])} · *{when}*")
                for name in card.get("no_clear_path_for", []):
                    st.markdown(f"- {name}: no clear path on today's criteria")
                st.caption(f":material/history: Assessed {fmt_date(card['assessed_on'])} · criteria "
                           f"{card['criteria_version']} · {card['banker_note']}")
            with st.expander("Full card", icon=":material/badge:"):
                st.json(card)

    st.caption(f":material/flight: Recorded as `{state.conversation_id}` — replay it on the Flight recorder page.")
    if st.button("New conversation", icon=":material/restart_alt:", width="stretch"):
        st.session_state.state = ConversationState()
        st.session_state.turns = []
        st.rerun()

# ---------------- Main: conversation ----------------
if not st.session_state.turns:
    st.title(f"{config.ASSISTANT_NAME}", anchor=False)
    st.markdown(f"##### {config.ASSISTANT_TAGLINE}")
    st.markdown("Home loans, cards and personal loans: clear answers, checked before you see them.")
    st.caption("Product information and indicative eligibility from DBS's published criteria. "
               "Not financial advice and not a loan approval. Every reply is reviewed by a compliance agent.")

for turn in st.session_state.turns:
    with st.chat_message("user"):
        st.markdown(md_safe(turn["user"]))
    with st.chat_message("assistant", avatar=AVATAR):
        render_assistant(turn["result"], turn.get("seconds"))

prompt = st.chat_input("Ask about home loans, cards or personal loans…", submit_mode="disable")
if st.session_state.turns and state.pending_readback:
    if st.button("Yes, that's right", icon=":material/check:", type="primary"):
        prompt = "Yes, that's right."
if not st.session_state.turns:
    suggestions = st.empty()
    picked = suggestions.pills("Try asking", list(SUGGESTIONS), label_visibility="collapsed")
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
                status.update(label=f":shimmer[{ev.agent}…]")
                with st.status(f"{ev.agent} · {md_safe(ev.summary)}", type="step", state="complete"):
                    st.caption(md_safe(ev.summary))

            state.listener = on_event
            n_messages = len(state.messages)
            started = time.time()
            try:
                result = run_turn(state, prompt)
            except Exception as exc:  # noqa: BLE001
                del state.messages[n_messages:]  # don't leave an unanswered user turn in the history
                status.update(label="Something went wrong", state="error")
                st.error(f"Something went wrong: {exc}", icon=":material/error:")
                st.stop()
            finally:
                state.listener = None
            seconds = time.time() - started
            status.update(label=f"Checked in {seconds:.0f}s", state="complete")
        st.markdown(md_safe(result.response))
    # Store the redacted message, so an NRIC or card number typed in never stays in the session.
    st.session_state.turns.append({"user": result.user_msg or prompt, "result": result, "seconds": seconds})
    st.rerun()
