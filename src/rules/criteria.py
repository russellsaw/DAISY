"""Eligibility criteria — the ground truth the engine computes from.

READ THIS BEFORE THE PITCH
--------------------------
Every entry has:
  verified    True only when the value was checked against the cited page
  source      the page it was checked against (also in the knowledge repository,
              data/sources/text/, so the evidence can be re-read offline)
  evidence    the sentence on that page that states the value, quoted
  checked_on  when it was checked

Values were checked on 2026-10-20 against the live MAS, HDB and DBS pages. Pages change:
re-run `python scripts/ingest.py` before demo day; data/sources/history.jsonl will show
any source whose text changed, and every criterion citing it needs re-checking.
The Evals and Knowledge pages show how many criteria are verified.

The LLM never sees these numbers until the engine has computed with them.
"""

CHECKED_ON = "2026-10-20"

_MAS_LTV = "https://www.mas.gov.sg/regulation/explainers/new-housing-loans/loan-tenure-and-loan-to-value-limits"
_MAS_MSR_TDSR = "https://www.mas.gov.sg/regulation/explainers/new-housing-loans/msr-and-tdsr-rules"
_MAS_CALC_TDSR = "https://www.mas.gov.sg/regulation/explainers/tdsr-for-property-loans/calculating-tdsr"
_DBS_UNSECURED_FAQ = "https://www.dbs.com.sg/personal/cards/credit-cards/faq-revised-credit-card-and-unsecured-credit-rules.page"
_DBS_PL = "https://www.dbs.com.sg/personal/loans/instant-cash/personal-loan"
_DBS_CASHLINE = "https://www.dbs.com.sg/personal/loans/instant-cash/cashline"
_DBS_CARD_ELIG = "https://www.dbs.com.sg/personal/support/card-application-eligibility.html"
_DBS_HDB_LOAN = "https://www.dbs.com.sg/personal/loans/home-loans/hdb-loan"
_DBS_PRIVATE = "https://www.dbs.com.sg/i-bank/loans/homeloans/buy-private-property"
_HDB_FAMILIES = "https://www.hdb.gov.sg/buying-a-flat/flat-grant-and-loan-eligibility/couples-and-families"

# What each cited page is called in a reply's source list.
SOURCE_TITLES = {
    _MAS_LTV: "MAS: Loan Tenure and Loan-to-Value Limits",
    _MAS_MSR_TDSR: "MAS: MSR and TDSR Rules",
    _MAS_CALC_TDSR: "MAS: Calculating TDSR",
    _DBS_UNSECURED_FAQ: "DBS: Credit Card and Unsecured Credit Rules FAQ",
    _DBS_PL: "DBS Personal Loan",
    _DBS_CASHLINE: "DBS Cashline",
    _DBS_CARD_ELIG: "Eligibility for a DBS Credit Card",
    _DBS_HDB_LOAN: "DBS HDB Loan",
    _DBS_PRIVATE: "DBS Private Property Home Loan",
    _HDB_FAMILIES: "HDB: Flat Eligibility for Couples and Families",
}

