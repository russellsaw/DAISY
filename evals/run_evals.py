"""Run the eval suite against the live pipeline.

    python evals/run_evals.py                      # everything
    python evals/run_evals.py --categories adversarial,boundary --limit 20

Each case starts a fresh conversation (optionally pre-seeded with a profile),
sends one message, and checks the response:
  - expected_intent      orchestrator routed correctly
  - must_contain         any one of the | separated phrases appears (case-insensitive)
  - must_not_contain     none of the | separated phrases appear
  - expected_status      engine status for expected_product (eligibility cases)
  - requires_citation    product_qa answers must cite at least one source
  - expected_behavior    LLM judge (boundary / adversarial / scope cases)
Writes evals/results/<timestamp>.json and latest.json (with run history).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.judge import judge  # noqa: E402
from src import config  # noqa: E402
from src.pipeline import run_turn  # noqa: E402
from src.state import ConversationState  # noqa: E402


def _split(value) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
        return []
    return [v.strip() for v in str(value).split("|") if v.strip()]


def run_case(case: dict) -> dict:
    state = ConversationState()
    if case.get("profile"):
        state.profile.update(json.loads(case["profile"]))

    started = time.time()
    try:
        result = run_turn(state, case["input"])
        response, intent = result.response, result.intent
        error = None
    except Exception as exc:  # noqa: BLE001
        response, intent, result, error = "", "error", None, str(exc)

    checks = []
    if error:
        checks.append({"name": "no_error", "passed": False, "detail": error})

    if case.get("expected_intent"):
        ok = intent == case["expected_intent"]
        checks.append({"name": "intent", "passed": ok, "detail": f"expected {case['expected_intent']}, got {intent}"})

    musts = _split(case.get("must_contain"))
    if musts:
        ok = any(m.lower() in response.lower() for m in musts)
        checks.append({"name": "must_contain", "passed": ok, "detail": f"any of {musts}"})

    nots = _split(case.get("must_not_contain"))
    if nots:
        hits = [n for n in nots if n.lower() in response.lower()]
        checks.append({"name": "must_not_contain", "passed": not hits, "detail": f"found {hits}" if hits else "clean"})

    if case.get("expected_status") and result is not None:
        prod = case.get("expected_product")
        statuses = {r["product"]: r["status"] for r in result.eligibility}
        got = statuses.get(prod)
        checks.append({"name": "engine_status", "passed": got == case["expected_status"],
                       "detail": f"{prod}: expected {case['expected_status']}, got {got}"})

    if str(case.get("requires_citation", "")).lower() in ("1", "true", "yes") and result is not None:
        checks.append({"name": "citation", "passed": bool(result.sources),
                       "detail": f"{len(result.sources)} source(s) cited"})

    if case.get("expected_behavior") and response:
        verdict = judge(case["input"], response, case["expected_behavior"])
        checks.append({"name": "judge", "passed": bool(verdict["passed"]), "detail": verdict["rationale"]})

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
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "seconds": round(time.time() - started, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--categories", default="")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    df = pd.read_csv(config.EVAL_CASES).fillna("")
    if args.categories:
        df = df[df["category"].isin(args.categories.split(","))]
    if args.limit:
        df = df.head(args.limit)

    results = []
    for i, case in enumerate(df.to_dict("records"), 1):
        res = run_case(case)
        results.append(res)
        print(f"[{i}/{len(df)}] {'PASS' if res['passed'] else 'FAIL'}  {res['id']}  ({res['seconds']}s)", flush=True)

    config.EVAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_at = datetime.now().isoformat(timespec="seconds")
    pass_rate = sum(r["passed"] for r in results) / len(results) if results else 0.0

    latest_path = config.EVAL_RESULTS_DIR / "latest.json"
    history = json.loads(latest_path.read_text()).get("history", []) if latest_path.exists() else []
    history.append({"run_at": run_at, "cases": len(results), "pass_rate": round(pass_rate, 3)})

    payload = {"run_at": run_at, "results": results, "history": history[-30:]}
    (config.EVAL_RESULTS_DIR / f"{run_at.replace(':', '-')}.json").write_text(json.dumps(payload, indent=1))
    latest_path.write_text(json.dumps(payload, indent=1))

    print(f"\n{sum(r['passed'] for r in results)}/{len(results)} passed ({pass_rate:.0%})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
