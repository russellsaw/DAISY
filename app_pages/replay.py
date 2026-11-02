"""Flight recorder: pick a conversation, step through its events, see what each agent saw and decided."""
import pandas as pd
import streamlit as st

from src import recorder
from src.ui import md_safe

ICONS = {
    "conversation_start": ":material/flag:", "user_turn": ":material/person:", "input_filter": ":material/shield_lock:",
    "routing": ":material/alt_route:", "readback": ":material/fact_check:", "retrieval": ":material/menu_book:",
    "engine": ":material/calculate:", "counterfactual": ":material/timeline:", "draft": ":material/edit_note:",
    "compliance": ":material/verified_user:", "handoff": ":material/support_agent:", "boundary": ":material/front_hand:",
    "defence": ":material/layers:", "final": ":material/chat:",
}

st.title("Flight recorder", icon=":material/flight:", anchor=False)
st.caption("Replay any conversation step by step: what the customer asked, what DAISY understood, which pages it "
           "used, what it calculated, what the review changed and the final reply. NRIC/FIN and card numbers are "
           "removed; other details, such as phone numbers and emails, aren't. Logs stay on this computer.")


@st.cache_data(ttl=10)
def conversations(folder: str) -> list[dict]:
    return [{**c, "path": str(c["path"])} for c in recorder.list_conversations(recorder.Path(folder))]


@st.cache_data(ttl=10)
def events_of(path: str, mtime: float) -> list[dict]:
    return recorder.load(recorder.Path(path))


source = st.segmented_control("Source", ["Live conversations", "Eval runs"], default="Live conversations")
folder = recorder.CONVERSATIONS_DIR
if source == "Eval runs":
    runs = recorder.eval_runs()
    if not runs:
        st.info("No eval run has been recorded yet. Run the suite from the Trust scorecard.", icon=":material/info:")
        st.stop()
    folder = st.selectbox("Eval run", runs, format_func=lambda p: p.name)

convs = conversations(str(folder))
if not convs:
    st.info("No conversations recorded yet. Chat with DAISY on the Assistant page.", icon=":material/info:")
    st.stop()

conv = st.selectbox(
    "Conversation", convs,
    format_func=lambda c: f"{c['started'].replace('T', ' ')}, {c['turns']} turn(s): {c['first_message'][:60]} "
                          f"({c['id']})")
path = recorder.Path(conv["path"])
events = events_of(str(path), path.stat().st_mtime)
turns = sorted({e["turn"] for e in events})

with st.container(horizontal=True, vertical_alignment="bottom"):
    turn = st.selectbox("Turn", turns, index=len(turns) - 1, format_func=lambda t: f"Turn {t}")
    mode = st.segmented_control("View", ["Step through", "Whole turn"], default="Step through")
turn_events = [e for e in events if e["turn"] == turn]


