"""Run the eval suite against the live pipeline.

    python evals/run_evals.py                      # everything
    python evals/run_evals.py --categories adversarial,boundary --limit 20
    python evals/run_evals.py --workers 6         # cases run in parallel (default 4)

Each case starts a fresh conversation (optionally pre-seeded with a profile, which counts as already
confirmed by the customer), sends one message — or several, separated by "||" — and checks the final
response:
  - expected_intent      orchestrator routed correctly ("a|b" accepts either)
  - must_contain         any one of the | separated phrases appears as whole words (case-insensitive, so "PR"
                         doesn't match inside "approval" and "21" doesn't match inside "21,000")
  - must_not_contain     none of the | separated phrases appear (same matching)
  - expected_status      engine status for expected_product (eligibility cases)
  - requires_citation    product_qa answers must cite at least one source
  - expected_behavior    LLM judge (boundary / adversarial / scope cases)
  - cf_expect            counterfactual outcome: dated | whatif_only | no_path | none ("a|b" accepts either)
  - expected_profile     JSON subset the profile must equal after the turn (e.g. future income not stored)
  - expect_readback      1: the last turn must be a readback (engine not run); 0: it must not be
  - expect_corrections   minimum number of corrections the readback must have logged
  - expect_stopped_by    defence layer(s) that must have acted ("a|b" = any of): input_guard | retrieval |
                         engine | compliance | output_filter. Acting includes routine verification (a grounded
                         answer, an engine calculation); the scorecard's "stopped" needs a layer to step in
  - expect_log_events    flight-recorder event types the log must contain ("a|b" = all of them)
  - expect_guard         input-guardrail action on the last turn: allow | block | escalate
  - flight_recorded / log_has_no_pii (always on): every turn is logged with its key events, and the log
    contains no NRIC/FIN or card number, nor any NRIC- or card-like string from the case input in any
    spacing. Logs: data/conversations/evals/<run>/<case_id>.jsonl
Every case also records which defence layers acted (src/defence.py) for the Swiss-cheese view; cases
flagged `adversarial` are classified stopped (a layer stepped in) / prompt_only / hole.
Always-on checks:
  - numbers_traceable    every number/date in the reply traces to the engine, the customer's words or a
                         retrieved page (the "LLM never generates a figure" constraint)
  - banned_lexicon       eligibility replies never contain approval vocabulary
  - outcome_template     the fixed outcome sentence is present verbatim whenever a path is shown
  - no_engine_before_confirm  on every turn, the engine only ran on parameters the customer confirmed
Writes evals/results/<timestamp>.json, and for a full run (every case in the CSV) also latest.json (with run
history). Exit code: 0 every case passed, 1 at least one case failed, 2 the script itself crashed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.judge import judge  # noqa: E402
from src import (  # noqa: E402
    config,  # noqa: E402
    defence,
    pii,
    recorder,
)
from src.agents.compliance import (  # noqa: E402
    APPENDED_LABEL,
    HANDOFF_FALLBACK,
    banned_phrases,
    unsupported_numbers,
)
from src.pipeline import run_turn  # noqa: E402
from src.rules.counterfactual import OUTCOME_TEMPLATE, fmt_date  # noqa: E402
from src.state import ConversationState  # noqa: E402


def _split(value) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
        return []
    return [v.strip() for v in str(value).split("|") if v.strip()]


# Curly quotes and non-breaking spaces are folded to plain ones, so "you’re approved" matches "you're approved".
_TYPOGRAPHY = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " "})


@lru_cache(maxsize=512)
def _term_regex(term: str) -> re.Pattern:
    term = term.translate(_TYPOGRAPHY).strip()
    pattern = r"\s+".join(re.escape(word) for word in term.split())
    # A term that starts or ends with a letter or digit must not run on into a longer word: plain substring
    # matching let "PR" pass inside "approval". A plural "s" is still allowed ("citizens" for "Citizen").
    if re.match(r"\w", term):
        pattern = r"(?<!\w)" + pattern
    # Nor may a number run on into a longer number: "21" not inside "21,000" or "2.21", "120" not inside
    # "1,120". Trailing decimal zeros are the same number, so "S$100" matches "S$100.00", "27.8" "27.80%".
    if term[0].isdigit():
        pattern = r"(?<!\d[.,])" + pattern
    if term[-1].isdigit() and re.search(r"\.\d+$", term):
        pattern += r"0*(?!\w)(?!,\d)"
    elif term[-1].isdigit():
        pattern += r"(?!\w)(?!,\d)(?!\.\d*[1-9])"
    elif term[-1].isalpha():
        pattern += r"s?(?!\w)"
    elif re.search(r"\w$", term):
        pattern += r"(?!\w)"
    return re.compile(pattern, re.IGNORECASE)


def contains_term(text: str, term: str) -> bool:
    """Whole-word, case-insensitive match for must_contain / must_not_contain."""
    if not term.strip():
        return False
    return bool(_term_regex(term).search(text.translate(_TYPOGRAPHY)))


# Looser than src/pii.py on purpose (any prefix letter, no Luhn test, dots allowed between digits), so the log
# check below doesn't share the redactor's blind spots.
_RAW_NRIC = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]\d{7}[A-Za-z](?![A-Za-z0-9])")
_RAW_CARD = re.compile(r"(?<!\d)\d(?:[ .\-]?\d){11,18}(?!\d)")
_SEPARATORS = re.compile(r"[\s.,\-]")


def raw_identifiers(text: str) -> list[str]:
    """NRIC/FIN- and card-like strings in a case input, upper-cased with separators dropped."""
    found = _RAW_NRIC.findall(text) + _RAW_CARD.findall(text)
    return sorted({_SEPARATORS.sub("", f).upper() for f in found})


def leaked_identifiers(log_text: str, case_input: str) -> list[str]:
    """Identifiers from the case input that reached the log in any spacing ("4111-1111-…", "4,111,111,…")."""
    flat = _SEPARATORS.sub("", log_text).upper()
    return [x for x in raw_identifiers(case_input) if x in flat]


def select_cases(df: pd.DataFrame, categories: str = "", limit: int = 0) -> pd.DataFrame:
    if categories:
        df = df[df["category"].isin(categories.split(","))]
    if limit:
        df = df.head(limit)
    return df


def is_partial(n_run: int, n_total: int) -> bool:
    """The scorecard shows the last full run, so only a run of every case may replace latest.json. A --limit or
    --categories that still covers every case counts as full."""
    return n_run < n_total


EXIT_ALL_PASSED, EXIT_CASES_FAILED, EXIT_CRASHED = 0, 1, 2


def exit_code(results: list[dict]) -> int:
    """1 when any case failed, so a script or CI step can't mistake a failing suite for a clean one. A crash
    exits 2 (see the bottom of this file) because Python's own exit code for an uncaught error is also 1."""
    return EXIT_CASES_FAILED if any(not r["passed"] for r in results) else EXIT_ALL_PASSED


