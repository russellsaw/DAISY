"""Print the rules engine's numbers for the personas in docs/CALCULATOR_CHECK.md, so they can be compared
with DBS's own online calculators by hand.

    python scripts/calculator_check.py

No API calls. For each persona it runs the same forward engine DAISY uses (src/rules/engine.py):
  - status and the binding checks (LTV, MSR, TDSR) for the requested loan
  - the monthly instalment at the stress rate (compare with DBS's Home Loan Repayment Calculator at that rate)
  - the largest loan the engine passes, found by searching the loan amount with the forward engine
    (src/rules/max_loan.py, which also answers "how much can I borrow?" in the app)
    (compare with the maximum loan from DBS's MyHome Planner)
The maximum loan is also shown at 4.65%, the medium-term rate DBS's planning tools currently use, while MAS's
rule (and the engine) use a 4% floor.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.rules import engine  # noqa: E402
from src.rules.criteria import DBS_PLANNER_RATE, MAS  # noqa: E402
from src.rules.max_loan import largest_loan  # noqa: E402
from src.state import CustomerProfile  # noqa: E402

BASE = {
    "age": 32,
    "residency": "pr",
    "monthly_income": 7000,
    "employment_type": "salaried",
    "existing_housing_loans": 0,
    "existing_monthly_debt": 0,
}
CAR = {"kind": "car loan", "monthly": 600, "end_date": None, "end_quote": ""}

PERSONAS = [
    (
        "P1",
        "Demo persona: first HDB flat",
        {**BASE, "property_type": "hdb", "property_price": 550000, "home_loan_amount": 380000, "tenure_years": 25},
    ),
    (
        "P2",
        "Same, borrowing S$400k",
        {**BASE, "property_type": "hdb", "property_price": 550000, "home_loan_amount": 400000, "tenure_years": 25},
    ),
    (
        "P3",
        "Same person, S$1M condo, S$600/month car loan",
        {
            **BASE,
            "property_type": "private",
            "property_price": 1000000,
            "home_loan_amount": 700000,
            "tenure_years": 30,
            "commitments": [CAR],
        },
    ),
    (
        "P4",
        "Lower income, first HDB flat",
        {
            **BASE,
            "monthly_income": 4500,
            "property_type": "hdb",
            "property_price": 550000,
            "home_loan_amount": 380000,
            "tenure_years": 25,
        },
    ),
    (
        "P5",
        "Age 50, private property, loan ending past 65",
        {
            **BASE,
            "age": 50,
            "residency": "citizen",
            "monthly_income": 12000,
            "property_type": "private",
            "property_price": 1200000,
            "home_loan_amount": 800000,
            "tenure_years": 25,
        },
    ),
]


def max_loan_at(rate: float, fields: dict) -> float | None:
    p = CustomerProfile(**fields)
    return largest_loan(p, p.tenure_years, rate)["max_loan"]


def money(x: float | None) -> str:
    return "none" if x is None else f"S${x:,.0f}"


def main() -> None:
    stress = MAS["stress_rate"]["value"]
    planner = DBS_PLANNER_RATE["value"]
    print(
        f"| # | Persona | Status | Instalment at {stress:.0%} | MSR | TDSR | LTV cap | Max loan at {stress:.0%} "
        f"| Max loan at {planner:.2%} |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for pid, label, fields in PERSONAS:
        r = engine.check_home_loan(CustomerProfile(**fields))
        c = r.computations
        msr = f"{c['msr']:.1%}" if "msr" in c else "n/a"
        tdsr = f"{c['tdsr']:.1%}" if "tdsr" in c else "n/a"
        print(
            f"| {pid} | {label} | {r.status} | S${c.get('stressed_monthly_instalment', 0):,.2f} | {msr} | {tdsr} "
            f"| {c['ltv_limit']:.0%} ({money(c['max_loan_by_ltv'])}) | {money(max_loan_at(stress, fields))} "
            f"| {money(max_loan_at(planner, fields))} |"
        )
    print("\nInputs:")
    for pid, _, f in PERSONAS:
        debt = CustomerProfile(**f).total_monthly_debt()
        print(
            f"  {pid}: age {f['age']}, {f['residency']}, S${f['monthly_income']:,}/month, {f['property_type']} "
            f"S${f['property_price']:,}, loan S${f['home_loan_amount']:,} over {f['tenure_years']}y, "
            f"other debt S${debt:,.0f}/month"
        )


if __name__ == "__main__":
    main()
