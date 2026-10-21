# DAISY — DBS AI, Straight with You

> Multi-agent chatbot for DBS home loans, credit cards and personal loans. The model never produces a
> number (a rules engine does), every response is reviewed by a compliance agent before the customer
> sees it, and an adversarial eval suite scores every build.

Hackathon MVP. See `PITCH.md` for the proposition and demo arc, and
**[`docs/ASSUMPTIONS_AND_LIMITATIONS.md`](docs/ASSUMPTIONS_AND_LIMITATIONS.md)** for every assumption,
limitation, criterion and design decision (the place to look when someone asks "why does it do that?").

## Architecture

```
customer message
   │
   ▼
PII guard ─── strips NRIC/FIN and card numbers before any model sees the message
   │
   ▼
Orchestrator ─── intent, profile facts, commitments (+ customer's words for end dates), readback replies
   │
   ├─ product_qa / comparison ──▶ Product Knowledge agent (BM25 over 66 DBS/POSB/MAS/HDB sources, cites [n])
   ├─ eligibility ──────────────▶ Eligibility agent:
   │                                1. details missing  → ask (no engine)
   │                                2. not confirmed    → code-written readback (no engine)
   │                                3. confirmed        → rules engine → counterfactual search ("not now, but when")
   ├─ advice_seeking ───────────▶ Boundary response + Handoff agent (lead card, built in parallel)
   ├─ handoff_request ──────────▶ Handoff reply + lead card
   └─ out_of_scope / chit_chat ─▶ short replies
   │
   ▼
Compliance agent (LLM) ─── approve / revise / handoff on advice boundary, grounding, approval language,
   │                        scope, fair dealing, prompt injection
Code filters ─── every number must trace to engine / customer / cited page; approval words banned on
   │             eligibility replies (one rewrite, else handoff)
Code-written outcome lines ─── "not now, but when" paths + the fixed outcome sentence, appended after review
   │
   ▼
response + sources + agent trace + defence-layer record  →  Streamlit chat
every event of the turn  →  flight-recorder log (data/conversations/)
```

```
app.py                     entry point: page config, logo, top navigation (st.navigation)
app_pages/chat.py          Assistant: chat, live agent timeline, profile side panel, lead card
app_pages/evals.py         Trust scorecard: pass rates, drill-down, run button, rules-verification status
app_pages/knowledge.py     Knowledge repository: documents, "search as the assistant", rule evidence, change history
app_pages/replay.py        Flight recorder: pick a conversation or eval case, step through every event
.streamlit/config.toml     "DBS 2030" theme (light + dark), static/ holds the DAISY logo
src/
  config.py                models, paths, settings (.env)
  llm.py                   Claude API wrapper; structured() uses structured outputs, effort per call,
                           server-side refusal fallbacks on Sonnet; a final refusal becomes a banker handoff
  state.py                 CustomerProfile + ConversationState
  pii.py                   strips NRIC/FIN and card numbers from every message before any agent sees it
  defence.py               Swiss-cheese record: which defence layer acted on each response
  recorder.py              flight recorder: per-conversation JSONL event log + readers
  commitments.py           turns a customer's stated end date into a date — in code, from their own words
  pipeline.py              run_turn(): orchestrator → agent → compliance
  agents/                  orchestrator, product_knowledge, eligibility, compliance, handoff, prompts
  rules/criteria.py        every eligibility number, each with verified flag, source URL and quoted evidence
  rules/engine.py          deterministic checks: age, income, TDSR, MSR, LTV, tenure, unsecured cap
  rules/counterfactual.py  "not now, but when": counterfactual search over the same engine
  knowledge/sources.py     allow-list: ~65 DBS/POSB/MAS/HDB pages and PDFs (+ explicit out-of-scope list)
  knowledge/ingest.py      fetch → snapshot (data/sources/) → strip boilerplate → chunk → data/kb.json
  knowledge/retriever.py   BM25 retrieval (no embeddings API needed)
evals/
  cases.csv                the suite: golden_qa, eligibility, boundary, adversarial, scope, handoff
  run_evals.py             runs cases through the live pipeline, writes results/latest.json
  judge.py                 LLM-as-judge for behavioural checks
tests/                     91 offline unit tests: engine, guards, counterfactual, readback, defence, recorder
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

## "Not now, but when" — counterfactual eligibility

When the engine says *not eligible*, `src/rules/counterfactual.py` searches for the smallest realistic
change that would clear today's criteria — by running the **same forward engine** on modified copies of
the profile (no inverse logic anywhere).

- **Dated levers** remove a commitment the customer stated *with* an end date. Only these carry a date,
  and it is exactly the stated end date. Dates are computed by code (`src/commitments.py`) and only from
  a verbatim quote of the customer that contains a time expression; "assume it's paid off" never dates.
- **What-if levers** (income higher, smaller loan, other tenure, undated debt cleared) are never dated.
- A lever is a **path** only if every ratio failing today clears by `CF_MARGIN` (3 points, `src/config.py`);
  otherwise it is **borderline**. Single levers first, pairs only if none works, never three. No path →
  banker handoff ("no clear path on today's criteria").
- The outcome lines and the fixed sentence are written by code and appended **after** compliance review;
  the LLM only explains the binding rule and the levers. Approval vocabulary is hard-banned in code, and
  every number in every reply must trace to the engine, the customer or a cited page (output filter).
- Every result carries `criteria_version`, so a criteria change visibly invalidates stored paths.

Scenario 2 note: for an HDB flat the binding rule is MSR, which counts only property loans, so a S$600
car loan can't block the demo persona's HDB loan (TDSR 37.2%). The dated-path demo therefore uses the same
person buying a S$1M condo with S$700k over 30 years: TDSR 56.3% today, 47.7% once the car loan ends.

## Swiss-cheese defence view

Every response records which of four defence layers acted on it (`src/defence.py`): **retrieval
grounding**, **rules engine**, **compliance agent**, **code filters** (PII in; numbers and wording out). The
chat shows the slices under each reply; the Trust scorecard's *Defence layers* tab shows, for every
adversarial eval case, the four slices with the ones that acted highlighted, and surfaces any **hole
alignment** (an attack that got through every layer) at the top, followed by attacks held only by the
model's own instructions. Definitions and the first run's findings are in
[`docs/ASSUMPTIONS_AND_LIMITATIONS.md`](docs/ASSUMPTIONS_AND_LIMITATIONS.md) §8.

## Flight recorder

Every turn is appended to `data/conversations/<conversation_id>.jsonl` (git-ignored; eval runs write
`data/conversations/evals/<run>/<case_id>.jsonl`): the customer's message, what the orchestrator extracted,
retrieval hits with URLs, the rules-engine input and output, **every counterfactual engine run**, the draft,
the compliance verdict and reasons, and the final reply, with timings. The **Flight recorder** page replays
any conversation (or eval case) event by event. Identifiers are redacted before agents see the message and
again on every log line. Details and limits: `docs/ASSUMPTIONS_AND_LIMITATIONS.md` §9.

## Demo persona

32-year-old PR, S$7,000/month, first HDB purchase at S$550k, S$380k home loan over 25 years.
Passes TDSR and MSR. (At S$400k the stressed instalment is S$2,111 — 30.2% of income — which fails
MSR by a hair. That's a good live moment: nudge the loan amount and watch the engine flip.)
