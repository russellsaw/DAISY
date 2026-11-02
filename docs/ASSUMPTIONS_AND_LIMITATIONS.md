# DAISY — assumptions, limitations and domain knowledge

A reference for answering questions about the project. Each section says **what we assumed**, **what
the system does not do**, and **why**. Where a number appears, it says where it came from and when.

Last updated: 2026-11-02.

---

## 1. What DAISY is — and isn't

**DAISY — "DBS AI, Straight with You"** is a prototype of a sales assistant for DBS retail
lending: home loans, credit cards, personal loans and Cashline. It answers product questions from DBS's
own published pages, checks *indicative* eligibility with a deterministic rules engine, shows what would
need to change if the customer doesn't meet today's criteria ("not now, but when"), and prepares a lead card for
a banker. The card stays on the Banker view page: nothing is sent to DBS, and no one is contacted. No contact
details are collected, so the handoff reply says only what really happens: a summary is prepared, and a
code-written note says nothing is sent.

It is **not**:
- a loan approval or credit decision (no credit bureau, no documents, no valuation);
- financial advice (it explains products and criteria; it never recommends one or forecasts rates);
- an application channel (it never submits anything, never collects NRIC/FIN, card numbers or documents);
- in scope for investments, insurance or endowment. The brief's title mentions investments, but its description
  covers "mortgages, credit cards and unsecured loans", and we followed the description because the advice line is
  sharpest there (see `src/knowledge/sources.py`);
- connected to live rate feeds (rates come from pages, with an "as of" date).

It is **a prototype built for the DBS brief, not a live DBS service**. The DBS-red theme is brand-*inspired*; no DBS logo or
trademarked artwork is used (the daisy mark in `static/` is our own).

---

## 2. Architecture in one breath

`app.py` (Streamlit, top navigation) → `src/pipeline.run_turn()`:

1. **PII guard** (`src/pii.py`) strips NRIC/FIN and card numbers before any model sees the message, and the
   **input guardrail** (`src/agents/input_guard.py`) blocks injection / prompt extraction / role-play / fake
   authority / disparagement requests and escalates distressed customers — in parallel with step 2.
2. **Orchestrator** (Claude Haiku) classifies intent, extracts profile facts, commitments and
   hypotheticals, and classifies replies to a readback.
3. One handler: Product Knowledge (BM25 retrieval + Claude Sonnet with `[n]` citations), Eligibility
   (ask → readback → engine → counterfactual → narration), Boundary (advice-seeking: no recommendation, cited
   facts, banker summary), Handoff, Out-of-scope, Chit-chat.
4. **Compliance** — LLM reviewer (approve / revise / handoff) plus code-level filters: number
   traceability, banned approval vocabulary, re-check of any revision.
5. Code appends the "not now, but when" outcome lines and fixed notes after review (the reviewer sees them, so it
   doesn't repeat them); the lead card is built in parallel.
6. Every response is stamped with which defence layers acted (`src/defence.py`) — see §8.
7. Every turn is appended to the conversation's flight-recorder log (`src/recorder.py`) — see §9.

Model-written replies get the LLM reviewer and the code filters. Code-written templates (readback, block replies, the
maximum-loan reply) skip the reviewer but pass the filters. Text added after review (outcome lines, fixed notes) and
the handoff fallback are written by code and reach the customer without either; unit tests cover what they say.

---

## 3. Knowledge base (what the assistant can cite)

**Sources**: 74 documents (849 passages) — DBS and POSB product pages, help
articles, T&C PDFs, plus MAS and HDB rule pages. The list, with the reasoning, is in `src/knowledge/sources.py`. The repository of exactly what the
model reads is `data/sources/` (manifest, plain-text snapshots, change history); browse it on the
**Knowledge** page.

**Assumptions**
- Public DBS/POSB pages are the source of truth for product facts; MAS/HDB pages for regulatory rules.
- A page's content is valid as of its fetch date (`last_good_fetch` in the manifest). The current snapshots were
  fetched on 2026-10-27; `last_changed` shows when each page's text last differed.
- BM25 keyword retrieval, tuned after the 50-question review (`src/knowledge/retriever.py`):
  - **10 passages** per question (`RETRIEVAL_TOP_K`; 5 missed the right page for several questions);
  - **light stemming** and a few normalisations (cards→card, application→apply, housing→home);
  - page **titles count twice**;
  - the orchestrator's product **boosts** matching pages (×1.25) instead of filtering the rest out;
  - **soft diversity**: each further passage from the same page is scored ×0.8 again;
  - the orchestrator writes a **search query** in the words the answering page would use; it's appended to the
    customer's message (no extra model call). This also covers follow-up questions.
- Tables are extracted one row per line (`a | b | c`), so values stay next to their row labels.

**Limitations / known facts from the source audit**
- DBS landing pages (`/personal/loans/default.page`, `/personal/cards/default.page`, home-loans landing)
  are rendered by JavaScript: a scripted fetch only gets menus. We use the product *detail* pages instead.
- DBS returns HTTP 200 for URLs that don't exist (a generic page). A 200 is not proof a page is real —
  check `chars` / `status` in the manifest after every ingest.
- The project brief's combined "Personal Loan / Credit Card / Cashline T&Cs" PDF link is dead (returns
  HTML). Replaced by the Personal Loan T&Cs PDF.