def render(e: dict) -> None:
    d = e.get("data") or {}
    kind = e["type"]
    if kind == "user_turn":
        with st.chat_message("user"):
            st.markdown(md_safe(d.get("message", "")))
        if d.get("pii_removed"):
            st.badge(f"PII removed before any agent saw it: {', '.join(d['pii_removed'])}", color="green",
                     icon=":material/shield_lock:")
    elif kind == "routing":
        with st.container(horizontal=True):
            st.badge(f"intent: {d.get('intent')}", color="blue")
            for prod in d.get("products") or []:
                st.badge(prod, color="gray")
            if d.get("readback_reply") and d["readback_reply"] != "not_a_reply":
                st.badge(f"readback reply: {d['readback_reply']}", color="orange")
            if d.get("injection_suspected"):
                st.badge("injection suspected", color="red", icon=":material/warning:")
        if d.get("reasoning"):
            st.caption(md_safe(d["reasoning"]))
        st.markdown("**Extracted from this message**")
        st.json({"profile_updates": d.get("profile_updates"), "commitments (as the model reported them)":
                 d.get("commitments"), "commitments (after code resolved dates)": d.get("commitments_resolved"),
                 "future / hypothetical (not stored)": d.get("future_or_hypothetical"),
                 "fields changed": d.get("profile_changed")}, expanded=True)
        with st.expander("Profile after this message", icon=":material/person:"):
            st.json(d.get("profile_after"))
    elif kind == "retrieval":
        st.markdown(f"**Searched for:** {md_safe(d.get('query', ''))}  \n"
                    f":gray[Product filter:] `{d.get('product_filter')}`")
        hits = d.get("hits") or []
        if hits:
            st.dataframe(pd.DataFrame(hits), hide_index=True,
                         column_config={"url": st.column_config.LinkColumn("Source"),
                                        "cited": st.column_config.CheckboxColumn("Cited")})
    elif kind == "engine":
        if isinstance(d, dict) and "results" in d:
            st.caption(f"Criteria version {d.get('criteria_version')}. Products: {', '.join(d.get('products') or [])}.")
            with st.expander("Input: the confirmed profile the engine ran on", icon=":material/input:"):
                st.json(d.get("input_profile"))
            for r in d["results"]:
                st.markdown(f"**{r['product_name']}** — `{r['status']}`")
                if r.get("checks"):
                    st.dataframe(pd.DataFrame(r["checks"]).astype(str), hide_index=True)
        else:
            st.json(d)
    elif kind == "counterfactual":
        for cf in d.get("counterfactuals", []):
            st.markdown(f"**{cf['product_name']}**: limiting rule `{cf['binding_rule']}` is {cf['current_value']} "
                        f"against {cf['threshold']} (gap {cf['gap']})  \n"
                        f":gray[Checked {cf['assessed_on']} against criteria version {cf['criteria_version']}.]")
            rows = [{"kind": x["lever_kind"], "label": x["label"], "change": x["description"],
                     "headroom": x["headroom"], "earliest_date": x["earliest_date"]}
                    for x in cf["paths"] + cf["borderline"]]
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True)
            else:
                st.warning("No path on today's criteria — banker handoff.", icon=":material/support_agent:")
        runs = d.get("runs") or []
        st.markdown(f"**Every engine run in the search ({len(runs)})**")
        if runs:
            st.dataframe(pd.DataFrame(runs).astype(str), hide_index=True, height=240)
    elif kind == "draft":
        with st.container(border=True):
            st.markdown(md_safe(d.get("text", "")))
    elif kind == "compliance":
        verdict = d.get("verdict")
        color = {"approve": "green", "revise": "orange", "handoff": "red"}.get(verdict, "gray")
        with st.container(horizontal=True):
            st.badge(f"verdict: {verdict}", color=color)
            st.badge(f"reviewer: {d.get('reviewer', 'llm')}", color="gray")
        for issue in d.get("issues") or []:
            st.markdown(f"- `{issue.get('rule')}` {md_safe(issue.get('detail', ''))}")
        st.json({k: d.get(k) for k in ("unsupported_numbers", "revision_unsupported_numbers", "output_filter", "lexicon")},
                expanded=False)
    elif kind == "defence":
        with st.container(horizontal=True):
            for key, layer in d.items():
                st.badge(f"{key}: {layer['status']}", color="green" if layer["acted"] else "gray")
        for key, layer in d.items():
            st.caption(f"{key}: {md_safe(layer['detail'])}")
    elif kind == "final":
        with st.chat_message("assistant", avatar="static/daisy-mark.svg"):
            st.markdown(md_safe(d.get("response", "")))
        st.caption(f"Took {d.get('elapsed_s')} seconds. Question type: {d.get('intent')}. Read back: "
                   f"{d.get('readback')}. Eligibility calculated: {d.get('engine_ran')}. Details confirmed: "
                   f"{d.get('confirmed')}."
                   + (f" Dated paths: {d['counterfactual_dates']}." if d.get("counterfactual_dates") else ""))
        if d.get("handoff_card"):
            with st.expander("Banker lead card", icon=":material/badge:"):
                st.json(d["handoff_card"])
    else:
        st.json(d)


def header(e: dict) -> str:
    return f"{e['agent']} at +{e['t_ms']:,} ms: {e['summary']}"


if mode == "Step through":
    key = f"step::{conv['id']}::{turn}"
    st.session_state.setdefault(key, 0)
    with st.container(horizontal=True, vertical_alignment="center"):
        if st.button("Previous", icon=":material/arrow_back:", disabled=st.session_state[key] == 0):
            st.session_state[key] -= 1
            st.rerun()
        if st.button("Next", icon=":material/arrow_forward:", disabled=st.session_state[key] >= len(turn_events) - 1):
            st.session_state[key] += 1
            st.rerun()
        st.caption(f"Event {st.session_state[key] + 1} of {len(turn_events)}")
    event = turn_events[min(st.session_state[key], len(turn_events) - 1)]
    with st.container(border=True):
        st.subheader(md_safe(f"{event['agent']}: {event['type'].replace('_', ' ')}"), anchor=False)
        st.caption(md_safe(header(event)))
        render(event)
    st.caption("Timeline: " + " → ".join(f"**{e['agent']}**" if i == st.session_state[key] else e["agent"]
                                          for i, e in enumerate(turn_events)))
else:
    for e in turn_events:
        with st.expander(md_safe(header(e)), type="step", icon=ICONS.get(e["type"])):
            render(e)

with st.expander("Raw log for this turn (JSON lines)", icon=":material/data_object:"):
    st.json(turn_events, expanded=False)
