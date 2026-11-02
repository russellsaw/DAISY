"""Flight recorder: a per-conversation, append-only event log, and helpers to read it back.

One JSON Lines file per conversation in data/conversations/<conversation_id>.jsonl (eval runs write to
data/conversations/evals/<run>/<case_id>.jsonl). Every turn appends, in time order:

  user_turn       the customer's message (after PII redaction) and what was removed
  routing         orchestrator: intent, extracted parameters, commitments, readback reply, profile after
  readback        read back / confirmed / corrections
  retrieval       query, product filter, every hit with title, URL, publisher, as-of date, score, cited?
  engine          rules-engine input profile, criteria_version and every product's result
  counterfactual  every counterfactual engine run (lever, candidate, verdict, headroom) and the paths
  draft           the reply as drafted, before review
  compliance      verdict, issues, unsupported numbers, output-filter and lexicon actions
  handoff         the banker lead card
  defence         which defence layers acted
  final           the reply the customer saw, sources, timings

Everything written passes through the PII redactor once more, so an NRIC or card number the redactor recognises
can't reach disk even if some agent detail carried one. Phone numbers and emails aren't redacted. Logs are local, git-ignored, and contain customer
conversations: treat them as such.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import config, pii

TYPES = {
    "PII guard": "input_filter",
    "Input guardrail": "input_guard",
    "Orchestrator": "routing",
    "Readback": "readback",
    "Product Knowledge": "retrieval",
    "Eligibility": "engine",
    "Counterfactual": "counterfactual",
    "Compliance": "compliance",
    "Handoff": "handoff",
    "Boundary": "boundary",
    "Drafting": "drafting",
    "Defence layers": "defence",
}

CONVERSATIONS_DIR = config.DATA_DIR / "conversations"


def directory(state) -> Path:
    return Path(state.recording_dir) if state.recording_dir else CONVERSATIONS_DIR


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")


def record_turn(state, *, started_at: float, user_msg: str, pii_found: list, draft: str, draft_agent: str,
                result, finished_at: float) -> Path:
    """Append one turn's events to the conversation's log. Returns the log path."""
    events: list[dict] = []

    def add(etype: str, agent: str, summary: str, data, at: float) -> None:
        events.append({"_ts": at, "conversation_id": state.conversation_id, "turn": state.turn_count, "at": _iso(at),
                       "t_ms": round((at - started_at) * 1000), "type": etype, "agent": agent,
                       "summary": summary, "data": data})

    if state.turn_count == 1:
        add("conversation_start", "DAISY", "conversation started", {"started_at": _iso(started_at)}, started_at - 1e-6)
    add("user_turn", "Customer", user_msg[:140], {"message": user_msg, "pii_removed": pii_found}, started_at)
    compliance_at = None
    for ev in result.trace:
        if ev.agent == "Compliance" and compliance_at is None:
            compliance_at = ev.at
        add(TYPES.get(ev.agent, ev.agent.lower().replace(" ", "_")), ev.agent, ev.summary, ev.detail, ev.at)
    add("draft", draft_agent, "reply as drafted, before review", {"text": draft},
        (compliance_at or finished_at) - 1e-4)
    add("final", "DAISY", result.response[:140], {
        "response": result.response, "intent": result.intent, "sources": result.sources,
        "readback": result.readback, "engine_ran": result.engine_ran, "confirmed": result.confirmed,
        "corrections": result.corrections, "handoff_card": result.handoff_card,
        "counterfactual_dates": [x["earliest_date"] for cf in result.counterfactuals for x in cf["paths"]
                                 if x["earliest_date"]],
        "elapsed_s": round(finished_at - started_at, 2),
    }, finished_at)
    events.sort(key=lambda e: e.pop("_ts"))

    folder = directory(state)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{state.conversation_id}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for seq, event in enumerate(events):
            event["seq"] = seq
            # Last line of defence: no NRIC/FIN or card number the patterns catch.
            f.write(json.dumps(pii.redact_values(event), ensure_ascii=False,
                               default=lambda o: pii.redact(str(o))[0]) + "\n")
    return path


def load(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def list_conversations(folder: Path) -> list[dict]:
    """Newest first: id, path, start time, number of turns, first customer message."""
    out = []
    for path in sorted(Path(folder).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            events = load(path)
        except (OSError, json.JSONDecodeError):
            continue
        first = next((e for e in events if e["type"] == "user_turn"), None)
        out.append({"id": path.stem, "path": path, "started": events[0]["at"][:19] if events else "",
                    "turns": max((e["turn"] for e in events), default=0),
                    "first_message": (first or {}).get("data", {}).get("message", "")})
    return out


def eval_runs(base: Path = CONVERSATIONS_DIR / "evals") -> list[Path]:
    return sorted((p for p in base.glob("*") if p.is_dir()), reverse=True) if base.exists() else []
