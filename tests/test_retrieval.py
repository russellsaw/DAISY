"""Retrieval behaviour tuned after the 50-question review. Offline: uses the built data/kb.json."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents import product_knowledge  # noqa: E402
from src.agents.product_knowledge import cited_sources, without_citations  # noqa: E402
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
    sources, cited = cited_sources("x [1] y [3] z [2] [9] [0]", chunks)
    assert cited == {1, 2, 3}  # [9] and [0] point at no excerpt: not a source, and not counted as cited
    assert sources == [{"n": [1, 3], "title": "A", "url": "u1"}, {"n": [2], "title": "B", "url": "u2"}]


def test_old_citation_markers_are_taken_out_of_the_history():
    history = [
        {"role": "user", "content": "What is the annual fee? See [1] on your page."},
        {"role": "assistant", "content": "The fee is S$196.20 [1][2].\n- Waiver: 1 year [3]\n[4] applies too."},
    ]
    before = [dict(m) for m in history]
    cleaned = without_citations(history)
    assert cleaned[1]["content"] == "The fee is S$196.20.\n- Waiver: 1 year\n applies too."
    assert cleaned[0] == history[0]  # the customer's own words are left alone
    assert history == before  # copies: the conversation itself is untouched


def test_a_follow_up_question_drafts_without_the_previous_numbering(monkeypatch):
    from src.state import ConversationState

    chunks = [{"title": "DBS Credit Card Annual Fee", "url": "u1", "text": "S$196.20", "score": 3.0}]
    monkeypatch.setattr(product_knowledge.retriever, "load", lambda: type("KB", (), {"ready": True})())
    monkeypatch.setattr(product_knowledge.retriever, "retrieve", lambda *a, **k: chunks)
    sent = {}

    def fake_chat(system, messages, **kwargs):
        sent["messages"] = messages
        return "The annual fee is S$196.20 [1], see also [11]."

    monkeypatch.setattr(product_knowledge.llm, "chat", fake_chat)
    state = ConversationState(record=False)
    state.messages = [
        {"role": "user", "content": "How much is the cash advance fee?"},
        {"role": "assistant", "content": "It is 8% or S$15, whichever is greater [2]."},
        {"role": "user", "content": "And the annual fee?"},
    ]
    out = product_knowledge.answer(state, "And the annual fee?", ["credit_card"])

    assert sent["messages"][1] == {"role": "assistant", "content": "It is 8% or S$15, whichever is greater."}
    assert state.messages[1]["content"] == "It is 8% or S$15, whichever is greater [2]."
    assert out["sources"] == [{"n": [1], "title": "DBS Credit Card Annual Fee", "url": "u1"}]
    assert state.trace[-1].summary == "retrieved 1 chunks, cited 1"  # [11] has no excerpt behind it


def test_superseded_rate_is_out_of_the_index_but_kept_in_the_snapshot():
    import json

    from src import config
    from src.knowledge import ingest

    assert "3.5% interest rate" in ingest.read_snapshot("dbs-mortgage-calculator")
    kb = json.loads(config.KB_PATH.read_text(encoding="utf-8"))
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
        ("income", "incomes"),
        ("charge", "charges"),
    ]:
        assert _stem(one) == _stem(many), (one, many)


def test_ing_and_ed_forms_meet_their_base_word():
    from src.knowledge.retriever import _stem

    for base, *forms in [
        ("refinance", "refinancing", "refinanced"),
        ("reprice", "repricing", "repriced"),
        ("waive", "waived", "waiving"),
        ("require", "required", "requiring"),
        ("charge", "charging", "charged"),
        ("secure", "secured"),
        ("lease", "leasing", "leased"),
        ("service", "servicing"),  # "debt servicing"
        ("change", "changing", "changed"),
    ]:
        for form in forms:
            assert _stem(form) == _stem(base), (base, form)
    # Short words keep their "e", so they don't run into other words.
    assert [_stem(w) for w in ("fee", "free", "rate", "home", "note", "care")] == [
        "fee",
        "free",
        "rate",
        "home",
        "note",
        "care",
    ]
    assert tokenize("refinancing my home loan") == tokenize("refinance home loans")


def test_a_thin_page_never_replaces_good_text():
    from src.knowledge.ingest import _keep_good_snapshot

    shell = {"ok": True, "text": "Menu"}
    assert _keep_good_snapshot(shell, {"chars": 5000}, True)["ok"] is False
    assert _keep_good_snapshot(shell, {"chars": 300}, True)["ok"] is True  # it has always been thin
    assert _keep_good_snapshot({"ok": True, "text": "x" * 900}, {"chars": 5000}, True)["ok"] is True
