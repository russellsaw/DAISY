"""Shared pieces of the DeepEval build loop: the judge, the goldens, and turning a DAISY reply into a test case.

The goldens come from two places, kept apart so either can be re-run on its own:
  - existing: evals/cases.csv — every golden_qa case, plus the broad questions routed to product Q&A or
    comparison. No expected output (those cases check facts with must_contain instead).
  - generated: tests/evals/goldens_generated.json — written by generate_goldens.py from DBS page chunks,
    each with the expected output the synthesiser derived from its context.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from deepeval.models import AnthropicModel

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

HERE = Path(__file__).resolve().parent
GENERATED = HERE / "goldens_generated.json"
PRODUCT_INTENTS = ("product_qa", "comparison")
# USD per million tokens, Anthropic API (2026-10). DeepEval has no prices for Claude 5.5 models; without
# them its cost tracking breaks (the synthesiser crashes adding up None).
PRICES = {"claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}


def judge() -> AnthropicModel:
    """The same judge model as evals/judge.py. DeepEval's registry doesn't list Claude 5.5 models, so it
    would fall back to parsing JSON from free text (which fails); they do support structured outputs."""
    price_in, price_out = PRICES.get(config.MODEL_JUDGE, (None, None))
    model = AnthropicModel(
        model=config.MODEL_JUDGE,
        cost_per_input_token=price_in / 1e6 if price_in else None,
        cost_per_output_token=price_out / 1e6 if price_out else None,
    )
    model.model_data.supports_structured_outputs = True
    return model


@dataclass
class DaisyGolden:
    id: str
    input: str
    expected_output: str | None = None
    source: str = ""


def existing_goldens() -> list[DaisyGolden]:
    cases = pd.read_csv(ROOT / "evals" / "cases.csv", dtype=str).fillna("")
    product_broad = (cases.category == "broad") & cases.expected_intent.str.contains("|".join(PRODUCT_INTENTS))
    chosen = cases[(cases.category == "golden_qa") | product_broad]
    return [DaisyGolden(r.id, r.input, source="evals/cases.csv") for r in chosen.itertuples()]


def generated_goldens() -> list[DaisyGolden]:
    if not GENERATED.exists():
        return []
    return [
        DaisyGolden(g["id"], g["input"], g.get("expected_output"), g.get("source_doc", ""))
        for g in json.loads(GENERATED.read_text(encoding="utf-8"))
    ]


def excerpts(sources_text: str) -> list[str]:
    """Split what the drafting agent saw back into one item per numbered excerpt ("[n] Title (...) — URL")."""
    parts = re.split(r"\n\n(?=\[\d+\] )", sources_text.strip())
    return [p for p in parts if p.strip()]
