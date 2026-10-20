"""Compliance agent: reviews every draft before the customer sees it. Has veto.

Two layers:
  1. Deterministic number check — any number in the draft that does not appear
     in the sources, the engine computations, the profile or the customer's own
     messages is flagged. Flags are evidence for the reviewer, not auto-fail.
  2. LLM review against the six rules in prompts.COMPLIANCE, returning
     approve / revise / handoff plus the issues found.
"""
from __future__ import annotations

import re

from .. import llm
from ..state import ConversationState
from . import prompts

_NUM = re.compile(r"(?<![\w\[])(\d[\d,]*(?:\.\d+)?)\s*(%?)")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "handoff"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string", "enum": ["advice_boundary", "grounding", "approval_language",
                                                        "scope", "fair_dealing", "prompt_injection"]},
                    "detail": {"type": "string"},
                },
                "required": ["rule", "detail"],
            },
        },
        "revised_response": {"type": "string", "description": "Required when verdict is revise; the full corrected customer-facing response."},
    },
    "required": ["verdict", "issues"],
}

HANDOFF_FALLBACK = (
    "I want to make sure you get accurate information on this, so rather than guess I'd suggest speaking "
    "with a DBS banker who can look at your specific situation. I'm happy to prepare a summary of what "
    "we've discussed to pass along, or to answer any other questions about DBS home loans, cards or personal loans."
)


def _mentions(text: str) -> set[tuple[str, bool]]:
    """(number, is_percentage) pairs, commas stripped."""
    return {(n.replace(",", "").rstrip("."), bool(pct)) for n, pct in _NUM.findall(text or "")}


def _numbers(text: str) -> set[str]:
    return {n for n, _ in _mentions(text)}


def unsupported_numbers(draft: str, allowed_text: str, computations: dict, profile: dict, user_text: str) -> list[str]:
    allowed = _numbers(allowed_text) | _numbers(user_text)
    for v in list(computations.values()) + list(profile.values()):
        if isinstance(v, (int, float)):
            allowed |= {str(v), f"{v:.0f}", f"{v:.1f}", f"{v:.2f}", f"{v * 100:.0f}", f"{v * 100:.1f}"}
    flagged = []
    for n, is_pct in _mentions(draft):
        if n in allowed:
            continue
        try:
            val = float(n)
        except ValueError:
            continue
        # Small integers are usually list markers, ages, years or citation refs — but never skip a
        # percentage: an invented "3% p.a." is exactly the hallucination this check exists for.
        if val < 32 and val == int(val) and not is_pct:
            continue
        flagged.append(f"{n}%" if is_pct else n)
    return sorted(set(flagged))


def review(state: ConversationState, user_msg: str, intent: str, draft: str, *,
           sources_text: str = "", computations: dict | None = None, injection_suspected: bool = False) -> dict:
    computations = computations or {}
    flagged = unsupported_numbers(draft, sources_text, computations, state.profile.to_dict(), user_msg)

    context = (
        f"Intent classified by orchestrator: {intent}\n"
        f"Injection suspected by orchestrator: {injection_suspected}\n"
        f"Numbers in draft NOT found in sources/computations/customer input: {flagged or 'none'}\n\n"
        f"Customer profile:\n{state.profile.describe()}\n\n"
        f"Customer's latest message:\n{user_msg}\n\n"
        f"Sources available to the drafting agent:\n{sources_text[:6000] or '(none)'}\n\n"
        f"Engine computations available:\n{computations or '(none)'}\n\n"
        f"DRAFT RESPONSE:\n{draft}"
    )
    try:
        verdict = llm.structured(prompts.COMPLIANCE, context, REVIEW_SCHEMA, max_tokens=8000,
                                 effort="medium")
    except llm.Refusal as exc:  # a reviewer that won't review fails closed
        verdict = {"verdict": "handoff",
                   "issues": [{"rule": "fair_dealing", "detail": f"reviewer model declined ({exc})"}]}

    revision_flags: list[str] = []
    if verdict["verdict"] == "approve":
        final = draft
    elif verdict["verdict"] == "revise" and verdict.get("revised_response"):
        final = verdict["revised_response"]
        # The rewrite goes straight to the customer, so it gets the same number check. Numbers the
        # reviewer kept from the draft were already judged; a NEW unsupported number is not allowed.
        revision_flags = [n for n in unsupported_numbers(final, sources_text, computations,
                                                         state.profile.to_dict(), user_msg)
                          if n not in flagged]
        if revision_flags:
            final = HANDOFF_FALLBACK
            verdict["verdict"] = "handoff"
            verdict.setdefault("issues", []).append(
                {"rule": "grounding", "detail": f"reviewer's revision introduced unsupported numbers {revision_flags}"})
    else:
        final = HANDOFF_FALLBACK
        verdict["verdict"] = "handoff"

    record = {
        "verdict": verdict["verdict"],
        "issues": verdict.get("issues", []),
        "unsupported_numbers": flagged,
        "revision_unsupported_numbers": revision_flags,
        "original_draft": draft if verdict["verdict"] != "approve" else None,
    }
    state.add_trace(
        "Compliance",
        f"{verdict['verdict'].upper()}  issues={len(record['issues'])}  unsupported_numbers={flagged or '-'}",
        record,
    )
    return {"final": final, "record": record}
