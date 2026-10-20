"""Conversation state shared across agents.

CustomerProfile is filled progressively as the customer talks. The rules
engine reads from it; the UI side panel displays it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# field -> (friendly label, description shown to the model for extraction)
PROFILE_FIELDS: dict[str, tuple[str, str]] = {
    "age": ("Age", "Customer's age in years (integer)."),
    "residency": ("Residency", "One of: citizen, pr, foreigner."),
    "monthly_income": ("Monthly income (S$)", "Gross MONTHLY income in SGD. If the customer gives an annual figure, divide by 12."),
    "employment_type": ("Employment", "One of: salaried, self_employed, variable, unemployed."),
    "existing_monthly_debt": ("Existing monthly debt (S$)", "Total monthly repayments on other loans/cards."),
    "existing_housing_loans": ("Existing housing loans", "Number of outstanding housing loans (integer, 0 if none)."),
    "property_type": ("Property type", "One of: hdb, ec, private."),
    "property_price": ("Property price (S$)", "Purchase price of the property."),
    "home_loan_amount": ("Home loan amount (S$)", "Housing loan amount the customer wants for the property."),
    "personal_loan_amount": ("Personal loan amount (S$)", "Amount the customer wants to borrow as a personal loan / instant cash."),
    "tenure_years": ("Tenure (years)", "Desired loan tenure in years (integer)."),
    "existing_unsecured_balance": ("Existing unsecured balance (S$)", "Outstanding balance across all unsecured credit (cards, personal loans, credit lines)."),
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

    def update(self, updates: dict[str, Any]) -> list[str]:
        """Apply extracted updates; returns the list of fields that changed."""
        changed = []
        updates = dict(updates or {})
        if "loan_amount" in updates and "home_loan_amount" not in updates:  # pre-split field name
            updates["home_loan_amount"] = updates.pop("loan_amount")
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

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    def missing(self, fields: list[str]) -> list[str]:
        return [f for f in fields if getattr(self, f) is None]

    def describe(self) -> str:
        if not self.to_dict():
            return "(nothing known yet)"
        return "\n".join(f"- {PROFILE_FIELDS[k][0]}: {v}" for k, v in self.to_dict().items())


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

    def add_trace(self, agent: str, summary: str, detail: Any = None) -> None:
        self.trace.append(TraceEvent(agent, summary, detail))

    def recent_history(self, max_turns: int) -> list[dict]:
        return self.messages[-max_turns * 2:]

    def transcript(self, max_turns: int = 20) -> str:
        return "\n".join(
            f"{'Customer' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in self.recent_history(max_turns)
        )
