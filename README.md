# DAISY — DBS AI, Straight with You

A prototype sales assistant for DBS home loans, credit cards and personal loans. It answers from DBS's own pages,
checks eligibility and works out the maximum home loan with a deterministic rules engine, shows customers who don't
qualify today what would change that, and prepares a summary for a banker. A compliance agent and code filters check replies before the customer
sees them, and an adversarial eval suite measures the result.

DAISY is a prototype built for the DBS brief. It isn't a live DBS service and isn't connected to any DBS system.

- [`PITCH.md`](PITCH.md): the proposition and the demo script.
- [`docs/ASSUMPTIONS_AND_LIMITATIONS.md`](docs/ASSUMPTIONS_AND_LIMITATIONS.md): every assumption, limitation and
  criterion.
- [`docs/GUIDELINES.md`](docs/GUIDELINES.md): DAISY against MAS, PDPA and DBS guidelines, requirement by requirement,
  including what's missing.

## Scope

**In scope:** home loans, credit cards, Personal Loan, Cashline and Balance Transfer.

**Investments and insurance are deliberately out of scope.** The brief's title mentions investments, but its project
description covers "mortgages, credit cards and unsecured loans", and we followed the description. These are the
products where the line between information and regulated financial advice is sharpest, so DAISY declines and points
to the DBS page or a banker. The excluded pages are listed under `OUT_OF_SCOPE` in `src/knowledge/sources.py`, so the
decision is visible.

## How DAISY meets the brief

| The brief asks for | Where it is |
|---|---|
| Accurate product Q&A from public DBS information | Product Knowledge agent over 74 snapshotted DBS, POSB, MAS and HDB pages (`data/sources/`), with citations; 20 golden Q&A cases, 14 checking a figure quoted from the page |
| Basic eligibility checks using rule-based logic | `src/rules/engine.py`: deterministic TDSR, MSR, LTV, tenure, age, income and unsecured-limit checks, and "how much can I borrow?" (`src/rules/max_loan.py`). Each criterion in `src/rules/criteria.py` carries its source URL and quoted evidence |
| Product discovery and comparison | `comparison` intent: side-by-side answers from the cited pages, needs-based shortlists without a recommendation, a discovery starter on the Assistant page, and an eligibility check offered as the next step (cases qa-004, qa-017, qa-020, qa-021) |
| A conversational chat interface | Streamlit app, **Assistant** page |
| Guardrails aligned to banking guidelines | PII redaction, input guardrail, compliance agent, code filters on numbers and approval wording, advice boundary with a banker summary. [`docs/GUIDELINES.md`](docs/GUIDELINES.md) maps these to each requirement, and is plain about the gaps: of 20 requirements, 2 are met in the prototype, 11 partly and 7 not, mostly bank-level and production controls |
| An evaluation framework | `evals/` (145 cases and an LLM judge, shown on the **Trust scorecard** page) and `tests/evals/` (DeepEval RAG metrics) |

## Run it

Tested on Python 3.14 with the exact versions in `requirements.txt`. You need an Anthropic API key. The knowledge base
(`data/kb.json`) and the latest eval results ship with the project, so nothing needs building first.

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # add your ANTHROPIC_API_KEY
pytest -q                                              # 237 offline tests, no API key needed
streamlit run app.py
```

| Page | For | What it shows |
|---|---|---|
| **Assistant** | Customer | The chat, the details DAISY has noted, and which checks applied to each reply |
| **Banker view** | Bank staff | The summaries DAISY prepares: who the customer is, what they asked, their results, what's still open |
| **Trust scorecard** | Bank staff | Eval results, including failures; the attacks and which checks acted |
| **Knowledge** | Bank staff | Every saved page, search as the assistant, the evidence for each rule, change history |
| **Flight recorder** | Bank staff | Replay any conversation event by event |

In this prototype all pages are open to anyone using the app. There's no login, and nothing is sent to DBS.

The buttons that spend API credit or overwrite data (*Run suite*, *Re-fetch all sources*) are hidden unless you set
`DAISY_STAFF_CONTROLS=1` in `.env`.

**Try this:** *"I'm 32, a PR earning $7k a month. Can I get a home loan for a $550k HDB flat? I'd borrow $380k over 25
years and have no other loans."* Reply yes to the readback, then ask what happens at $400k. Then start a new conversation
and ask *"How much can I borrow for a home?"*

## How it works

```
customer message
   │
   ▼
PII guard ─── strips NRIC/FIN and card numbers before any model sees the message
   │
   ▼
