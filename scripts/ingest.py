"""Build the knowledge base. Run from the repo root:

    python scripts/ingest.py                # fetch all sources, then rebuild data/kb.json
    python scripts/ingest.py --build-only   # rebuild from saved snapshots, no network
    python scripts/ingest.py --only dbs-personal-loan
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.knowledge.ingest import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
