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
from .criteria import CHECKED_ON, MAS, PRODUCTS, criteria_version


@dataclass
class EligibilityResult:
    product: str
    product_name: str
    status: str
    reasons: list[str] = field(default_factory=list)
    computations: dict = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    # One entry per rule evaluated: {rule, value, threshold, kind, passed}. kind is "ratio"
    # (value <= threshold, a fraction), "max" (value <= threshold), "min" (value >= threshold) or
    # "rule" (yes/no). The counterfactual search reads these; it never re-implements a rule.
    checks: list[dict] = field(default_factory=list)
    # What the check took as read because the customer didn't say, so the reply can say so.
    assumptions: list[str] = field(default_factory=list)
    criteria_version: str = field(default_factory=criteria_version)
    caveat: str = (
        "Indicative only, based on the information you have shared. "
        "Final approval is subject to the bank's credit assessment."
    )

    def to_dict(self) -> dict:
        return asdict(self)


# Fields each check needs before it can run. Shared with the eligibility agent, which uses it to ask for
# missing details and to build the readback WITHOUT calling the engine (no engine call
# until the customer has confirmed the parameters).
# home_loan needs property_price: without it the LTV cap can't be checked, and "eligible" without an
# LTV check would overstate the result.
REQUIRED_FIELDS = {
    "home_loan": [
        "age",
        "residency",
        "monthly_income",
        "property_type",
        "property_price",
        "home_loan_amount",
        "tenure_years",
    ],
    "personal_loan": ["age", "residency", "monthly_income"],
    "cashline": ["age", "residency", "monthly_income"],
    "credit_card": ["age", "residency", "monthly_income"],
    # "How much can I borrow?" (src/rules/max_loan.py) needs no loan amount, price or tenure.
    "max_home_loan": ["age", "residency", "monthly_income", "property_type"],
}


def missing_fields(p: CustomerProfile, product: str) -> list[str]:
    """REQUIRED_FIELDS, plus what MSR needs: someone with an existing housing loan must give its repayment."""
    missing = p.missing(REQUIRED_FIELDS[product])
    if product in ("home_loan", "max_home_loan"):
        if p.existing_housing_loans and p.existing_property_loan_monthly is None and not p.property_loan_monthly():
            missing.append("existing_property_loan_monthly")
        if p.commitments_missing_amount:  # TDSR can't be worked out with a repayment unknown
            missing.append("commitments_missing_amount")
    return missing


def _mas(key):
    return MAS[key]["value"]


def _check(result: "EligibilityResult", rule: str, value, threshold, kind: str, passed: bool) -> bool:
    result.checks.append({"rule": rule, "value": value, "threshold": threshold, "kind": kind, "passed": bool(passed)})
    return bool(passed)


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
    if p.employment_type in ("self_employed", "variable", "unemployed"):  # income not from a fixed salary
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
    if annual < threshold:
        result.computations["income_shortfall_annual"] = round(threshold - annual, 2)
        result.computations["income_shortfall_monthly"] = round((threshold - annual) / 12, 2)
    if _check(result, "min_annual_income", round(annual, 2), threshold, "min", annual >= threshold):
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
    if not _check(result, "min_age", p.age, min_age, "min", p.age >= min_age):
        result.reasons.append(f"Age {p.age} is below the minimum age of {min_age}.")
        return False
    if max_age and not _check(result, "max_age", p.age, max_age, "max", p.age <= max_age):
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
        result.assumptions.append(
            "Existing unsecured balances weren't mentioned, so they aren't counted. "
            "Any such balance would reduce the amount available under the limit."
        )
        # The balance is unknown, but a request above the whole limit fails whatever the balance is.
        if requested:
            utilisation = requested / cap if cap > 0 else float("inf")
            within = _check(result, "unsecured_utilisation", round(utilisation, 4), 1.0, "ratio", requested <= cap)
            if not within:
                result.reasons.append(
                    f"Requested S${requested:,.0f} exceeds the industry-wide unsecured borrowing "
                    f"limit of {cap_months}x monthly income (S${cap:,.0f})."
                )
                return False
        result.reasons.append(
            f"Industry-wide unsecured borrowing limit is {cap_months}x monthly income "
            f"(S${cap:,.0f}); existing unsecured balance not provided, so remaining headroom not checked."
        )
        return True
    headroom = cap - p.existing_unsecured_balance
    result.computations["unsecured_headroom"] = round(headroom, 2)
    utilisation = (p.existing_unsecured_balance + (requested or 0)) / cap if cap > 0 else float("inf")
    _check(
        result,
        "unsecured_utilisation",
        round(utilisation, 4),
        1.0,
        "ratio",
        headroom > 0 and not (requested and requested > headroom),
    )
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
    result = EligibilityResult(
        product_key,
        prod["name"],
        "need_more_info",
        sources=[prod["source"], MAS["unsecured_borrowing_cap_months"]["source"]],
    )
    missing = missing_fields(p, product_key)
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
    missing = missing_fields(p, "credit_card")
    if missing:
        result.missing_fields = missing
        return result
    ok = _age_check(p, "credit_card", result)
    ok = _income_check(p, "credit_card", result) and ok
    result.status = "eligible" if ok else "not_eligible"
    return result


