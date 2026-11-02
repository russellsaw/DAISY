"""Conversation state shared across agents.

CustomerProfile is filled progressively as the customer talks. The rules
engine reads from it; the UI side panel displays it.
"""
from __future__ import annotations

import copy
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

# field -> (friendly label, description shown to the model for extraction)
PROFILE_FIELDS: dict[str, tuple[str, str]] = {
    "age": ("Age", "Customer's age in years (integer)."),
    "residency": ("Residency", "One of: citizen, pr, foreigner."),
    "monthly_income": ("Monthly income (S$)", "Gross MONTHLY income in SGD. If the customer gives an annual figure, divide by 12."),
    "employment_type": ("Employment", "One of: salaried, self_employed, variable, unemployed."),
    "existing_monthly_debt": ("Other monthly debt (S$)", "Monthly repayments the customer gave only as a total, NOT itemised as a commitment."),
    "existing_housing_loans": ("Existing housing loans", "Number of outstanding housing loans (integer, 0 if none)."),
    "existing_property_loan_monthly": ("Existing property-loan repayments (S$)", "Total monthly repayment on the customer's existing housing or property loans."),
    "property_type": ("Property type", "One of: hdb, ec, private."),
    "property_price": ("Property price (S$)", "Purchase price of the property."),
    "home_loan_amount": ("Home loan amount (S$)", "Housing loan amount the customer wants for the property."),
    "personal_loan_amount": ("Personal loan amount (S$)", "Amount the customer wants to borrow as a personal loan / instant cash."),
    "tenure_years": ("Tenure (years)", "Desired loan tenure in years (integer)."),
    "existing_unsecured_balance": ("Existing unsecured balance (S$)", "Outstanding balance across all unsecured credit (cards, personal loans, credit lines)."),
    "commitments": ("Existing commitments", "Itemised monthly commitments (car loan, student loan, ...), each with an end date only if the customer stated one."),
}

# Commitments that are property loans count towards MSR as well as TDSR. A renovation loan is not a property loan.
_PROPERTY_LOAN = re.compile(r"home|mortgage|housing|hdb|property|condo|flat", re.IGNORECASE)


def is_property_loan(kind: str | None) -> bool:
    kind = kind or ""
    return bool(_PROPERTY_LOAN.search(kind)) and "renovation" not in kind.lower()


RESIDENCY_ALIASES = {
    "singaporean": "citizen", "singapore citizen": "citizen", "citizen": "citizen", "sc": "citizen",
    "pr": "pr", "permanent resident": "pr", "spr": "pr",
    "foreigner": "foreigner", "foreign": "foreigner", "expat": "foreigner", "ep": "foreigner",
}


