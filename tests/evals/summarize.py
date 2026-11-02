"""Read the last `deepeval test run`: per-metric stats, then every failing metric with the judge's reason.

    python tests/evals/summarize.py                  # .deepeval/.latest_test_run.json
    python tests/evals/summarize.py path/to/run.json

Each run is also archived to evals/results/deepeval-<identifier>.json (git-ignored), so rounds of the build
loop can be compared. A failing golden's full trace is its flight-recorder log in
data/conversations/deepeval/<run>/<golden id>.jsonl.
"""

from __future__ import annotations

import json
import shutil
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".deepeval" / ".latest_test_run.json"
    run = json.loads(path.read_text(encoding="utf-8"))["testRunData"]
    print(
        f"run {run.get('identifier')}: {run['testPassed']} passed / {run['testFailed']} failed, "
        f"{run['runDuration'] / 60:.1f} min, judge ${run['evaluationCost']:.2f}"
    )
    for m in run["metricsScores"]:
        s = m["scores"]
        print(
            f"  {m['metric']:<20} n={len(s):>3}  mean={statistics.mean(s):.3f}  min={min(s):.2f}  "
            f"pass={m['passes']} fail={m['fails']} err={m['errors']}"
        )
    print()
    for case in sorted(run["testCases"], key=lambda c: c["name"]):
        failing = [md for md in case["metricsData"] if not md["success"]]
        if not failing:
            continue
        meta = case.get("metadata") or {}
        print(f"== {meta.get('id', case['name'])}  intent={meta.get('intent')}  source={meta.get('source')}")
        print(f"   Q: {case['input']}")
        print(f"   trace: {meta.get('log')}")
        for md in failing:
            score = "error" if md["score"] is None else f"{md['score']:.2f}"
            print(f"   {md['name']} {score} (< {md['threshold']}): {md.get('error') or md['reason']}")

    if run.get("identifier"):
        archive = ROOT / "evals" / "results" / f"deepeval-{run['identifier']}.json"
        if path.resolve() != archive.resolve():
            shutil.copy(path, archive)


if __name__ == "__main__":
    main()
