"""Product Knowledge agent: grounded Q&A with mandatory citations."""

from __future__ import annotations

import re

from .. import config, llm
from ..knowledge import retriever
from ..rules.counterfactual import fmt_date
from ..state import ConversationState
from . import prompts

_CITE = re.compile(r"\[(\d+)\]")


def _as_of(chunk: dict) -> str:
    fetched = (chunk.get("fetched_at") or "")[:10]
    return f"as of {fmt_date(fetched)}" if fetched else "date unknown"


def search(user_msg: str, products: list[str], search_query: str = "", k: int | None = None) -> tuple[list[dict], str]:
    """Retrieve excerpts for a question: the customer's words plus the orchestrator's search query in
    DBS/MAS terms. Returns (chunks, numbered excerpt text exactly as the drafting model will see it)."""
    product = products[0] if len(products) == 1 else None
    chunks = retriever.retrieve(f"{user_msg} {search_query or ''}".strip(), k=k, product=product)
    excerpts = "\n\n".join(
        f"[{i + 1}] {c['title']} ({c.get('publisher', 'DBS')}, {_as_of(c)}) — {c['url']}\n{c['text']}"
        for i, c in enumerate(chunks)
    )
    return chunks, excerpts


def cited_sources(draft: str, chunks: list[dict]) -> tuple[list[dict], set[int]]:
    """One entry per cited page, with every [n] that points at it (two excerpts can share a page)."""
    cited = {int(n) for n in _CITE.findall(draft)}
    by_url: dict[str, dict] = {}
    for n in sorted(cited):
        if 1 <= n <= len(chunks):
            c = chunks[n - 1]
            by_url.setdefault(c["url"], {"n": [], "title": c["title"], "url": c["url"]})["n"].append(n)
    return list(by_url.values()), cited


def hits_detail(user_msg: str, search_query: str, products: list[str], chunks: list[dict], cited: set[int]) -> dict:
    return {
        "query": user_msg,
        "search_query": search_query,
        "product_boost": products[0] if len(products) == 1 else None,
        "hits": [
            {
                "n": i + 1,
                "title": c["title"],
                "url": c["url"],
                "publisher": c.get("publisher"),
                "as_of": (c.get("fetched_at") or "")[:10],
                "score": c["score"],
                "cited": (i + 1) in cited,
            }
            for i, c in enumerate(chunks)
        ],
    }


def answer(state: ConversationState, user_msg: str, products: list[str], search_query: str = "") -> dict:
    if not retriever.load().ready:
        draft = (
            "I don't have the product knowledge base loaded yet, so I can't answer that reliably. "
            "The official DBS loans and cards pages have the details, or a DBS banker can help."
        )
        state.add_trace("Product Knowledge", "knowledge base missing — run scripts/ingest.py")
        return {"draft": draft, "sources": [], "chunks": []}

    chunks, excerpts = search(user_msg, products, search_query)
    user_block = (
        f"Customer profile (for context only):\n{state.profile.describe()}\n\n"
        f"Source excerpts:\n{excerpts if excerpts else '(no relevant excerpts found)'}\n\n"
        f"Customer question:\n{user_msg}"
    )
    history = state.recent_history(config.MAX_HISTORY_TURNS)[:-1]  # exclude current user msg
    messages = history + [{"role": "user", "content": user_block}]
    draft = llm.chat(prompts.PRODUCT_KNOWLEDGE, messages, model=config.MODEL_MAIN, max_tokens=4096, effort="medium")

    sources, cited = cited_sources(draft, chunks)
    state.add_trace(
        "Product Knowledge",
        f"retrieved {len(chunks)} chunks, cited {len(cited)}",
        hits_detail(user_msg, search_query, products, chunks, cited),
    )
    # `excerpts` is exactly what the drafting model saw ([n] numbering, title, as-of date, URL): the compliance
    # reviewer and the traceability checks get the same text, so citations and URLs can be verified.
    return {"draft": draft, "sources": sources, "chunks": chunks, "excerpts": excerpts}
