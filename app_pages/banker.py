"""Banker view: the lead cards DAISY prepares, for bank staff rather than the customer."""

import streamlit as st

from src import recorder
from src.rules.counterfactual import fmt_date
from src.state import PROFILE_FIELDS
from src.ui import STATUS_WORDS, md_safe, plain_value

URGENCY_COLOR = {"high": "red", "medium": "orange"}

st.title("Banker view", icon=":material/support_agent:", anchor=False)
st.caption(
    "The summaries DAISY prepares when a customer asks for a banker, asks for advice, or has no clear way to "
    "meet today's criteria. This is a prototype: nothing here is sent to DBS."
)


@st.cache_data(ttl=10)
def load_leads(folder: str) -> list[dict]:
    return recorder.latest_leads(recorder.Path(folder))


def bullets(heading: str, items: list[str]) -> None:
    if items:
        st.markdown(f":gray[{heading}]\n\n" + "\n".join(f"- {md_safe(str(item))}" for item in items))


leads = load_leads(str(recorder.CONVERSATIONS_DIR))
if not leads:
    st.info("No summaries yet. Ask DAISY for a banker on the Assistant page.", icon=":material/info:")
    st.stop()

with st.container(horizontal=True):
    st.metric("Summaries", len(leads), border=True)
    st.metric("High priority", sum(lead["card"].get("urgency") == "high" for lead in leads), border=True)

lead = st.selectbox(
    "Summary",
    leads,
    format_func=lambda x: (
        f"{x['started'].replace('T', ' ')}, {x['card'].get('urgency', '?')} priority: "
        f"{x['card'].get('customer_summary', '')[:70]} ({x['id']})"
    ),
)
card = lead["card"]

left, right = st.columns([3, 2])
with left:
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.badge(
                f"Priority: {card.get('urgency', 'unknown')}", color=URGENCY_COLOR.get(card.get("urgency"), "gray")
            )
            if card.get("parameters_confirmed_by_customer"):
                st.badge("Details confirmed by the customer", icon=":material/verified:", color="green")
            else:
                st.badge("Details not confirmed", icon=":material/help:", color="gray")
        st.markdown(md_safe(card.get("customer_summary")))
        st.markdown(f":gray[Suggested next step]  \n{md_safe(card.get('suggested_next_step'))}")
        st.caption(md_safe(f"Why this was prepared: {card.get('trigger', 'not recorded')}."))

    bullets("Products discussed", card.get("products_discussed", []))
    bullets("Questions asked", card.get("questions_asked", []))
    bullets("Still open", card.get("open_questions", []))

    if card.get("paths") or card.get("no_clear_path_for"):
        st.markdown(":gray[What could change the result]")
        for path in card.get("paths", []):
            when = f"from {fmt_date(path['earliest_date'])}" if path.get("earliest_date") else "if this changes"
            st.markdown(f"- {md_safe(path['description'])}, *{when}*")
        for name in card.get("no_clear_path_for", []):
            st.markdown(f"- {md_safe(name)}: no clear way to meet today's criteria")
        st.caption(
            md_safe(
                f"Checked on {fmt_date(card['assessed_on'])} against criteria version "
                f"{card['criteria_version']}: {card['banker_note']}."
            )
        )

with right:
    st.subheader("Eligibility", anchor=False)
    for row in card.get("eligibility_snapshot") or []:
        st.markdown(f"**{md_safe(row.get('product'))}**  \n{STATUS_WORDS.get(row.get('status'), row.get('status'))}")
        if row.get("note"):
            st.caption(md_safe(row["note"]))
    if not card.get("eligibility_snapshot"):
        st.caption("No eligibility check ran in this conversation.")

    if card.get("max_loan"):
        loan = card["max_loan"]
        st.markdown(
            md_safe(
                f"**Maximum home loan over {loan['tenure_years']} years**  \n"
                f"S${loan['at_floor']:,.0f} at the MAS floor, S${loan['at_planner_rate']:,.0f} at DBS's "
                "planning rate"
            )
            if loan["at_floor"] and loan["at_planner_rate"]
            else "**Maximum home loan**  \nNo amount met today's criteria"
        )

    st.subheader("Customer details", anchor=False)
    profile = card.get("profile") or {}
    if not profile:
        st.caption("The customer didn't share any details.")
    for key, value in profile.items():
        if key == "commitments":
            for c in value:
                ends = f"until {fmt_date(c['end_date'])}" if c.get("end_date") else "no end date given"
                st.markdown(md_safe(f":gray[{c['kind'].capitalize()}]  \nS${c['monthly']:,.0f} a month, {ends}"))
        else:
            label = PROFILE_FIELDS.get(key, (key,))[0]
            st.markdown(
                md_safe(
                    f":gray[{label}]  \n{value:,.0f}"
                    if isinstance(value, float)
                    else f":gray[{label}]  \n{plain_value(key, value)}"
                )
            )
    for c in card.get("readback_corrections") or []:
        st.caption(md_safe(f"Corrected by the customer: {c['field']}, {c['from']} to {c['to']}."))

st.caption(md_safe(f"Replay this conversation on the Flight recorder page: {lead['id']}."))
with st.expander("Full lead card", icon=":material/data_object:"):
    st.json(card, expanded=False)
