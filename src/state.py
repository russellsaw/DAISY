"""Conversation state shared across agents.

CustomerProfile is filled progressively as the customer talks. The rules
engine reads from it; the UI side panel displays it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

# field -> (friendly label, description shown to the model for extraction)
PROFILE_FIELDS: dict[str, tuple[str, str]] = {
    "age": ("Age", "Customer's age in years (integer)."),
    "residency": ("Residency", "One of: citizen, pr, foreigner."),
    "monthly_income": ("Monthly income (S$)", "Gross MONTHLY income in SGD. If the customer gives an annual figure, divide by 12."),
    "employment_type": ("Employment", "One of: salaried, self_employed, variable, unemployed."),
    "existing_monthly_debt": ("Other monthly debt (S$)", "Monthly repayments the customer gave only as a total, NOT itemised as a commitment."),
    "existing_housing_loans": ("Existing housing loans", "Number of outstanding housing loans (integer, 0 if none)."),
    "property_type": ("Property type", "One of: hdb, ec, private."),
    "property_price": ("Property price (S$)", "Purchase price of the property."),
    "home_loan_amount": ("Home loan amount (S$)", "Housing loan amount the customer wants for the property."),
    "personal_loan_amount": ("Personal loan amount (S$)", "Amount the customer wants to borrow as a personal loan / instant cash."),
    "tenure_years": ("Tenure (years)", "Desired loan tenure in years (integer)."),
    "existing_unsecured_balance": ("Existing unsecured balance (S$)", "Outstanding balance across all unsecured credit (cards, personal loans, credit lines)."),
    "commitments": ("Existing commitments", "Itemised monthly commitments (car loan, student loan, ...), each with an end date only if the customer stated one."),
}

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

    def total_monthly_debt(self) -> float:
        """Every monthly obligation the TDSR numerator counts."""
        return (self.existing_monthly_debt or 0.0) + sum(c.get("monthly") or 0.0 for c in self.commitments)

    def update(self, updates: dict[str, Any]) -> list[str]:
        """Apply extracted updates; returns the list of fields that changed."""
        changed = []
        updates = dict(updates or {})
        if "loan_amount" in updates and "home_loan_amount" not in updates:  # pre-split field name
            updates["home_loan_amount"] = updates.pop("loan_amount")
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
            if key in ("monthly_income", "existing_monthly_debt", "property_price",
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


@dataclass
class ConversationState:
    messages: list[dict] = field(default_factory=list)  # [{"role": "user"|"assistant", "content": str}]
    profile: CustomerProfile = field(default_factory=CustomerProfile)
    trace: list[TraceEvent] = field(default_factory=list)
    handoff_card: Optional[dict] = None
    last_sources: list[dict] = field(default_factory=list)
    last_eligibility: list[dict] = field(default_factory=list)
    last_counterfactuals: list[dict] = field(default_factory=list)
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

    def recent_history(self, max_turns: int) -> list[dict]:
        """Last N exchanges, always starting on a user turn (the API requires it)."""
        msgs = self.messages[-max_turns * 2:]
        while msgs and msgs[0]["role"] != "user":
            msgs = msgs[1:]
        return msgs

    def transcript(self, max_turns: int = 20) -> str:
        return "\n".join(
            f"{'Customer' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in self.recent_history(max_turns)
        )