Input guardrail ─── code patterns + LLM classifier, in parallel with the orchestrator:
   │                block injection / prompt extraction / role-play / fake authority with a code-written reply;
   │                flag a distressed customer for a banker; otherwise allow
   ▼
Orchestrator ─── intent, profile facts, commitments, replies to a readback
   │
   ├─ product_qa / comparison ──▶ Product Knowledge agent: BM25 over the saved pages, cites [n]
   ├─ eligibility ──────────────▶ Eligibility agent:
   │                                1. details missing  → ask (no engine)
   │                                2. not confirmed    → code-written readback (no engine)
   │                                3. confirmed        → rules engine → counterfactual search
   │                                "how much can I borrow?" → same steps → largest loan the engine passes
   ├─ advice_seeking ───────────▶ No recommendation; cited facts + banker summary
   ├─ handoff_request ──────────▶ Handoff reply + banker summary
   └─ out_of_scope / chit_chat ─▶ short replies
   │
   ▼
Compliance agent (LLM) ─── approve / revise / handoff on advice boundary, grounding, approval language,
   │                        scope, fair dealing, prompt injection (code-written templates skip this step)
Code filters ─── untraceable numbers rewritten or replaced; on eligibility and advice replies, approval words too
Code-written lines ─── "not now, but when" outcomes and fixed notes, appended after review
   │
   ▼
reply + sources + which checks acted  →  chat       every event of the turn  →  flight recorder
```

Design decisions worth knowing:

- **Eligibility numbers come from the engine, not the model.** The model narrates what the engine computed. On every
  reply, a code filter checks that numbers trace to the engine, the customer or a retrieved page. It checks every
  percentage and amount of money; bare whole numbers below 32 are exempt, because they're usually list markers, ages or
  years. It checks that a number exists in those places, not that it's attached to the right fact, which is the
  compliance agent's job.
- **How much can I borrow?** `src/rules/max_loan.py` searches the same forward engine for the largest loan that passes,
  at both the MAS floor and DBS's planning rate, and the whole reply is written by code. At DBS's rate it matches DBS's
  planner to the dollar on the four test customers compared like for like.
- **Read back before running.** The engine runs only on details the customer has confirmed. Anything it would treat as
  none because the customer didn't mention it is shown in the readback, and repeated with the result.
- **Not now, but when.** When the engine says no, `src/rules/counterfactual.py` runs the same forward engine on changed
  profiles to find the smallest realistic change. Only a debt with a stated end date gets a date, computed in code from
  the customer's own words. Everything else is a labelled "what if". A change has to clear a failing ratio by 3 points
  to count as a path.
- **Information, not advice.** `advice_seeking` is its own intent. DAISY gives the facts and a banker summary, and never
  says which product to pick or where rates are going.
- **Honest handoff.** DAISY collects no contact details, so it doesn't promise a call. It prepares the summary and a
  code-written note says what happens next.
- **Keyword retrieval on purpose.** BM25 over 74 pages is fast and easy to debug, and needs no second API. Its limit is
  paraphrased questions: see the known limits below.
- **Everything is traceable.** Each reply records which of the five checks applied and acted (`src/defence.py`). The flight recorder
  (`src/recorder.py`) logs every turn to `data/conversations/`, with NRIC/FIN and card numbers removed.

## Results

| Measure | Result | Date |
|---|---|---|
| Eval suite (`evals/`) | **144/145 passed** on the final code. 20 attacks: 20 stopped, 0 got through. No ordinary question was blocked | 2026-11-02 |
| DeepEval RAG metrics (77 questions, Claude Sonnet as judge) | **76/77 passed.** Faithfulness 77/77 (mean 1.00), Answer Relevancy 77/77 (0.97), Contextual Recall 18/19 (0.95) | 2026-11-02 |
| Helpfulness (50 broad questions, rated by a reviewer model; not re-run since 2026-10-29) | good answers 27 → 39–40 after fixes, then 46 good, 2 partial, 2 knowledge gaps. Unsafe 0 | 2026-10-25, 2026-10-29 |
| Offline unit tests | 237 passed | 2026-11-02 |
| Engine vs DBS's calculators | Instalments match on 5 of 5 personas. At DBS's 4.65% the maximum loan matches DBS's planner to the dollar. The eligibility check uses the MAS 4% floor: see below | 2026-11-02 |

"Stopped" means the case passed and at least one check acted. It's weaker than "that check is what stopped it": a check
can act on an attack the model would have refused anyway.

The failures, left visible rather than changing a test or a threshold:

- **Eval suite, b13:** the judge read the next-step line after a comparison ("I can run an indicative check") as edging towards
  approval language. We kept the line: it promises nothing and offers the check the brief asks for.
- **DeepEval, gen-15:** a real miss. A question about the credit report showing total unsecured balances is routed as out of
  scope and declined, although a saved DBS page answers it.

DeepEval ran just before the last small changes: a routing clarification for "could I qualify?" questions, approval words
banned on advice replies, and the next-step line skipped when details are known.

Model-written replies vary between runs, so treat one or two eval cases either way as noise. See
[`docs/ASSUMPTIONS_AND_LIMITATIONS.md`](docs/ASSUMPTIONS_AND_LIMITATIONS.md) §13 for how the suite works and what it can't tell you.

```bash
python evals/run_evals.py --workers 4                  # the eval suite (calls the API)

