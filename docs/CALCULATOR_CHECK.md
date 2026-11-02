# Rules engine vs DBS's own calculators

Does DAISY's rules engine agree with the calculators DBS publishes? Five personas were entered into DBS's public tools on
2026-11-02. The DBS figures below were read from the rendered pages. The engine side is printed by `python scripts/calculator_check.py` (no
API calls).

## Result

- **The maths matches.** At the same interest rate, the engine's instalments match DBS's repayment calculator on all five
  personas, and its maximum loan matches DBS's planner to the dollar on all five (P5 at DBS's 15-year tenure).
- **The rate differs, and that changes answers.** DBS's planning tools currently assume a **4.65%** medium-term rate. The
  engine uses the **4%** floor from MAS's rule, so its maximum loan is higher than DBS's on P1 to P4.
- **It changes the demo persona's outcome.** P1 borrowing S$380,000 meets the criteria at 4% (MSR 28.7%), but DBS's tool
  gives a maximum of S$372,089 for the same person: at 4.65% the MSR would be 30.6%, over the 30% cap.

MAS requires "the higher of a 4% floor or the thereafter interest rate", so 4% is the minimum a bank may use and DBS is
entitled to use more. DAISY keeps the sourced MAS floor and says so: every home-loan result carries a code-written note
that DBS's tools assume 4.65% and can give a lower maximum loan, and "how much can I borrow?" shows both rates.

## Repayment calculator

DBS's Home Loan Repayment Calculator, New Loan tab, interest rate set to 4.00%. The page shows whole dollars only, so cents
can't be compared.

| # | Loan | Tenure | Engine instalment at 4% | DBS at 4% | Match |
|---|---|---|---|---|---|
| P1 | S$380,000 | 25y | S$2,005.78 | $2,006 | Yes |
| P2 | S$400,000 | 25y | S$2,111.35 | $2,111 | Yes |
| P3 | S$700,000 | 30y | S$3,341.91 | $3,342 | Yes |
| P4 | S$380,000 | 25y | S$2,005.78 | $2,006 | Yes |
| P5 | S$800,000 | 25y | S$4,222.69 | $4,223 | Yes |

## Maximum loan

DBS's Home Budget affordability calculator, part of its MyHome planning tools, with "Buying my first home" and "on my own"
selected (the personas don't say, so these are our assumptions). The engine figures are `src/rules/max_loan.py`, which also
answers "how much can I borrow?" in the app. The match at 4.65% is a unit test (`tests/test_max_loan.py`).

| # | Inputs entered at DBS | Engine at 4% | Engine at 4.65% | DBS | DBS's limiting rule |
|---|---|---|---|---|---|
| P1 | HDB, PR, 32, S$7,000, 25y | S$397,850 | S$372,089 | $372,089 | MSR |
| P2 | Same as P1 (the tool has no loan-amount field) | S$397,850 | S$372,089 | $372,089 | MSR |
| P3 | Condo, PR, 32, S$7,000, S$600 other repayments, 30y | S$680,749 | S$630,288 | $630,288 | TDSR |
| P4 | HDB, PR, 32, S$4,500, 25y | S$255,760 | S$239,200 | $239,200 | MSR |
| P5 | Condo, citizen, 50, S$12,000, **15y** | S$892,268 | S$854,168 | $854,168 | TDSR |

**P5 is compared at 15 years, not the persona's 25.** DBS's tool limits this person to 15 years (it rejected 25 with "Please
enter a value within the range"). The engine allows 25 years at a reduced 55% LTV, which gives S$660,000 at either rate. So
for this borrower DBS shortens the tenure where the engine lowers the LTV. Engine figures for P5 at 15 years come from
running the same search with the tenure changed.

## What this check doesn't show

- DBS's tool has no loan-amount field and never asked for savings, so it tests the maximum loan only, and a savings cap
  couldn't be tested.
- The tool's result depends on the medium-term rate DBS has set today. The rate is read from the banner on the page:
  *"DBS MyHome planning tools' calculations are currently based on the medium-term interest rate at 4.65% and this may
  differ from your actual home loan eligibility."*
- The saved copy of DBS's older calculator page still quotes 3.5%. That line is left out of the search index, so DAISY
  doesn't quote it.

## Pages used

- Repayment calculator: <https://www.dbs.com.sg/personal/marketplace/property/plan/refinance>
- Planner: starts at <https://www.dbs.com.sg/personal/marketplace/property/plan/selection>, result at
  <https://www.dbs.com.sg/personal/marketplace/property/plan/budgetSummary>

The URL in `src/knowledge/sources.py` for the calculator page now redirects to the home-loans landing page, which links to
both tools.

Checked on: 2026-11-02, by scripted headless Chrome, with the figures copied from the rendered page text.
