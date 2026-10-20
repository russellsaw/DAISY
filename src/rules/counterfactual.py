"""'Not now, but when' — counterfactual eligibility.

There is ONE engine (engine.py). This module never re-implements a rule or solves a rule
backwards. It builds modified copies of the customer's profile ("levers"), runs the same forward
check on each, and keeps a lever as a path only if the forward engine passes it with headroom.

Two kinds of lever:
  dated   remove a commitment the customer stated WITH an end date. Evaluated as of that date
          (the customer is aged to it). The only lever that may carry a date, and the date is
          exactly the stated end date.
  whatif  income higher, home loan smaller, tenure changed, an undated commitment or balance
          cleared. Reported as "if ..., then ..." and never given a date.

Search budget: single levers first; pairs only if no single lever is a path; never three.
A lever that passes but clears a ratio that fails today by less than config.CF_MARGIN is
"borderline", not a path. If nothing is a path, `path` is None and the caller hands off to a banker.
"""
from __future__ import annotations

import copy
import itertools
import math
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterator

from .. import config
from ..state import CustomerProfile
from .criteria import criteria_version
from .engine import CHECKS

# The outcome sentence. Inserted verbatim by code after any path is shown; the LLM never writes it.
OUTCOME_TEMPLATE = (
    "Based on what you've told us and DBS's criteria as of {assessed_on}, this would clear the eligibility "
    "check — it's indicative, not an approval, and a banker would re-run it at the time."
)
NO_PATH_TEMPLATE = (
    "On DBS's criteria as of {assessed_on}, I can't see a clear change that would get the {product_name} over "
    "the line, so I've prepared a summary for a DBS banker who can look at your full situation."
)
NO_PATH_TRIGGER = "no clear path on today's criteria"

RULE_LABELS = {
    "tdsr": "TDSR (total debt servicing ratio)",
    "msr": "MSR (mortgage servicing ratio)",
    "ltv": "loan-to-value limit",
    "unsecured_utilisation": "industry-wide unsecured borrowing limit",
    "min_annual_income": "minimum annual income",
    "min_age": "minimum age",
    "max_age": "maximum age",
    "max_tenure": "maximum loan tenure",
    "hdb_buyer_residency": "HDB buyer residency rule",
    "assessable_income": "assessable income",
}


@dataclass
class Lever:
    kind: str                                   # "dated" | "whatif"
    name: str
    apply: Callable[[CustomerProfile], CustomerProfile] | None = None
    describe: Callable[[CustomerProfile], str] | None = None
    when: date | None = None                    # dated levers only
    steps: Callable[[CustomerProfile], Iterator[tuple[CustomerProfile, str]]] | None = None  # searched what-ifs


def fmt_date(d: date | str) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d.day} {d.strftime('%b %Y')}"


def _aged(p: CustomerProfile, when: date, today: date) -> CustomerProfile:
    """The profile as of `when`. Birthdays are unknown, so age is rounded UP (conservative for the
    age-65 LTV rule and maximum ages)."""
    q = copy.deepcopy(p)
    if when > today and q.age is not None:
        q.age = q.age + math.ceil((when - today).days / 365.25)
    return q


# ---------------------------------------------------------------- levers

