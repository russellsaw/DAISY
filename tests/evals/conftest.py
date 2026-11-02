"""DeepEval build-loop setup (calls the Claude API; never part of the offline `pytest -q` run).

- Telemetry off unless already configured (DeepEval also reads DEEPEVAL_TELEMETRY_OPT_OUT from .env).
- DeepEval's Anthropic integration: importing deepeval.anthropic patches the SDK's messages.create for this
  run only, so src/llm.py is unchanged. Spans are viewable only on Confident AI (not used); locally, every
  golden's full trace is DAISY's own flight-recorder log, written to data/conversations/deepeval/<run>/.
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")

import deepeval.anthropic  # noqa: E402,F401  (Anthropic integration: auto-instruments messages.create)
import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daisy_deepeval import ROOT  # noqa: E402


def pytest_configure(config):
    # Set once in the controller; pytest-xdist workers inherit it, so one run writes one log folder.
    os.environ.setdefault("DAISY_DEEPEVAL_RUN", time.strftime("%Y%m%d-%H%M%S"))


@pytest.fixture(scope="session")
def run_dir() -> Path:
    path = ROOT / "data" / "conversations" / "deepeval" / os.environ["DAISY_DEEPEVAL_RUN"]
    path.mkdir(parents=True, exist_ok=True)
    return path