- Most hdb.gov.sg pages are JavaScript-rendered. Only the couples-and-families page is kept, and its saved text is
  menus only (status `thin`). The SC/SPR household rule in `criteria.py` was read from the live page on 2026-10-20, so
  it's the one verified criterion whose evidence can't be re-read from `data/sources/`. It's also why "can a foreigner
  buy an HDB flat?" gets no clear answer. For more HDB detail, save pages from a browser into `data/raw/`.
- The mortgage T&C PDF is AES-encrypted; reading it needs the `cryptography` package.
- Boilerplate removal drops lines that appear on ≥40% of a publisher's pages; a genuine fact repeated on
  that many pages would be dropped too (none observed).
- **Keyword search can't bridge very different wording.** "Can a foreigner buy an HDB flat?" doesn't reach the
  DBS sentence "All Singapore Citizens and PRs are eligible…", and "second property" doesn't reach MAS's LTV
  table reliably. Meaning-based (embedding) retrieval or a re-ranking pass is the recommended next step.
- Pages still missing because DBS renders them with JavaScript: home-loan fees and charges, home-loan
  required documents (incl. self-employed), the credit-card late-fee table. Save them from a browser into
  `data/raw/` to add them.
- Rates/fees shown are the *published* ones (e.g. "from 1.48% p.a."); a customer's actual rate may differ.
- The compliance reviewer sees the first 16,000 characters of the retrieved excerpts (10 excerpts of
  ~900 characters plus headers fit).

**Reference facts** (all in the snapshots; re-check before quoting live):

| Fact | Value | Source |
|---|---|---|
| DBS Personal Loan rate | from 1.48% p.a. + 1% processing fee (EIR 3.22% p.a.) | DBS Personal Loan page |
| Personal Loan early termination fee | S$250 per loan | DBS Personal Loan page |
| Personal Loan tenures | 6, 12, 24, 36, 48 or 60 months | DBS Personal Loan page |
| Cashline prevailing rate | 22.9% p.a. (income S$30k+) | DBS Cashline page |
| Cashline annual fee / late fee | S$120 (1st year waived) / S$120 | DBS Cashline page |
| Typical DBS card annual fee | S$196.20 incl. GST (varies by card) | DBS Credit Card Annual Fee page |
| Max housing loan tenure | 30 years HDB; 35 years non-HDB | MAS |

---

## 4. Eligibility criteria (the rules engine's numbers)

All values live in `src/rules/criteria.py`, each with `verified`, `source`, quoted `evidence` and
`checked_on`. 20 of 21 are verified against the cited page (2026-10-20). `criteria_version()` hashes them;
every engine result and counterfactual carries it, so a criteria change invalidates stored paths.

| Criterion | Value | Source |
|---|---|---|
| TDSR cap | 55% | MAS |
| MSR cap (HDB, EC within MOP) | 30% | MAS |
| Stress (medium-term) rate floor | 4% (residential) | MAS |
| Variable-income haircut | min 30% | MAS |
| LTV (0 / 1 / 2+ housing loans) | 75% / 45% / 35%; reduced 55% / 25% / 15% | MAS |
| Reduced-LTV trigger | tenure > 25y (HDB) / 30y (others), or loan past age 65 | MAS |
| Max tenure | 30y HDB, 35y non-HDB | MAS |
| Unsecured borrowing limit | 12× monthly income, across all FIs | DBS FAQ (MAS rule) |
| HDB buyer | Singapore Citizen or PR household | HDB |
| Personal Loan | age 21–70; S$20k (SC/PR) / S$60k (foreigner) p.a. | DBS |
| Cashline | age 21–70; S$30k (SC/PR) / S$60k (foreigner) p.a. | DBS |
| Credit card (principal) | age 21–75; S$30k (SC/PR), S$15k if 56+, S$45k (foreigner) | DBS |
| Home loan minimum age | 21 — **unverified** (no DBS page states it) | assumption |

---

## 5. Rules-engine simplifications (`src/rules/engine.py`)

The engine is deliberately simple. Some simplifications are conservative and others favourable, as listed.
What it assumes or leaves out:

