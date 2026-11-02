"""Deterministic PII guard. Runs on every customer message before any agent sees it.

The assistant never needs an NRIC/FIN or a card number, so they are removed from the
message rather than trusted to a prompt instruction: the redacted text is what gets
stored in the conversation, sent to the model, put on the lead card and logged.
"""

from __future__ import annotations

import re

_NRIC = re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE)
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def redact_values(value):
    """Redact every string inside a JSON-like value. Numbers and structure are left alone, so a long number
    can't be mistaken for a card and leave the line unreadable."""
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {k: redact_values(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_values(v) for v in value]
    return value


def redact(text: str) -> tuple[str, list[str]]:
    """Return (redacted_text, kinds_found)."""
    found: list[str] = []

    def _nric(_m: re.Match) -> str:
        found.append("nric")
        return "[NRIC removed]"

    def _card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append("card_number")
            return "[card number removed]"
        return m.group(0)

    text = _NRIC.sub(_nric, text)
    text = _CARD.sub(_card, text)
    return text, sorted(set(found))
