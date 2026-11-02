"""Synthesise goldens from DBS page chunks with DeepEval and write tests/evals/goldens_generated.json.

    python tests/evals/generate_goldens.py            # 20 goldens, fixed seed
    python tests/evals/generate_goldens.py --n 30

Contexts are chunks from data/kb.json (run `python scripts/ingest.py --build-only` first), drawn from DBS/POSB
product and FAQ pages only (no T&C legal text), spread across home loans, cards and personal loans/Cashline.
Contexts are passed in directly, so no embedding model is needed. Read every golden before committing the
file: a synthetic question can be ambiguous or answerable only from the one chunk it came from, and an
expected answer must state facts only. The first batch (2026-10-29) had nine expected answers claiming "the
context doesn't say X"; Contextual Recall then failed DAISY for retrieving pages that did say X. Those were
corrected by hand (see each golden's `vetting_note`).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daisy_deepeval import GENERATED, ROOT, judge  # noqa: E402
from deepeval.synthesizer import Synthesizer  # noqa: E402
from deepeval.synthesizer.config import EvolutionConfig, FiltrationConfig, StylingConfig  # noqa: E402

# Share of goldens per product group; together they cover the brief's mortgages, cards and unsecured loans.
GROUPS = {"home_loan": 0.35, "credit_card": 0.35, "unsecured": 0.30}
SKIP_DOCS = ("tncs", "cardmember-agreement", "terms")


def group_of(chunk: dict) -> str | None:
    products = set(chunk.get("products") or [])
    if products & {"personal_loan", "cashline"}:
        return "unsecured"
    for g in ("home_loan", "credit_card"):
        if g in products:
            return g
    return None


def pick_contexts(n: int, seed: int) -> list[dict]:
    chunks = json.loads((ROOT / "data" / "kb.json").read_text(encoding="utf-8"))["chunks"]
    usable = [c for c in chunks if c.get("publisher") in ("DBS", "POSB") and len(c["text"]) >= 400
              and not any(s in c["doc_id"] for s in SKIP_DOCS)]
    rng = random.Random(seed)
    picked: list[dict] = []
    for group, share in GROUPS.items():
        pool = [c for c in usable if group_of(c) == group]
        rng.shuffle(pool)
        seen_docs: set[str] = set()
        for c in pool:  # one chunk per page, so no single page dominates
            if len([p for p in picked if group_of(p) == group]) >= round(n * share):
                break
            if c["doc_id"] not in seen_docs:
                seen_docs.add(c["doc_id"])
                picked.append(c)
    return picked


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    contexts = pick_contexts(args.n, args.seed)
    model = judge()
    synthesizer = Synthesizer(
        model=model,
        max_concurrent=6,
        cost_tracking=True,
        filtration_config=FiltrationConfig(critic_model=model),
        evolution_config=EvolutionConfig(num_evolutions=1),
        styling_config=StylingConfig(
            scenario="A retail banking customer in Singapore asking DBS's chat assistant about DBS home loans, "
                     "credit cards, personal loans or Cashline.",
            task="Answer the customer's product question from DBS's public product pages.",
            input_format="One short question a customer would type into a chat, in plain English, without "
                         "referring to 'the context', 'the passage' or 'the document'.",
            expected_output_format="A short factual answer stating the relevant figures and conditions. State only "
                                   "facts from the context; never say what the context, passage or document does "
                                   "or doesn't contain.",
        ),
    )
    goldens = synthesizer.generate_goldens_from_contexts(
        contexts=[[f"{c['title']} ({c['publisher']}) — {c['url']}\n{c['text']}"] for c in contexts],
        max_goldens_per_context=1,
        include_expected_output=True,
    )
    by_context = {f"{c['title']} ({c['publisher']}) — {c['url']}\n{c['text']}": c for c in contexts}
    out = []
    for i, g in enumerate(goldens, 1):
        chunk = by_context.get((g.context or [""])[0], {})
        out.append({"id": f"gen-{i:02d}", "input": g.input, "expected_output": g.expected_output,
                    "source_doc": chunk.get("doc_id", ""), "source_url": chunk.get("url", ""),
                    "context": g.context})
    GENERATED.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(out)} goldens -> {GENERATED}")


if __name__ == "__main__":
    main()