# Regulatory limits (MAS). Apply across all banks.
MAS = {
    "tdsr_cap": {
        "value": 0.55,
        "verified": True,
        "note": "Total Debt Servicing Ratio cap: all monthly debt obligations / gross monthly income.",
        "source": _MAS_MSR_TDSR,
        "evidence": "A borrower's TDSR should be less than or equal to 55%.",
        "checked_on": CHECKED_ON,
    },
    "msr_cap": {
        "value": 0.30,
        "verified": True,
        "note": "Mortgage Servicing Ratio cap. Applies to HDB flats and Executive Condominiums (MOP not expired) only.",
        "source": _MAS_MSR_TDSR,
        "evidence": "MSR is capped at 30% of a borrower's gross monthly income. It applies only to housing loans "
                    "for the purchase of an HDB flat, or an executive condominium where the minimum occupation "
                    "period of the executive condominium has not expired.",
        "checked_on": CHECKED_ON,
    },
    "stress_rate": {
        "value": 0.04,
        "verified": True,
        "note": "Medium-term interest rate floor used to compute TDSR/MSR instalments (not the actual loan rate). "
                "Banks use the higher of this floor and the package's 'thereafter' rate; the engine uses the floor.",
        "source": _MAS_CALC_TDSR,
        "evidence": "Residential property purchase loans and mortgage equity withdrawal loans: The higher of a 4% "
                    "floor or the thereafter interest rate.",
        "checked_on": CHECKED_ON,
    },
    "variable_income_haircut": {
        "value": 0.30,
        "verified": True,
        "note": "Minimum haircut on variable income (commission, bonus, allowance) and rental income before TDSR/MSR. "
                "The engine applies it to all income of self-employed / variable earners.",
        "source": _MAS_CALC_TDSR,
        "evidence": "FIs are required to apply a minimum haircut of 30% to: Variable income (e.g. commission, "
                    "bonus and allowance). Rental income.",
        "checked_on": CHECKED_ON,
    },
    # Loan-to-Value limits by number of outstanding housing loans.
    # "full" applies when tenure <= the LTV tenure threshold AND loan ends by the age limit;
    # otherwise "reduced" applies.
    "ltv": {
        "value": {
            0: {"full": 0.75, "reduced": 0.55},
            1: {"full": 0.45, "reduced": 0.25},
            2: {"full": 0.35, "reduced": 0.15},  # 2 or more
        },
        "verified": True,
        "note": "LTV limits for individuals' bank housing loans by count of outstanding housing loans "
                "(OTP on or after 6 July 2018).",
        "source": _MAS_LTV,
        "evidence": "None: 75% or 55%. 1: 45% or 25%. 2 or more: 35% or 15%.",
        "checked_on": CHECKED_ON,
    },
    "ltv_tenure_threshold": {
        "value": {"hdb": 25, "ec": 30, "private": 30},
        "verified": True,
        "note": "Tenure above which the reduced LTV applies.",
        "source": _MAS_LTV,
        "evidence": "Apply the lower LTV limit if the loan tenure exceeds 30 years (or 25 years for HDB flats), "
                    "or the loan period extends beyond the borrower's age of 65 years.",
        "checked_on": CHECKED_ON,
    },
    "max_tenure": {
        "value": {"hdb": 30, "ec": 35, "private": 35},
        "verified": True,
        "note": "Absolute maximum tenure for a housing loan.",
        "source": _MAS_LTV,
        "evidence": "The maximum loan tenure for housing loans is capped at: 30 years for HDB flats. "
                    "35 years for non-HDB properties.",
        "checked_on": CHECKED_ON,
    },
    "ltv_age_limit": {
        "value": 65,
        "verified": True,
        "note": "If the loan extends past this borrower age, the reduced LTV applies.",
        "source": _MAS_LTV,
        "evidence": "...or the loan period extends beyond the borrower's age of 65 years.",
        "checked_on": CHECKED_ON,
    },
    "unsecured_borrowing_cap_months": {
        "value": 12,
        "verified": True,
        "note": "Industry-wide limit on interest-bearing unsecured balances, as a multiple of monthly income, "
                "aggregated across all FIs. Excludes education, business and medical loans.",
        "source": _DBS_UNSECURED_FAQ,
        "evidence": "12 times of monthly income from 1 June 2019.",
        "checked_on": CHECKED_ON,
    },
    "hdb_buyer_residency": {
        "value": ["citizen", "pr"],
        "verified": True,
        "note": "An HDB flat must be bought by a Singapore Citizen (SC) or Singapore PR household. A foreigner "
                "cannot buy one on their own (an SC may buy with a non-resident spouse — banker to confirm).",
        "source": _HDB_FAMILIES,
        "evidence": "You are buying a flat as a Singapore Citizen (SC) or Singapore Permanent Resident (SPR) "
                    "household. ... You are a Singapore Citizen (SC) buying a flat with your non-resident spouse.",
        "checked_on": CHECKED_ON,
    },
}

# The medium-term rate DBS's own planning tools assume. Not used by the engine, which applies the MAS floor; customers
# are told about the gap, because it can make DBS's maximum loan lower than this check suggests.
DBS_PLANNER_RATE = {
    "value": 0.0465,
    "source": "https://www.dbs.com.sg/personal/marketplace/property/plan/selection",
    "evidence": "DBS MyHome planning tools' calculations are currently based on the medium-term interest rate at 4.65% "
                "and this may differ from your actual home loan eligibility.",
    "checked_on": "2026-11-02",
}

