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

from .. import config, llm
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

# Hard-banned on eligibility replies, in any form (even "you're not approved"): eligibility is
# indicative, so the reply must never use approval vocabulary. Enforced here in code, not by prompt.
BANNED_PHRASES = ["will be approved", "approved", "guaranteed", "you will qualify", "you'll qualify",
                  "you'll get", "confirmed"]
_BANNED = re.compile(
    r"(?<![\w])(" + "|".join(re.escape(p).replace("'", "['\u2019]") for p in sorted(BANNED_PHRASES, key=len, reverse=True))
    + r")(?![\w])", re.IGNORECASE)


def banned_phrases(text: str) -> list[str]:
    return sorted({m.group(0).lower().replace("\u2019", "'") for m in _BANNED.finditer(text or "")})


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
            allowed |= {str(v), f"{v:.0f}", f"{v:.1f}", f"{v:.2f}", f"{v * 100:.0f}", f"{v * 100:.1f}",
                        f"{v * 100:.2f}", f"{v / 12:.0f}", f"{v * 12:.0f}"}
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
           sources_text: str = "", computations: dict | None = None, injection_suspected: bool = False,
           deterministic: bool = False) -> dict:
    """deterministic=True: the draft was written by code from a fixed template (e.g. the readback), so the
    LLM reviewer is skipped; the code-level filters (traceability, lexicon) still run."""
    computations = computations or {}
    flagged = unsupported_numbers(draft, sources_text, computations, state.profile.to_dict(), user_msg)

    context = (
        f"Intent classified by orchestrator: {intent}\n"
        f"Injection suspected by orchestrator: {injection_suspected}\n"
        f"Numbers in draft NOT found in sources/computations/customer input: {flagged or 'none'}\n\n"
        f"Customer profile:\n{state.profile.describe()}\n\n"
        f"Customer's latest message:\n{user_msg}\n\n"
        f"Sources available to the drafting agent:\n{sources_text[:12000] or '(none)'}\n\n"
        f"Engine computations available:\n{computations or '(none)'}\n\n"
        f"DRAFT RESPONSE:\n{draft}"
    )
    try:
        if deterministic:
            verdict = {"verdict": "approve", "issues": [], "deterministic_template": True}
        else:
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

    output_filter = {"untraceable": [], "action": None}
    if final != HANDOFF_FALLBACK:
        final, output_filter = _enforce_traceable(final, sources_text, computations, state, user_msg)
        if output_filter["action"] == "replaced":
            verdict["verdict"] = "handoff"
            verdict.setdefault("issues", []).append(
                {"rule": "grounding", "detail": f"untraceable numbers survived a rewrite: {output_filter['untraceable']}"})

    lexicon = {"found": [], "action": None}
    if intent == "eligibility" and final != HANDOFF_FALLBACK:
        final, lexicon = _enforce_lexicon(final, sources_text, computations, state, user_msg, flagged)
        if lexicon["action"] == "replaced":
            verdict["verdict"] = "handoff"
            verdict.setdefault("issues", []).append(
                {"rule": "approval_language", "detail": f"banned phrases survived a rewrite: {lexicon['found']}"})

    record = {
        "verdict": verdict["verdict"],
        "issues": verdict.get("issues", []),
        "unsupported_numbers": flagged,
        "revision_unsupported_numbers": revision_flags,
        "lexicon": lexicon,
        "output_filter": output_filter,
        "reviewer": "code template (LLM review skipped)" if deterministic else "llm",
        "original_draft": draft if (verdict["verdict"] != "approve" or lexicon["action"] or output_filter["action"])
        else None,
    }
    state.add_trace(
        "Compliance",
        f"{verdict['verdict'].upper()}  issues={len(record['issues'])}  unsupported_numbers={flagged or '-'}"
        + (f"  lexicon={lexicon['found']}→{lexicon['action']}" if lexicon["found"] else "")
        + (f"  untraceable={output_filter['untraceable']}→{output_filter['action']}"
           if output_filter["untraceable"] else ""),
        record,
    )
    return {"final": final, "record": record}


def _enforce_lexicon(text: str, sources_text: str, computations: dict, state: ConversationState, user_msg: str,
                     already_flagged: list[str]) -> tuple[str, dict]:
    """Hard fail on banned approval vocabulary: one constrained rewrite, re-checked in code;
    if anything banned (or any new unsupported number) survives, the reply becomes the handoff."""
    found = banned_phrases(text)
    if not found:
        return text, {"found": [], "action": None}
    try:
        rewritten = llm.chat(prompts.LEXICON_REWRITE,
                             [{"role": "user", "content": f"Banned phrases found: {found}\n\nRESPONSE:\n{text}"}],
                             model=config.MODEL_FAST, max_tokens=4096)
    except llm.Refusal:
        rewritten = ""
    new_numbers = [n for n in unsupported_numbers(rewritten, sources_text, computations, state.profile.to_dict(),
                                                  user_msg) if n not in already_flagged]
    if rewritten and not banned_phrases(rewritten) and not new_numbers:
        return rewritten, {"found": found, "action": "rewritten"}
    return HANDOFF_FALLBACK, {"found": found, "action": "replaced"}


def _enforce_traceable(text: str, sources_text: str, computations: dict, state: ConversationState,
                       user_msg: str) -> tuple[str, dict]:
    """Hard constraint: every number in the reply must come from the engine, the customer or a retrieved
    page. Anything else gets one rewrite that removes it (re-checked in code), else the handoff."""
    untraceable = unsupported_numbers(text, sources_text, computations, state.profile.to_dict(), user_msg)
    if not untraceable:
        return text, {"untraceable": [], "action": None}
    try:
        rewritten = llm.chat(prompts.NUMBER_REWRITE,
                             [{"role": "user", "content": f"Numbers to remove: {untraceable}\n\nRESPONSE:\n{text}"}],
                             model=config.MODEL_FAST, max_tokens=4096)
    except llm.Refusal:
        rewritten = ""
    if rewritten and not unsupported_numbers(rewritten, sources_text, computations, state.profile.to_dict(), user_msg):
        return rewritten, {"untraceable": untraceable, "action": "rewritten"}
    return HANDOFF_FALLBACK, {"untraceable": untraceable, "action": "replaced"}
