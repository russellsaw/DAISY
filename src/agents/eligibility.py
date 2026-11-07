"""Eligibility agent: the only agent with a tool, and the tool is the rules engine.

The LLM extracts nothing here (the orchestrator did that). In order:
  1. details missing  -> the LLM asks for them (no engine call)
  2. not yet confirmed -> a code-written readback of every parameter (no engine call)
  3. confirmed        -> the engine runs, then the LLM narrates what it computed
"""

from __future__ import annotations

import json
import re
from datetime import date

from .. import config, llm
from ..rules import assess_all, counterfactual, max_loan
from ..rules.counterfactual import fmt_date
from ..rules.criteria import PRODUCTS, SOURCE_TITLES
from ..rules.engine import EligibilityResult, missing_fields
from ..state import PROFILE_FIELDS, ConversationState, CustomerProfile, is_property_loan
from ..ui import EMPLOYMENT_LABELS, PROPERTY_LABELS, RESIDENCY_LABELS
from . import product_knowledge, prompts

DEFAULT_PRODUCTS = ["personal_loan", "credit_card", "home_loan"]


def _no_quote(description: str) -> str:
    """The lever without the customer's words about its end date, which can contain the date."""
    return re.sub(r" \(you told us: [^)]*\)", "", description)


def _for_narration(cf: dict) -> dict:
    """What the narrating LLM may see of a counterfactual: the rule, the working, the levers.
    Dates and outcome wording are deliberately left out — code writes those."""

    def after(path):
        return next((c for c in path["engine_result"]["checks"] if c["rule"] == cf["binding_rule"]), None)

    return {
        "product": cf["product_name"],
        "binding_rule": cf["binding_label"],
        "current_value": cf["current_value"],
        "threshold": cf["threshold"],
        "gap": cf["gap"],
        "other_failing_rules": [r for r in cf["failing_rules"] if r != cf["binding_rule"]],
        "levers": [
            {
                "kind": x["lever_kind"],
                "change": _no_quote(x["description"]),
                "binding_rule_value_after": (after(x) or {}).get("value"),
                "headroom": x["headroom"],
            }
            for x in cf["paths"]
        ],
        "borderline_levers": [
            {"change": _no_quote(x["description"]), "headroom": x["headroom"]} for x in cf["borderline"]
        ],
        "no_path": not cf["paths"],
    }


def _to_ask(profile: CustomerProfile, product: str) -> list[str]:
    """What's needed before a check. The engine needs missing_fields; on top of that, a home-loan check asks about
    employment rather than assume a salary, because self-employed and variable income count for less."""
    missing = missing_fields(profile, product)
    if product in ("home_loan", "max_home_loan") and profile.employment_type is None:
        missing = [*missing, "employment_type"]
    return missing


def _not_checked(profile: CustomerProfile, product: str) -> EligibilityResult:
    """A product the customer named that still needs details: reported as such, never run on an assumption. The engine
    alone would run a home loan without employment (counting income in full), so this is decided here, from _to_ask."""
    prod = PRODUCTS[product]
    return EligibilityResult(
        product, prod["name"], "need_more_info", missing_fields=_to_ask(profile, product), sources=[prod["source"]]
    )


def _waiting_lines(waiting: dict[str, list[str]]) -> str:
    """Named products the check will leave out, and what each still needs, so the customer knows before saying yes."""
    lines = []
    for product, fields in waiting.items():
        labels = [PROFILE_FIELDS[f][0][0].lower() + PROFILE_FIELDS[f][0][1:] for f in fields]
        name = PRODUCTS[product]["name"]
        lines.append(f"I can't include the {name} in this check yet. It also needs: {', '.join(labels)}.")
    return "".join(f"\n\n{line}" for line in lines)