def _levers(p: CustomerProfile, product: str, today: date) -> list[Lever]:
    levers: list[Lever] = []

    for i, c in enumerate(p.commitments):
        def remove(q, i=i):
            q = copy.deepcopy(q)
            q.commitments = [x for j, x in enumerate(q.commitments) if j != i]
            return q
        label = f"your {c['kind']} (S${c['monthly']:,.0f}/month)"
        if c.get("end_date"):
            end = date.fromisoformat(c["end_date"])
            levers.append(Lever("dated", f"commitment_ends:{c['kind']}",
                                apply=lambda q, f=remove, end=end: _aged(f(q), end, today),
                                describe=lambda q, label=label, quote=c.get("end_quote"):
                                    f"{label} has ended" + (f" (you told us: \u201c{quote}\u201d)" if quote else ""),
                                when=end))
        else:
            levers.append(Lever("whatif", f"commitment_cleared:{c['kind']}", apply=remove,
                                describe=lambda q, label=label: f"{label} were cleared (you haven't given an end date)"))

    if p.existing_monthly_debt:
        def clear_other(q):
            q = copy.deepcopy(q)
            q.existing_monthly_debt = 0.0
            return q
        levers.append(Lever("whatif", "other_debt_cleared", apply=clear_other,
                            describe=lambda q, v=p.existing_monthly_debt: f"your other monthly repayments (S${v:,.0f}) were cleared"))

    if product != "home_loan" and p.existing_unsecured_balance:
        def clear_unsecured(q):
            q = copy.deepcopy(q)
            q.existing_unsecured_balance = 0.0
            return q
        levers.append(Lever("whatif", "unsecured_balance_cleared", apply=clear_unsecured,
                            describe=lambda q, v=p.existing_unsecured_balance: f"your existing unsecured balance (S${v:,.0f}) were paid down"))

    # ---- searched what-ifs: smallest step first, within realistic bounds ----
    if p.monthly_income:
        def income_steps(q):
            base = q.monthly_income
            limit = base * config.CF_MAX_INCOME_UPLIFT
            amount = config.CF_INCOME_STEP
            while amount <= limit + 1e-9:
                r = copy.deepcopy(q)
                r.monthly_income = base + amount
                yield r, f"your gross monthly income were S${base + amount:,.0f} (S${amount:,.0f} more than today)"
                amount += config.CF_INCOME_STEP
        levers.append(Lever("whatif", "income_higher", steps=income_steps))

    if product == "home_loan" and p.home_loan_amount:
        def loan_steps(q):
            base = q.home_loan_amount
            limit = base * config.CF_MAX_HOME_LOAN_CUT
            amount = config.CF_HOME_LOAN_STEP
            while amount <= limit + 1e-9:
                r = copy.deepcopy(q)
                r.home_loan_amount = base - amount
                yield r, f"you borrowed S${base - amount:,.0f} instead of S${base:,.0f} (S${amount:,.0f} less)"
                amount += config.CF_HOME_LOAN_STEP
        levers.append(Lever("whatif", "home_loan_smaller", steps=loan_steps))

    if product == "home_loan" and p.tenure_years:
        def tenure_steps(q):
            current = q.tenure_years
            for t in sorted(range(5, 36), key=lambda t: (abs(t - current), t)):
                if t == current:
                    continue
                r = copy.deepcopy(q)
                r.tenure_years = t
                yield r, f"the loan tenure were {t} years instead of {current}"
        levers.append(Lever("whatif", "tenure_changed", steps=tenure_steps))

    if product == "personal_loan" and p.personal_loan_amount:
        def pl_steps(q):
            base = q.personal_loan_amount
            amount = config.CF_PERSONAL_LOAN_STEP
            while amount < base:
                r = copy.deepcopy(q)
                r.personal_loan_amount = base - amount
                yield r, f"you borrowed S${base - amount:,.0f} instead of S${base:,.0f}"
                amount += config.CF_PERSONAL_LOAN_STEP
        levers.append(Lever("whatif", "personal_loan_smaller", steps=pl_steps))

    return levers


# ---------------------------------------------------------------- evaluation

def _binding(failed: list[dict]) -> dict:
    """The rule to explain first: absolute rules (residency, no income) before ratios/thresholds,
    then the largest relative gap."""
    def severity(c):
        if c["kind"] == "rule":
            return (0, 0)
        gap = (c["value"] - c["threshold"]) if c["kind"] in ("ratio", "max") else (c["threshold"] - c["value"])
        return (1, -abs(gap) / (abs(c["threshold"]) or 1))
    return sorted(failed, key=severity)[0]


def _gap(c: dict):
    if c["kind"] in ("ratio", "max"):
        return round(c["value"] - c["threshold"], 4)
    if c["kind"] == "min":
        return round(c["threshold"] - c["value"], 4)
    return None