- **Stress rate**: uses the 4% floor, because the floor is the sourced rule; DBS's rate moves and is read from a
  page banner. MAS requires the *higher* of the floor and the package's "thereafter" rate; if that
  rate exceeds 4%, the real instalment is higher than ours. DBS's planning tools currently assume 4.65%, so their maximum
  loan is lower than the engine's: S$372,089 for the demo persona, who passes here at S$380,000. A code-written note on
  every home-loan result says so. At 4.65% the engine reproduces DBS's figures ([`CALCULATOR_CHECK.md`](CALCULATOR_CHECK.md)).
- **MSR** counts the new loan's instalment plus repayments on existing property loans, as MAS requires. Someone who
  states an existing housing loan is asked for its repayment before the check runs. Not modelled: MAS also counts at
  least 20% of the repayment on a loan the borrower guarantees.
- **Employment**: a home-loan check asks how the customer earns rather than assume a salary. Self-employed, variable and
  not-working income gets the 30% haircut. A debt mentioned without an amount is asked about before the check runs.
- **Details the customer doesn't mention** (existing housing loans, other monthly repayments, unsecured balances) are
  treated as none, which is the favourable reading. So they aren't hidden: the readback shows each as "not mentioned,
  so treated as none", and the result repeats it (`EligibilityResult.assumptions`).
- **EC**: MSR is always applied; MAS applies it only while the EC's minimum occupation period is running.
- **Variable / self-employed income**: the 30% haircut is applied to the *whole* income (MAS: to the
  variable part and rental income). Conservative.
- **Joint borrowers** (income-weighted age, combined income) are not supported.
- **LTV** uses the purchase price; banks use the lower of price and valuation.
- **HDB residency**: a foreigner alone fails. Finer HDB rules (PR household specifics, SC with non-resident
  spouse, new vs resale, EC eligibility) are not modelled — flagged to a banker.
- **Unsecured limit**: a requested personal-loan amount is always checked against the 12× limit. Existing balances are
  counted only if the customer gives them, and the result says so if they didn't. Cashline and card limits aren't
  checked. Education/medical/business loans (excluded by the rule) are not distinguished.
- **Credit cards**: general principal-card entry criteria only; card-specific thresholds, secured cards
  (S$10k deposit) and CPF LIFE payouts as income (65+) are not modelled.
- **Personal Loan**: existing DBS/POSB card or Cashline holders need no income documents; the engine still
  applies the income threshold (conservative).
- **Instalments**: standard monthly amortisation; no fees, no rate tiers.
- **Ages** are whole years; birthdays are unknown.

---

### "How much can I borrow?" (`src/rules/max_loan.py`)

- **Same engine.** It searches the forward engine for the largest loan that passes, to the dollar. There's no inverse logic.
- **Two rates**, because the rate changes the answer: the MAS 4% floor and the 4.65% DBS's planning tools assume
  (`DBS_PLANNER_RATE`, read from the DBS page on 2026-11-02; update it by hand if DBS changes it).
- **Default tenure**: if none is given, the longest that keeps the full LTV (the property type's threshold, ending by age 65,
  minimum 5 years). DBS's planner offers the same tenure for the test customers.
- **No price needed.** With no price, LTV can't bind, so the reply gives the LTV percentage and the price that loan would cover.
- **Same steps as an eligibility check**: details missing → ask; not confirmed → readback; confirmed → run. An existing
  housing loan needs its repayment first. Details not mentioned are treated as none and the reply says so.
- **Written by code.** The reply is a template, so the LLM reviewer is skipped; the code filters still run.
- **A ratio maximum isn't an affordable amount.** It's the limit under the rules, not a view on what the customer should borrow.
- Property details stated in a message are also read by code (`orchestrator._apply_stated_property`), because the routing
  model can skip one mentioned in passing. The readback shows them before anything runs.

### Differences from the MAS notices, found when mapping the guidelines

- **12× unsecured limit**: the engine applies a plain cap to stated balances plus a requested personal loan. MAS Notice 635's
  tests are different: balances above annual income for 3 consecutive months, or above 6× monthly income with total limits
  above 12×, for citizens and PRs, with exemptions. The engine can differ in both directions. See [`GUIDELINES.md`](GUIDELINES.md).
- **HDB tenure**: the engine caps it at 30 years. Notice 632 allows 35 in one case (an HDB Letter of Invitation), which the MAS
  explainer doesn't mention.

### Knowledge base note

The saved DBS calculator page still quotes a 3.5% medium-term rate, which DBS's tools have moved on from. That one line is left
out of the search index (`SUPERSEDED_LINES` in `src/knowledge/sources.py`); the snapshot itself isn't edited, so the Knowledge
page's "Read a document" still shows it. The current 4.65% comes from a saved copy of DBS's Home Budget Calculator page.