def readback_text(
    profile: CustomerProfile,
    products: list[str] | None = None,
    max_loan_check: bool = False,
    waiting: dict[str, list[str]] | None = None,
) -> str:
    """The readback, written by code: exact values, nothing paraphrased by a model. Anything the check
    would otherwise treat as none is shown as such, so the customer can correct it before it runs.
    waiting: products the customer named that this check leaves out, with the details each still needs."""
    d = profile.to_dict()
    products = products or []
    not_mentioned = "not mentioned, so treated as none"
    money = lambda v: f"S${v:,.0f}"  # noqa: E731
    lines = []
    if "age" in d:
        lines.append(f"Age: {d['age']}")
    if "residency" in d:
        lines.append(f"Residency: {RESIDENCY_LABELS.get(d['residency'], d['residency'])}")
    if "monthly_income" in d:
        lines.append(f"Gross monthly income: {money(d['monthly_income'])}")
    if "employment_type" in d:
        lines.append(f"Employment: {EMPLOYMENT_LABELS.get(d['employment_type'], d['employment_type'])}")
    for c in d.get("commitments", []):
        ends = (
            f"ending {fmt_date(c['end_date'])} (you said \u201c{c['end_quote']}\u201d)"
            if c.get("end_date")
            else "no end date given"
        )
        label = f"Existing {c['kind']}" if is_property_loan(c["kind"]) else c["kind"].capitalize()
        lines.append(f"{label}: {money(c['monthly'])} a month, {ends}")
    for kind in d.get("commitments_missing_amount", []):
        lines.append(f"{kind.capitalize()}: amount not given")
    itemised = sum(c["monthly"] for c in profile.commitments if is_property_loan(c["kind"]))
    if (d.get("existing_property_loan_monthly") or 0) > itemised:  # the figure the check will use
        lines.append(f"Existing property-loan repayments: {money(d['existing_property_loan_monthly'])} a month")
    if "existing_monthly_debt" in d:
        lines.append(
            "Other monthly repayments: " + (money(d["existing_monthly_debt"]) if d["existing_monthly_debt"] else "none")
        )
    elif "home_loan" in products and not profile.commitments:
        lines.append(f"Other monthly repayments: {not_mentioned}")
    if "existing_unsecured_balance" in d:
        lines.append(f"Unsecured balances (cards, credit lines): {money(d['existing_unsecured_balance'])}")
    elif "personal_loan" in products or "cashline" in products:
        lines.append(f"Unsecured balances (cards, credit lines): {not_mentioned}")
    if "property_type" in d or "property_price" in d:
        prop = PROPERTY_LABELS.get(d.get("property_type"), "property")
        lines.append(f"Property: {prop}" + (f" at {money(d['property_price'])}" if "property_price" in d else ""))
    if "existing_housing_loans" in d:
        lines.append(f"Existing housing loans: {d['existing_housing_loans'] or 'none'}")
    elif "home_loan" in products and not profile.property_loan_monthly():
        lines.append(f"Existing housing loans: {not_mentioned}")
    if "home_loan_amount" in d:
        lines.append(
            f"New home loan: {money(d['home_loan_amount'])}"
            + (f" over {d['tenure_years']} years" if "tenure_years" in d else "")
        )
    if "personal_loan_amount" in d:
        lines.append(f"Personal loan: {money(d['personal_loan_amount'])}")
    if max_loan_check and "tenure_years" not in d:
        tenure = max_loan.default_tenure(profile)
        lines.append(
            f"Tenure: not given, so I'll use {tenure} years, "
            + (
                "the longest that keeps the full loan-to-value limit"
                if max_loan.keeps_full_ltv(profile, tenure)
                else "the shortest available (the lower loan-to-value limit applies at your age)"
            )
        )
    elif max_loan_check and "home_loan_amount" not in d:
        lines.append(f"Tenure: {d['tenure_years']} years")
    body = "\n".join(f"- {line}" for line in lines)
    return (
        "Before I run the eligibility check, here's what I've noted. Is it all right?\n\n"
        + body
        + _waiting_lines(waiting or {})
        + "\n\nReply **yes** if that's right, or tell me what to change. I won't run the check until you do."
    )


def readback_numbers(profile: CustomerProfile) -> dict:
    """Numbers and date parts the readback may contain (all from the customer's own statements)."""
    out = {}
    for i, c in enumerate(profile.commitments):
        out[f"commitment.{i}.monthly"] = c.get("monthly")
        # The readback prints the customer's own words about the end date, on later turns too.
        for j, number in enumerate(re.findall(r"\d+(?:\.\d+)?", (c.get("end_quote") or "").replace(",", ""))):
            out[f"commitment.{i}.quote.{j}"] = float(number)
        if c.get("end_date"):
            d = date.fromisoformat(c["end_date"])
            out[f"commitment.{i}.end.y"], out[f"commitment.{i}.end.d"] = d.year, d.day
    return out


