"""Knowledge page: every document the assistant can cite, what it retrieves, and the rule evidence."""
import json
import subprocess
import sys

import pandas as pd
import streamlit as st

from src import config
from src.knowledge import ingest, retriever
from src.rules.criteria import unverified_count
from src.rules.engine import rules_summary

st.title("Knowledge repository", icon=":material/library_books:", anchor=False)
st.caption("Every page DAISY can answer from, where it came from and when we last checked it.")


@st.cache_data(ttl=60)
def load_manifest(mtime: float) -> dict:
    return ingest.load_manifest()


@st.cache_data(ttl=60)
def load_history(mtime: float, limit: int = 200) -> list[dict]:
    lines = ingest.HISTORY_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in reversed(lines) if line.strip()]


def _mtime(path) -> float:
    return path.stat().st_mtime if path.exists() else 0.0


def run_ingest(args: list[str], label: str) -> None:
    cmd = [sys.executable, str(config.ROOT / "scripts" / "ingest.py"), *args]
    with st.status(label, expanded=True) as status:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(config.ROOT))
        log = st.empty()
        lines: list[str] = []
        for line in proc.stdout:
            lines.append(line.rstrip())
            log.code("\n".join(lines[-25:]))
        proc.wait()
        status.update(label="Done" if proc.returncode == 0 else "Finished with errors", state="complete")
    st.cache_data.clear()
    st.rerun()


manifest = load_manifest(_mtime(ingest.MANIFEST_PATH))
kb = retriever.load()

# ---------------- Summary + refresh controls ----------------
if not manifest:
    st.info("The repository is empty. Run `python scripts/ingest.py` to build it.", icon=":material/info:")
else:
    entries = list(manifest.values())
    live = [e for e in entries if e.get("status") != "retired"]
    problems = [e for e in live if e.get("status") in ("failed", "stale", "thin")]
    unverified, total_rules = unverified_count()
    with st.container(horizontal=True):
        st.metric("Documents", len(live), border=True)
        st.metric("Chunks indexed", len(kb.chunks), border=True)
        st.metric("Publishers", len({e["publisher"] for e in live}), border=True)
        st.metric("Need attention", len(problems), border=True)
        st.metric("Criteria verified", f"{total_rules - unverified}/{total_rules}", border=True)
        st.metric("Index built", (kb.meta.get("built_at") or kb.meta.get("built_on") or "–").replace("T", " ")[:16],
                  border=True)

# These overwrite the saved pages and index, so they only show when DAISY_STAFF_CONTROLS is on.
if config.STAFF_CONTROLS:
    with st.container(horizontal=True, vertical_alignment="bottom"):
        if st.button("Re-fetch all sources", icon=":material/cloud_download:", type="primary"):
            run_ingest([], "Fetching sources and rebuilding the index…")
        if st.button("Rebuild index from snapshots", icon=":material/build:"):
            run_ingest(["--build-only"], "Rebuilding the index (no network)…")

if not manifest:
    st.stop()

tab_docs, tab_search, tab_read, tab_rules, tab_history = st.tabs(
    ["Documents", "Search as the assistant", "Read a document", "Rule evidence", "Change history"])

# ---------------- Documents ----------------
with tab_docs:
    df = pd.DataFrame(entries)
    chunk_counts = {d["id"]: d.get("chunks", 0) for d in kb.meta.get("documents", [])}
    df["chunks"] = df["id"].map(chunk_counts).fillna(0).astype(int)
    df["products"] = df["products"].apply(lambda p: ", ".join(p or []))
    publishers = sorted(df["publisher"].unique())
    picked = st.pills("Publisher", publishers, selection_mode="multi", default=publishers)
    status_filter = st.segmented_control("Status", ["All", "Needs attention"], default="All")
    view = df[df["publisher"].isin(picked or publishers)]
    if status_filter == "Needs attention":
        view = view[view["status"].isin(["failed", "stale", "thin"])]
    st.dataframe(
        view[["status", "publisher", "title", "products", "kind", "chars", "chunks", "last_changed",
              "last_good_fetch", "url", "id", "error"]].sort_values(["publisher", "title"]),
        hide_index=True,
        column_config={
            "url": st.column_config.LinkColumn("Source", display_text="open"),
            "chars": st.column_config.NumberColumn("Characters", format="%d"),
            "last_changed": st.column_config.TextColumn("Content last changed"),
            "last_good_fetch": st.column_config.TextColumn("Last good fetch"),
        },
    )
    st.caption("**ok**: saved and searchable. **thin**: under 800 characters, usually a page that only loads in a "
               "browser. **stale**: the last check failed, so the previous copy is used. **failed**: never saved. "
               "**retired**: no longer used, kept for the record.")

# ---------------- Search ----------------
with tab_search:
    st.caption("Search the pages the same way DAISY does, to see exactly which passages it would answer from.")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        query = st.text_input("Customer question", placeholder="e.g. Is there an early repayment fee on a personal loan?")
        product = st.selectbox("Product filter", ["(none)", "home_loan", "credit_card", "personal_loan", "cashline"])
        k = st.number_input("Top k", min_value=1, max_value=20, value=config.RETRIEVAL_TOP_K)
    if query:
        hits = retriever.retrieve(query, k=int(k), product=None if product == "(none)" else product)
        if not hits:
            st.warning("Nothing matches. DAISY would say it doesn't have that detail.", icon=":material/search_off:")
        for i, hit in enumerate(hits, 1):
            with st.container(border=True):
                st.markdown(f"**[{i}] {hit['title']}**  \n:gray[{hit.get('publisher', '')}, score {hit['score']}.] "
                            f"[Open source]({hit['url']})")
                st.text(hit["text"])

# ---------------- Read ----------------
with tab_read:
    options = sorted(e["id"] for e in entries if (ingest.TEXT_DIR / f"{e['id']}.md").exists())
    doc = st.selectbox("Document", options, format_func=lambda i: f"{manifest[i]['title']}  ({i})")
    if doc:
        entry = manifest[doc]
        st.markdown(f"[{entry['url']}]({entry['url']})  \n:gray[Saved {entry.get('last_good_fetch', '–')}. "
                    f"{entry.get('chars', 0):,} characters. Fingerprint] `{(entry.get('sha256') or '')[:12]}`")
        st.text_area("Saved text (what DAISY reads)",
                     ingest.read_snapshot(doc), height=500, disabled=True)

# ---------------- Rules ----------------
with tab_rules:
    unverified, total_rules = unverified_count()
    if unverified:
        st.warning(f"{unverified} of {total_rules} criteria are not verified against a source.",
                   icon=":material/warning:")
    else:
        st.success(f"All {total_rules} criteria verified against a source.", icon=":material/verified:")
    st.dataframe(pd.DataFrame(rules_summary()), hide_index=True,
                 column_config={"source": st.column_config.LinkColumn("Source"),
                                "evidence": st.column_config.TextColumn("Evidence (quoted)", width="large")})

# ---------------- History ----------------
with tab_history:
    if ingest.HISTORY_PATH.exists():
        st.caption("One row each time a document is added, its text changes, a fetch fails, or a source is retired. "
                   "A `changed` row means the page was edited — re-check any rate, fee or criterion that cites it.")
        st.dataframe(pd.DataFrame(load_history(_mtime(ingest.HISTORY_PATH))), hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("Source")})
    else:
        st.info("No history yet.", icon=":material/history:")
