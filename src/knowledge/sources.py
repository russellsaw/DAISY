"""The only sources the assistant is allowed to answer from.

Nothing outside this list (or data/raw/) ever enters the knowledge base.

Each entry:
  url        the page or PDF
  title      shown to the customer in citations
  publisher  DBS | POSB | MAS | HDB
  products   which product filters this document serves; "general" matches every product
  kind       html | pdf

Notes from the 2026-10-20 source audit:
  - DBS landing pages (/personal/loans/default.page, /personal/cards/default.page,
    /personal/loans/home-loans/default.page) are rendered by JavaScript; a scripted fetch
    gets only navigation menus. They were removed in favour of the product detail pages
    below, which are server-rendered and carry the actual rates, fees and criteria.
  - DBS URLs that don't exist return HTTP 200 with a generic page, so a 200 is not proof
    a page is real. Check `chars` in data/sources/manifest.json after every ingest.
  - POSB pages are mostly near-duplicates of the DBS ones; only POSB-specific products
    are listed, to avoid the same chunk appearing twice in retrieval.
  - The brief's combined "Personal Loan / Credit Card / Cashline T&Cs" PDF
    (/personal/mobile/sms/personal-loans/pdf/dbspersonal-loan-credit-card-cashline-tnc.pdf)
    now returns an HTML page — dead link. Replaced by the Personal Loan T&Cs PDF below.
  - Most hdb.gov.sg pages render their content with JavaScript (only headings come through).
    The couples-and-families page is kept because it states the SC/SPR household rule the
    engine relies on. For more HDB detail, save pages from a browser into data/raw/.
"""

_HOME = ["home_loan"]
_PL = ["personal_loan"]
_CL = ["cashline"]
_CC = ["credit_card"]
_UNSECURED = ["personal_loan", "cashline", "credit_card"]