def _ask_for(state: ConversationState, user_msg: str, missing: list[str], keys: list[str], search_query: str) -> dict:
    labels = [f"{PROFILE_FIELDS[f][0]} — {PROFILE_FIELDS[f][1]}" for f in missing]
    # The published criteria relevant to the question ("can I get a card at 66?"), so the reply can
    # state the rule with a citation before asking — the engine still isn't called.
    chunks, excerpts = product_knowledge.search(user_msg, keys, search_query, k=4)
    prompt = (
        f"Products being checked: {', '.join(keys)}\n"
        f"Known so far:\n{state.profile.describe()}\n\n"
        f"Missing fields to ask for:\n- "
        + "\n- ".join(labels)
        + f"\n\nSource excerpts (published criteria):\n{excerpts or '(none)'}"
        f"\n\nCustomer's latest message:\n{user_msg}"
    )
    draft = llm.chat(prompts.ELIGIBILITY_ASK, [{"role": "user", "content": prompt}], model=config.MODEL_MAIN)
    sources, cited = product_knowledge.cited_sources(draft, chunks)
    state.last_counterfactuals, state.last_eligibility = [], []
    state.add_trace(
        "Eligibility",
        f"needs {', '.join(missing)} before anything can run (engine not called)",
        product_knowledge.hits_detail(user_msg, search_query, keys, chunks, cited),
    )
    return {
        "draft": draft,
        "results": [],
        "computations": readback_numbers(state.profile),
        "asked_for": missing,
        "counterfactuals": [],
        "engine_ran": False,
        "sources": sources,
        "excerpts": excerpts,
    }


def assess_max_loan(state: ConversationState, user_msg: str, search_query: str = "") -> dict:
    """'How much can I borrow?' The same steps as an eligibility check (ask, read back, run on confirmed details),
    but the result is the largest loan the engine passes, written by code."""
    state.pending_max_loan = True
    missing = _to_ask(state.profile, "max_home_loan")
    if missing:
        return _ask_for(state, user_msg, missing, ["home_loan"], search_query)
    if not state.is_confirmed():
        state.start_readback(["home_loan"])
        state.last_counterfactuals, state.last_eligibility = [], []
        state.add_trace(
            "Readback", "read back the parameters for the maximum loan; engine waits for a yes", state.readback_profile
        )
        return {
            "draft": readback_text(state.profile, ["home_loan"], max_loan_check=True),
            "results": [],
            "computations": readback_numbers(state.profile),
            "counterfactuals": [],
            "engine_ran": False,
            "readback": True,
        }
    result = max_loan.find(state.profile)
    state.pending_max_loan = False
    state.last_max_loan = result
    state.last_counterfactuals, state.last_eligibility = [], []
    state.add_trace(
        "Eligibility",
        "engine searched for the largest loan at the MAS floor and DBS's planning rate: "
        f"{result['at_floor']['max_loan']} and {result['at_planner']['max_loan']}",
        {"input_profile": state.profile.to_dict(), "criteria_version": result["criteria_version"], "max_loan": result},
    )
    sources = [{"n": None, "title": SOURCE_TITLES.get(url, url), "url": url} for url in result["sources"]]
    return {
        "draft": max_loan.text(result),
        "results": [],
        "computations": {**max_loan.numbers_in(result), **readback_numbers(state.profile)},
        "sources": sources,
        "counterfactuals": [],
        "engine_ran": True,
        "code_written": True,
        "max_loan": result,
    }