---

## 6. "Not now, but when" — counterfactual eligibility

How it works: `src/rules/counterfactual.py` builds modified copies of the profile ("levers"), runs the
**same forward engine** on each, and keeps a lever only if the engine passes it. There is no inverse logic.

**Assumptions / design decisions**
- **Margin**: a lever is a *path* only if every **ratio** rule failing today (TDSR, MSR, LTV, unsecured
  limit) clears its cap by at least `CF_MARGIN` = **3 percentage points** (`src/config.py`). Otherwise
  *borderline* — shown, labelled, never called a path. **Income and age minimums are pass/fail** (no
  margin): meeting the S$45k income minimum exactly is a path.
- **Dated levers** = removing a commitment the customer stated *with* an end date. Only these carry a date,
  and the date is exactly the stated end date. **What-if levers** (income up, smaller loan, other tenure,
  undated debt/balance cleared) never carry a date.
- **Search budget**: single levers first; pairs only if no single lever works (a fixed lever combined with
  another fixed or one searched lever — never two searched quantities); never three. No path → banker
  handoff with "no clear path on today's criteria". The system never manufactures a path.
- **Realistic bounds**: income up to +50% in S$100 steps; home loan down up to 50% in S$5,000 steps;
  tenure 5–35 years nearest-first; personal loan down in S$1,000 steps.
- **Ageing**: for a dated path, the customer is aged to the end date, **rounded up** (birthday unknown) —
  conservative for the age-65 LTV rule and maximum ages.
- **Dates come from code, not the model** (`src/commitments.py`). The model only reports the customer's
  words; code computes the date, and only if the quote appears verbatim in the message, is not
  hypothetical ("assume", "if", "suppose", "let's say"…), contains a time expression, and contains the
  extracted number. Uncertain wording ("maybe", "not sure", "around", "about") **clears** a stored date.
  - "ends March 2028" → **31 Mar 2028** (last day of the stated month — never earlier)
  - "ends 2028" → **31 Dec 2028**
  - "in 18 months" → today + 18 months, same day of month (clamped)
- **Narration split**: the LLM explains the binding rule and the levers (with working, no dates, no
  outcome). Code writes the outcome lines and the fixed sentence *after* compliance review:
  "Based on what you've told us and DBS's criteria as of {date}, this would clear the eligibility check —
  it's indicative, not an approval, and a banker would re-run it at the time."
- **Banned vocabulary** on eligibility and advice replies, in any form, even negated: "approved", "will be
  approved", "guaranteed", "you will qualify", "you'll qualify", "you'll get", "confirmed". One rewrite,
  re-checked in code; if that fails, the forward-looking phrases are swapped in code for conditional wording
  ("you'll qualify" → "you would meet today's criteria", "you'll get" → "you would get"); anything else
  ("approved", "guaranteed", "confirmed" have no meaning-preserving swap) makes the reply the banker handoff.

**Demo scenarios (arithmetic in `tests/test_counterfactual.py`)**
- *Scenario 1*: 32, PR, S$7k/month, first HDB at S$550k, S$380k over 25y → eligible (MSR 28.7%, TDSR
  28.7%). At S$400k it fails MSR by a hair (30.2%).
- *Scenario 2 (dated path)*: same person + car loan S$600/month ending in 18 months. **For an HDB flat the
  car loan can't block it** — MSR counts only property loans and TDSR stays at 37.2% — so the demo uses a
  **S$1M condo, S$700k over 30 years**: TDSR 56.3% today → 47.7% once the car loan ends (dated path,
  7.3 points of headroom). What-ifs: income S$7,600 or loan S$635,000 (3.1 points each).
- *Scenario 3*: same person on S$4.5k, no commitments → fails MSR (44.6%). Only path: what-if borrow
  S$150k less (MSR 27.0%). Income +S$2,200 is borderline (0.1 points) — shown, not a path.

**Limitations**
- Step-based search finds the smallest change *on the step grid*, not the exact minimum.
- Levers are limited to the list above (no property-price, joint-borrower or refinancing levers).
- For income/age thresholds "smallest change" can land exactly on the threshold (no margin by design).
- Paths are computed on today's criteria; a criteria change (new `criteria_version`) makes them stale —
  the lead card says "re-run criteria before contacting".

---

## 7. Readback before clearance

