"""'How much can I borrow?': the largest home loan the forward engine passes.

Like the counterfactual search, this never solves a rule backwards: it runs the same forward check on
different loan amounts. Two rates, because the answer depends on which one is used: the floor MAS sets (what
the eligibility check uses) and the rate DBS's planning tools currently assume (docs/CALCULATOR_CHECK.md).
Every line the customer sees is written here by code, not by a model.
"""
from __future__ import annotations

import copy

from ..state import CustomerProfile
from .counterfactual import RULE_LABELS
from .criteria import DBS_PLANNER_RATE, MAS, PRODUCTS, criteria_version
from .engine import check_home_loan

MIN_TENURE = 5  # the shortest tenure DBS's planning tools offer
_NO_PRICE = 1e12  # with no price given the loan-to-value limit can't bind; it's reported as a percentage instead
_FAILED = ("exceeds", "is below", "is above", "can only", "No assessable")


def default_tenure(p: CustomerProfile) -> int:
    """The longest tenure that keeps the full loan-to-value limit: below the property type's threshold and ending by
    the age limit. This is also the tenure DBS's planner offers (25y for an HDB flat at 32, 15y for a condo at 50)."""
    ptype = p.property_type if p.property_type in ("hdb", "ec", "private") else "private"
    return max(min(MAS["ltv_tenure_threshold"]["value"][ptype], MAS["ltv_age_limit"]["value"] - p.age), MIN_TENURE)


def largest_loan(p: CustomerProfile, tenure: int, rate: float) -> dict:
    """Largest loan (to the dollar) the forward engine passes at this rate, and which rule stops it going higher."""
    price = p.property_price or _NO_PRICE

    def run(amount: int):
        q = copy.deepcopy(p)
        q.home_loan_amount, q.property_price, q.tenure_years = float(amount), price, tenure
        return check_home_loan(q, stress_rate=rate)

    first = run(1)
    if first.status != "eligible":
        return {"rate": rate, "max_loan": None, "blocked_by": [c["rule"] for c in first.checks if not c["passed"]],
                "reasons": [r for r in first.reasons if any(word in r for word in _FAILED)]}
    # Zero interest and all of income is more than any rule allows, so this bound always fails or equals the price.
    lo, hi = 1, int(min(price, (p.monthly_income or 0) * 12 * tenure + 1))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if run(mid).status == "eligible" else (lo, mid - 1)
    at_max, above = run(lo), run(lo + 1)
    c = at_max.computations
    limit = next((x["rule"] for x in above.checks if not x["passed"]), None)
    return {"rate": rate, "max_loan": lo, "limit_rule": limit, "limit_label": RULE_LABELS.get(limit, limit),
            "limit_cap": next((x["threshold"] for x in above.checks if x["rule"] == limit), None),
            "instalment": c["stressed_monthly_instalment"], "ltv_limit": c["ltv_limit"],
            "assessable_income": c["assessable_monthly_income"], "assumptions": at_max.assumptions,
            "sources": at_max.sources}


def find(p: CustomerProfile) -> dict:
    tenure = p.tenure_years or default_tenure(p)
    floor = largest_loan(p, tenure, MAS["stress_rate"]["value"])
    planner = largest_loan(p, tenure, DBS_PLANNER_RATE["value"])
    assumptions = floor.get("assumptions") or planner.get("assumptions") or []
    sources = list(dict.fromkeys((floor.get("sources") or [PRODUCTS["home_loan"]["source"]])
                                 + [DBS_PLANNER_RATE["source"]]))
    return {"tenure_years": tenure, "tenure_stated": bool(p.tenure_years), "property_price": p.property_price,
            "at_floor": floor, "at_planner": planner, "assumptions": assumptions, "sources": sources,
            "criteria_version": criteria_version()}


def numbers_in(result: dict) -> dict:
    """Every number the reply can contain, for the code filter that checks numbers trace to the engine."""
    out = {"tenure": result["tenure_years"], "price": result["property_price"]}
    for key in ("at_floor", "at_planner"):
        at = result[key]
        for field in ("rate", "max_loan", "instalment", "ltv_limit", "limit_cap", "assessable_income"):
            out[f"{key}.{field}"] = at.get(field)
        ltv, loan = at.get("ltv_limit"), at.get("max_loan")
        if ltv and loan:
            out[f"{key}.price"] = loan / ltv
            out[f"{key}.cap"] = (result["property_price"] or 0) * ltv
    return {k: v for k, v in out.items() if isinstance(v, (int, float))}


def _money(v: float) -> str:
    return f"S${v:,.0f}"


def _rate_line(at: dict, label: str) -> str:
    cap = f"{at['limit_cap']:.0%} of income" if at.get("limit_rule") in ("msr", "tdsr") else ""
    limit = f"The limit reached is {at['limit_label']}{', ' + cap if cap else ''}." if at.get("limit_rule") else ""
    return (f"- **{_money(at['max_loan'])}** at the {at['rate'] * 100:g}% {label}. The test instalment is "
            f"{_money(at['instalment'])} a month. {limit}".rstrip())


def text(result: dict) -> str:
    """The customer's reply, written by code."""
    floor, planner = result["at_floor"], result["at_planner"]
    if floor["max_loan"] is None and planner["max_loan"] is None:
        reasons = "\n".join(f"- {r}" for r in dict.fromkeys(floor["reasons"] + planner["reasons"]))
        return ("On the details you checked, I can't show a maximum home loan, because no amount meets today's criteria:"
                f"\n\n{reasons}\n\nThis is an indicative check, not an approval. A DBS banker can look at your full "
                "situation.")
    parts = [f"Here's your indicative maximum home loan over {result['tenure_years']} years. It depends on the "
             "interest rate used in the test, so here are both. This is not an approval.", ""]
    for at, label in ((floor, "floor MAS sets for residential property loans"),
                      (planner, "DBS's planning tools currently assume")):
        if at["max_loan"] is not None:
            parts.append(_rate_line(at, label))
    parts.append("")
    known = [at for at in (planner, floor) if at["max_loan"] is not None]
    ltv = known[0]["ltv_limit"]
    if result["property_price"]:
        parts.append(f"**Property price:** the loan-to-value limit of {ltv:.0%} caps the loan at "
                     f"{_money(result['property_price'] * ltv)} on a {_money(result['property_price'])} property.")
    else:
        prices = sorted({_money(at["max_loan"] / at["ltv_limit"]) for at in known})
        parts.append(f"**Property price:** the loan also can't be more than {ltv:.0%} of the price, so "
                     f"{' to '.join(prices)} is the most these loans would cover at that limit.")
    parts.append(f"**Tenure:** {result['tenure_years']} years, "
                 + ("the tenure you gave." if result["tenure_stated"]
                    else "the longest that keeps the full loan-to-value limit. A different tenure changes these figures."))
    if result["assumptions"]:
        parts += ["", "**Things you didn't mention:**"] + [f"- {a}" for a in result["assumptions"]]
    parts += ["", "A banker's figure may differ, because it depends on the bank's full credit assessment and the package "
                  "rate. To check a specific loan, tell me the property price and the amount you'd borrow."]
    return "\n".join(parts)
