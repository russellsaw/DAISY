"""Compliance agent: reviews every draft before the customer sees it. Has veto.

In order:
  1. Deterministic number check — any number in the draft that does not appear in the sources, the
     engine computations, the profile or the customer's own messages is flagged, as evidence for step 2.
  2. LLM review against the six rules in prompts.COMPLIANCE, returning approve / revise / handoff plus
     the issues found.
  3. Code filters on the outcome: untraceable numbers and, on eligibility replies, approval vocabulary.
"""

from __future__ import annotations

import re

from .. import config, llm
from ..state import ConversationState
from . import prompts

_NUM = re.compile(r"(?<![\w\[])(\d[\d,]*(?:\.\d+)?)\s*(%|per\s?cent\b)?", re.IGNORECASE)
# The "3" in "3-5%" or "3 to 5%" is a percentage too.
_PCT_RANGE = re.compile(
    r"(?<![\w\[])(\d+(?:\.\d+)?)\s*(?:-|\u2013|to)\s*\d+(?:\.\d+)?\s*(?:%|per\s?cent\b)", re.IGNORECASE
)
_MONEY = re.compile(r"\$\s*(\d[\d,]*(?:\.\d+)?)")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise", "handoff"]},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {
                        "type": "string",
                        "enum": [
                            "advice_boundary",
                            "grounding",
                            "approval_language",
                            "scope",
                            "fair_dealing",
                            "prompt_injection",
                        ],
                    },
                    "detail": {"type": "string"},
                },
                "required": ["rule", "detail"],
            },
        },
        "revised_response": {
            "type": "string",
            "description": "Required when verdict is revise; the full corrected customer-facing response.",
        },
    },
    "required": ["verdict", "issues"],
}

# Hard-banned on replies about the customer's own chances (eligibility and advice), in any form (even "you're not
# approved"): eligibility is indicative, so the reply must never use approval vocabulary. Enforced here in code, not
# by prompt. Product answers may say "you'll get 3% cashback".
BANNED_PHRASES = [
    "will be approved",
    "approved",
    "guaranteed",
    "you will qualify",
    "you'll qualify",
    "you'll get",
    "confirmed",
]
_BANNED = re.compile(
    r"(?<![\w])("
    + "|".join(re.escape(p).replace("'", "['\u2019]") for p in sorted(BANNED_PHRASES, key=len, reverse=True))
    + r")(?![\w])",
    re.IGNORECASE,
)


def banned_phrases(text: str) -> list[str]:
    return sorted({m.group(0).lower().replace("\u2019", "'") for m in _BANNED.finditer(text or "")})


HANDOFF_FALLBACK = (
    "I want to make sure you get accurate information on this, so rather than guess I'd suggest speaking "
    "with a DBS banker who can look at your specific situation. I'm happy to prepare a summary of what "
    "we've discussed to pass along, or to answer any other questions about DBS home loans, cards or personal loans."
)


APPENDED_LABEL = (
    "TEXT THE SYSTEM APPENDS AFTER THE REPLY (written by code from verified criteria; not part of the draft and not "
    "under review: don't flag it or repeat it):"
)
_SAME_START = 48  # characters: a line starting like a code-written line is a copy of it, even if its end was reworded
_LABEL = re.compile(r"^(.{3,40}?:)\s")  # "A note on this check: ...": a label before a colon near the start