def check_home_loan(p: CustomerProfile, stress_rate: float | None = None) -> EligibilityResult:
    """stress_rate: test the instalment at this rate instead of the MAS floor (src/rules/max_loan.py)."""
    prod = PRODUCTS["home_loan"]
    result = EligibilityResult("home_loan", prod["name"], "need_more_info", sources=[prod["source"]])
    missing = missing_fields(p, "home_loan")
    if missing:
        result.missing_fields = missing
        return result

    ok = _age_check(p, "home_loan", result)
    ptype = p.property_kind
    existing_property = p.property_loan_monthly()
    # A stated mortgage repayment means at least one housing loan, whatever the count says.
    n_loans = min(max(p.existing_housing_loans or 0, int(existing_property > 0)), 2)
    existing_debt = p.total_monthly_debt()
    loan = p.home_loan_amount

    # The pages behind the checks that follow: the product, then the rules applied to it.
    result.sources = [
        prod["source_private"] if ptype == "private" else prod["source"],
        MAS["ltv"]["source"],
        MAS["msr_cap"]["source"],
        MAS["stress_rate"]["source"],
    ]
    if ptype == "hdb":
        result.sources.append(MAS["hdb_buyer_residency"]["source"])
    if p.existing_housing_loans is None and not existing_property:
        result.assumptions.append(
            "Existing housing loans weren't mentioned, so they were treated as none. "
            "Having one would lower the loan-to-value limit."
        )
    if p.employment_type is None:  # the agent asks first; this only shows if the engine is called directly
        result.assumptions.append(
            "Employment wasn't mentioned, so income was counted in full. Self-employed or "
            "variable income would count for less."
        )
    if not existing_debt and p.existing_monthly_debt is None:
        result.assumptions.append(
            "Other monthly repayments weren't mentioned, so they were treated as S$0. "
            "Any such repayment would raise the debt ratios."
        )

    # --- HDB buyer residency ---
    allowed = _mas("hdb_buyer_residency")
    if not _check(
        result, "hdb_buyer_residency", p.residency, allowed, "rule", ptype != "hdb" or p.residency in allowed
    ):
        result.reasons.append(
            "HDB flats can only be bought by a Singapore Citizen or PR household; a foreigner cannot buy one on "
            "their own (a Singapore Citizen buying with a non-resident spouse is a separate case for a banker)."
        )
        ok = False

    # --- Tenure ---
    max_tenure = _mas("max_tenure")[ptype]
    result.computations["max_tenure_years"] = max_tenure
    if not _check(result, "max_tenure", p.tenure_years, max_tenure, "max", p.tenure_years <= max_tenure):
        result.reasons.append(f"Tenure {p.tenure_years}y exceeds the maximum of {max_tenure}y for {ptype.upper()}.")
        ok = False

    if not _check(result, "min_tenure", p.tenure_years, 1, "min", p.tenure_years >= 1):
        result.reasons.append("The loan tenure must be at least 1 year.")
        ok = False
    if not _check(result, "loan_amount", loan, 1, "min", loan >= 1):
        result.reasons.append("The loan amount must be more than zero.")
        ok = False

    # --- LTV ---
    ltv_threshold = _mas("ltv_tenure_threshold")[ptype]
    age_at_end = p.age + p.tenure_years
    reduced = p.tenure_years > ltv_threshold or age_at_end > _mas("ltv_age_limit")
    ltv_limit = _mas("ltv")[n_loans]["reduced" if reduced else "full"]
    result.computations.update(
        {
            "existing_housing_loans": n_loans,
            "age_at_loan_end": age_at_end,
            "ltv_tier": "reduced" if reduced else "full",
            "ltv_limit": ltv_limit,
        }
    )
    if reduced:
        result.reasons.append(
            f"Reduced LTV of {ltv_limit:.0%} applies (tenure over {ltv_threshold}y or loan ending past age "
            f"{_mas('ltv_age_limit')} — ends at age {age_at_end})."
        )
    else:
        result.reasons.append(f"LTV limit of {ltv_limit:.0%} applies with {n_loans} existing housing loan(s).")
    max_loan = p.property_price * ltv_limit
    result.computations["max_loan_by_ltv"] = round(max_loan, 2)
    ltv_ratio = loan / p.property_price if p.property_price else float("inf")
    result.computations["ltv_requested"] = round(ltv_ratio, 4)
    if not _check(result, "ltv", round(ltv_ratio, 4), ltv_limit, "ratio", loan <= max_loan):
        result.reasons.append(
            f"Requested S${loan:,.0f} exceeds the LTV cap of S${max_loan:,.0f} "
            f"({ltv_limit:.0%} of S${p.property_price:,.0f})."
        )
        ok = False
    else:
        result.reasons.append(f"Requested S${loan:,.0f} is within the LTV cap of S${max_loan:,.0f}.")

    # --- Instalment at stress rate ---
    stress = _mas("stress_rate") if stress_rate is None else stress_rate
    instalment = monthly_instalment(loan, stress, p.tenure_years)
    income, income_note = _assessable_income(p)
    result.computations.update(
        {
            "stress_rate": stress,
            "stressed_monthly_instalment": round(instalment, 2),
            "assessable_monthly_income": round(income, 2),
            "assessable_income_working": income_note,
        }
    )
    if not _check(result, "assessable_income", income, 0, "rule", income > 0):
        result.reasons.append("No assessable income.")
        result.status = "not_eligible"
        return result

    # --- MSR (HDB / EC only) ---
    if ptype in ("hdb", "ec"):
        # MAS: "all the borrower's property loans", not only the one being applied for.
        msr = (instalment + existing_property) / income
        cap = _mas("msr_cap")
        working = (
            f"(S${instalment:,.0f} + S${existing_property:,.0f} existing property loans) / S${income:,.0f}"
            if existing_property
            else f"S${instalment:,.0f} / S${income:,.0f}"
        )
        result.computations.update(
            {"msr": round(msr, 5), "msr_cap": cap, "existing_property_loan_monthly": existing_property}
        )
        if not _check(result, "msr", round(msr, 5), cap, "ratio", msr <= cap):
            result.reasons.append(f"MSR {msr:.1%} exceeds the {cap:.0%} cap ({working}).")
            ok = False
        else:
            result.reasons.append(f"MSR {msr:.1%} is within the {cap:.0%} cap ({working}).")

    # --- TDSR ---
    tdsr = (instalment + existing_debt) / income
    cap = _mas("tdsr_cap")
    result.computations.update({"existing_monthly_debt": existing_debt, "tdsr": round(tdsr, 5), "tdsr_cap": cap})
    if not _check(result, "tdsr", round(tdsr, 5), cap, "ratio", tdsr <= cap):
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
    """Flat list of every criterion (with its evidence) for the Knowledge page."""
    rows = []
    for key, item in MAS.items():
        rows.append(
            {
                "scope": "Regulatory",
                "criterion": key,
                "value": str(item["value"]),
                "verified": item["verified"],
                "evidence": item.get("evidence", ""),
                "source": item["source"],
                "checked_on": item.get("checked_on", ""),
            }
        )
    for prod in PRODUCTS.values():
        for key, item in prod.items():
            if isinstance(item, dict) and "verified" in item:
                rows.append(
                    {
                        "scope": prod["name"],
                        "criterion": key,
                        "value": str(item["value"]),
                        "verified": item["verified"],
                        "evidence": item.get("evidence", ""),
                        "source": prod["source"],
                        "checked_on": CHECKED_ON,
                    }
                )
    return rows