@dataclass
class CustomerProfile:
    age: Optional[int] = None
    residency: Optional[str] = None
    monthly_income: Optional[float] = None
    employment_type: Optional[str] = None
    existing_monthly_debt: Optional[float] = None
    existing_housing_loans: Optional[int] = None
    existing_property_loan_monthly: Optional[float] = None
    property_type: Optional[str] = None
    property_price: Optional[float] = None
    home_loan_amount: Optional[float] = None
    personal_loan_amount: Optional[float] = None
    tenure_years: Optional[int] = None
    existing_unsecured_balance: Optional[float] = None
    # [{"kind": "car loan", "monthly": 600.0, "end_date": "2028-04-20" | None, "end_quote": "..."}]
    # end_date is only ever set from the customer's own words (see src/commitments.py).
    commitments: list[dict] = field(default_factory=list)

    def __post_init__(self):
        # Own copies: a profile must never share commitment dicts with the caller (or another profile).
        self.commitments = [dict(c) for c in self.commitments]

    def property_loan_monthly(self) -> float:
        """Repayments on existing property loans: the MSR numerator, besides the loan being applied for.
        The customer may have given them as a total or as a commitment; the larger is used, so one
        mortgage stated both ways isn't counted twice."""
        itemised = sum(c.get("monthly") or 0.0 for c in self.commitments if is_property_loan(c.get("kind")))
        return max(self.existing_property_loan_monthly or 0.0, itemised)

    def total_monthly_debt(self) -> float:
        """Every monthly obligation the TDSR numerator counts."""
        other = sum(c.get("monthly") or 0.0 for c in self.commitments if not is_property_loan(c.get("kind")))
        return (self.existing_monthly_debt or 0.0) + other + self.property_loan_monthly()

    def update(self, updates: dict[str, Any]) -> list[str]:
        """Apply extracted updates; returns the list of fields that changed."""
        changed = []
        updates = dict(updates or {})
        if updates.get("commitments"):
            if self._merge_commitments(updates.pop("commitments")):
                changed.append("commitments")
        updates.pop("commitments", None)
        for key, value in updates.items():
            if key not in self.__dataclass_fields__ or value is None or value == "":
                continue
            if key == "residency" and isinstance(value, str):
                value = RESIDENCY_ALIASES.get(value.strip().lower(), value.strip().lower())
            if key in ("property_type", "employment_type") and isinstance(value, str):
                value = value.strip().lower()
            if key in ("age", "existing_housing_loans", "tenure_years"):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            if key in ("monthly_income", "existing_monthly_debt", "existing_property_loan_monthly", "property_price",
                       "home_loan_amount", "personal_loan_amount", "existing_unsecured_balance"):
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
            if getattr(self, key) != value:
                setattr(self, key, value)
                changed.append(key)
        return changed

    def _merge_commitments(self, incoming: list[dict]) -> bool:
        """Add or update commitments, matched by kind. An incoming item carrying an `end_date` key
        replaces the stored end date (None clears it); one without the key leaves it alone."""
        changed = False
        for item in incoming or []:
            kind = str(item.get("kind") or "commitment").strip().lower()
            existing = next((c for c in self.commitments if c["kind"] == kind), None)
            if existing is None:
                existing = {"kind": kind, "monthly": None, "end_date": None, "end_quote": None}
                self.commitments.append(existing)
                changed = True
            if item.get("monthly") not in (None, ""):
                try:
                    monthly = float(item["monthly"])
                except (TypeError, ValueError):
                    monthly = None
                if monthly is not None and monthly != existing["monthly"]:
                    existing["monthly"] = monthly
                    changed = True
            if "end_date" in item and item["end_date"] != existing["end_date"]:
                existing["end_date"] = item["end_date"]
                existing["end_quote"] = item.get("end_quote")
                changed = True
        self.commitments = [c for c in self.commitments if c.get("monthly")]
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != []}

    def missing(self, fields: list[str]) -> list[str]:
        return [f for f in fields if getattr(self, f) is None]

    def describe(self) -> str:
        if not self.to_dict():
            return "(nothing known yet)"
        lines = []
        for k, v in self.to_dict().items():
            if k == "commitments":
                for c in v:
                    ends = f"ends {c['end_date']} (customer said: \"{c['end_quote']}\")" if c.get("end_date") \
                        else "no end date stated"
                    lines.append(f"- Commitment: {c['kind']}, S${c['monthly']:,.0f}/month, {ends}")
            else:
                lines.append(f"- {PROFILE_FIELDS[k][0]}: {v}")
        return "\n".join(lines)


@dataclass
class TraceEvent:
    agent: str
    summary: str
    detail: Any = None
    at: float = field(default_factory=time.time)  # when it happened (flight recorder timings)


