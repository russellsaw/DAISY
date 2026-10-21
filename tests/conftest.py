"""Shared test setup: the flight recorder never writes into the real data/conversations/ during tests."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def _recorder_to_tmp(tmp_path, monkeypatch):
    from src import recorder

    monkeypatch.setattr(recorder, "CONVERSATIONS_DIR", tmp_path / "conversations")
