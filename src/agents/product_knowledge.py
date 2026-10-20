"""Product Knowledge agent: grounded Q&A with mandatory citations."""
from __future__ import annotations

import re

from .. import config, llm
from ..knowledge import retriever
from ..state import ConversationState
from . import prompts

_CITE = re.compile(r"\[(\d+)\]")


def answer(state: ConversationState, user_msg: str, products: list[str]) -> dict:
    product = products[0] if len(products) == 1 else None
    chunks = retriever.retrieve(user_msg, product=product)
    if not chunks and product:
        chunks = retriever.retrieve(user_msg)  # widen if the product filter starved us

    if not retriever.load().ready:
        draft = ("I don't have the product knowledge base loaded yet, so I can't answer that reliably. "
                 "The official DBS loans and cards pages have the details, or a DBS banker can help.")
        state.add_trace("Product Knowledge", "knowledge base missing — run scripts/ingest.py")
        return {"draft": draft, "sources": [], "chunks": []}

    excerpts = "\n\n".join(
        f"[{i + 1}] {c['title']} ({c.get('publisher', 'DBS')}, as of {(c.get('fetched_at') or '')[:10]}) — "
        f"{c['url']}\n{c['text']}" for i, c in enumerate(chunks)
    )
    user_block = (
        f"Customer profile (for context only):\n{state.profile.describe()}\n\n"
        f"Source excerpts:\n{excerpts if excerpts else '(no relevant excerpts found)'}\n\n"
        f"Customer question:\n{user_msg}"
    )
    history = state.recent_history(config.MAX_HISTORY_TURNS)[:-1]  # exclude current user msg
    messages = history + [{"role": "user", "content": user_block}]
    draft = llm.chat(prompts.PRODUCT_KNOWLEDGE, messages, model=config.MODEL_MAIN, max_tokens=4096, effort="medium")

    cited = {int(n) for n in _CITE.findall(draft)}
    sources = []
    seen = set()
    for n in sorted(cited):
        if 1 <= n <= len(chunks) and chunks[n - 1]["url"] not in seen:
            seen.add(chunks[n - 1]["url"])
            sources.append({"n": n, "title": chunks[n - 1]["title"], "url": chunks[n - 1]["url"]})

    state.add_trace(
        "Product Knowledge",
        f"retrieved {len(chunks)} chunks, cited {len(cited)}",
        [{"title": c["title"], "score": c["score"]} for c in chunks],
    )
    # `excerpts` is exactly what the drafting model saw ([n] numbering, title, as-of date, URL): the compliance
    # reviewer and the traceability checks get the same text, so citations and URLs can be verified.
    return {"draft": draft, "sources": sources, "chunks": chunks, "excerpts": excerpts}
