"""Central configuration. Everything tunable lives here or in .env."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Main model does the customer-facing drafting and the eval judging; fast model handles routing,
# compliance review, the lead card and rewrites (cheaper, quicker).
MODEL_MAIN = os.getenv("MODEL_MAIN", "claude-sonnet-5-5")
MODEL_FAST = os.getenv("MODEL_FAST", "claude-haiku-5-5")
# The eval judge decides pass/fail on behaviour; Haiku produced verdicts that contradicted its own
# rationale, so the judge uses the main model.
MODEL_JUDGE = os.getenv("MODEL_JUDGE", MODEL_MAIN)

DATA_DIR = ROOT / "data"
KB_PATH = DATA_DIR / "kb.json"
RAW_DIR = DATA_DIR / "raw"

EVAL_CASES = ROOT / "evals" / "cases.csv"
EVAL_RESULTS_DIR = ROOT / "evals" / "results"

LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "90"))
RETRIEVAL_TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "10"))  # 5 missed the right page in the 50-question review
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "12"))

ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "DAISY")
ASSISTANT_TAGLINE = "DBS AI, Straight with You"
BANK_NAME = "DBS"

# Buttons that spend money or overwrite data (run the eval suite, re-fetch sources). Hidden unless switched on.
STAFF_CONTROLS = os.getenv("DAISY_STAFF_CONTROLS", "").strip().lower() in ("1", "true", "yes")

# ---- "Not now, but when" (counterfactual eligibility) ----
# A counterfactual only counts as a path if every ratio rule that fails today (TDSR / MSR / LTV /
# unsecured utilisation) clears its cap by at least this margin. Below it: "borderline", not a path.
CF_MARGIN = 0.03  # 3 percentage points
# What-if levers are searched in these steps, and never beyond these "realistic" bounds.
CF_INCOME_STEP = 100  # S$ per month
CF_MAX_INCOME_UPLIFT = 0.50  # at most +50% of today's income
CF_HOME_LOAN_STEP = 5_000  # S$
CF_MAX_HOME_LOAN_CUT = 0.50  # at most half the requested home loan
CF_PERSONAL_LOAN_STEP = 1_000  # S$
