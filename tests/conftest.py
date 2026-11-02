"""Shared test setup: the flight recorder never writes into the real data/conversations/ during tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

EVALS = Path(__file__).resolve().parent / "evals"


def pytest_ignore_collect(collection_path, config):
    """tests/evals is the DeepEval build loop: it calls the Claude API, so plain `pytest` skips it.
    It runs only when named, e.g. `deepeval test run tests/evals/test_daisy.py`."""
    if collection_path == EVALS and not any(
            str(Path(arg.split("::")[0]).resolve()).startswith(str(EVALS)) for arg in config.args):
        return True
    return None


@pytest.fixture(autouse=True)
def _recorder_to_tmp(tmp_path, monkeypatch):
    from src import recorder

    monkeypatch.setattr(recorder, "CONVERSATIONS_DIR", tmp_path / "conversations")
