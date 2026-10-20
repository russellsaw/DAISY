"""Central configuration. Everything tunable lives here or in .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Main model does the customer-facing reasoning; fast model handles
# classification, compliance review and eval judging (cheaper, quicker).
MODEL_MAIN = os.getenv("MODEL_MAIN", "claude-sonnet-5-5")
MODEL_FAST = os.getenv("MODEL_FAST", "claude-haiku-5-5")

DATA_DIR = ROOT / "data"
KB_PATH = DATA_DIR / "kb.json"
RAW_DIR = DATA_DIR / "raw"

EVAL_CASES = ROOT / "evals" / "cases.csv"
EVAL_RESULTS_DIR = ROOT / "evals" / "results"

RETRIEVAL_TOP_K = int(os.getenv("RETRIEVAL_TOP_K", "5"))
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "12"))

ASSISTANT_NAME = os.getenv("ASSISTANT_NAME", "Level")
ASSISTANT_TAGLINE = "by DBS"
BANK_NAME = "DBS"
