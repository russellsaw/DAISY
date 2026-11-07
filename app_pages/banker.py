"""Banker view: the lead cards DAISY prepares, for bank staff rather than the customer."""

import streamlit as st

from src import recorder
from src.rules.counterfactual import fmt_date
from src.state import PROFILE_FIELDS
from src.ui import STATUS_WORDS, md_escape, md_safe, plain_value

URGENCY_COLOR = {"high": "red", "medium": "orange", "low": "gray"}

st.title("Banker view", icon=":material/support_agent:", anchor=False)
st.caption(
    "The summaries DAISY prepares when a customer asks for a banker, asks for advice, or has no clear way to "
    "meet today's criteria. This is a prototype: nothing here is sent to DBS."
)


@st.cache_data(ttl=10)
def load_leads(folder: str) -> list[dict]:
    return recorder.latest_leads(recorder.Path(folder))


# The model writes most of a card from the transcript, so anything the customer typed can reach it. Card text is shown
# literally (md_escape): no images loading from elsewhere, links or look-alike badges such as "Details confirmed".
def bullets(heading: str, items: list[str]) -> None:
    if items:
        st.markdown(f":gray[{heading}]\n\n" + "\n".join(f"- {md_escape(item)}" for item in items))


leads = load_leads(str(recorder.CONVERSATIONS_DIR))
if not leads:
    st.info("No summaries yet. Ask DAISY for a banker on the Assistant page.", icon=":material/info:")
    st.stop()

with st.container(horizontal=True):
    st.metric("Summaries", len(leads), border=True)
    st.metric("High priority", sum(lead["card"].get("urgency") == "high" for lead in leads), border=True)

# Keyed, with conversation ids as the options: otherwise a new summary (it re-sorts the list) makes Streamlit treat it
# as a new widget and jump back to the newest within the 10 s cache window.
by_id = {lead["id"]: lead for lead in leads}
labels = {
    lid: f"{x['started'].replace('T', ' ')}, {x['card'].get('urgency', '?')} priority: "
    f"{x['card'].get('customer_summary', '')[:70]} ({lid})"
    for lid, x in by_id.items()
}
lead = by_id[st.selectbox("Summary", list(by_id), format_func=lambda lid: labels.get(lid, lid), key="lead")]
card = lead["card"]
urgency = card.get("urgency") if card.get("urgency") in URGENCY_COLOR else "unknown"  # badge text: known words only

left, right = st.columns([3, 2])
with left:
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.badge(f"Priority: {urgency}", color=URGENCY_COLOR.get(urgency, "gray"))
            if card.get("parameters_confirmed_by_customer"):
                st.badge("Details confirmed by the customer", icon=":material/verified:", color="green")
            else:
                st.badge("Details not confirmed", icon=":material/help:", color="gray")
        st.markdown(md_escape(card.get("customer_summary")))
        st.markdown(f":gray[Suggested next step]  \n{md_escape(card.get('suggested_next_step'))}")
        st.caption(f"Why this was prepared: {md_escape(card.get('trigger', 'not recorded'))}.")

    bullets("Products discussed", card.get("products_discussed", []))
    bullets("Questions asked", card.get("questions_asked", []))
    bullets("Still open", card.get("open_questions", []))

    if card.get("paths") or card.get("no_clear_path_for"):
        st.markdown(":gray[What could change the result]")
        for path in card.get("paths", []):
            when = f"from {fmt_date(path['earliest_date'])}" if path.get("earliest_date") else "if this changes"
            st.markdown(f"- {md_escape(path['description'])}, *{when}*")
        for name in card.get("no_clear_path_for", []):
            st.markdown(f"- {md_escape(name)}: no clear way to meet today's criteria")
        st.caption(
            md_safe(
                f"Checked on {fmt_date(card['assessed_on'])} against criteria version "
                f"{card['criteria_version']}: {card['banker_note']}."
            )
        )

with right:
    st.subheader("Eligibility", anchor=False)
    for row in card.get("eligibility_snapshot") or []:
        status = STATUS_WORDS.get(row.get("status"), row.get("status"))
        st.markdown(f"**{md_escape(row.get('product'))}**  \n{md_escape(status)}")
        if row.get("note"):
            st.caption(md_escape(row["note"]))
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
    for key, value in profile.items():  # values the model extracted from the customer's words
        if key == "commitments":
            for c in value:
                ends = f"until {fmt_date(c['end_date'])}" if c.get("end_date") else "no end date given"
                st.markdown(
                    f":gray[{md_escape(str(c['kind']).capitalize())}]  \n"
                    + md_escape(f"S${c['monthly']:,.0f} a month, {ends}")
                )
        else:
            label = md_escape(PROFILE_FIELDS.get(key, (key,))[0])
            shown = f"{value:,.0f}" if isinstance(value, float) else plain_value(key, value)
            shown = ", ".join(map(str, shown)) if isinstance(shown, list) else shown
            st.markdown(f":gray[{label}]  \n{md_escape(shown)}")
    for c in card.get("readback_corrections") or []:
        st.caption(md_escape(f"Corrected by the customer: {c['field']}, {c['from']} to {c['to']}."))

st.caption(f"Replay this conversation on the Flight recorder page: {md_escape(lead['id'])}.")
with st.expander("Full lead card", icon=":material/data_object:"):
    st.json(card, expanded=False)
