"""Retrieval behaviour tuned after the 50-question review. Offline: uses the built data/kb.json."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.product_knowledge import cited_sources  # noqa: E402
from src.knowledge import retriever  # noqa: E402
from src.knowledge.retriever import tokenize  # noqa: E402

needs_kb = pytest.mark.skipif(not retriever.load().ready, reason="knowledge base not built")


def test_word_forms_match():
    assert tokenize("credit cards") == tokenize("credit card")
    assert tokenize("application documents") == tokenize("apply document")
    assert tokenize("outstanding housing loans") == tokenize("outstanding home loan")


@needs_kb
@pytest.mark.parametrize(
    "question,product,search_query,expected",
    [
        (
            "What's a balance transfer and when would someone use one?",
            "credit_card",
            "DBS balance transfer",
            "DBS Balance Transfer",
        ),  # was filtered out by the credit-card product filter
        (
            "What documents do I need to apply for a credit card?",
            "credit_card",
            "credit card application documents checklist",
            "Card Application",
        ),
        (
            "What interest rate is charged on unpaid credit card balances?",
            "credit_card",
            "credit card interest rate finance charge unpaid balance",
            "Credit Card Interest Rate",
        ),
        (
            "How does having an existing home loan affect a loan for a second property?",
            "home_loan",
            "LTV limit outstanding housing loans second property TDSR",
            "Loan Tenure and Loan-to-Value",
        ),
        (
            "Can a foreigner buy an HDB flat with a DBS loan?",
            "home_loan",
            "DBS HDB Loan eligibility Singapore Citizens PR foreigner",
            "DBS HDB Loan",
        ),
    ],
)
def test_right_page_in_top_k(question, product, search_query, expected):
    hits = retriever.retrieve(f"{question} {search_query}", product=product)
    assert any(expected in h["title"] for h in hits), [h["title"] for h in hits]


def test_citations_grouped_per_page():
    chunks = [{"title": "A", "url": "u1"}, {"title": "B", "url": "u2"}, {"title": "A", "url": "u1"}]
    sources, cited = cited_sources("x [1] y [3] z [2] [9]", chunks)
    assert cited == {1, 2, 3, 9}
    assert sources == [{"n": [1, 3], "title": "A", "url": "u1"}, {"n": [2], "title": "B", "url": "u2"}]


def test_superseded_rate_is_out_of_the_index_but_kept_in_the_snapshot():
    import json

    from src import config
    from src.knowledge import ingest

    assert "3.5% interest rate" in ingest.read_snapshot("dbs-mortgage-calculator")
    kb = json.loads(config.KB_PATH.read_text())
    assert not any("3.5% interest rate" in c["text"] for c in kb["chunks"])
    assert any("4.65%" in c["text"] and c["doc_id"] == "dbs-home-budget-calculator" for c in kb["chunks"])


def test_singular_and_plural_become_the_same_word():
    from src.knowledge.retriever import _stem

    for one, many in [
        ("rate", "rates"),
        ("mile", "miles"),
        ("home", "homes"),
        ("balance", "balances"),
        ("purchase", "purchases"),
        ("branch", "branches"),
        ("card", "cards"),
        ("fee", "fees"),
    ]:
        assert _stem(one) == _stem(many), (one, many)


def test_a_thin_page_never_replaces_good_text():
    from src.knowledge.ingest import _keep_good_snapshot

    shell = {"ok": True, "text": "Menu"}
    assert _keep_good_snapshot(shell, {"chars": 5000}, True)["ok"] is False
    assert _keep_good_snapshot(shell, {"chars": 300}, True)["ok"] is True  # it has always been thin
    assert _keep_good_snapshot({"ok": True, "text": "x" * 900}, {"chars": 5000}, True)["ok"] is True
