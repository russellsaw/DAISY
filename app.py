"""Chat page. Run: streamlit run app.py"""
import streamlit as st

from src import config
from src.knowledge import retriever
from src.pipeline import run_turn
from src.state import PROFILE_FIELDS, ConversationState
from src.ui import md_safe

st.set_page_config(page_title=f"{config.ASSISTANT_NAME} · Chat", page_icon="💬", layout="wide")

if "state" not in st.session_state:
    st.session_state.state = ConversationState()
if "turns" not in st.session_state:
    st.session_state.turns = []  # list of (user_msg, TurnResult)

state: ConversationState = st.session_state.state

# ---------------- Sidebar: live customer profile + eligibility ----------------
with st.sidebar:
    st.title(config.ASSISTANT_NAME)
    st.caption(f"{config.BANK_NAME} retail lending · product information only")

    kb = retriever.load()
    if kb.ready:
        st.success(f"Knowledge base: {len(kb.meta.get('documents', []))} documents · {len(kb.chunks)} chunks · "
                   f"built {kb.meta.get('built_on')}")
    else:
        st.error("Knowledge base missing. Run `python scripts/ingest.py`.")

    st.subheader("Customer profile")
    profile = state.profile.to_dict()
    if profile:
        for key, value in profile.items():
            label = PROFILE_FIELDS[key][0]
            if isinstance(value, float):
                value = f"{value:,.0f}"
            st.markdown(f"**{label}:** {value}")
    else:
        st.caption("Fills in as the customer shares details.")

    if state.last_eligibility:
        st.subheader("Eligibility (indicative)")
        icon = {"eligible": "✅", "not_eligible": "❌", "need_more_info": "❔"}
        for r in state.last_eligibility:
            st.markdown(f"{icon[r['status']]} **{r['product_name']}** — {r['status'].replace('_', ' ')}")

    if state.handoff_card:
        st.subheader("Banker lead card")
        card = state.handoff_card
        st.info(md_safe(card["customer_summary"]))
        st.markdown(f"**Next step:** {md_safe(card['suggested_next_step'])}  \n**Urgency:** {card['urgency']}")
        with st.expander("Full card"):
            st.json(card)

    if st.button("Reset conversation"):
        st.session_state.state = ConversationState()
        st.session_state.turns = []
        st.rerun()

# ---------------- Main: chat ----------------
st.markdown(f"### Ask about {config.BANK_NAME} home loans, credit cards and personal loans")
st.caption("Product information and indicative eligibility only — not financial advice and not a loan approval.")

for user_msg, result in st.session_state.turns:
    with st.chat_message("user"):
        st.markdown(md_safe(user_msg))
    with st.chat_message("assistant"):
        st.markdown(md_safe(result.response))
        if result.sources:
            st.caption("Sources: " + " · ".join(f"[{s['title']}]({s['url']})" for s in result.sources))
        badge = {"approve": "🟢", "revise": "🟡", "handoff": "🔴"}.get(result.compliance.get("verdict"), "")
        with st.expander(f"{badge} Agent trace · intent: {result.intent} · compliance: {result.compliance.get('verdict')}"):
            for ev in result.trace:
                st.markdown(f"**{ev.agent}** — {ev.summary}")
            if result.compliance.get("issues"):
                st.markdown("**Compliance issues:**")
                for issue in result.compliance["issues"]:
                    st.markdown(f"- `{issue['rule']}` {md_safe(issue['detail'])}")
            if result.compliance.get("original_draft"):
                st.markdown("**Original draft (blocked/revised):**")
                st.code(result.compliance["original_draft"])
            if result.eligibility:
                st.markdown("**Engine output:**")
                st.json(result.eligibility)

if prompt := st.chat_input("e.g. I'm 32, a PR earning $7k a month — can I get a home loan for a $550k HDB flat?"):
    with st.chat_message("user"):
        st.markdown(md_safe(prompt))
    with st.chat_message("assistant"):
        with st.spinner("Routing → drafting → compliance review…"):
            n_messages = len(state.messages)
            try:
                result = run_turn(state, prompt)
            except Exception as exc:  # noqa: BLE001
                del state.messages[n_messages:]  # don't leave an unanswered user turn in the history
                st.error(f"Something went wrong: {exc}")
                st.stop()
        st.markdown(md_safe(result.response))
    # Store the redacted message, so an NRIC or card number typed in never stays in the session.
    st.session_state.turns.append((result.user_msg or prompt, result))
    st.rerun()
