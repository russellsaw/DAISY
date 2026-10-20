# Level — a trust-first sales assistant for DBS retail lending

> Multi-agent chatbot for DBS home loans, credit cards and personal loans. The model never produces a
> number (a rules engine does), every response is reviewed by a compliance agent before the customer
> sees it, and an adversarial eval suite scores every build.

Hackathon MVP. See `PITCH.md` for the proposition and demo arc.

## Architecture

```
customer message
   │
   ▼
Orchestrator ─── classifies intent, extracts profile facts (age, income, residency…)
   │
   ├─ product_qa / comparison ──▶ Product Knowledge agent (BM25 RAG over DBS pages, cites [n])
   ├─ eligibility ──────────────▶ Eligibility agent ──▶ rules engine (deterministic, shows working)
   ├─ advice_seeking ───────────▶ Boundary response + Handoff agent (lead card)
   ├─ handoff_request ──────────▶ Handoff agent
   └─ out_of_scope / chit_chat ─▶ templated / short replies
   │
   ▼
Compliance agent ─── reviews the draft: advice boundary, grounding, approval language,
   │                 scope, fair dealing, prompt injection. Verdict: approve / revise / handoff.
   ▼
response + sources + agent trace  →  Streamlit chat
```

```
app.py                     entry point: page config, logo, top navigation (st.navigation)
app_pages/chat.py          Assistant: chat, live agent timeline, profile side panel, lead card
app_pages/evals.py         Trust scorecard: pass rates, drill-down, run button, rules-verification status
app_pages/knowledge.py     Knowledge repository: documents, "search as the assistant", rule evidence, change history
.streamlit/config.toml     "DBS 2030" theme (light + dark), static/ holds the Level logo
src/
  config.py                models, paths, settings (.env)
  llm.py                   Claude API wrapper; structured() uses structured outputs, effort per call,
                           server-side refusal fallbacks on Sonnet; a final refusal becomes a banker handoff
  state.py                 CustomerProfile + ConversationState
  pii.py                   strips NRIC/FIN and card numbers from every message before any agent sees it
  pipeline.py              run_turn(): orchestrator → agent → compliance
  agents/                  orchestrator, product_knowledge, eligibility, compliance, handoff, prompts
  rules/criteria.py        every eligibility number, each with verified flag, source URL and quoted evidence
  rules/engine.py          deterministic checks: age, income, TDSR, MSR, LTV, tenure, unsecured cap
  knowledge/sources.py     allow-list: ~65 DBS/POSB/MAS/HDB pages and PDFs (+ explicit out-of-scope list)
  knowledge/ingest.py      fetch → snapshot (data/sources/) → strip boilerplate → chunk → data/kb.json
  knowledge/retriever.py   BM25 retrieval (no embeddings API needed)
evals/
  cases.csv                the suite: golden_qa, eligibility, boundary, adversarial, scope, handoff
  run_evals.py             runs cases through the live pipeline, writes results/latest.json
  judge.py                 LLM-as-judge for behavioural checks
tests/test_engine.py       unit tests for the rules engine (no API calls)
scripts/ingest.py          build the knowledge base
scripts/write_starter_cases.py   regenerates the starter cases.csv
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # add your ANTHROPIC_API_KEY
python scripts/ingest.py                               # fetches the sources and builds data/kb.json
                                                       # (--build-only rebuilds from saved snapshots, offline)
pytest -q                                              # rules engine tests, no API needed
streamlit run app.py
```

If a page blocks scripted fetches, save it from your browser into `data/raw/` (HTML or PDF),
describe it in `data/raw/sources.json` (`{"file.pdf": {"url": "...", "title": "...", "publisher": "DBS",
"products": ["home_loan"]}}`), and re-run ingest.

## The knowledge repository

Everything the assistant can cite is kept in `data/sources/`, so any answer can be traced back to
the exact text it was grounded on:

| Path | What it holds |
|---|---|
| `manifest.json` | one entry per document: URL, publisher, products, status, characters, sha256, last fetch, last change |
| `text/<id>.md` | the extracted text the model reads, with a provenance header. Plain text — diff it in git |
| `raw/<id>.html\|pdf` | the bytes as fetched, for audit (git-ignored) |
| `history.jsonl` | every add / change / failed fetch / retirement, timestamped |

A failed fetch never wipes a good snapshot: the document is marked `stale` and its last good text
stays in the index. Re-run `python scripts/ingest.py` before demo day and check `history.jsonl`
(or the Knowledge page's *Change history* tab): a `changed` row means a DBS/MAS page was edited, and
any rate, fee or criterion citing it needs re-checking.

Things learned from the source audit (details in `src/knowledge/sources.py`):
- DBS landing pages are rendered by JavaScript, so a script only gets menus. The product detail
  pages (e.g. `/personal/loans/instant-cash/personal-loan`) are server-rendered and carry the criteria.
- DBS returns HTTP 200 for URLs that don't exist. Check `chars`/`status` in the manifest; `thin`
  means under 800 characters.
- The brief's combined Personal Loan / Card / Cashline T&Cs PDF link is dead (returns HTML).

Run the evals from the terminal (`python evals/run_evals.py`, parallel with `--workers`) or from the
Trust scorecard page.

## Before demo day — the checklist that matters

1. **Re-verify the criteria** in `src/rules/criteria.py`. 20 of 21 were checked against the cited
   MAS/HDB/DBS page on 2026-10-20 and carry the quoted evidence; the home-loan minimum age is still
   unverified (no DBS page states it). Re-run ingest first and re-check anything whose source changed.
2. **Validate the engine against DBS's own online calculators** for three or four personas. Record the
   comparison; it's a slide.
3. **Fill the `must_contain` fields** for the remaining `golden_qa` cases (qa-001, qa-007–010 are done).
4. **Grow the suite** to 150+ cases. Every failure you find during rehearsal becomes a case.
5. **Freeze the code 24h before.** Rehearse the live red-team with someone who hasn't seen the bot.

## Design decisions worth saying out loud

- **The LLM never produces a number.** Eligibility comes from `rules/engine.py`; the model narrates.
  The compliance agent additionally flags any number in a draft that doesn't trace to a source,
  the engine, or the customer's own words.
- **Information, not advice.** `advice_seeking` is a first-class intent. The assistant explains products
  and criteria; it never says which product to pick or where rates are going (FAA / Fair Dealing).
- **Scope is a compliance feature.** Investments, insurance and endowment are deliberately excluded —
  `knowledge/sources.py` lists them under `OUT_OF_SCOPE` so the decision is visible.
- **Keyword retrieval on purpose.** BM25 over a few dozen pages is fast, debuggable and needs no second
  API. Swap for embeddings only if recall is measurably a problem.
- **Trust is measured.** The eval suite has adversarial cases (hallucination bait, approval pressure,
  prompt injection, PII fishing, competitor disparagement). The scorecard shows failures, not just wins.

## Demo persona

32-year-old PR, S$7,000/month, first HDB purchase at S$550k, S$380k home loan over 25 years.
Passes TDSR and MSR. (At S$400k the stressed instalment is S$2,111 — 30.2% of income — which fails
MSR by a hair. That's a good live moment: nudge the loan amount and watch the engine flip.)