# Product-specific criteria from DBS/POSB pages.
PRODUCTS = {
    "personal_loan": {
        "name": "DBS Personal Loan",
        "min_age": {"value": 21, "verified": True, "evidence": "Age 21 – 70 years old"},
        "max_age": {"value": 70, "verified": True, "evidence": "Age 21 – 70 years old"},
        "min_annual_income": {
            "value": {"citizen": 20000, "pr": 20000, "foreigner": 60000},
            "verified": True,
            "evidence": "For customers without DBS/POSB Cashline and DBS/POSB Credit Cards: SG/PR: $20,000 p.a "
                        "Foreigners: $60,000 p.a",
        },
        "notes": "Existing DBS/POSB Cashline or credit card holders apply on that account with no income documents; "
                 "the income threshold applies to customers without one (their Personal Loan comes with a Cashline "
                 "account).",
        "source": _DBS_PL,
    },
    "cashline": {
        "name": "DBS Cashline",
        "min_age": {"value": 21, "verified": True, "evidence": "21-70 Years Old"},
        "max_age": {"value": 70, "verified": True, "evidence": "21-70 Years Old"},
        "min_annual_income": {
            "value": {"citizen": 30000, "pr": 30000, "foreigner": 60000},
            "verified": True,
            "evidence": "Singaporeans/PRs: S$30,000 p.a Foreigners: S$60,000 p.a",
        },
        "notes": "Revolving credit line.",
        "source": _DBS_CASHLINE,
    },
    "credit_card": {
        "name": "DBS / POSB Credit Cards (general entry criteria)",
        "min_age": {"value": 21, "verified": True, "evidence": "You need to be 21 to 75 years of age."},
        "max_age": {"value": 75, "verified": True, "evidence": "You need to be 21 to 75 years of age."},
        "min_annual_income": {
            "value": {"citizen": 30000, "pr": 30000, "foreigner": 45000},
            "verified": True,
            "evidence": "Singaporean/ Permanent Residents 55 years old and below: Minimum gross annual income of "
                        "S$30K ... Foreigners: Minimum gross annual income of S$45K",
        },
        "senior_income": {
            "value": {"from_age": 56, "residency": ["citizen", "pr"], "min_annual_income": 15000},
            "verified": True,
            "evidence": "56 years old and above: Minimum gross annual income of S$15K",
        },
        "notes": "Principal card criteria. Individual cards may have higher thresholds (e.g. premium cards). "
                 "A secured card (S$10,000 fixed deposit) is an alternative when the income minimum isn't met.",
        "source": _DBS_CARD_ELIG,
    },
    "home_loan": {
        "name": "DBS Home Loan",
        "min_age": {"value": 21, "verified": False,
                    "evidence": "Not stated on the DBS HDB Loan page; 21 is the legal age to contract. "
                                "Confirm with DBS before relying on it."},
        "notes": "Eligibility assessed on TDSR, MSR (HDB/EC), LTV, tenure and HDB residency rules. "
                 "Actual approval depends on credit assessment and valuation.",
        "source": _DBS_HDB_LOAN,
        "source_private": _DBS_PRIVATE,
    },
}


def unverified_count() -> tuple[int, int]:
    """(unverified, total) across MAS and PRODUCTS — surfaced on the Evals and Knowledge pages."""
    total = unverified = 0
    for item in MAS.values():
        total += 1
        unverified += 0 if item["verified"] else 1
    for prod in PRODUCTS.values():
        for item in prod.values():
            if isinstance(item, dict) and "verified" in item:
                total += 1
                unverified += 0 if item["verified"] else 1
    return unverified, total


def criteria_version() -> str:
    """Short hash of every criterion value. Stored with each engine result and counterfactual, so a
    change to the criteria visibly invalidates paths computed under the old ones."""
    import hashlib
    import json

    values = {"mas": {k: v["value"] for k, v in MAS.items()},
              "products": {p: {k: v["value"] for k, v in prod.items() if isinstance(v, dict) and "value" in v}
                           for p, prod in PRODUCTS.items()}}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()[:10]
    return f"{CHECKED_ON}.{digest}"
