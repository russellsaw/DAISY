"""Trust scorecard: pass rates, Swiss-cheese defence layers, case drill-down, criteria, live run."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from src import config, defence
from src.rules.criteria import unverified_count
from src.rules.engine import rules_summary
from src.ui import md_safe

st.title("Trust scorecard", icon=":material/fact_check:", anchor=False)
st.caption("How DAISY did in the last full run of our test conversations, including attempts to break it. The suite "
           "is run by hand, not automatically, and failures stay visible. Model-written replies vary, so a result can "
           "move by a case or two between runs.")

PASS_COLOR, FAIL_COLOR = "#0F8A5F", "#CC2929"  # green for a pass, red only for a failure

latest = config.EVAL_RESULTS_DIR / "latest.json"

# ---------------- Run controls ----------------
# Running the suite costs API credit, so the controls only show when DAISY_STAFF_CONTROLS is on.
if config.STAFF_CONTROLS:
    col_run, col_cat, col_limit = st.columns([1, 2, 1], vertical_alignment="bottom")
    with col_cat:
        cases = pd.read_csv(config.EVAL_CASES, dtype=str, keep_default_na=False)
        categories = sorted(cases["category"].unique())
        selected = st.multiselect("Categories", categories, default=categories)
    with col_limit:
        limit = st.number_input("Max cases", min_value=1, max_value=500, value=len(cases))
    with col_run:
        if st.button("Run suite", icon=":material/play_arrow:", type="primary", width="stretch"):
            cmd = [sys.executable, str(config.ROOT / "evals" / "run_evals.py"), "--limit", str(int(limit))]
            if selected and len(selected) < len(categories):
                cmd += ["--categories", ",".join(selected)]
            with st.status("Running evals…", expanded=True) as status:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        cwd=str(config.ROOT))
                log = st.empty()
                lines = []
                for line in proc.stdout:
                    lines.append(line.rstrip())
                    log.code("\n".join(lines[-20:]))
                proc.wait()
                status.update(label="Done" if proc.returncode == 0 else "Finished with errors", state="complete")
            st.rerun()

# ---------------- Results ----------------
if not latest.exists():
    st.info("No results yet. Run `python evals/run_evals.py` from the terminal.", icon=":material/info:")
    st.stop()

data = json.loads(latest.read_text())
df = pd.DataFrame(data["results"])
total = len(df)
passed = int(df["passed"].sum())

with st.container(horizontal=True):
    st.metric("Cases", total, border=True)
    st.metric("Pass rate", f"{passed / total:.0%}" if total else "–", border=True)
    st.metric("Failures", total - passed, border=True)
    st.metric("Last run", datetime.fromisoformat(data["run_at"]).strftime("%-d %b %Y, %H:%M"), border=True)

tab_rates, tab_layers, tab_cases, tab_rules = st.tabs(
    [":material/bar_chart: Pass rates", ":material/layers: Safety checks", ":material/list: Cases",
     ":material/gavel: Criteria"])

# ---------------- Pass rates (by category) ----------------
with tab_rates:
    by_cat = df.groupby("category")["passed"].agg(["count", "sum"]).rename(columns={"count": "cases", "sum": "passed"})
    by_cat["failed"] = by_cat["cases"] - by_cat["passed"]
    by_cat["pass_rate"] = by_cat["passed"] / by_cat["cases"]
    st.bar_chart(by_cat[["passed", "failed"]], color=[PASS_COLOR, FAIL_COLOR], y_label="Cases",
                 alt="Cases passed and failed in each test category")
    st.dataframe(by_cat.style.format({"pass_rate": "{:.0%}"}))
    if len(data.get("history", [])) > 1:
        st.subheader("Pass rate across runs", anchor=False)
        hist = pd.DataFrame(data["history"]).set_index("run_at")
        st.line_chart(hist["pass_rate"], color=PASS_COLOR, y_label="Pass rate",
                      alt="Pass rate of each test run over time")

# ---------------- Swiss cheese: which defence layer stopped each attack ----------------
with tab_layers:
    st.caption("Every reply passes five safety checks. The **input guardrail** removes personal details and blocks "
               "attempts to manipulate DAISY. **Retrieval grounding** checks the answer against DBS pages. The "
               "**rules engine** does every calculation. The **compliance agent** reviews each reply. **Code filters** "
               "remove numbers that can't be traced and, on eligibility replies, approval wording. A check is "
               "highlighted when it stepped in, which includes routine work such as holding the calculation until "
               "details are confirmed, so an attack marked stopped may also have been refused by the model anyway.")
    if "defence" not in df.columns:
        st.info("These results predate layer instrumentation. Run the suite again.", icon=":material/info:")
    else:
        include_all = st.toggle("Include non-adversarial cases", value=False)
        attacks = df if include_all else df[df["adversarial"].fillna(False).astype(bool)]
        order = {"hole": 0, "prompt_only": 1, "stopped": 2}
        attacks = attacks.assign(_o=attacks["defended"].map(order)).sort_values(["_o", "id"])
        holes = attacks[attacks["defended"] == "hole"]
        prompt_only = attacks[attacks["defended"] == "prompt_only"]

        with st.container(horizontal=True):
            st.metric("Attacks", len(attacks), border=True)
            st.metric("Stopped by a check", int((attacks["defended"] == "stopped").sum()), border=True)
            st.metric("Stopped by instructions only", len(prompt_only), border=True)
            st.metric("Got through every check", len(holes), border=True)

        if len(holes):
            st.error(f"{len(holes)} attack(s) got through every check and reached the customer: "
                     + ", ".join(holes["id"]), icon=":material/report:")
        else:
            st.success("No attack in this run got through every check.", icon=":material/verified:")
        if len(prompt_only):
            st.warning(f"{len(prompt_only)} attack(s) were stopped only by DAISY's own instructions, with no check "
                       "stepping in. That's acceptable, but nothing would catch it if it changed: "
                       + ", ".join(prompt_only["id"]), icon=":material/warning:")

        acted_counts = {label: int(attacks["defence"].map(lambda d, k=key: bool(d.get(k, {}).get("acted"))).sum())
                        for key, label in defence.LAYERS}
        st.subheader("Attacks where each check stepped in", anchor=False)
        st.bar_chart(pd.Series(acted_counts, name="attacks"), horizontal=True, color=PASS_COLOR,
                     alt="Number of attacks in which each safety check stepped in")

        verdict_badge = {"hole": ("Got through", "red", ":material/report:"),
                         "prompt_only": ("Stopped by instructions only", "orange", ":material/warning:"),
                         "stopped": ("Stopped", "green", ":material/shield:")}
        for _, row in attacks.iterrows():
            with st.container(border=True):
                label, color, icon = verdict_badge[row["defended"]]
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.badge(label, color=color, icon=icon)
                    st.markdown(f"**{row['id']}**: {md_safe(row['input'][:110])}")
                with st.container(horizontal=True):
                    for key, name in defence.LAYERS:
                        layer = row["defence"].get(key, {"status": "n/a", "acted": False})
                        if layer["acted"]:
                            st.badge(f"{name}: {layer['status']}", color="green", icon=":material/shield:")
                        elif row["defended"] == "hole" and layer["status"] != "n/a":
                            st.badge(f"{name}: {layer['status']}", color="red", icon=":material/close:")
                        else:
                            st.badge(f"{name}: {layer['status']}", color="gray")
                with st.expander("What each check did", icon=":material/info:"):
                    for key, name in defence.LAYERS:
                        layer = row["defence"].get(key, {})
                        st.markdown(f"- **{name}** ({layer.get('status', 'n/a')}): {md_safe(layer.get('detail', ''))}")
                    failed = [c for c in row["checks"] if not c["passed"]]
                    if failed:
                        st.markdown("**Failed checks:** " + "; ".join(f"`{c['name']}` {md_safe(str(c['detail']))}"
                                                                    for c in failed))
                    st.markdown(f"**Response:** {md_safe(row['response'][:600])}")

# ---------------- Case drill-down ----------------
with tab_cases:
    show_failed_only = st.toggle("Failures only", value=False)
    view = df[~df["passed"]] if show_failed_only else df
    for _, row in view.iterrows():
        icon = ":material/check_circle:" if row["passed"] else ":material/cancel:"
        with st.expander(f"[{row['category']}] {row['id']} — {row['input'][:90]}", icon=icon):
            st.markdown(f"**Input:** {md_safe(row['input'])}")
            if row.get("profile"):
                st.markdown(f"**Pre-seeded profile:** `{row['profile']}`")
            st.markdown(f"**Response:**\n\n{md_safe(row['response'])}")
            st.markdown("**Checks:**")
            for check in row["checks"]:
                mark = ":green[:material/check:]" if check["passed"] else ":red[:material/close:]"
                st.markdown(f"- {mark} `{check['name']}` {md_safe(str(check['detail']))}")
            if row.get("compliance_verdict"):
                st.caption(f"Compliance review: {row['compliance_verdict']}. Question type: {row.get('intent')}.")

# ---------------- Rules verification ----------------
with tab_rules:
    unverified, total_rules = unverified_count()
    if unverified:
        st.warning(f"{unverified} of {total_rules} criteria are not verified against a source. "
                   "Evidence for each is on the Knowledge page.", icon=":material/warning:")
    else:
        st.success(f"All {total_rules} criteria verified.", icon=":material/verified:")
    st.dataframe(pd.DataFrame(rules_summary()), hide_index=True,
                 column_config={"source": st.column_config.LinkColumn("Source"),
                                "evidence": st.column_config.TextColumn("Evidence (quoted)", width="large")})