def _evaluate(product: str, q: CustomerProfile, watch: set[str], margin: float):
    """Run the forward engine; classify as fail / borderline / path."""
    res = CHECKS[product](q)
    if res.status != "eligible":
        return res, "fail", None
    ratios = [c for c in res.checks if c["rule"] in watch and c["kind"] == "ratio"]
    headroom = round(min(c["threshold"] - c["value"] for c in ratios), 4) if ratios else None
    if headroom is not None and headroom < margin - 1e-9:
        return res, "borderline", headroom
    return res, "path", headroom


def _entry(kind: str, name: str, description: str, q: CustomerProfile, res, headroom, when: date | None,
           label: str = "path") -> dict:
    return {
        "lever_kind": kind,
        "lever": name,
        "description": description,
        "counterfactual_profile": q.to_dict(),
        "engine_result": {"status": res.status, "checks": res.checks, "computations": res.computations,
                          "criteria_version": res.criteria_version},
        "headroom": headroom,
        "earliest_date": when.isoformat() if (kind == "dated" and when) else None,
        "label": label,
    }


def _try(product, lever: Lever, base: CustomerProfile, watch, margin, prefix=None):
    """Return (best_path_entry | None, borderline_entry | None) for one lever applied to `base`."""
    pre_desc, pre_kind, pre_when, pre_name = prefix or ("", None, None, "")
    if lever.steps is None:
        q = lever.apply(base)
        res, verdict, headroom = _evaluate(product, q, watch, margin)
        kind = "dated" if lever.kind == "dated" and pre_kind in (None, "dated") else "whatif"
        when = max([d for d in (lever.when, pre_when) if d], default=None) if kind == "dated" else None
        desc = (pre_desc + " and " if pre_desc else "") + lever.describe(base)
        name = (pre_name + "+" if pre_name else "") + lever.name
        if verdict == "path":
            return _entry(kind, name, desc, q, res, headroom, when), None
        if verdict == "borderline":
            return None, _entry(kind, name, desc, q, res, headroom, when, label="borderline")
        return None, None
    borderline = None
    for q, step_desc in lever.steps(base):
        res, verdict, headroom = _evaluate(product, q, watch, margin)
        desc = (pre_desc + " and " if pre_desc else "") + step_desc
        name = (pre_name + "+" if pre_name else "") + lever.name
        if verdict == "path":
            return _entry("whatif", name, desc, q, res, headroom, None), borderline
        if verdict == "borderline" and borderline is None:
            borderline = _entry("whatif", name, desc, q, res, headroom, None, label="borderline")
    return None, borderline


def search(p: CustomerProfile, product: str, *, today: date | None = None,
           margin: float | None = None) -> dict | None:
    """Counterfactual search for one product. None if the product isn't failing today."""
    today = today or date.today()
    margin = config.CF_MARGIN if margin is None else margin
    current = CHECKS[product](p)
    if current.status != "not_eligible":
        return None

    failed = [c for c in current.checks if not c["passed"]]
    binding = _binding(failed)
    watch = {c["rule"] for c in failed if c["kind"] == "ratio"}
    levers = _levers(p, product, today)

    paths, borderline = [], []
    for lever in levers:  # single levers
        path, near = _try(product, lever, p, watch, margin)
        if path:
            paths.append(path)
        elif near:
            borderline.append(near)

    if not paths:  # pairs, only when no single lever works; never three
        fixed = [lv for lv in levers if lv.steps is None]
        for a, b in itertools.combinations(levers, 2):
            first, second = (a, b) if a.steps is None else (b, a)
            if first.steps is not None:
                continue  # never search two quantities at once
            prefix = (first.describe(p), first.kind, first.when, first.name)
            path, near = _try(product, second, first.apply(p), watch, margin, prefix=prefix)
            if path:
                paths.append(path)
            elif near:
                borderline.append(near)
        del fixed

    paths.sort(key=lambda x: 0 if x["lever_kind"] == "dated" else 1)  # dated first, then what-ifs
    return {
        "product": product,
        "product_name": current.product_name,
        "binding_rule": binding["rule"],
        "binding_label": RULE_LABELS.get(binding["rule"], binding["rule"]),
        "current_value": binding["value"],
        "threshold": binding["threshold"],
        "gap": _gap(binding),
        "failing_rules": [c["rule"] for c in failed],
        "profile_today": p.to_dict(),
        "paths": paths,
        "borderline": borderline,
        "path": paths[0] if paths else None,
        "assessed_on": today.isoformat(),
        "criteria_version": criteria_version(),
        "margin": margin,
    }