def _without_appended(text: str, appended: str) -> str:
    """A rewrite can copy in the code-written text the reviewer was shown, sometimes reworded, sometimes with the label
    that introduced it. The pipeline appends that text itself, so the customer would read it twice, or read our prompt.
    Lines that start like a code-written line are taken out, and the label with them."""
    starts = {line.strip()[:_SAME_START] for line in appended.splitlines() if len(line.strip()) >= _SAME_START}
    # A code-written line that opens with a label ("A note on this check:", "Next step:") owns that label: a rewrite
    # once paraphrased the rate note under it, so the customer read two notes that said different things.
    labels = tuple({m[1] for line in appended.splitlines() if (m := _LABEL.match(line.strip()))})

    def copied(line: str) -> bool:
        line = line.strip()
        return (len(line) >= _SAME_START and line[:_SAME_START] in starts) or bool(labels and line.startswith(labels))

    kept = [line for line in text.replace(APPENDED_LABEL, "").splitlines() if not copied(line)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _mentions(text: str) -> set[tuple[str, bool]]:
    """(number, is_percentage) pairs, commas stripped."""
    text = text or ""
    return {(n.replace(",", "").rstrip("."), bool(pct)) for n, pct in _NUM.findall(text)} | {
        (n, True) for n in _PCT_RANGE.findall(text)
    }


def _traced_forms(key: str, value: float) -> tuple[set[str], set[str]]:
    """How an engine or profile value may appear in a reply: (as a plain number, as a percentage). Only what it can
    honestly be written as. A wider rule let "3% p.a." through because 32 (an age) / 12 rounds to 3."""
    plain = {str(value), f"{value:.0f}", f"{value:.1f}", f"{value:.2f}"}
    pct: set[str] = set()
    if isinstance(value, float) and 0 < abs(value) < 10:  # a ratio or rate: 0.2865 is written 28.65%
        pct = {f"{value * 100:.0f}", f"{value * 100:.1f}", f"{value * 100:.2f}"}
    if key.endswith("_pp"):  # already in percentage points
        pct |= plain
    if "income" in key:  # the only figure quoted both monthly and yearly
        plain |= {f"{value * 12:.0f}", f"{value / 12:.0f}"}
    return plain | pct, pct


def unsupported_numbers(draft: str, allowed_text: str, computations: dict, profile: dict, user_text: str) -> list[str]:
    mentions = _mentions(allowed_text) | _mentions(user_text)
    allowed = {n for n, _ in mentions}
    # A percentage must trace to a percentage: "3 years" on a page doesn't support "3% p.a.".
    allowed_pct = {n for n, is_pct in mentions if is_pct}
    for key, v in list(computations.items()) + list(profile.items()):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            plain, pct = _traced_forms(str(key), v)
            allowed |= plain
            allowed_pct |= pct
    money = {n.replace(",", "") for n in _MONEY.findall(draft or "")}
    flagged = []
    for n, is_pct in _mentions(draft):
        if n in (allowed_pct if is_pct else allowed):
            continue
        try:
            val = float(n)
        except ValueError:
            continue
        # Small integers are usually list markers, ages, years or citation refs — but never skip a
        # percentage or an amount of money: an invented "3% p.a." or "S$25 fee" is exactly the
        # hallucination this check exists for.
        if val < 32 and val == int(val) and not is_pct and n not in money:
            continue
        flagged.append(f"{n}%" if is_pct else n)
    return sorted(set(flagged))


def review(
    state: ConversationState,
    user_msg: str,
    intent: str,
    draft: str,
    *,
    sources_text: str = "",
    computations: dict | None = None,
    injection_suspected: bool = False,
    deterministic: bool = False,
    appended: str = "",
) -> dict:
    """deterministic=True: the draft was written by code from a fixed template (e.g. the readback), so the
    LLM reviewer is skipped; the code-level filters (traceability, lexicon) still run.
    appended: code-written text that will follow the reply. The reviewer sees it, so it can tell what the
    customer will read in full, and doesn't flag the draft for leaving those lines out or write them in."""
    computations = computations or {}
    flagged = unsupported_numbers(draft, sources_text, computations, state.profile.to_dict(), user_msg)

    context = (
        f"Intent classified by orchestrator: {intent}\n"
        f"Injection suspected by orchestrator: {injection_suspected}\n"
        f"Numbers in draft NOT found in sources/computations/customer input: {flagged or 'none'}\n\n"
        f"Customer profile:\n{state.profile.describe()}\n\n"
        f"Customer's latest message:\n{user_msg}\n\n"
        f"Sources available to the drafting agent:\n{sources_text[:16000] or '(none)'}\n\n"
        f"Engine computations available:\n{computations or '(none)'}\n\n"
        f"DRAFT RESPONSE:\n{draft}"
        + (
            f"\n\n{APPENDED_LABEL}\n{appended}"
            if appended
            else ""
        )
    )
    try:
        if deterministic:
            verdict = {"verdict": "approve", "issues": [], "deterministic_template": True}
        else:
            verdict = llm.structured(prompts.COMPLIANCE, context, REVIEW_SCHEMA, max_tokens=8000, effort="medium")
    except (llm.Refusal, RuntimeError, ValueError) as exc:
        # A reviewer that won't review, or whose verdict is cut off or malformed, fails closed. An API outage isn't
        # caught here: the chat page rolls the turn back so the customer can try again.
        verdict = {
            "verdict": "handoff",
            "issues": [{"rule": "fair_dealing", "detail": f"reviewer gave no usable verdict ({exc})"}],
        }

    revision_flags: list[str] = []
    if verdict["verdict"] == "approve":
        final = draft
    elif verdict["verdict"] == "revise" and verdict.get("revised_response"):
        final = _without_appended(verdict["revised_response"], appended)
        # The rewrite goes straight to the customer, so it gets the same number check. Numbers the
        # reviewer kept from the draft were already judged; a NEW unsupported number is not allowed.
        revision_flags = [
            n
            for n in unsupported_numbers(final, sources_text, computations, state.profile.to_dict(), user_msg)
            if n not in flagged
        ]
        if revision_flags:
            final = HANDOFF_FALLBACK
            verdict["verdict"] = "handoff"
            verdict.setdefault("issues", []).append(
                {"rule": "grounding", "detail": f"reviewer's revision introduced unsupported numbers {revision_flags}"}
            )
    else:
        final = HANDOFF_FALLBACK
        verdict["verdict"] = "handoff"

    output_filter = {"untraceable": [], "action": None}
    if final != HANDOFF_FALLBACK:
        final, output_filter = _enforce_traceable(final, sources_text, computations, state, user_msg)
        if output_filter["action"] == "replaced":
            verdict["verdict"] = "handoff"
            verdict.setdefault("issues", []).append(
                {
                    "rule": "grounding",
                    "detail": f"untraceable numbers survived a rewrite: {output_filter['untraceable']}",
                }
            )

    lexicon = {"found": [], "action": None}
    if intent in ("eligibility", "advice_seeking") and final != HANDOFF_FALLBACK:
        final, lexicon = _enforce_lexicon(final, sources_text, computations, state, user_msg, flagged)
        if lexicon["action"] == "replaced":
            verdict["verdict"] = "handoff"
            verdict.setdefault("issues", []).append(
                {"rule": "approval_language", "detail": f"banned phrases survived a rewrite: {lexicon['found']}"}
            )

    record = {
        "verdict": verdict["verdict"],
        "issues": verdict.get("issues", []),
        "unsupported_numbers": flagged,
        "revision_unsupported_numbers": revision_flags,
        "lexicon": lexicon,
        "output_filter": output_filter,
        "reviewer": "code template (LLM review skipped)" if deterministic else "llm",
        "original_draft": draft
        if (verdict["verdict"] != "approve" or lexicon["action"] or output_filter["action"])
        else None,
    }
    state.add_trace(
        "Compliance",
        f"{verdict['verdict'].upper()}  issues={len(record['issues'])}  unsupported_numbers={flagged or '-'}"
        + (f"  lexicon={lexicon['found']}→{lexicon['action']}" if lexicon["found"] else "")
        + (
            f"  untraceable={output_filter['untraceable']}→{output_filter['action']}"
            if output_filter["untraceable"]
            else ""
        ),
        record,
    )
    return {"final": final, "record": record}


def _enforce_lexicon(
    text: str,
    sources_text: str,
    computations: dict,
    state: ConversationState,
    user_msg: str,
    already_flagged: list[str],
) -> tuple[str, dict]:
    """Hard fail on banned approval vocabulary: one constrained rewrite, re-checked in code;
    if anything banned (or any new unsupported number) survives, the reply becomes the handoff."""
    found = banned_phrases(text)
    if not found:
        return text, {"found": [], "action": None}
    try:
        rewritten = llm.chat(
            prompts.LEXICON_REWRITE,
            [{"role": "user", "content": f"Banned phrases found: {found}\n\nRESPONSE:\n{text}"}],
            model=config.MODEL_FAST,
            max_tokens=4096,
        )
    except llm.Refusal:
        rewritten = ""
    new_numbers = [
        n
        for n in unsupported_numbers(rewritten, sources_text, computations, state.profile.to_dict(), user_msg)
        if n not in already_flagged
    ]
    if rewritten and not banned_phrases(rewritten) and not new_numbers:
        return rewritten, {"found": found, "action": "rewritten"}
    # Last resort before the handoff: the forward-looking promises have a safe conditional equivalent,
    # so swap them in code rather than throw a correct explanation away over two words. "approved",
    # "guaranteed" and "confirmed" have no meaning-preserving swap, so they still end in the handoff.
    substituted = _SAFE_SWAP.sub(lambda m: _SAFE_WORDING[m.group(0).lower().replace("\u2019", "'")], text)
    if not banned_phrases(substituted):
        return substituted, {"found": found, "action": "substituted"}
    return HANDOFF_FALLBACK, {"found": found, "action": "replaced"}


_SAFE_WORDING = {
    "you will qualify": "you would meet today's criteria",
    "you'll qualify": "you would meet today's criteria",
    "you'll get": "you would get",
}
_SAFE_SWAP = re.compile(r"(?<![\w])(you will qualify|you['\u2019]ll qualify|you['\u2019]ll get)(?![\w])", re.IGNORECASE)


def _enforce_traceable(
    text: str, sources_text: str, computations: dict, state: ConversationState, user_msg: str
) -> tuple[str, dict]:
    """Hard constraint: every number in the reply must come from the engine, the customer or a retrieved
    page. Anything else gets one rewrite that removes it (re-checked in code), else the handoff."""
    untraceable = unsupported_numbers(text, sources_text, computations, state.profile.to_dict(), user_msg)
    if not untraceable:
        return text, {"untraceable": [], "action": None}
    try:
        rewritten = llm.chat(
            prompts.NUMBER_REWRITE,
            [{"role": "user", "content": f"Numbers to remove: {untraceable}\n\nRESPONSE:\n{text}"}],
            model=config.MODEL_FAST,
            max_tokens=4096,
        )
    except llm.Refusal:
        rewritten = ""
    if rewritten and not unsupported_numbers(rewritten, sources_text, computations, state.profile.to_dict(), user_msg):
        return rewritten, {"untraceable": untraceable, "action": "rewritten"}
    return HANDOFF_FALLBACK, {"untraceable": untraceable, "action": "replaced"}
