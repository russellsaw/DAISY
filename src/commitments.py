"""Turn the orchestrator's extraction of a commitment into profile data — dates by code, not by the model.

The model only reports what the customer said (a verbatim quote plus the year / month / "in N
months" it contains). The date itself is computed here, and only when:
  - the quote really appears in the customer's message (verbatim, case-insensitive),
  - it is a statement, not a hypothetical ("assume", "if", "suppose", "let's say", ...),
  - it actually contains a time expression, and
  - the numbers the model extracted appear in that quote.
Anything else yields no date. An uncertain statement ("maybe", "not sure", "around") about a
commitment's end clears any date stored earlier, so a contradiction can never leave a stale date.
"""
from __future__ import annotations

import calendar
import re
from datetime import date

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "eighteen": 18, "twenty-four": 24}
_HYPOTHETICAL = re.compile(r"\b(assume|assuming|suppose|supposing|pretend|imagine|let'?s say|what if|if)\b")
_UNCERTAIN = re.compile(r"\b(maybe|perhaps|not sure|unsure|around|about|roughly|probably|i think|i guess|might)\b")
_TIME = re.compile(r"\d|\b(" + "|".join(_MONTHS) + r")[a-z]*\b|\b(" + "|".join(_NUMBER_WORDS) + r"|a|half a)\s+(year|month)s?\b")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("’", "'")).strip()


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def resolve(item: dict, user_msg: str, today: date) -> dict:
    """Extraction item -> profile commitment update. See module docstring for the rules."""
    out: dict = {"kind": item.get("kind") or "commitment", "monthly": item.get("monthly_payment")}
    quote = _norm(item.get("end_quote") or "")
    if not quote:
        return out  # the customer didn't talk about an end date: leave any stored date alone
    if quote not in _norm(user_msg):
        return out  # not the customer's words: ignore the extraction
    if _HYPOTHETICAL.search(quote) or not _TIME.search(quote):
        return out  # "assume it's paid off" is a what-if, never a date
    if _UNCERTAIN.search(quote):
        out.update(end_date=None, end_quote=item.get("end_quote"))  # contradiction / doubt clears the date
        return out

    end: date | None = None
    n = item.get("ends_in_months")
    year = item.get("end_year")
    if n and 1 <= int(n) <= 600 and (str(int(n)) in quote or
                                     any(w in quote for w, v in _NUMBER_WORDS.items() if v == int(n)) or
                                     (int(n) == 12 and re.search(r"\ba year\b", quote)) or
                                     (int(n) == 6 and "half a year" in quote)):
        end = add_months(today, int(n))
    elif year and str(int(year)) in quote:
        month = int(item.get("end_month") or 12)
        if not 1 <= month <= 12:
            return out
        last_day = calendar.monthrange(int(year), month)[1]
        day = int(item.get("end_day") or last_day)  # month-only -> last day of that month (never earlier)
        end = date(int(year), month, min(max(day, 1), last_day))
    if end is None or end <= today:
        return out
    out.update(end_date=end.isoformat(), end_quote=item.get("end_quote"))
    return out