def assess(
    state: ConversationState, user_msg: str, products: list[str], today: date | None = None, search_query: str = ""
) -> dict:
    keys = products or DEFAULT_PRODUCTS
    ready = [k for k in keys if not _to_ask(state.profile, k)]

    # 1. Missing details: ask for them. Decided from the required fields — the engine is not called.
    if not ready:
        missing = sorted({f for k in keys for f in _to_ask(state.profile, k)}, key=list(PROFILE_FIELDS).index)
        return _ask_for(state, user_msg, missing, keys, search_query)

    # 2. Readback before clearance: no engine call — forward or counterfactual — until the customer
    #    has said yes to exactly these parameters.
    if not state.is_confirmed():
        # Keep a product the customer named but hasn't given details for, so the result can say it wasn't checked.
        state.start_readback(keys if products else ready)
        state.last_counterfactuals, state.last_eligibility = [], []
        state.add_trace(
            "Readback",
            f"read back the parameters for {', '.join(ready)}; engine waits for a yes",
            state.readback_profile,
        )
        waiting = {k: _to_ask(state.profile, k) for k in keys if k not in ready} if products else {}
        return {
            "draft": readback_text(state.profile, ready, waiting=waiting),
            "results": [],
            "computations": readback_numbers(state.profile),
            "counterfactuals": [],
            "engine_ran": False,
            "readback": True,
        }

    # 3. Confirmed: run the engine on each product whose details are complete. A named product still waiting on
    #    something (e.g. employment for a home loan, asked about together with a card) comes back as needing more
    #    details instead of being run on an assumed salary.
    checked = {r.product: r for r in assess_all(state.profile, ready)}
    results = [checked[k] if k in checked else _not_checked(state.profile, k) for k in keys if k in PRODUCTS]
    state.last_eligibility = [r.to_dict() for r in results]
    runnable = [r for r in results if r.status != "need_more_info"]
    blocked = [r for r in results if r.status == "need_more_info"]
    state.add_trace(
        "Eligibility",
        "engine ran on confirmed parameters: " + ", ".join(f"{r.product}={r.status}" for r in results),
        {
            "input_profile": state.profile.to_dict(),
            "products": keys,
            "criteria_version": results[0].criteria_version if results else None,
            "results": state.last_eligibility,
        },
    )

    # "Not now, but when": counterfactual search for each product that fails today.
    runs: list[dict] = []  # every counterfactual evaluation, for the flight recorder
    cfs = [
        cf
        for cf in (
            counterfactual.search(state.profile, r.product, today=today, log=runs)
            for r in runnable
            if r.status == "not_eligible"
        )
        if cf
    ]
    state.last_counterfactuals = cfs
    if cfs:
        state.add_trace(
            "Counterfactual",
            "; ".join(
                f"{cf['product']}: binding {cf['binding_rule']}, "
                f"{len(cf['paths'])} path(s) ({', '.join(x['lever_kind'] for x in cf['paths']) or 'none'}), "
                f"{len(cf['borderline'])} borderline"
                for cf in cfs
            )
            + f"; {len(runs)} engine runs",
            {"counterfactuals": cfs, "runs": runs},
        )

    payload = {
        "profile": state.profile.to_dict(),
        "results": [{k: v for k, v in r.to_dict().items() if k != "checks"} for r in runnable],
        "what_would_change": [_for_narration(cf) for cf in cfs],
        "still_need_info_for": [
            {"product": r.product_name, "missing": [PROFILE_FIELDS[f][0] for f in r.missing_fields]} for r in blocked
        ],
    }
    prompt = "Engine output (JSON):\n" + json.dumps(payload, indent=1) + f"\n\nCustomer's latest message:\n{user_msg}"
    draft = llm.chat(
        prompts.ELIGIBILITY_NARRATE,
        [{"role": "user", "content": prompt}],
        model=config.MODEL_MAIN,
        max_tokens=4096,
        effort="medium",
    )

    # Collect every number the engine produced so compliance can whitelist them.
    computations = {}
    for r in runnable:
        computations.update({f"{r.product}.{k}": v for k, v in r.computations.items()})
    computations.update(counterfactual.numbers_in(cfs))
    computations.update(readback_numbers(state.profile))
    # Every page behind the checks that ran: the product page and the MAS/HDB rules applied to it.
    urls = dict.fromkeys(url for r in runnable for url in r.sources)
    sources = [{"n": None, "title": SOURCE_TITLES.get(url, url), "url": url} for url in urls]
    return {
        "draft": draft,
        "results": state.last_eligibility,
        "computations": computations,
        "sources": sources,
        "counterfactuals": cfs,
        "engine_ran": True,
    }
