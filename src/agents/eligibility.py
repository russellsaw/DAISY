"""Eligibility agent: the only agent with a tool, and the tool is the rules engine.

The LLM extracts nothing here (the orchestrator did that). In order:
  1. details missing  -> the LLM asks for them (no engine call)
  2. not yet confirmed -> a code-written readback of every parameter (no engine call)
  3. confirmed        -> the engine runs, then the LLM narrates what it computed
"""
from __future__ import annotations

import json
from datetime import date

from .. import config, llm
from ..rules import assess_all
from ..rules import counterfactual
from ..rules.counterfactual import fmt_date
from ..rules.engine import REQUIRED_FIELDS
from ..state import PROFILE_FIELDS, ConversationState, CustomerProfile
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


_RESIDENCY = {"citizen": "Singapore Citizen", "pr": "Singapore PR", "foreigner": "Foreigner"}
_PROPERTY = {"hdb": "HDB flat", "ec": "Executive Condominium", "private": "private property"}


def readback_text(profile: CustomerProfile) -> str:
    """The readback, written by code: exact values, nothing paraphrased by a model."""
    d = profile.to_dict()
    money = lambda v: f"S${v:,.0f}"  # noqa: E731
    lines = []
    if "age" in d:
        lines.append(f"Age: {d['age']}")
    if "residency" in d:
        lines.append(f"Residency: {_RESIDENCY.get(d['residency'], d['residency'])}")
    if "monthly_income" in d:
        lines.append(f"Gross monthly income: {money(d['monthly_income'])}")
    if "employment_type" in d:
        lines.append(f"Employment: {d['employment_type'].replace('_', '-')}")
    for c in d.get("commitments", []):
        ends = (f"ending {fmt_date(c['end_date'])} (you said \u201c{c['end_quote']}\u201d)" if c.get("end_date")
                else "no end date given")
        lines.append(f"{c['kind'].capitalize()}: {money(c['monthly'])} a month, {ends}")
    if d.get("existing_monthly_debt"):
        lines.append(f"Other monthly repayments: {money(d['existing_monthly_debt'])}")
    if "existing_unsecured_balance" in d:
        lines.append(f"Unsecured balances (cards, credit lines): {money(d['existing_unsecured_balance'])}")
    if "property_type" in d or "property_price" in d:
        prop = _PROPERTY.get(d.get("property_type"), "property")
        lines.append(f"Property: {prop}" + (f" at {money(d['property_price'])}" if "property_price" in d else ""))
    if "existing_housing_loans" in d:
        lines.append(f"Existing housing loans: {d['existing_housing_loans'] or 'none'}")
    if "home_loan_amount" in d:
        lines.append(f"Home loan: {money(d['home_loan_amount'])}"
                     + (f" over {d['tenure_years']} years" if "tenure_years" in d else ""))
    if "personal_loan_amount" in d:
        lines.append(f"Personal loan: {money(d['personal_loan_amount'])}")
    body = "\n".join(f"- {line}" for line in lines)
    return ("Before I run the eligibility check, here's what I've noted. Is it all right?\n\n" + body +
            "\n\nReply **yes** if that's right, or tell me what to change. I won't run the check until you do.")


def readback_numbers(profile: CustomerProfile) -> dict:
    """Numbers and date parts the readback may contain (all from the customer's own statements)."""
    out = {}
    for i, c in enumerate(profile.commitments):
        out[f"commitment.{i}.monthly"] = c.get("monthly")
        if c.get("end_date"):
            d = date.fromisoformat(c["end_date"])
            out[f"commitment.{i}.end.y"], out[f"commitment.{i}.end.d"] = d.year, d.day
    return out


def assess(state: ConversationState, user_msg: str, products: list[str], today: date | None = None) -> dict:
    keys = products or DEFAULT_PRODUCTS
    ready = [k for k in keys if not state.profile.missing(REQUIRED_FIELDS[k])]

    # 1. Missing details: ask for them. Decided from REQUIRED_FIELDS — the engine is not called.
    if not ready:
        missing = sorted({f for k in keys for f in state.profile.missing(REQUIRED_FIELDS[k])},
                         key=list(PROFILE_FIELDS).index)
        labels = [f"{PROFILE_FIELDS[f][0]} — {PROFILE_FIELDS[f][1]}" for f in missing]
        prompt = (
            f"Products being checked: {', '.join(keys)}\n"
            f"Known so far:\n{state.profile.describe()}\n\n"
            f"Missing fields to ask for:\n- " + "\n- ".join(labels) +
            f"\n\nCustomer's latest message:\n{user_msg}"
        )
        draft = llm.chat(prompts.ELIGIBILITY_ASK, [{"role": "user", "content": prompt}], model=config.MODEL_MAIN)
        state.last_counterfactuals = []
        state.add_trace("Eligibility", f"needs {', '.join(missing)} before anything can run (engine not called)")
        return {"draft": draft, "results": [], "computations": readback_numbers(state.profile),
                "asked_for": missing, "counterfactuals": [], "engine_ran": False}

    # 2. Readback before clearance: no engine call — forward or counterfactual — until the customer
    #    has said yes to exactly these parameters.
    if not state.is_confirmed():
        state.start_readback(ready)
        state.last_counterfactuals = []
        state.add_trace("Readback", f"read back the parameters for {', '.join(ready)}; engine waits for a yes",
                        state.readback_profile)
        return {"draft": readback_text(state.profile), "results": [], "computations": readback_numbers(state.profile),
                "counterfactuals": [], "engine_ran": False, "readback": True}

    # 3. Confirmed: run the engine.
    results = assess_all(state.profile, keys)
    state.last_eligibility = [r.to_dict() for r in results]
    runnable = [r for r in results if r.status != "need_more_info"]
    blocked = [r for r in results if r.status == "need_more_info"]
    state.add_trace(
        "Eligibility",
        "engine ran on confirmed parameters: " + ", ".join(f"{r.product}={r.status}" for r in results),
        state.last_eligibility,
    )

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
    computations.update(readback_numbers(state.profile))
    sources = [{"n": None, "title": r.product_name, "url": r.sources[0]} for r in runnable if r.sources]
    return {"draft": draft, "results": state.last_eligibility, "computations": computations, "sources": sources,
            "counterfactuals": cfs, "engine_ran": True}
