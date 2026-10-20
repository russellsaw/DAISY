"""Evals page: scorecard, drill-down, rules verification status, live run."""
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

from src import config
from src.rules.criteria import unverified_count
from src.rules.engine import rules_summary
from src.ui import md_safe

st.title("Evaluation scorecard", icon=":material/fact_check:", anchor=False)
st.caption("Trust is measured, not asserted. Every build runs against this suite.")

latest = config.EVAL_RESULTS_DIR / "latest.json"

# ---------------- Run controls ----------------
col_run, col_cat, col_limit = st.columns([1, 2, 1])
with col_cat:
    cases = pd.read_csv(config.EVAL_CASES)
    categories = sorted(cases["category"].unique())
    selected = st.multiselect("Categories", categories, default=categories)
with col_limit:
    limit = st.number_input("Max cases", min_value=1, max_value=500, value=len(cases))
with col_run:
    st.write("")
    st.write("")
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
    st.info("No results yet. Run the suite above, or `python evals/run_evals.py` from the terminal.")
else:
    data = json.loads(latest.read_text())
    df = pd.DataFrame(data["results"])
    total = len(df)
    passed = int(df["passed"].sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Cases", total)
    m2.metric("Pass rate", f"{passed / total:.0%}" if total else "–")
    m3.metric("Failures", total - passed)
    m4.metric("Run at", data["run_at"][:16].replace("T", " "))

    st.subheader("By category")
    by_cat = df.groupby("category")["passed"].agg(["count", "sum"]).rename(columns={"count": "cases", "sum": "passed"})
    by_cat["pass_rate"] = by_cat["passed"] / by_cat["cases"]
    st.bar_chart(by_cat["pass_rate"])
    st.dataframe(by_cat.style.format({"pass_rate": "{:.0%}"}), width="stretch")

    st.subheader("Cases")
    show_failed_only = st.toggle("Failures only", value=False)
    view = df[~df["passed"]] if show_failed_only else df
    for _, row in view.iterrows():
        icon = "✅" if row["passed"] else "❌"
        with st.expander(f"{icon} [{row['category']}] {row['id']} — {row['input'][:90]}"):
            st.markdown(f"**Input:** {md_safe(row['input'])}")
            if row.get("profile"):
                st.markdown(f"**Pre-seeded profile:** `{row['profile']}`")
            st.markdown(f"**Response:**\n\n{md_safe(row['response'])}")
            st.markdown("**Checks:**")
            for check in row["checks"]:
                st.markdown(f"- {'✅' if check['passed'] else '❌'} `{check['name']}` {md_safe(str(check['detail']))}")
            if row.get("compliance_verdict"):
                st.caption(f"Compliance verdict: {row['compliance_verdict']} · intent: {row.get('intent')}")

    if len(data.get("history", [])) > 1:
        st.subheader("Pass rate across runs")
        hist = pd.DataFrame(data["history"]).set_index("run_at")
        st.line_chart(hist["pass_rate"])

# ---------------- Rules verification ----------------
st.divider()
unverified, total_rules = unverified_count()
st.subheader("Rules engine — criteria verification")
if unverified:
    st.warning(f"{unverified} of {total_rules} criteria are still UNVERIFIED against DBS/MAS sources. "
               f"Fix them in `src/rules/criteria.py` before demo day. Evidence for each is on the Knowledge page.")
else:
    st.success(f"All {total_rules} criteria verified.")
st.dataframe(pd.DataFrame(rules_summary()), hide_index=True)
