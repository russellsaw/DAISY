"""Unit tests for the deterministic engine. Run: pytest -q"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.rules.criteria import MAS, PRODUCTS  # noqa: E402
from src.rules.engine import (  # noqa: E402
    check_cashline, check_credit_card, check_home_loan, check_personal_loan, monthly_instalment)
from src.state import CustomerProfile  # noqa: E402

DEMO = dict(age=32, residency="pr", monthly_income=7000, property_type="hdb",
            property_price=550_000, home_loan_amount=380_000, tenure_years=25,
            existing_housing_loans=0, existing_monthly_debt=0)


def test_instalment_math():
    # 400k over 25y at 4% ≈ 2,111/month
    assert abs(monthly_instalment(400_000, 0.04, 25) - 2111.3) < 1


def test_need_more_info_when_fields_missing():
    r = check_personal_loan(CustomerProfile(age=30))
    assert r.status == "need_more_info"
    assert "monthly_income" in r.missing_fields


def test_personal_loan_pr_eligible():
    r = check_personal_loan(CustomerProfile(age=32, residency="pr", monthly_income=7000))
    assert r.status == "eligible"
    assert r.computations["annual_income"] == 84000


def test_personal_loan_under_age():
    r = check_personal_loan(CustomerProfile(age=19, residency="citizen", monthly_income=3000))
    assert r.status == "not_eligible"


def test_personal_loan_max_age_70():
    assert check_personal_loan(CustomerProfile(age=70, residency="citizen", monthly_income=5000)).status == "eligible"
    assert check_personal_loan(CustomerProfile(age=71, residency="citizen", monthly_income=5000)).status == "not_eligible"


def test_personal_loan_foreigner_needs_60k():
    assert check_personal_loan(CustomerProfile(age=30, residency="foreigner", monthly_income=4500)).status == "not_eligible"
    assert check_personal_loan(CustomerProfile(age=30, residency="foreigner", monthly_income=5000)).status == "eligible"


def test_personal_loan_ignores_home_loan_amount():
    # Regression: one shared loan_amount made the S$380k home loan count as a personal loan request.
    r = check_personal_loan(CustomerProfile(**DEMO, existing_unsecured_balance=0))
    assert r.status == "eligible"


def test_personal_loan_request_over_headroom():
    p = CustomerProfile(age=32, residency="pr", monthly_income=5000,
                        existing_unsecured_balance=50_000, personal_loan_amount=20_000)
    r = check_personal_loan(p)  # cap 60k, headroom 10k
    assert r.status == "not_eligible"
    assert r.computations["unsecured_headroom"] == 10_000


def test_cashline_thresholds():
    assert check_cashline(CustomerProfile(age=30, residency="citizen", monthly_income=2000)).status == "not_eligible"  # 24k < 30k
    assert check_cashline(CustomerProfile(age=30, residency="citizen", monthly_income=2500)).status == "eligible"
    assert check_cashline(CustomerProfile(age=30, residency="foreigner", monthly_income=4500)).status == "not_eligible"


def test_credit_card_income_threshold_by_residency():
    citizen = check_credit_card(CustomerProfile(age=25, residency="citizen", monthly_income=2600))
    foreigner = check_credit_card(CustomerProfile(age=25, residency="foreigner", monthly_income=2600))
    assert citizen.status == "eligible"
    assert foreigner.status == "not_eligible"


def test_credit_card_senior_threshold():
    # SC/PR aged 56+: S$15k p.a. instead of S$30k
    assert check_credit_card(CustomerProfile(age=58, residency="citizen", monthly_income=1500)).status == "eligible"
    assert check_credit_card(CustomerProfile(age=55, residency="citizen", monthly_income=1500)).status == "not_eligible"
    assert check_credit_card(CustomerProfile(age=58, residency="foreigner", monthly_income=1500)).status == "not_eligible"


def test_credit_card_max_age_75():
    assert check_credit_card(CustomerProfile(age=76, residency="citizen", monthly_income=5000)).status == "not_eligible"


def test_home_loan_demo_persona_hdb():
    # 32yo PR, $7k/month, first HDB, $380k over 25y, no other debt.
    # (Note: $400k fails MSR by a hair — 2,111/7,000 = 30.2%. Nice demo moment.)
    r = check_home_loan(CustomerProfile(**DEMO))
    assert r.status == "eligible"
    assert r.computations["ltv_limit"] == 0.75
    assert r.computations["msr"] <= 0.30
    assert r.computations["tdsr"] <= 0.55


def test_home_loan_demo_persona_400k_fails_msr():
    r = check_home_loan(CustomerProfile(**{**DEMO, "home_loan_amount": 400_000}))
    assert r.status == "not_eligible"
    assert r.computations["msr"] > 0.30


def test_home_loan_fails_msr():
    p = CustomerProfile(age=30, residency="citizen", monthly_income=4000, property_type="hdb",
                        property_price=600_000, home_loan_amount=400_000, tenure_years=25)
    r = check_home_loan(p)
    assert r.status == "not_eligible"
    assert any("MSR" in reason for reason in r.reasons)


def test_home_loan_needs_property_price():
    p = CustomerProfile(**{k: v for k, v in DEMO.items() if k != "property_price"})
    r = check_home_loan(p)
    assert r.status == "need_more_info"
    assert r.missing_fields == ["property_price"]


def test_home_loan_foreigner_cannot_buy_hdb():
    r = check_home_loan(CustomerProfile(**{**DEMO, "residency": "foreigner", "monthly_income": 12000}))
    assert r.status == "not_eligible"
    assert any("HDB" in reason for reason in r.reasons)


def test_home_loan_foreigner_private_ok():
    p = CustomerProfile(**{**DEMO, "residency": "foreigner", "property_type": "private", "monthly_income": 12000})
    assert check_home_loan(p).status == "eligible"


def test_home_loan_reduced_ltv_past_65():
    p = CustomerProfile(age=50, residency="citizen", monthly_income=15000, property_type="private",
                        property_price=1_000_000, home_loan_amount=700_000, tenure_years=25)
    r = check_home_loan(p)
    assert r.computations["ltv_tier"] == "reduced"
    assert r.status == "not_eligible"  # 700k > 55% of 1m


def test_every_verified_criterion_has_evidence():
    for key, item in MAS.items():
        if item["verified"]:
            assert item.get("evidence") and item.get("source"), key
    for pkey, prod in PRODUCTS.items():
        for key, item in prod.items():
            if isinstance(item, dict) and item.get("verified"):
                assert item.get("evidence"), f"{pkey}.{key}"


# ---------------- MSR counts every property loan (MAS: "all the borrower's property loans") ----------------

SECOND_LOAN = dict(age=32, residency="pr", monthly_income=7000, property_type="hdb", property_price=550_000,
                   home_loan_amount=240_000, tenure_years=25, existing_housing_loans=1)
MORTGAGE = {"kind": "home loan", "monthly": 1800.0}


def test_msr_counts_an_existing_mortgage():
    r = check_home_loan(CustomerProfile(**SECOND_LOAN, commitments=[MORTGAGE]))
    assert r.status == "not_eligible"
    assert r.computations["msr"] == pytest.approx(0.4381, abs=1e-4)
    assert any(c["rule"] == "msr" and not c["passed"] for c in r.checks)


def test_existing_mortgage_is_counted_once_in_tdsr():
    both = CustomerProfile(**SECOND_LOAN, commitments=[MORTGAGE], existing_property_loan_monthly=1800)
    assert both.total_monthly_debt() == 1800


def test_renovation_loan_counts_for_tdsr_but_not_msr():
    p = CustomerProfile(**{**SECOND_LOAN, "existing_housing_loans": 0},
                        commitments=[{"kind": "home renovation loan", "monthly": 500.0}])
    r = check_home_loan(p)
    assert p.property_loan_monthly() == 0
    assert r.computations["msr"] < r.computations["tdsr"]


def test_existing_housing_loan_without_its_repayment_needs_more_info():
    r = check_home_loan(CustomerProfile(**SECOND_LOAN))
    assert r.status == "need_more_info" and r.missing_fields == ["existing_property_loan_monthly"]


def test_stated_mortgage_implies_a_housing_loan_for_ltv():
    r = check_home_loan(CustomerProfile(**{k: v for k, v in SECOND_LOAN.items() if k != "existing_housing_loans"},
                                        commitments=[MORTGAGE]))
    assert r.computations["ltv_limit"] == 0.45


def test_demo_persona_msr_is_28_7_percent():
    # 2,005.78 / 7,000 = 28.654%. The figure the pitch quotes: it must round the same way everywhere.
    r = check_home_loan(CustomerProfile(**DEMO))
    assert f"{r.computations['msr']:.1%}" == "28.7%"
    assert any("MSR 28.7%" in reason for reason in r.reasons)


# ---------------- unsecured limit ----------------

def test_personal_loan_over_the_limit_fails_with_balance_unknown():
    r = check_personal_loan(CustomerProfile(age=30, residency="citizen", monthly_income=3000,
                                            personal_loan_amount=100_000))
    assert r.status == "not_eligible"
    assert any(c["rule"] == "unsecured_utilisation" and not c["passed"] for c in r.checks)


def test_personal_loan_within_the_limit_passes_and_says_balance_unknown():
    r = check_personal_loan(CustomerProfile(age=30, residency="citizen", monthly_income=3000,
                                            personal_loan_amount=20_000))
    assert r.status == "eligible"
    assert any("unsecured balances" in a for a in r.assumptions)


# ---------------- what was assumed, and what is cited ----------------

def test_unstated_loans_and_debts_are_reported_as_assumptions():
    r = check_home_loan(CustomerProfile(age=40, residency="citizen", monthly_income=10_000, property_type="private",
                                        property_price=1_000_000, home_loan_amount=750_000, tenure_years=25))
    assert any("housing loans" in a for a in r.assumptions)
    assert any("monthly repayments" in a for a in r.assumptions)


def test_stated_no_loans_means_no_assumptions():
    assert check_home_loan(CustomerProfile(**DEMO)).assumptions == []


def test_condo_check_cites_the_private_page_and_mas_not_the_hdb_page():
    r = check_home_loan(CustomerProfile(**{**DEMO, "property_type": "private", "property_price": 1_000_000}))
    assert any("buy-private-property" in s for s in r.sources)
    assert not any("hdb-loan" in s or "hdb.gov.sg" in s for s in r.sources)
    assert sum("mas.gov.sg" in s for s in r.sources) == 3


def test_every_cited_page_has_a_title():
    from src.rules.criteria import SOURCE_TITLES
    for p in (DEMO, {**DEMO, "property_type": "private"}):
        assert all(s in SOURCE_TITLES for s in check_home_loan(CustomerProfile(**p)).sources)
    assert all(s in SOURCE_TITLES for s in check_personal_loan(CustomerProfile(**DEMO)).sources)
