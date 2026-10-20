"""Deterministic eligibility engine.

No LLM here. Pure functions over CustomerProfile -> EligibilityResult.
Every result carries `computations` (the working) and `sources` so the
narrating agent can show its maths and cite where the rule came from.

Status values:
  eligible        - passes every check we can run
  not_eligible    - fails at least one hard check
  need_more_info  - a required field is missing; `missing_fields` lists them
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..state import CustomerProfile
from .criteria import CHECKED_ON, MAS, PRODUCTS


@dataclass
class EligibilityResult:
    product: str
    product_name: str
    status: str
    reasons: list[str] = field(default_factory=list)
    computations: dict = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    caveat: str = (
        "Indicative only, based on the information you have shared. "
        "Final approval is subject to the bank's credit assessment."
    )

    def to_dict(self) -> dict:
        return asdict(self)


def _mas(key):
    return MAS[key]["value"]


def monthly_instalment(principal: float, annual_rate: float, years: int) -> float:
    """Standard amortising instalment."""
    if principal <= 0 or years <= 0:
        return 0.0
    r = annual_rate / 12
    n = years * 12
    if r == 0:
        return principal / n
    return principal * r / (1 - (1 + r) ** (-n))


def _assessable_income(p: CustomerProfile) -> tuple[float, str]:
    """Apply the variable-income haircut where it applies."""
    income = p.monthly_income or 0.0
    if p.employment_type in ("self_employed", "variable"):
        haircut = _mas("variable_income_haircut")
        return income * (1 - haircut), f"{income:,.0f} x (1 - {haircut:.0%}) [variable income haircut]"
    return income, f"{income:,.0f}"


def _income_check(p: CustomerProfile, product_key: str, result: EligibilityResult) -> bool:
    prod = PRODUCTS[product_key]
    thresholds = prod["min_annual_income"]["value"]
    threshold = thresholds.get(p.residency, thresholds["foreigner"])
    senior = prod.get("senior_income", {}).get("value")
    if senior and p.age >= senior["from_age"] and p.residency in senior["residency"]:
        threshold = senior["min_annual_income"]
    annual = (p.monthly_income or 0) * 12
    result.computations["annual_income"] = round(annual, 2)
    result.computations["min_annual_income_for_residency"] = threshold
    if annual >= threshold:
        result.reasons.append(
            f"Annual income S${annual:,.0f} meets the minimum of S${threshold:,.0f} for {p.residency}."
        )
        return True
    result.reasons.append(
        f"Annual income S${annual:,.0f} is below the minimum of S${threshold:,.0f} for {p.residency}."
    )
    return False


def _age_check(p: CustomerProfile, product_key: str, result: EligibilityResult) -> bool:
    prod = PRODUCTS[product_key]
    min_age = prod["min_age"]["value"]
    max_age = prod.get("max_age", {}).get("value")
    result.computations["min_age"] = min_age
    if p.age < min_age:
        result.reasons.append(f"Age {p.age} is below the minimum age of {min_age}.")
        return False
    if max_age and p.age > max_age:
        result.computations["max_age"] = max_age
        result.reasons.append(f"Age {p.age} is above the maximum age of {max_age}.")
        return False
    result.reasons.append(f"Age {p.age} meets the minimum age of {min_age}.")
    return True


def _unsecured_cap_check(p: CustomerProfile, result: EligibilityResult, requested: float | None = None) -> bool:
    cap_months = _mas("unsecured_borrowing_cap_months")
    cap = (p.monthly_income or 0) * cap_months
    result.computations["unsecured_borrowing_cap"] = round(cap, 2)
    result.computations["unsecured_cap_months"] = cap_months
    if p.existing_unsecured_balance is None:
        result.reasons.append(
            f"Industry-wide unsecured borrowing limit is {cap_months}x monthly income "
            f"(S${cap:,.0f}); existing unsecured balance not provided, so headroom not checked."
        )
        return True
    headroom = cap - p.existing_unsecured_balance
    result.computations["unsecured_headroom"] = round(headroom, 2)
    if headroom <= 0:
        result.reasons.append(
            f"Existing unsecured balance S${p.existing_unsecured_balance:,.0f} already meets or exceeds "
            f"the {cap_months}x income limit of S${cap:,.0f}."
        )
        return False
    if requested and requested > headroom:
        result.reasons.append(
            f"Requested S${requested:,.0f} exceeds remaining unsecured headroom of S${headroom:,.0f}."
        )
        return False
    result.reasons.append(f"Remaining unsecured headroom: S${headroom:,.0f}.")
    return True


def _simple_unsecured(p: CustomerProfile, product_key: str, requested: float | None = None) -> EligibilityResult:
    prod = PRODUCTS[product_key]
    result = EligibilityResult(product_key, prod["name"], "need_more_info", sources=[prod["source"]])
    required = ["age", "residency", "monthly_income"]
    missing = p.missing(required)
    if missing:
        result.missing_fields = missing
        return result
    ok = _age_check(p, product_key, result)
    ok = _income_check(p, product_key, result) and ok
    ok = _unsecured_cap_check(p, result, requested) and ok
    result.status = "eligible" if ok else "not_eligible"
    return result


def check_personal_loan(p: CustomerProfile) -> EligibilityResult:
    return _simple_unsecured(p, "personal_loan", p.personal_loan_amount)


def check_cashline(p: CustomerProfile) -> EligibilityResult:
    return _simple_unsecured(p, "cashline")


def check_credit_card(p: CustomerProfile) -> EligibilityResult:
    prod = PRODUCTS["credit_card"]
    result = EligibilityResult("credit_card", prod["name"], "need_more_info", sources=[prod["source"]])
    missing = p.missing(["age", "residency", "monthly_income"])
    if missing:
        result.missing_fields = missing
        return result
    ok = _age_check(p, "credit_card", result)
    ok = _income_check(p, "credit_card", result) and ok
    result.status = "eligible" if ok else "not_eligible"
    return result


def check_home_loan(p: CustomerProfile) -> EligibilityResult:
    prod = PRODUCTS["home_loan"]
    result = EligibilityResult("home_loan", prod["name"], "need_more_info", sources=[prod["source"]])
    # property_price is required: without it the LTV cap can't be checked, and "eligible"
    # without an LTV check would overstate the result.
    required = ["age", "residency", "monthly_income", "property_type", "property_price",
                "home_loan_amount", "tenure_years"]
    missing = p.missing(required)
    if missing:
        result.missing_fields = missing
        return result

    ok = _age_check(p, "home_loan", result)
    ptype = p.property_type if p.property_type in ("hdb", "ec", "private") else "private"
    n_loans = min(p.existing_housing_loans or 0, 2)
    existing_debt = p.existing_monthly_debt or 0.0
    loan = p.home_loan_amount

    # --- HDB buyer residency ---
    allowed = _mas("hdb_buyer_residency")
    if ptype == "hdb" and p.residency not in allowed:
        result.reasons.append(
            "HDB flats can only be bought by a Singapore Citizen or PR household; a foreigner cannot buy one on "
            "their own (a Singapore Citizen buying with a non-resident spouse is a separate case for a banker)."
        )
        ok = False

    # --- Tenure ---
    max_tenure = _mas("max_tenure")[ptype]
    result.computations["max_tenure_years"] = max_tenure
    if p.tenure_years > max_tenure:
        result.reasons.append(f"Tenure {p.tenure_years}y exceeds the maximum of {max_tenure}y for {ptype.upper()}.")
        ok = False

    # --- LTV ---
    ltv_threshold = _mas("ltv_tenure_threshold")[ptype]
    age_at_end = p.age + p.tenure_years
    reduced = p.tenure_years > ltv_threshold or age_at_end > _mas("ltv_age_limit")
    ltv_limit = _mas("ltv")[n_loans]["reduced" if reduced else "full"]
    result.computations.update({
        "existing_housing_loans": n_loans,
        "age_at_loan_end": age_at_end,
        "ltv_tier": "reduced" if reduced else "full",
        "ltv_limit": ltv_limit,
    })
    if reduced:
        result.reasons.append(
            f"Reduced LTV of {ltv_limit:.0%} applies (tenure over {ltv_threshold}y or loan ending past age "
            f"{_mas('ltv_age_limit')} — ends at age {age_at_end})."
        )
    else:
        result.reasons.append(f"LTV limit of {ltv_limit:.0%} applies with {n_loans} existing housing loan(s).")
    max_loan = p.property_price * ltv_limit
    result.computations["max_loan_by_ltv"] = round(max_loan, 2)
    if loan > max_loan:
        result.reasons.append(
            f"Requested S${loan:,.0f} exceeds the LTV cap of S${max_loan:,.0f} "
            f"({ltv_limit:.0%} of S${p.property_price:,.0f})."
        )
        ok = False
    else:
        result.reasons.append(f"Requested S${loan:,.0f} is within the LTV cap of S${max_loan:,.0f}.")

    # --- Instalment at stress rate ---
    stress = _mas("stress_rate")
    instalment = monthly_instalment(loan, stress, p.tenure_years)
    income, income_note = _assessable_income(p)
    result.computations.update({
        "stress_rate": stress,
        "stressed_monthly_instalment": round(instalment, 2),
        "assessable_monthly_income": round(income, 2),
        "assessable_income_working": income_note,
    })
    if income <= 0:
        result.reasons.append("No assessable income.")
        result.status = "not_eligible"
        return result

    # --- MSR (HDB / EC only) ---
    if ptype in ("hdb", "ec"):
        msr = instalment / income
        cap = _mas("msr_cap")
        result.computations.update({"msr": round(msr, 4), "msr_cap": cap})
        if msr > cap:
            result.reasons.append(f"MSR {msr:.1%} exceeds the {cap:.0%} cap (S${instalment:,.0f} / S${income:,.0f}).")
            ok = False
        else:
            result.reasons.append(f"MSR {msr:.1%} is within the {cap:.0%} cap (S${instalment:,.0f} / S${income:,.0f}).")

    # --- TDSR ---
    tdsr = (instalment + existing_debt) / income
    cap = _mas("tdsr_cap")
    result.computations.update({"existing_monthly_debt": existing_debt, "tdsr": round(tdsr, 4), "tdsr_cap": cap})
    if tdsr > cap:
        result.reasons.append(
            f"TDSR {tdsr:.1%} exceeds the {cap:.0%} cap ((S${instalment:,.0f} + S${existing_debt:,.0f}) / S${income:,.0f})."
        )
        ok = False
    else:
        result.reasons.append(
            f"TDSR {tdsr:.1%} is within the {cap:.0%} cap ((S${instalment:,.0f} + S${existing_debt:,.0f}) / S${income:,.0f})."
        )

    result.status = "eligible" if ok else "not_eligible"
    return result


CHECKS = {
    "home_loan": check_home_loan,
    "credit_card": check_credit_card,
    "personal_loan": check_personal_loan,
    "cashline": check_cashline,
}


def assess_all(p: CustomerProfile, products: list[str] | None = None) -> list[EligibilityResult]:
    keys = products or list(CHECKS)
    return [CHECKS[k](p) for k in keys if k in CHECKS]


def rules_summary() -> list[dict]:
    """Flat list of every criterion (with its evidence) for the Evals and Knowledge pages."""
    rows = []
    for key, item in MAS.items():
        rows.append({"scope": "Regulatory", "criterion": key, "value": str(item["value"]),
                     "verified": item["verified"], "evidence": item.get("evidence", ""),
                     "source": item["source"], "checked_on": item.get("checked_on", "")})
    for prod in PRODUCTS.values():
        for key, item in prod.items():
            if isinstance(item, dict) and "verified" in item:
                rows.append({"scope": prod["name"], "criterion": key, "value": str(item["value"]),
                             "verified": item["verified"], "evidence": item.get("evidence", ""),
                             "source": prod["source"], "checked_on": CHECKED_ON})
    return rows