pip install -r requirements-dev.txt                    # DeepEval, not needed to run the app
deepeval test run tests/evals/test_daisy.py --num-processes 4 --identifier my-run
python tests/evals/summarize.py
```

Both call the API. A DeepEval run takes about 9 minutes and costs about US$5 in judge calls, plus DAISY's own calls.
Plain `pytest` skips `tests/evals/`.

## Known limits

- **The check uses the MAS floor, and DBS's own tools are stricter.** The eligibility check uses the 4% floor MAS sets.
  DBS's planning tools currently assume 4.65%, so their maximum loan is lower: S$372,089 for the demo persona, who passes
  here at S$380,000. At the same rate the engine reproduces DBS's figures, so the maths isn't the difference. Every
  home-loan result says this, and "how much can I borrow?" shows both.
- **The 12× unsecured check is simpler than the MAS Notice.** The engine applies a plain cap; Notice 635's tests are
  narrower and differ in both directions ([`docs/GUIDELINES.md`](docs/GUIDELINES.md)).
  Details: [`docs/CALCULATOR_CHECK.md`](docs/CALCULATOR_CHECK.md).
- **Not a credit decision.** There's no credit bureau, documents or valuation. Results are indicative.
- **Engine simplifications** are listed in the assumptions doc, §5. For example: no joint borrowers, the 4% floor rather
  than a package's thereafter rate, guarantor obligations aren't counted, and "how much can I borrow?" is a ratio maximum,
  not what's affordable.
- **Retrieval misses some paraphrased questions.** "Can a foreigner buy an HDB flat?" doesn't reach the page that answers
  it, so DAISY says it doesn't have a clear answer.
- **Some DBS pages aren't captured** because they only render in a browser: home-loan fees and documents, and the card
  late-fee table.
- **PII:** phone numbers and emails aren't redacted. Logs are local files with no encryption or retention policy.
- **Latency:** about 10 s per turn at the median and 15 s at the 90th percentile in the last eval run. Several model calls run in sequence on each turn.
- **Criteria:** 20 of 21 were checked against the cited page on 2026-10-20 and carry the evidence. The HDB buyer rule was read from the live page, but the saved copy holds only menus, so it can't be re-read offline. The home-loan minimum age is unverified because no DBS page states it.
- **Not production-ready:** no authentication, rate limiting or monitoring.

## Refreshing the knowledge base

Optional. It re-crawls the sources and moves their "as of" dates.

```bash
python scripts/ingest.py                               # fetch and rebuild
python scripts/ingest.py --build-only                  # rebuild from the saved snapshots, offline
```

A failed fetch never wipes a good snapshot. A `changed` row in `data/sources/history.jsonl` means a page was edited, so
any rate, fee or criterion citing it needs re-checking. If a page blocks scripted fetches, save it from a browser into
`data/raw/` and describe it in `data/raw/sources.json`.

## Project layout

```
app.py                     entry point and navigation
app_pages/                 chat (Assistant), banker, evals (Trust scorecard), knowledge, replay (Flight recorder)
src/pipeline.py            run_turn(): one message in, one reviewed reply out
src/agents/                orchestrator, input guard, product knowledge, eligibility, compliance, handoff, prompts
src/rules/                 criteria (the numbers and their evidence), engine, counterfactual search, maximum loan
src/knowledge/             source list, ingest, BM25 retriever
src/pii.py, defence.py, recorder.py, commitments.py, state.py, llm.py, config.py
data/kb.json               the search index
data/sources/              manifest, plain-text snapshots of every page, change history
evals/                     cases.csv, the runner, the LLM judge, latest results
tests/                     offline unit tests; tests/evals/ is the DeepEval suite (calls the API)
scripts/                   ingest, eval case generator, broad-question review, calculator check
docs/                      assumptions and limitations, guidelines, calculator check and its evidence, broad-question
                           review, build log
```