@dataclass
class ConversationState:
    # Flight recorder: one append-only log per conversation, in recording_dir
    # (default data/conversations/). record=False turns it off.
    conversation_id: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    record: bool = True
    recording_dir: Optional[str] = None
    turn_count: int = 0
    messages: list[dict] = field(default_factory=list)  # [{"role": "user"|"assistant", "content": str}]
    profile: CustomerProfile = field(default_factory=CustomerProfile)
    trace: list[TraceEvent] = field(default_factory=list)
    handoff_card: Optional[dict] = None
    last_eligibility: list[dict] = field(default_factory=list)
    last_counterfactuals: list[dict] = field(default_factory=list)
    last_max_loan: Optional[dict] = None     # the last "how much can I borrow?" result (src/rules/max_loan.py)
    pending_max_loan: bool = False           # that question is waiting on missing details or a readback
    # ---- Readback before clearance ----
    # readback_profile: what was last read back to the customer, awaiting a yes or a correction.
    # confirmed_profile: the snapshot the customer said yes to. The profile counts as confirmed only
    # while it still equals that snapshot, so any later change (a correction, a contradiction, new
    # facts) needs a fresh readback before the engine may run again.
    readback_profile: Optional[dict] = None
    confirmed_profile: Optional[dict] = None
    pending_products: list[str] = field(default_factory=list)
    corrections: list[dict] = field(default_factory=list)  # evidence the readback caught an error
    # Optional UI hook: called with each TraceEvent as it happens (the chat page uses it to
    # draw the live agent timeline). Never required for correctness.
    listener: Optional[Callable[[TraceEvent], None]] = field(default=None, repr=False, compare=False)

    def add_trace(self, agent: str, summary: str, detail: Any = None) -> None:
        event = TraceEvent(agent, summary, detail)
        self.trace.append(event)
        if self.listener is not None:
            try:
                self.listener(event)
            except Exception:  # noqa: BLE001 — a UI hook must never break a turn
                pass

    # ---- readback helpers ----
    @property
    def pending_readback(self) -> bool:
        return self.readback_profile is not None

    def is_confirmed(self) -> bool:
        return self.confirmed_profile is not None and self.confirmed_profile == self.profile.to_dict()

    def field_confirmed(self, key: str) -> bool:
        return bool(self.confirmed_profile) and self.confirmed_profile.get(key) == self.profile.to_dict().get(key)

    def start_readback(self, products: list[str]) -> None:
        self.readback_profile = copy.deepcopy(self.profile.to_dict())
        self.pending_products = list(products)

    def confirm(self) -> None:
        self.confirmed_profile = copy.deepcopy(self.profile.to_dict())
        self.readback_profile = None

    def log_corrections(self, reference: dict | None) -> list[dict]:
        """Values the customer had seen read back (or confirmed) and has now changed or withdrawn."""
        if not reference:
            return []
        now = self.profile.to_dict()
        found = []
        for key, before in reference.items():
            after = now.get(key)
            if key == "commitments":
                after_by_kind = {c["kind"]: c for c in after or []}
                for c in before:
                    a = after_by_kind.get(c["kind"], {})
                    for sub in ("monthly", "end_date"):
                        if c.get(sub) != a.get(sub):
                            found.append({"field": f"{c['kind']}.{sub}", "from": c.get(sub), "to": a.get(sub)})
            elif before != after:
                found.append({"field": key, "from": before, "to": after})
        stamp = datetime.now().isoformat(timespec="seconds")
        for f in found:
            f["at"] = stamp
        self.corrections.extend(found)
        return found

    def recent_history(self, max_turns: int) -> list[dict]:
        """Last N exchanges, always starting on a user turn (the API requires it)."""
        msgs = self.messages[-max_turns * 2:]
        while msgs and msgs[0]["role"] != "user":
            msgs = msgs[1:]
        return msgs

    def transcript(self, max_turns: int = 20, before: str | None = None) -> str:
        """before: the message being handled now. It's left out if it's the latest, so a prompt that also states it
        separately doesn't show it twice (the routing model then treats it as old and skips its facts)."""
        history = self.recent_history(max_turns)
        if before is not None and history and history[-1]["role"] == "user" and history[-1]["content"] == before:
            history = history[:-1]
        return "\n".join(f"{'Customer' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in history)