# ---------------------------------------------------------------- deterministic narration pieces

def _fmt_value(rule: str, value) -> str:
    if isinstance(value, float) and value <= 5 and rule in ("tdsr", "msr", "ltv", "unsecured_utilisation"):
        return f"{value:.0%}" if round(value * 100, 6) == int(round(value * 100)) else f"{value:.1%}"
    if rule == "min_annual_income":
        return f"S${value:,.0f} a year"
    return str(value)


def outcome_block(cfs: list[dict]) -> str:
    """The code-written part of the reply: each path / borderline line plus the fixed outcome sentence.
    Nothing here comes from the LLM."""
    parts: list[str] = []
    for cf in cfs:
        if cf["paths"]:
            lines = [f"**What would change for the {cf['product_name']}** (on criteria as of {fmt_date(cf['assessed_on'])}):"]
            for path in cf["paths"]:
                after = next((c for c in path["engine_result"]["checks"] if c["rule"] == cf["binding_rule"]), None)
                result = (f"your {cf['binding_label']} would be {_fmt_value(cf['binding_rule'], after['value'])} "
                          f"against the {_fmt_value(cf['binding_rule'], after['threshold'])} limit") if after else \
                    "today's criteria would be met"
                if path["headroom"] is not None:
                    result += f", {path['headroom'] * 100:.1f} points of headroom"
                if path["lever_kind"] == "dated":
                    lines.append(f"- **From {fmt_date(path['earliest_date'])}**, once {path['description']}: {result}.")
                else:
                    lines.append(f"- **What if** {path['description']}: {result}. "
                                 f"(No date: this depends on a change, not on something you've told us will happen.)")
            for near in cf["borderline"][:2]:
                lines.append(f"- *Borderline, not a path*: if {near['description']}, it would only just clear "
                             f"({near['headroom'] * 100:.1f} points inside the limit), so I wouldn't rely on it.")
            lines.append("Each line above is a separate scenario, not a forecast.")
            lines.append(OUTCOME_TEMPLATE.format(assessed_on=fmt_date(cf["assessed_on"])))
            parts.append("\n".join(lines))
        else:
            parts.append(NO_PATH_TEMPLATE.format(assessed_on=fmt_date(cf["assessed_on"]),
                                                 product_name=cf["product_name"]))
    return "\n\n".join(parts)


def numbers_in(cfs: list[dict]) -> dict:
    """Every number and date component a counterfactual can put in a reply, for the compliance
    number check (so it can tell engine numbers from invented ones)."""
    out: dict = {}
    def add(key, v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = v
    for cf in cfs:
        pk = cf["product"]
        add(f"cf.{pk}.current", cf["current_value"])
        add(f"cf.{pk}.threshold", cf["threshold"])
        add(f"cf.{pk}.gap", cf["gap"])
        add(f"cf.{pk}.margin_pp", cf["margin"] * 100)
        for d in [cf["assessed_on"]] + [x["earliest_date"] for x in cf["paths"] if x["earliest_date"]]:
            dd = date.fromisoformat(d)
            add(f"cf.date.{d}.y", dd.year)
            add(f"cf.date.{d}.d", dd.day)
        for i, x in enumerate(cf["paths"] + cf["borderline"]):
            for k, v in x["counterfactual_profile"].items():
                add(f"cf.{pk}.{i}.profile.{k}", v)
                before = cf["profile_today"].get(k)
                if isinstance(v, (int, float)) and isinstance(before, (int, float)):
                    add(f"cf.{pk}.{i}.change.{k}", abs(v - before))  # e.g. "S$65,000 less"
            for k, v in x["engine_result"]["computations"].items():
                add(f"cf.{pk}.{i}.{k}", v)
            for c in x["engine_result"]["checks"]:
                add(f"cf.{pk}.{i}.check.{c['rule']}", c["value"])
            if x["headroom"] is not None:
                add(f"cf.{pk}.{i}.headroom_pp", x["headroom"] * 100)
    return out
