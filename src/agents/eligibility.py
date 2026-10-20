"""Eligibility agent: the only agent with a tool, and the tool is the rules engine.

The LLM extracts nothing here (the orchestrator did that). It either asks for
the fields the engine says are missing, or narrates what the engine computed.
"""
from __future__ import annotations

import json

from .. import config, llm
from ..rules import assess_all
from ..state import PROFILE_FIELDS, ConversationState
from . import prompts

DEFAULT_PRODUCTS = ["personal_loan", "credit_card", "home_loan"]


def assess(state: ConversationState, user_msg: str, products: list[str]) -> dict:
    keys = products or DEFAULT_PRODUCTS
    results = assess_all(state.profile, keys)
    state.last_eligibility = [r.to_dict() for r in results]

    runnable = [r for r in results if r.status != "need_more_info"]
    blocked = [r for r in results if r.status == "need_more_info"]

    state.add_trace(
        "Eligibility",
        "engine ran: " + ", ".join(f"{r.product}={r.status}" for r in results),
        state.last_eligibility,
    )

    # Nothing could run: ask for the smallest set of missing fields.
    if not runnable:
        missing = sorted({f for r in blocked for f in r.missing_fields}, key=list(PROFILE_FIELDS).index)
        # Prefer asking for the home-loan set only if home loan was explicitly requested.
        labels = [f"{PROFILE_FIELDS[f][0]} — {PROFILE_FIELDS[f][1]}" for f in missing]
        prompt = (
            f"Products being checked: {', '.join(r.product_name for r in blocked)}\n"
            f"Known so far:\n{state.profile.describe()}\n\n"
            f"Missing fields to ask for:\n- " + "\n- ".join(labels) +
            f"\n\nCustomer's latest message:\n{user_msg}"
        )
        draft = llm.chat(prompts.ELIGIBILITY_ASK, [{"role": "user", "content": prompt}], model=config.MODEL_MAIN)
        return {"draft": draft, "results": state.last_eligibility, "computations": {}, "asked_for": missing}

    payload = {
        "profile": state.profile.to_dict(),
        "results": [r.to_dict() for r in runnable],
        "still_need_info_for": [
            {"product": r.product_name, "missing": [PROFILE_FIELDS[f][0] for f in r.missing_fields]}
            for r in blocked
        ],
    }
    prompt = (
        "Engine output (JSON):\n" + json.dumps(payload, indent=1) +
        f"\n\nCustomer's latest message:\n{user_msg}"
    )
    draft = llm.chat(prompts.ELIGIBILITY_NARRATE, [{"role": "user", "content": prompt}],
                     model=config.MODEL_MAIN, max_tokens=1200)

    # Collect every number the engine produced so compliance can whitelist them.
    computations = {}
    for r in runnable:
        computations.update({f"{r.product}.{k}": v for k, v in r.computations.items()})
    sources = [{"n": None, "title": r.product_name, "url": r.sources[0]} for r in runnable if r.sources]
    return {"draft": draft, "results": state.last_eligibility, "computations": computations, "sources": sources}
