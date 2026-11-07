"""Deterministic PII guard. Runs on every customer message before any agent sees it.

The assistant never needs an NRIC/FIN, a card number, a phone number or an email address, so they are removed from
the message rather than trusted to a prompt instruction: the redacted text is what gets stored in the conversation,
sent to the model, put on the lead card and logged. Not caught: names, addresses, passport and bank account numbers,
and phone numbers from outside Singapore.
"""

from __future__ import annotations

import re

_NRIC = re.compile(r"\b[STFGM]\d{7}[A-Z]\b", re.IGNORECASE)
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# A Singapore number: 8 digits starting 6 (landline), 8 or 9 (mobile), optionally +65, with at most one space or hyphen
# in the middle. Never touching a "$", a comma, a decimal point or another group of digits, so amounts like S$90,000,000
# or 380000 and grouped reference numbers stay. A bare 8-digit amount with no "$" (98765432) would be removed.
_PHONE = re.compile(
    r"(?<![\w$.,+])(?<!\d\s)(?<!\d-)"
    r"(?:\+?65[\s-]?)?[689]\d{3}[\s-]?\d{4}"
    r"(?![\w,]|\.\d|[\s-]\d)"
)


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

    def _sub(kind: str, label: str):
        def replace(_m: re.Match) -> str:
            found.append(kind)
            return f"[{label} removed]"

        return replace

    text = _NRIC.sub(_nric, text)
    text = _CARD.sub(_card, text)
    text = _EMAIL.sub(_sub("email", "email"), text)  # before phone numbers, so digits in an address aren't one
    text = _PHONE.sub(_sub("phone", "phone number"), text)
    return text, sorted(set(found))