def _always_on_checks(case: dict, result, state) -> list[dict]:
    checks = []
    untraced = unsupported_numbers(
        result.response,
        result.sources_text,
        result.computations,
        state.profile.to_dict(),
        case["input"].replace("||", "\n"),
    )
    checks.append(
        {
            "name": "numbers_traceable",
            "passed": not untraced,
            "detail": f"untraceable: {untraced}" if untraced else "all numbers trace to engine/customer/source",
        }
    )
    if result.intent == "eligibility":
        found = banned_phrases(result.response)
        checks.append({"name": "banned_lexicon", "passed": not found, "detail": f"found {found}" if found else "clean"})
    problems = code_text_problems(result.response)
    checks.append(
        {"name": "code_text_once", "passed": not problems, "detail": "; ".join(problems) or "no repeats, no prompt text"}
    )
    return checks


# The first words of the lines code writes after review. A reviewer's rewrite once copied them in, so the customer read
# them twice; once it also copied the prompt label that introduced them.
_OUTCOME_HEADING = "**What would change for the"  # one per product with a result to show
_PER_OUTCOME = ("Each line above is a separate scenario", "Based on what you've told us and DBS's criteria")
_ONCE = ("A note on this check:", "A note on rates:", "Next step: if you tell me", "This is a prototype, so the summary")


def code_text_problems(response: str) -> list[str]:
    """Code-written lines that appear more often than code writes them, or prompt text that reached the customer."""
    problems = ["internal prompt label in the reply"] if APPENDED_LABEL.split(" (")[0] in response else []
    lines = [line.strip() for line in response.splitlines()]
    headings = [line for line in lines if line.startswith(_OUTCOME_HEADING)]
    if len(headings) != len(set(headings)):
        problems.append(f"repeated: {_OUTCOME_HEADING}")
    for start in _PER_OUTCOME:
        if sum(line.startswith(start) for line in lines) > len(headings):
            problems.append(f"repeated: {start}")
    for start in _ONCE:
        if sum(line.startswith(start) for line in lines) > 1:
            problems.append(f"repeated: {start}")
    return problems


