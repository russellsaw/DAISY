"""DAISY product Q&A as a RAG chatbot, scored by DeepEval.

    deepeval test run tests/evals/test_daisy.py --num-processes 4 --identifier round-1
    deepeval test run tests/evals/test_daisy.py -k "gen-"        # generated goldens only

Each golden is asked in a fresh conversation through the real pipeline (run_turn). The test case's
retrieval context is exactly the numbered excerpts the drafting agent saw.

Metrics (thresholds fixed when the suite was set up; fix the app, never the threshold):
  Faithfulness        0.8  the answer doesn't contradict the excerpts it was given
  Answer relevancy    0.7  the answer addresses the question asked
  Contextual recall   0.7  retrieval found what the expected answer needs (goldens with an expected output)
Contextual relevancy (retrieval precision) is left out on purpose: DAISY retrieves 10 excerpts because 5
missed the right page in the 50-question review, so a precision metric would push that fix back out.
"""
from __future__ import annotations

import pytest
from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric, ContextualRecallMetric, FaithfulnessMetric
from deepeval.test_case import LLMTestCase

from daisy_deepeval import excerpts, existing_goldens, generated_goldens, judge
from src.pipeline import run_turn
from src.state import ConversationState

GOLDENS = existing_goldens() + generated_goldens()
JUDGE = judge()


@pytest.mark.parametrize("golden", GOLDENS, ids=[g.id for g in GOLDENS])
def test_daisy_answers(golden, run_dir):
    result = run_turn(ConversationState(conversation_id=golden.id, recording_dir=str(run_dir)), golden.input)
    test_case = LLMTestCase(
        input=golden.input,
        actual_output=result.response,
        expected_output=golden.expected_output,
        retrieval_context=excerpts(result.sources_text) or ["(no excerpts retrieved)"],
        additional_metadata={"id": golden.id, "intent": result.intent, "source": golden.source,
                             "log": str(run_dir / f"{golden.id}.jsonl")},
    )
    metrics = [FaithfulnessMetric(threshold=0.8, model=JUDGE), AnswerRelevancyMetric(threshold=0.7, model=JUDGE)]
    if golden.expected_output:
        metrics.append(ContextualRecallMetric(threshold=0.7, model=JUDGE))
    assert_test(test_case, metrics)