SOURCES = [
    # ---------------- Home loans: DBS product pages ----------------
    {"url": "https://www.dbs.com.sg/personal/loans/home-loans/hdb-loan",
     "title": "DBS HDB Loan", "publisher": "DBS", "products": _HOME, "kind": "html"},
    # Added after the 50-question review (2026-10-25): package types (fixed / SORA / FHR / Easy Switch),
    # refinancing subsidies. Lives under /i-bank/, which the /personal/ crawl had missed.
    {"url": "https://www.dbs.com.sg/i-bank/loans/homeloans/buy-private-property",
     "title": "DBS Private Property Home Loan", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/loans/home-loans/FHR-revision.page",
     "title": "DBS Fixed Deposit Home Rate (FHR)", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/loans/home-loans/dbs-green-home-loan",
     "title": "DBS Green Home Loan", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/loans/home-loans/repricing",
     "title": "Reprice your DBS Home Loan", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/loans/home-loans/manage-mortgage",
     "title": "Manage your DBS Home Loan", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/landing/loans/homeloans/calculate-loans.html",
     "title": "DBS Mortgage Calculator", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/articles/nav/my-home/home-loan-glossary",
     "title": "DBS Home Loan Glossary", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/articles/nav/my-home/buy-your-home-with-ease-and-confidence",
     "title": "DBS MyHome Planning Tool", "publisher": "DBS", "products": _HOME, "kind": "html"},
    # Home loans: DBS help & support articles
    {"url": "https://www.dbs.com.sg/personal/support/loans-homeloan-reno-loan-application.html",
     "title": "Apply for a DBS Renovation Loan", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/loans-homeloan-partial-repayment.html",
     "title": "Partial Repayment of a DBS Home Loan", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/loans-homeloan-full-repayment.html",
     "title": "Full Repayment of a DBS Home Loan", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/loans-homeloan-repay-using-cpf-funds.html",
     "title": "Repay a DBS Home Loan with CPF", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/loans-homeloan-revise-cpf-monthly-repayment.html",
     "title": "Using CPF for Monthly Home Loan Repayment", "publisher": "DBS", "products": _HOME, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/loans-homeloan-repricing-documents-required.html",
     "title": "Documents Required for Home Loan Repricing", "publisher": "DBS", "products": _HOME, "kind": "html"},
    # Home loans: T&Cs and explainers (PDF)
    {"url": "https://www.dbs.com.sg/iwov-resources/pdf/loan/tnc_mortgageloan.pdf",
     "title": "DBS Mortgage Loan T&Cs", "publisher": "DBS", "products": _HOME, "kind": "pdf"},
    {"url": "https://www.dbs.com.sg/iwov-resources/media/pdf/loans/dbs-hdb-loan-lpa-tnc.pdf",
     "title": "DBS HDB Loan T&Cs", "publisher": "DBS", "products": _HOME, "kind": "pdf"},
    {"url": "https://www.dbs.com.sg/iwov-resources/media/pdf/loans/tnc-for-green-home-loan-package.pdf",
     "title": "DBS Green Home Loan T&Cs", "publisher": "DBS", "products": _HOME, "kind": "pdf"},
    {"url": "https://www.dbs.com.sg/iwov-resources/pdf/loan/home-loans/manage-your-mortgage/sora_dbs.pdf",
     "title": "DBS SORA Home Loan Explainer", "publisher": "DBS", "products": _HOME, "kind": "pdf"},

    # ---------------- Personal loans, Cashline, balance transfer ----------------
    {"url": "https://www.dbs.com.sg/personal/loans/instant-cash/personal-loan",
     "title": "DBS Personal Loan", "publisher": "DBS", "products": _PL, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/loans/instant-cash/cashline",
     "title": "DBS Cashline", "publisher": "DBS", "products": _CL, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/loans/personal-loans/balance-transfer",
     "title": "DBS Balance Transfer", "publisher": "DBS", "products": _UNSECURED, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/loans-application-balance-transfer.html",
     "title": "Apply for DBS Balance Transfer", "publisher": "DBS", "products": _UNSECURED, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/loans/instant-cash/how-to-pick-a-loan",
     "title": "DBS Instant Cash: Which Loan Suits You", "publisher": "DBS", "products": _PL + _CL, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/loans/instant-cash/cashline/credit-limit-review",
     "title": "DBS Cashline Credit Limit Review", "publisher": "DBS", "products": _CL, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/card-application-dbs-personal-loan.html",
     "title": "Apply for a DBS Personal Loan", "publisher": "DBS", "products": _PL, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/loans-application-new-cashline.html",
     "title": "Apply for DBS Cashline", "publisher": "DBS", "products": _CL, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/loans-application-cashline-documents.html",
     "title": "Documents Required for a Cashline Application", "publisher": "DBS", "products": _CL, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/loans-cashline-fees-and-charges.html",
     "title": "DBS Cashline Fees & Charges", "publisher": "DBS", "products": _CL, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/card-waive-credit-card-cashline-charges.html",
     "title": "Waive Credit Card / Cashline Fees & Charges", "publisher": "DBS", "products": _CC + _CL, "kind": "html"},
    {"url": "https://www.dbs.com.sg/iwov-resources/pdf/loan/personal-loan/dbs_personal_loan_tnc.pdf",
     "title": "DBS Personal Loan T&Cs", "publisher": "DBS", "products": _PL, "kind": "pdf"},

    # ---------------- Credit cards: eligibility, fees, services ----------------
    {"url": "https://www.dbs.com.sg/personal/support/card-application-eligibility.html",
     "title": "Eligibility for a DBS Credit Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/cards-application-56yo.html",
     "title": "Credit Card Eligibility for Customers 65 and Above", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/cards-application-checklist.page",
     "title": "DBS Cards Application Checklist", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/card-application-documents.html",
     "title": "Documents Required for a DBS Card Application", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/card-application-new-card.html",
     "title": "DBS Credit Card Application Process", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/card-application-supplementary-card.html",
     "title": "Apply for a DBS Supplementary Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/card-services/supplementary-cards",
     "title": "DBS Supplementary Cards", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/faq-revised-credit-card-and-unsecured-credit-rules.page",
     "title": "Credit Card and Unsecured Credit Rules (FAQ)", "publisher": "DBS", "products": _UNSECURED, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/card-charges-and-fees-annual-fee.html",
     "title": "DBS Credit Card Annual Fee", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/card-charges-and-fees-late-fee.html",
     "title": "Credit Card Late Payment Fees", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/card-charges-and-fees-finance-charge.html",
     "title": "Credit Card Interest Rate & Finance Charges", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/support/card-charges-and-fees-overseas-transaction-fees.html",
     "title": "Credit Card Foreign Transaction Fees", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/card-services/instalment-payment-plan",
     "title": "DBS Credit Card Instalment Payment Plans", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/card-services/cash-advance",
     "title": "DBS Credit Card Cash Advance", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/card-services/credit-limit-review",
     "title": "DBS Credit Card Credit Limit Review", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/customer-overlimit-faq.page",
     "title": "Credit Card Overlimit Suspension (FAQ)", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/rewards/card-rewards",
     "title": "DBS Credit Card Rewards", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/cards-comparator.page",
     "title": "DBS Cards Comparator", "publisher": "DBS", "products": _CC, "kind": "html"},
    # Credit cards: individual cards
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-chromo-card",
     "title": "DBS Chromo Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-altitude-cards",
     "title": "DBS Altitude Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-yuu-cards",
     "title": "DBS yuu Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-vantage-visa-infinite-card",
     "title": "DBS Vantage Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-woman-mastercard-card",
     "title": "DBS Woman's Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-esso-platinum-card",
     "title": "DBS Esso Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-safra-credit-card",
     "title": "SAFRA DBS Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.dbs.com.sg/personal/cards/credit-cards/dbs-takashimaya-card",
     "title": "DBS Takashimaya Card", "publisher": "DBS", "products": _CC, "kind": "html"},
    {"url": "https://www.posb.com.sg/personal/cards/credit-cards/posb-everyday-card",
     "title": "POSB Everyday Card", "publisher": "POSB", "products": _CC, "kind": "html"},
    # Credit cards: agreements and T&Cs (PDF)
    {"url": "https://www.dbs.com.sg/iwov-resources/pdf/cards/dbs-agreement-2019.pdf",
     "title": "DBS Cardmember Agreement", "publisher": "DBS", "products": _CC, "kind": "pdf"},
    {"url": "https://www.dbs.com.sg/iwov-resources/pdf/cards/rewards_programme_tnc.pdf",
     "title": "DBS Rewards Programme T&Cs", "publisher": "DBS", "products": _CC, "kind": "pdf"},
    {"url": "https://www.dbs.com.sg/iwov-resources/pdf/cards/altitude-card-tnc.pdf",
     "title": "DBS Altitude Card T&Cs", "publisher": "DBS", "products": _CC, "kind": "pdf"},
    {"url": "https://www.dbs.com.sg/iwov-resources/media/pdf/cards/dbs-chromo-tnc.pdf",
     "title": "DBS Chromo Card T&Cs", "publisher": "DBS", "products": _CC, "kind": "pdf"},
    {"url": "https://www.dbs.com.sg/iwov-resources/pdf/cards/vantage-card-T&Cs.pdf",
     "title": "DBS Vantage Card T&Cs", "publisher": "DBS", "products": _CC, "kind": "pdf"},
    {"url": "https://www.dbs.com.sg/iwov-resources/media/pdf/cards/dbs-womans-card-tnc.pdf",
     "title": "DBS Woman's Card T&Cs", "publisher": "DBS", "products": _CC, "kind": "pdf"},
    {"url": "https://www.posb.com.sg/iwov-resources/media/pdf/cards/posb-everyday-card-tncs.pdf",
     "title": "POSB Everyday Card T&Cs", "publisher": "POSB", "products": _CC, "kind": "pdf"},

    # ---------------- Regulators: the rules the eligibility engine encodes ----------------
    {"url": "https://www.mas.gov.sg/regulation/explainers/new-housing-loans",
     "title": "MAS: Rules for New Housing Loans", "publisher": "MAS", "products": _HOME, "kind": "html"},
    {"url": "https://www.mas.gov.sg/regulation/explainers/new-housing-loans/loan-tenure-and-loan-to-value-limits",
     "title": "MAS: Loan Tenure and Loan-to-Value Limits", "publisher": "MAS", "products": _HOME, "kind": "html"},
    {"url": "https://www.mas.gov.sg/regulation/explainers/new-housing-loans/msr-and-tdsr-rules",
     "title": "MAS: MSR and TDSR Rules", "publisher": "MAS", "products": _HOME, "kind": "html"},
    {"url": "https://www.mas.gov.sg/regulation/explainers/new-housing-loans/borrower-mortgagor-guarantor-borrower-requirements",
     "title": "MAS: Borrower, Mortgagor and Guarantor Requirements", "publisher": "MAS", "products": _HOME, "kind": "html"},
    {"url": "https://www.mas.gov.sg/regulation/explainers/tdsr-for-property-loans/calculating-tdsr",
     "title": "MAS: Calculating TDSR", "publisher": "MAS", "products": _HOME, "kind": "html"},
    {"url": "https://www.mas.gov.sg/regulation/explainers/tdsr-for-property-loans/calculating-tdsr-thresholds",
     "title": "MAS: TDSR Thresholds", "publisher": "MAS", "products": _HOME, "kind": "html"},
    {"url": "https://www.mas.gov.sg/regulation/explainers/tdsr-for-property-loans/who-tdsr-applies-to",
     "title": "MAS: Who TDSR Applies To", "publisher": "MAS", "products": _HOME, "kind": "html"},
    {"url": "https://www.hdb.gov.sg/buying-a-flat/flat-grant-and-loan-eligibility/couples-and-families",
     "title": "HDB: Flat Eligibility for Couples and Families", "publisher": "HDB", "products": _HOME, "kind": "html"},
]

# Deliberately NOT ingested (out of scope — advice boundary): insurance, endowment,
# investments. Listed here so the decision is visible.
# Lines left out of the search index because a newer source replaces them. The saved snapshot isn't edited, so it
# stays a faithful record of the page. {document id: [text that identifies the line]}
SUPERSEDED_LINES = {
    # This page still quotes 3.5%. DBS's planning tools now state 4.65% (dbs-home-budget-calculator).
    "dbs-mortgage-calculator": ["medium-term 3.5% interest rate"],
}

OUT_OF_SCOPE = [
    "https://www.dbs.com.sg/personal/insurance/default.page",
    "https://www.dbs.com.sg/personal/insurance/endowment/default.page",
    "https://www.dbs.com.sg/personal/investments/default.page",
    "https://www.dbs.com.sg/personal/articles/nav/investing/how-to-start-investing",
]