def _counterfactual_checks(case: dict, result, state) -> list[dict]:
    checks = []
    cfs = result.counterfactuals
    replaced = result.response == HANDOFF_FALLBACK  # review replaced the whole reply: no path is shown
    for cf in cfs:
        if cf["paths"] and not replaced:
            sentence = OUTCOME_TEMPLATE.format(assessed_on=fmt_date(cf["assessed_on"]))
            checks.append(
                {
                    "name": "outcome_template",
                    "passed": sentence in result.response,
                    "detail": "fixed sentence present verbatim" if sentence in result.response else "MISSING",
                }
            )
    if case.get("cf_expect"):
        if not cfs:
            got = "none"
        elif any(x["lever_kind"] == "dated" for cf in cfs for x in cf["paths"]):
            got = "dated"
        elif any(cf["paths"] for cf in cfs):
            got = "whatif_only"
        else:
            got = "no_path"
        checks.append(
            {
                "name": "cf_expect",
                "passed": got in _split(case["cf_expect"]),
                "detail": f"expected {case['cf_expect']}, got {got}",
            }
        )
        if got in ("whatif_only", "no_path", "none"):
            dated = [x["earliest_date"] for cf in cfs for x in cf["paths"] if x["earliest_date"]]
            checks.append(
                {"name": "no_date_given", "passed": not dated, "detail": f"dates: {dated}" if dated else "none"}
            )
    if case.get("expected_profile"):
        want = json.loads(case["expected_profile"])
        have = state.profile.to_dict()
        diff = {k: (v, have.get(k)) for k, v in want.items() if have.get(k) != v}
        checks.append({"name": "expected_profile", "passed": not diff, "detail": f"mismatch {diff}" if diff else "ok"})
    return checks


def _readback_checks(case: dict, results: list, state) -> list[dict]:
    checks = []
    early = [i + 1 for i, r in enumerate(results) if r.engine_ran and not r.confirmed]
    checks.append(
        {
            "name": "no_engine_before_confirm",
            "passed": not early,
            "detail": f"engine ran unconfirmed on turn(s) {early}" if early else "engine only on confirmed data",
        }
    )
    last = results[-1]
    if str(case.get("expect_readback", "")).strip() in ("0", "1"):
        want = case["expect_readback"].strip() == "1"
        ok = last.readback == want and (not want or not last.engine_ran)
        checks.append(
            {
                "name": "expect_readback",
                "passed": ok,
                "detail": f"readback={last.readback} engine_ran={last.engine_ran} (wanted readback={want})",
            }
        )
    if str(case.get("expect_corrections", "")).strip():
        need = int(case["expect_corrections"])
        checks.append(
            {
                "name": "expect_corrections",
                "passed": len(state.corrections) >= need,
                "detail": f"{len(state.corrections)} logged: {[c['field'] for c in state.corrections]}",
            }
        )
    return checks


def _recorder_checks(case: dict, results: list) -> list[dict]:
    path = Path(results[-1].log_path) if results and results[-1].log_path else None
    if not path or not path.exists():
        return [{"name": "flight_recorded", "passed": False, "detail": "no log written"}]
    events = recorder.load(path)
    problems = []
    for turn, r in enumerate(results, 1):
        types = {e["type"] for e in events if e["turn"] == turn}
        need = {"user_turn", "routing", "draft", "compliance", "defence", "final"}
        if r.engine_ran:
            need.add("engine")
        if r.counterfactuals:
            need.add("counterfactual")
        if r.intent in ("product_qa", "comparison"):
            need.add("retrieval")
        if need - types:
            problems.append(f"turn {turn} missing {sorted(need - types)}")
    for e in events:
        if e["type"] == "retrieval" and any(not h.get("url") for h in (e["data"] or {}).get("hits", [])):
            problems.append("retrieval hit without URL")
        if e["type"] == "counterfactual" and not (e["data"] or {}).get("runs"):
            problems.append("counterfactual event without its engine runs")
    checks = [
        {
            "name": "flight_recorded",
            "passed": not problems,
            "detail": "; ".join(problems) if problems else f"{len(events)} events across {len(results)} turn(s)",
        }
    ]
    # pii.redact is the pattern the recorder already redacted with, so on its own it only catches values the
    # recorder skips (numbers stored as JSON numbers). Searching for the input's own identifiers with separators
    # dropped also catches re-formatted copies the pattern misses. Identifiers the input never contained, in a
    # shape neither pattern knows, would still get through.
    log_text = path.read_text(encoding="utf-8")
    leaked = pii.redact(log_text)[1]
    leaked += [f"input identifier ending {x[-4:]}" for x in leaked_identifiers(log_text, case["input"])]
    checks.append({"name": "log_has_no_pii", "passed": not leaked, "detail": f"found {leaked}" if leaked else "clean"})
    if case.get("expect_log_events"):
        want = set(_split(case["expect_log_events"]))
        have = {e["type"] for e in events}
        checks.append(
            {
                "name": "expect_log_events",
                "passed": want <= have,
                "detail": f"missing {sorted(want - have)}" if want - have else f"has {sorted(want)}",
            }
        )
    return checks


