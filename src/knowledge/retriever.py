"""BM25 retrieval over data/kb.json.

Keyword retrieval is deliberately simple: no embeddings API, no vector DB,
nothing to break at 3am. Tuned after the 50-question review (2026-10-25):
  - light stemming, so "cards"/"card", "application"/"apply" and "reprice"/"repricing" match;
  - page titles count twice, so "Cashline" finds the Cashline page;
  - the product the orchestrator names BOOSTS matching chunks instead of filtering
    the rest out (a balance-transfer question tagged "credit card" lost the
    Balance Transfer page to the filter);
  - the orchestrator adds a search query in DBS/MAS terms ("outstanding housing
    loans", "finance charge"), appended to the customer's words;
  - soft diversity: each further passage from the same page has its score multiplied by
    SAME_DOC_DECAY again. A page that is clearly the answer still fills several slots (the
    yuu Card question needs its fees AND rewards), but a page with many similar passages
    can't crowd out the one that answers the question (5 of 8 slots once went to one
    page). A hard cap of 2 per page was tried and starved single-product questions.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from rank_bm25 import BM25Okapi

from .. import config

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?%?")
_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "of",
    "to",
    "for",
    "in",
    "on",
    "is",
    "are",
    "what",
    "how",
    "do",
    "i",
    "my",
    "me",
    "can",
    "with",
    "be",
    "it",
    "this",
    "that",
    "does",
    "you",
    "your",
    "s",
    "dbs",
}


# Word forms customers and pages use for the same idea.
_NORMAL = {
    "application": "apply",
    "applications": "apply",
    "applying": "apply",
    "applied": "apply",
    "repay": "repayment",
    "repaying": "repayment",
    "repaid": "repayment",
    "borrowing": "borrow",
    "borrowed": "borrow",
    "charged": "charge",
    "charges": "charge",
    "documents": "document",
    "eligible": "eligibility",
    "qualify": "eligibility",
    "mortgage": "home",
    "housing": "home",
}
_PRODUCT_BOOST = 1.25
SAME_DOC_DECAY = 0.8


def _strip_suffix(token: str) -> str:
    if token in _NORMAL:
        return _NORMAL[token]
    if len(token) <= 3 or not token.isalpha():
        return token
    # "es" only after a sibilant (branches, boxes). Otherwise just the "s": rates -> rate, miles -> mile.
    if token.endswith(("ches", "shes", "xes", "zes")):
        return token[:-2]
    for suffix, keep in (("ies", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("s", "")):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3 and not token.endswith(("ss", "us", "is")):
            return _NORMAL.get(token[: -len(suffix)] + keep, token[: -len(suffix)] + keep)
    return token


def _stem(token: str) -> str:
    stem = _strip_suffix(token)
    # "-ing"/"-ed" take the final "e" with them (repricing -> repric), so the base word drops its "e" too:
    # reprice/repricing, waive/waived and charge/charging then meet. Words of 4 letters or fewer keep it
    # (fee, rate, home, free), which also keeps "note" apart from "not" and "care" from "car".
    return stem[:-1] if len(stem) > 4 and stem.endswith("e") and stem.isalpha() else stem


def tokenize(text: str) -> list[str]:
    return [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def _chunk_products(c: dict) -> list[str]:
    return c.get("products") or [c.get("product", "general")]


class KnowledgeBase:
    def __init__(self, chunks: list[dict], meta: dict):
        self.chunks = chunks
        self.meta = meta
        # The title is indexed twice with the text so "Cashline fees" finds the Cashline fees page.
        corpus = [tokenize(f"{c.get('title', '')}\n{c.get('title', '')}\n{c['text']}") for c in chunks]
        self._bm25 = BM25Okapi(corpus) if chunks else None

    @property
    def ready(self) -> bool:
        return bool(self.chunks)

    def retrieve(self, query: str, k: int | None = None, product: str | None = None) -> list[dict]:
        if not self._bm25:
            return []
        k = k or config.RETRIEVAL_TOP_K
        scores = list(self._bm25.get_scores(tokenize(query)))
        if product:  # boost, don't filter: a page tagged for a sibling product may be the right one
            for i, c in enumerate(self.chunks):
                products = _chunk_products(c)
                if product in products or "general" in products:
                    scores[i] *= _PRODUCT_BOOST
        pool = sorted((i for i in range(len(scores)) if scores[i] > 0), key=lambda i: scores[i], reverse=True)[:200]
        out, per_doc = [], {}
        while pool and len(out) < k:  # greedy: best score after decaying for passages already taken

            def adjusted(i):
                doc = self.chunks[i].get("doc_id") or self.chunks[i].get("url")
                return scores[i] * SAME_DOC_DECAY ** per_doc.get(doc, 0)

            best = max(pool, key=adjusted)
            pool.remove(best)
            c = self.chunks[best]
            doc = c.get("doc_id") or c.get("url")
            per_doc[doc] = per_doc.get(doc, 0) + 1
            out.append({**c, "score": round(float(scores[best]), 3)})
        return out


@lru_cache(maxsize=1)
def _load(mtime: float) -> KnowledgeBase:  # keyed on mtime so a re-ingest is picked up without restart
    # ingest writes kb.json to a temp file and swaps it in, so this never reads half a rebuild.
    data = json.loads(config.KB_PATH.read_text(encoding="utf-8"))
    return KnowledgeBase(data.get("chunks", []), {k: v for k, v in data.items() if k != "chunks"})


def load() -> KnowledgeBase:
    if not config.KB_PATH.exists():
        return KnowledgeBase([], {"built_on": None, "documents": []})
    return _load(config.KB_PATH.stat().st_mtime)


def retrieve(query: str, k: int | None = None, product: str | None = None) -> list[dict]:
    return load().retrieve(query, k, product)
