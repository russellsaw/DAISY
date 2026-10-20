"""Eligibility agent: the only agent with a tool, and the tool is the rules engine.

The LLM extracts nothing here (the orchestrator did that). It either asks for
the fields the engine says are missing, or narrates what the engine computed.
"""
from __future__ import annotations

import json
from datetime import date

from .. import config, llm
from ..rules import assess_all
from ..rules import counterfactual
from ..state import PROFILE_FIELDS, ConversationState
from . import prompts

DEFAULT_PRODUCTS = ["personal_loan", "credit_card", "home_loan"]


def _for_narration(cf: dict) -> dict:
    """What the narrating LLM may see of a counterfactual: the rule, the working, the levers.
    Dates and outcome wording are deliberately left out — code writes those."""
    def after(path):
        return next((c for c in path["engine_result"]["checks"] if c["rule"] == cf["binding_rule"]), None)
    return {
        "product": cf["product_name"],
        "binding_rule": cf["binding_label"],
        "current_value": cf["current_value"], "threshold": cf["threshold"], "gap": cf["gap"],
        "other_failing_rules": [r for r in cf["failing_rules"] if r != cf["binding_rule"]],
        "levers": [{"kind": x["lever_kind"], "change": x["description"].split(" (you told us")[0],
                    "binding_rule_value_after": (after(x) or {}).get("value"), "headroom": x["headroom"]}
                   for x in cf["paths"]],
        "borderline_levers": [{"change": x["description"], "headroom": x["headroom"]} for x in cf["borderline"]],
        "no_path": not cf["paths"],
    }


def assess(state: ConversationState, user_msg: str, products: list[str], today: date | None = None) -> dict:
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
        state.last_counterfactuals = []
        return {"draft": draft, "results": state.last_eligibility, "computations": {}, "asked_for": missing,
                "counterfactuals": []}

    # "Not now, but when": counterfactual search for each product that fails today.
    cfs = [cf for cf in (counterfactual.search(state.profile, r.product, today=today)
                         for r in runnable if r.status == "not_eligible") if cf]
    state.last_counterfactuals = cfs
    if cfs:
        state.add_trace(
            "Counterfactual",
            "; ".join(f"{cf['product']}: binding {cf['binding_rule']}, "
                      f"{len(cf['paths'])} path(s) ({', '.join(x['lever_kind'] for x in cf['paths']) or 'none'}), "
                      f"{len(cf['borderline'])} borderline" for cf in cfs),
            cfs,
        )

    payload = {
        "profile": state.profile.to_dict(),
        "results": [{k: v for k, v in r.to_dict().items() if k != "checks"} for r in runnable],
        "what_would_change": [_for_narration(cf) for cf in cfs],
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
                     model=config.MODEL_MAIN, max_tokens=4096, effort="medium")

    # Collect every number the engine produced so compliance can whitelist them.
    computations = {}
    for r in runnable:
        computations.update({f"{r.product}.{k}": v for k, v in r.computations.items()})
    computations.update(counterfactual.numbers_in(cfs))
    for i, c in enumerate(state.profile.commitments):
        computations[f"commitment.{i}.monthly"] = c.get("monthly")
    sources = [{"n": None, "title": r.product_name, "url": r.sources[0]} for r in runnable if r.sources]
    return {"draft": draft, "results": state.last_eligibility, "computations": computations, "sources": sources,
            "counterfactuals": cfs}