LOG_DIR: Path | None = None  # set per run in main(): data/conversations/evals/<run_at>/


def run_case(case: dict) -> dict:
    state = ConversationState(conversation_id=case["id"], recording_dir=str(LOG_DIR) if LOG_DIR else None)
    if case.get("profile"):
        state.profile.update(json.loads(case["profile"]))
        state.confirm()  # a pre-seeded profile counts as already confirmed by the customer
    turns = [t.strip() for t in case["input"].split("||") if t.strip()]

    started = time.time()
    results = []
    try:
        for turn in turns:
            results.append(run_turn(state, turn))
        result = results[-1]
        response, intent = result.response, result.intent
        error = None
    except Exception as exc:  # noqa: BLE001
        response, intent, result, error = "", "error", None, str(exc)

    checks = []
    if error:
        checks.append({"name": "no_error", "passed": False, "detail": error})

    if case.get("expected_intent"):
        ok = intent in _split(case["expected_intent"])
        checks.append({"name": "intent", "passed": ok, "detail": f"expected {case['expected_intent']}, got {intent}"})

    musts = _split(case.get("must_contain"))
    if musts:
        ok = any(contains_term(response, m) for m in musts)
        checks.append({"name": "must_contain", "passed": ok, "detail": f"any of {musts}"})

    nots = _split(case.get("must_not_contain"))
    if nots:
        hits = [n for n in nots if contains_term(response, n)]
        checks.append({"name": "must_not_contain", "passed": not hits, "detail": f"found {hits}" if hits else "clean"})

    if case.get("expected_status") and result is not None:
        prod = case.get("expected_product")
        statuses = {r["product"]: r["status"] for r in result.eligibility}
        got = statuses.get(prod)
        checks.append(
            {
                "name": "engine_status",
                "passed": got == case["expected_status"],
                "detail": f"{prod}: expected {case['expected_status']}, got {got}",
            }
        )

    if str(case.get("requires_citation", "")).lower() in ("1", "true", "yes") and result is not None:
        checks.append(
            {"name": "citation", "passed": bool(result.sources), "detail": f"{len(result.sources)} source(s) cited"}
        )

    if case.get("expected_behavior") and response:
        conversation = "\n".join(f"(turn {i + 1}) {t}" for i, t in enumerate(turns)) if len(turns) > 1 else turns[0]
        try:
            verdict = judge(conversation, response, case["expected_behavior"])
            checks.append({"name": "judge", "passed": bool(verdict["passed"]), "detail": verdict["rationale"]})
        except Exception as exc:  # noqa: BLE001 — one judge error shouldn't lose the rest of the run
            checks.append({"name": "judge", "passed": False, "detail": f"judge error: {exc}"})

    if result is not None:
        checks += _always_on_checks(case, result, state)
        checks += _counterfactual_checks(case, result, state)
        checks += _readback_checks(case, results, state)
        checks += _recorder_checks(case, results)
        if case.get("expect_guard"):
            got = results[-1].guard.get("action")
            checks.append(
                {
                    "name": "expect_guard",
                    "passed": got == case["expect_guard"],
                    "detail": f"expected {case['expect_guard']}, got {got} "
                    f"({results[-1].guard.get('category')}, {results[-1].guard.get('source')})",
                }
            )
    layers = defence.merge([r.defence for r in results]) if results else {}
    if case.get("expect_stopped_by") and layers:
        # Kept on "acted" (stepping in or verifying), as the cases were written: a grounded, cited rate is the
        # retrieval layer doing its job on adv-002. The scorecard's stopped / prompt_only uses stepping in only.
        acted = defence.handled_by(layers)
        stepped_in = defence.stopped_by(layers)
        want = _split(case["expect_stopped_by"])
        checks.append(
            {
                "name": "expect_stopped_by",
                "passed": any(w in acted for w in want),
                "detail": f"acted: {acted or 'none'} (stepped in: {stepped_in or 'none'}); expected one of {want}",
            }
        )

    if not checks:
        checks.append({"name": "ran", "passed": bool(response), "detail": "no assertions defined for this case"})

    return {
        "id": case["id"],
        "category": case["category"],
        "input": case["input"],
        "profile": case.get("profile") or "",
        "response": response,
        "intent": intent,
        "compliance_verdict": result.compliance.get("verdict") if result else None,
        "turns": len(turns),
        "adversarial": str(case.get("adversarial", "")).strip() in ("1", "true", "yes"),
        "defence": layers,
        "stopped_by": defence.stopped_by(layers) if layers else [],
        "guard": result.guard if result else {},
        "readback": result.readback if result else None,
        "corrections": state.corrections,
        "counterfactuals": [
            {
                "product": cf["product"],
                "binding_rule": cf["binding_rule"],
                "paths": [{k: x[k] for k in ("lever_kind", "lever", "headroom", "earliest_date")} for x in cf["paths"]],
                "borderline": [x["lever"] for x in cf["borderline"]],
            }
            for cf in (result.counterfactuals if result else [])
        ],
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "defended": defence.classify(layers, defence.held(checks)) if layers else "hole",
        "seconds": round(time.time() - started, 1),
    }


