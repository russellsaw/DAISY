"""BM25 retrieval over data/kb.json.

Keyword retrieval is deliberately simple: no embeddings API, no vector DB,
nothing to break at 3am. For a few dozen pages of banking product text it
performs well because customers use the same vocabulary the pages do.
Swap `retrieve()` for an embedding search later if recall is a problem.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from rank_bm25 import BM25Okapi

from .. import config

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?%?")
_STOP = {"the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "is", "are", "what",
         "how", "do", "i", "my", "me", "can", "with", "be", "it", "this", "that", "does", "you",
         "your", "s", "dbs"}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


def _chunk_products(c: dict) -> list[str]:
    return c.get("products") or [c.get("product", "general")]


class KnowledgeBase:
    def __init__(self, chunks: list[dict], meta: dict):
        self.chunks = chunks
        self.meta = meta
        # The title is indexed with the text so "Cashline fees" finds the Cashline fees page.
        corpus = [tokenize(f"{c.get('title', '')}\n{c['text']}") for c in chunks]
        self._bm25 = BM25Okapi(corpus) if chunks else None

    @property
    def ready(self) -> bool:
        return bool(self.chunks)

    def retrieve(self, query: str, k: int | None = None, product: str | None = None) -> list[dict]:
        if not self._bm25:
            return []
        k = k or config.RETRIEVAL_TOP_K
        scores = self._bm25.get_scores(tokenize(query))
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out = []
        for i in ranked:
            if scores[i] <= 0:
                break
            c = self.chunks[i]
            products = _chunk_products(c)
            if product and product not in products and "general" not in products:
                continue
            out.append({**c, "score": round(float(scores[i]), 3)})
            if len(out) >= k:
                break
        return out


@lru_cache(maxsize=1)
def _load(mtime: float) -> KnowledgeBase:  # keyed on mtime so a re-ingest is picked up without restart
    data = json.loads(config.KB_PATH.read_text())
    return KnowledgeBase(data.get("chunks", []), {k: v for k, v in data.items() if k != "chunks"})


def load() -> KnowledgeBase:
    if not config.KB_PATH.exists():
        return KnowledgeBase([], {"built_on": None, "documents": []})
    return _load(config.KB_PATH.stat().st_mtime)


def retrieve(query: str, k: int | None = None, product: str | None = None) -> list[dict]:
    return load().retrieve(query, k, product)