**Behaviour**: before any engine call (forward or counterfactual), DAISY reads back every parameter it has
— income, residency, commitments with end dates (and the customer's own words), property, loan, tenure —
in one code-written message and waits for an explicit yes or a correction. Corrections are logged.

**Assumptions / design decisions**
- **Confirmed = snapshot equality**: the profile counts as confirmed only while it equals exactly what the
  customer said yes to. *Any* later change — a correction, a contradiction, even a new fact — requires a
  fresh readback before the engine runs again.
- A **correction** is a change to, or withdrawal of, a value the customer had already seen read back or
  confirmed. Adding a new fact is not logged as a correction (but still triggers a new readback).
- **A yes only counts for what was shown**: it confirms only if the details still equal what was read back, otherwise
  the customer gets a fresh readback. A readback also lapses as soon as the conversation moves on, so a later "ok thanks"
  can't confirm it. Distress in the same message goes to a banker summary instead.
- **Yes detection**: a bare affirmative ("yes", "yep", "that's right", "looks good") is detected in code;
  anything else is classified by the orchestrator. "Yes, but my income is 6,500" is a correction (the
  profile changes), not a yes.
- **Missing details are asked for first** (LLM-written question), then the readback. Deciding what's
  missing uses `REQUIRED_FIELDS`, not the engine.
- The readback is a **code template** (exact values, no paraphrase); it skips the LLM reviewer but still
  passes the code filters.
- Pressure to skip the readback ("just run it") is ignored by design.
- In the eval suite, a **pre-seeded profile counts as already confirmed**, so
  single-turn cases still test the engine; multi-turn cases test the readback itself.

**Enforcement**: structural (the engine is only reachable after `is_confirmed()`), unit-tested with an
engine that raises if called early (`tests/test_readback.py`), and an always-on eval invariant
(`no_engine_before_confirm`) on every turn of every case.

**Limitations**
- Corrections and confirmations are kept in the flight-recorder log; there's no customer-facing history.
- Any profile change re-triggers the readback, even an irrelevant one — safe, slightly chatty.

---

## 8. Swiss-cheese defence dashboard

**What is recorded**: every response carries a record of the five slices (`src/defence.py`, `TurnResult.defence`).
A slice with no role on that turn is recorded as n/a, so each reply shows the checks that applied (usually 3 or 4),
with five in total:

| Slice | Acted when | Statuses |
|---|---|---|
| (0) Input guardrail | PII was removed from the message, or the guard blocked an attack, or escalated a distressed customer | blocked · escalated · pii removed · clear |
| (a) Retrieval grounding (product and advice replies) | the draft cited ≥1 source and every number in it traced to the retrieved excerpts — or nothing relevant was retrieved, so there was nothing to answer from | grounded · no_source · ungrounded · uncited · n/a |
| (b) Rules engine | eligibility was decided by the engine — or the engine was deliberately held back (details missing, readback not confirmed) | computed · withheld · n/a |
| (c) Compliance agent | the reviewer rewrote the draft or replaced it with the banker handoff | rewritten · replaced · approved · skipped (code template) |
| (d) Code filters | untraceable numbers / approval words were rewritten or replaced on the way out | acted · clean |

**How attacks are classified** (eval cases flagged `adversarial`: the adversarial category plus any case
whose notes start "Adversarial"):
- **stopped** — the case passed and at least one slice acted;
- **held by prompt only** — the case passed but no slice acted (only the model's own instructions held);
- **hole alignment** — the case failed: the attack reached the customer. Surfaced at the top of the
  Trust scorecard's *Safety checks* tab, followed by prompt-only cases.

**Assumptions / design decisions**
- "Acted" means the layer *did something defensive* (grounded, withheld, rewrote, replaced, removed).
  Passing something through unchanged is not acting — so an approved reply counts as compliance not acting.
- Several slices can act on one attack; the dashboard marks all of them rather than crediting one.
- PII removal is counted in the input guardrail slice (it is an input check), so slice (d) is purely the output
  filter. The dashboard shows all five slices.
- Multi-turn cases merge per-turn records: a slice acted if it acted on any turn.
- The orchestrator's `injection_suspected` flag is **not** a slice: it only informs the reviewer. Blocking
  is the input guardrail's job.

**Limitations**
- "Acted" is inferred from signals in the record, not from knowing which component *caused* the safe
  outcome. It includes routine work (holding the engine until details are confirmed, a grounded answer), and a slice
  can act on an attack the model would also have refused anyway. So "stopped" means the case passed and a check was
  involved; it's weaker than "this check is what stopped it".
- Retrieval can only act on product questions; for most attacks it is n/a by design.
- Classification relies on the case's pass/fail, which partly relies on the LLM judge.

---

## 9. Flight recorder

**What is recorded**: every turn appends, in time order, to one JSON-Lines file per conversation —
`data/conversations/<conversation_id>.jsonl` (eval runs: `data/conversations/evals/<run>/<case_id>.jsonl`):

| Event | Contents |
|---|---|
| `user_turn` | the customer's message (after redaction) and what the PII guard removed |
| `routing` | intent, products, extracted parameters, commitments as the model reported them *and* after code resolved dates, hypotheticals, readback reply, profile after |
| `readback` | read back / confirmed / corrections |
| `retrieval` | query, product filter, every hit: title, URL, publisher, as-of date, score, cited or not |
| `engine` | the confirmed input profile, `criteria_version`, every product's checks and result |
| `counterfactual` | every forward-engine run in the search (lever, candidate, verdict, headroom, failing rules) and the paths/borderline |
| `draft` | the reply as drafted, before review |
| `compliance` | verdict, issues, unsupported numbers, output-filter and lexicon actions, reviewer type |
| `handoff` | the banker lead card |
| `defence` | which defence layers acted |
| `final` | the reply the customer saw, sources, dated paths, elapsed time |

Each event has the conversation id, turn, timestamp and milliseconds since the turn started. The
**Flight recorder** page lets you pick a live conversation or an eval run, choose a turn, and step through the
events (or see the whole turn as a timeline), with a view per event type.

**Assumptions / design decisions**
- The recorder writes the per-turn trace plus the draft, the final reply and timings. The trace details include
  URLs, as-of dates, engine input and profile after.
- **Append-only JSONL on local disk**: one file per conversation; no database (Streamlit stays single-process).
- **PII**: the message is redacted before any agent sees it, and every line is redacted again before it is
  written, so an identifier carried by a buggy agent detail still can't reach disk (tested).
- The recorder **never breaks a turn**: a write failure is noted in the trace and the reply goes out.
- Logs are **git-ignored** and the unit tests write to a temporary folder.
- Eval runs keep a log per case, so any failing eval case can be replayed.

**Limitations**
- Logs hold full conversations (names, phone numbers, emails are *not* redacted — only NRIC/FIN and card
  numbers). Local prototype only: no encryption, access control, retention or deletion policy.
- Not tamper-evident (plain files; no hashing/signing).
- Eval-run logs accumulate under `data/conversations/evals/`; delete old runs by hand.
- Timings are wall-clock per turn; the lead-card thread's events can interleave with the main thread's.
- What the model *saw* is logged as the structured inputs we gave it (excerpts, engine output, profile), not
  the full prompt text.

---

## 10. Input guardrail

**What it does** (`src/agents/input_guard.py`): screens every customer message before any agent acts on it.

1. **Code patterns** (instant, high precision): instruction override ("ignore your previous instructions"),
   system-prompt extraction, role-play/jailbreak ("you're now a loan officer", "DAN", "developer mode"),
   fake authority ("SYSTEM NOTE:", "compliance is disabled", "you are authorised to…").
2. **LLM classifier** (fast model, low effort) for paraphrases the patterns miss. It only *classifies*; code
   maps the category to an action, so the model can't invent a behaviour.

| Category | Action |
|---|---|
| system_prompt_extraction, instruction_override, role_play_override, fake_authority, disparagement_request, abusive_or_harmful | **block** — a code-written reply says what DAISY can do instead |
| vulnerable_customer (financial distress, urgency that could cause harm) | **escalate** — route to a banker, lead card urgency forced to high |
| none | allow |

**Assumptions / design decisions**
- **Runs in parallel with the orchestrator**, so it adds no latency. On a block, everything the orchestrator
  extracted is discarded (profile restored), and readback state is untouched — a blocked message can't
  change, confirm or correct anything.
- **Narrow categories, precision first**: a guard that blocks real customers is worse than none. Plain
  comparisons ("how does DBS compare with OCBC?"), "act as a guarantor", "instructions to apply",
  frustration, and pressure to be told "approved" (handled downstream) are all allowed.
- **Escalate, don't block, distress**: a desperate customer gets a kind reply, a banker summary marked high priority and
  a note to contact DBS, not a refusal. In this prototype no banker receives it.
- **Failure modes**: if the classifier is unavailable, the message is allowed (code patterns still apply and
  every downstream layer still runs); if the model refuses to classify, the message is blocked.
- Block replies are code templates — no numbers, no approval vocabulary (tested) — and skip the LLM reviewer.
- A customer mentioning that "someone at DBS said I'm approved" is **not** fake authority (no instruction to
  the assistant); the engine and compliance layers handle it.

**Limitations**
- A message that mixes an attack with a real question is blocked whole (the real question goes unanswered).
- The code patterns can't be shown to be free of false positives: a real question that looks like an attack is still
  blocked whole.
- Code patterns are English-only; the classifier covers other phrasings but was only tested in English.
- The classifier is a model: novel attacks may get through it; the downstream layers (engine, compliance,
  code filters) remain the backstop, and the Swiss-cheese view will show it if one does.
- Multi-turn attacks spread across several innocent-looking messages are judged one message at a time.

---

## 11. Safety and compliance layers

| Layer | What it does | Where |
|---|---|---|
| Input guardrail | Blocks injection, prompt extraction, role-play, fake authority, disparagement requests; escalates distressed customers | `src/agents/input_guard.py` |
| PII guard | Removes NRIC/FIN (`[STFGM]1234567X`) and Luhn-valid 13–19 digit card numbers before anything else — and again on every flight-recorder line | `src/pii.py`, `src/recorder.py` |
| Rules engine | Every eligibility number is computed, never generated | `src/rules/` |
| Readback | No engine call until the customer confirms the parameters | `src/agents/eligibility.py` |
| Compliance reviewer (LLM) | approve / revise / handoff on advice boundary, grounding, approval language, scope, fair dealing, prompt injection | `src/agents/compliance.py` |
| Number traceability (code) | Every number must trace to the engine, the customer or a retrieved page; else one rewrite, else handoff | `compliance._enforce_traceable` |
| Banned vocabulary (code) | Approval words on eligibility and advice replies; one rewrite, else handoff. Other replies rely on the persona and the reviewer | `compliance._enforce_lexicon` |
| Code-written outcome | Outcome lines + fixed sentence appended after review | `src/pipeline.py` |
| Refusal handling | Sonnet requests use server-side fallbacks; a final refusal becomes the banker handoff | `src/llm.py` |

The reviewer and the traceability checks get exactly the excerpts the drafting model saw, headers included (`[n]`,
title, as-of date, URL), so valid dates, links and citations don't look unsupported.

**Limitations**
- PII guard does not catch phone numbers, emails, addresses, passport numbers or bank account numbers.
- Number traceability ignores whole numbers below 32 unless they are a percentage or an amount of money (the rest are
  usually list markers, ages, years, citation refs), so an invented "25 years" would pass. A percentage must trace to a
  percentage. The check is that the number exists in the engine output, the customer's words or a retrieved page, not
  that it's attached to the right fact: an invented "S$250 annual fee" passes if S$250 appears anywhere on the retrieved
  pages, and a figure the customer states themselves counts as traced. The LLM reviewer covers those cases.
- The LLM reviewer and the drafting agents are the same model family; the code filters exist because a
  reviewer model can miss things (it approved invented shortfalls before the filter existed).
- A citation on every product answer is measured by the eval suite, not enforced at runtime.
- Banned vocabulary is enforced on eligibility and advice replies only (words like "you'll get" are legitimate in
  product answers, e.g. "you'll get 3% cashback").

---

## 12. Models and API usage

- Main model `claude-sonnet-5-5` (drafting, narration, judge); fast model `claude-haiku-5-5` (routing,
  compliance review, lead card, rewrites). Override with `MODEL_MAIN` / `MODEL_FAST` / `MODEL_JUDGE`.
- **No `temperature`**: Sonnet 5.5 and Haiku 5.5 reject non-default sampling parameters.
- **Structured outputs** (`output_config.format`), not forced tool use (forced `tool_choice` 400s on
  Sonnet 5.5). Schemas must stay under the API's limit on union-typed (nullable) fields — 17 failed every
  request; fields are now optional instead of nullable (a unit test guards this).
- **Effort** per call: low for routing/short replies, medium for compliance, product answers and narration.
  Thinking is adaptive and counts against `max_tokens`.
- **Latency**: about 10 s per turn at the median, 16 s at the 90th percentile in the final 2026-11-02 eval run (3–4 sequential model calls). The compliance review is the slowest step.
- Cost was not measured.

---

## 13. Evaluation suite

- The suite is **run by hand**; there is no CI. `evals/cases.csv` (generated by `scripts/write_starter_cases.py` —
  edit the script, then regenerate): 148 cases across golden Q&A, eligibility, counterfactual, readback, recorder, guardrail, boundary,
  adversarial, scope, handoff, chit-chat and **broad** (the 50 broad questions, as handling checks);
  20 flagged adversarial. Multi-turn cases separate turns with `||`.
- Checks: intent, must / must-not phrases, engine status, citations, LLM judge (Sonnet), counterfactual
  outcome, expected profile, readback, corrections, `expect_stopped_by`, `expect_log_events`, `expect_guard`, plus
  always-on `numbers_traceable`, `banned_lexicon`, `outcome_template`, `no_engine_before_confirm`,
  `flight_recorded`, `log_has_no_pii`.
- Latest full run (2026-11-02): 148 of 148 cases passed; 20 attacks, 20 stopped, 0 holes; no ordinary question blocked. In
  earlier runs that day b13 failed once or twice, because the judge read the next-step line after a comparison as edging
  towards approval language. The line stays.
- Every case result also stores its defence-layer record and (for attacks) stopped / prompt-only / hole.
- Helpfulness was reviewed separately: a reviewer model rated each of the 50 broad questions good / partial / knowledge gap /
  wrong handling / unsafe. Latest review (2026-10-29): 46 good, 2 partial, 2 knowledge gaps, 0 unsafe. The eval suite checks they're
  *handled* safely; that review checked they're *useful*.
- Offline unit tests (`pytest`): 246, no API calls; the flight recorder writes to a temp folder during tests.
- Known flaky case: **qa-006** ("which cards give cashback on groceries?") occasionally names cards whose cited
  pages don't mention groceries; failed 1 of 4 recent runs (passed 10/10 on two immediate re-runs).
- The CSV is read with every column as text (`dtype=str`), so sparse flag columns aren't turned into floats.

**Limitations**
- Pass rates come from single runs of a non-deterministic system; re-runs of the eligibility,
  counterfactual and adversarial categories have been stable, but treat ±1 case as noise. The Swiss-cheese
  classification varies too: adv-010 has flipped between "stopped" and "held by prompt only" across runs.
- Substring checks can't tell negation ("I can't promise…") from assertion; must-not lists use affirmative
  phrases for that reason.
- Engine vs DBS's own calculators: see [`CALCULATOR_CHECK.md`](CALCULATOR_CHECK.md).
- **DeepEval** (`tests/evals/`, 78 questions: 59 from `cases.csv`, 19 generated from DBS pages and vetted): 76/78 on 2026-11-02,
  run just before the last extraction fix. The failures: b26, Answer Relevancy 0.64, where the judge marked down the disclaimers
  around a correct answer; and gen-08, Contextual Recall 0.00, a real miss, where a question about registering a Vantage Card for
  Priority Pass is routed as out of scope and declined (6 of 8 tries when re-tested). Thresholds weren't changed.
- Golden Q&A covers 22 questions: 15 check a figure quoted from a DBS/MAS page (`must_contain`), the rest
  are judged for behaviour. Accuracy across the wider question set is reviewer-rated, not fact-checked.

---

## 14. Deliberately out of scope

Investments, insurance and endowment (sharpest advice boundary — handed to a banker); car loans (the brief covers
home loans, cards and personal loans); live rate feeds;
identity capture; application submission; authentication, PDPA/data-residency controls, rate limiting
and production monitoring.

---

## 15. Open items

- Verify the home-loan minimum age.
- Staff pages have no login.
- Meaning-based (embedding) retrieval or a re-ranking pass for paraphrased questions (b37, b38 in the 50).
- Save the JavaScript-rendered DBS pages (home-loan fees, home-loan documents, card late-fee table) into
  `data/raw/`.
- More HDB detail (pages need saving from a browser).
- Latency: each turn makes 3–4 model calls in sequence.
- Input guardrail: per-message only (multi-turn attack patterns aren't tracked); English-tested.
- Flight-recorder logs need a retention/encryption policy before any real customer data.

---

## 16. Glossary

- **TDSR** — Total Debt Servicing Ratio: all monthly debt repayments ÷ gross monthly income. Cap 55%.
- **MSR** — Mortgage Servicing Ratio: property-loan repayments ÷ gross monthly income. Cap 30%; HDB flats and
  ECs within their minimum occupation period only.
- **LTV** — Loan-to-Value: loan ÷ property value. Limits depend on outstanding housing loans, tenure and age.
- **Medium-term (stress) interest rate** — the rate MAS requires banks to use when computing TDSR/MSR
  instalments (4% floor for residential), so customers aren't stretched if rates rise.
- **Thereafter rate** — a package's highest rate after the lock-in period.
- **Lock-in period** — the period in which repaying or refinancing a home loan incurs a penalty.
- **SORA** — Singapore Overnight Rate Average, the benchmark for floating home-loan rates.
- **EIR** — Effective Interest Rate: the true annual cost including fees and the repayment pattern.
- **Cashline** — DBS's revolving personal line of credit.
- **Unsecured borrowing limit** — industry-wide cap on interest-bearing unsecured debt across all banks:
  12× monthly income (since June 2019).
- **MOP** — Minimum Occupation Period for HDB flats / ECs.
- **HFE letter** — HDB Flat Eligibility letter, needed before applying for an HDB flat or HDB loan.
- **Binding rule** — the rule that blocks eligibility today (largest relative gap).
- **Headroom** — how far a ratio sits below its cap, in percentage points.
- **Dated / what-if lever** — a change backed by a stated end date (dated) vs a hypothetical (what-if).
- **Input guardrail** — the first layer: decides whether a message may reach the agents (block / escalate / allow).
- **Flight recorder** — the per-conversation event log and its replay page.
- **Swiss-cheese model** — safety from several imperfect layers stacked so their holes rarely line up;
  *hole alignment* is when an attack passes through every layer.