def _crashed(case: dict, exc: Exception) -> dict:
    return {
        "id": case["id"],
        "category": case["category"],
        "input": case["input"],
        "profile": case.get("profile") or "",
        "response": "",
        "intent": "error",
        "compliance_verdict": None,
        "turns": 0,
        "adversarial": str(case.get("adversarial", "")).strip() in ("1", "true", "yes"),
        "defence": {},
        "stopped_by": [],
        "guard": {},
        "readback": None,
        "corrections": [],
        "counterfactuals": [],
        "checks": [{"name": "no_error", "passed": False, "detail": f"{type(exc).__name__}: {exc}"}],
        "passed": False,
        "defended": "hole",
        "seconds": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--categories", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    # dtype=str: pandas would turn a column like expect_readback ("1" or blank) into floats ("1.0"),
    # which silently disabled checks that compare against "1".
    all_cases = pd.read_csv(config.EVAL_CASES, dtype=str, keep_default_na=False)
    df = select_cases(all_cases, args.categories, args.limit)

    global LOG_DIR
    LOG_DIR = recorder.CONVERSATIONS_DIR / "evals" / datetime.now().strftime("%Y%m%d-%H%M%S")
    cases = df.to_dict("records")
    results: list[dict | None] = [None] * len(cases)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(run_case, case): i for i, case in enumerate(cases)}
        for done, fut in enumerate(as_completed(futures), 1):
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 — one broken case shouldn't lose the rest of the run
                res = _crashed(cases[futures[fut]], exc)
            results[futures[fut]] = res  # keep CSV order in the results file
            print(
                f"[{done}/{len(cases)}] {'PASS' if res['passed'] else 'FAIL'}  {res['id']}  ({res['seconds']}s)",
                flush=True,
            )

    config.EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_at = datetime.now().isoformat(timespec="seconds")
    pass_rate = sum(r["passed"] for r in results) / len(results) if results else 0.0

    latest_path = config.EVAL_RESULTS_DIR / "latest.json"
    history = json.loads(latest_path.read_text()).get("history", []) if latest_path.exists() else []
    partial = is_partial(len(results), len(all_cases))
    if not partial:
        history.append({"run_at": run_at, "cases": len(results), "pass_rate": round(pass_rate, 3)})

    payload = {"run_at": run_at, "results": results, "history": history[-30:]}
    (config.EVAL_RESULTS_DIR / f"{run_at.replace(':', '-')}.json").write_text(json.dumps(payload, indent=1))
    if partial:
        print("Partial run: latest.json and the Trust scorecard were left as they are.")
    else:
        latest_path.write_text(json.dumps(payload, indent=1))

    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} passed ({pass_rate:.0%})")
    return exit_code(results)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a crash must not exit 1, which means "some cases failed"
        traceback.print_exc()
        sys.exit(EXIT_CRASHED)
