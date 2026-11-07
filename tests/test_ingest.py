"""Building the knowledge base: chunking, table flattening, atomic writes, re-extraction and saved-page dates.
Offline: small strings and a temporary folder, never the network or the real data/ folder."""

import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import config  # noqa: E402
from src.knowledge import ingest  # noqa: E402
from src.knowledge.ingest import CHUNK_CHARS, OVERLAP_CHARS, chunk, html_to_text  # noqa: E402

FEE_HEADER = "Card Name | Principal Card's Annual Fee | Supplementary Card's Annual Fee | Fee Waiver"


def _rows_have_their_header(chunks: list[str], header: str, rows: list[str]) -> None:
    """Every row in every chunk has the table's header above it, in the same chunk."""
    for c in chunks:
        lines = c.splitlines()
        assert lines.count(header) <= 1, c
        for j, line in enumerate(lines):
            if line in rows:
                assert header in lines[:j], (line, c)


# ---------------------------------------------------------------- chunk()


def test_chunks_are_whole_lines_with_a_short_overlap():
    lines = [f"Line {i:03d} " + "x" * 61 for i in range(40)]  # 70 characters each
    chunks = chunk("\n".join(lines))
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= CHUNK_CHARS
        assert all(line in lines for line in c.splitlines())  # never a line cut in two
    for a, b in zip(chunks, chunks[1:]):
        a_lines, b_lines = a.splitlines(), b.splitlines()
        overlap = [line for line in b_lines if line in a_lines]
        assert overlap and overlap == a_lines[-len(overlap) :]  # the next chunk opens with the previous one's tail
        assert sum(len(line) for line in overlap) <= OVERLAP_CHARS
    covered = {line for c in chunks for line in c.splitlines()}
    assert covered == set(lines)


def test_a_very_long_line_is_split_with_an_overlap():
    intro = "An introductory paragraph that is comfortably longer than eighty characters, so it is kept."
    long_line = "".join(chr(97 + i % 26) for i in range(2500))
    chunks = chunk(f"{intro}\n{long_line}")
    assert chunks[0] == intro  # what was buffered goes out first, on its own
    step = CHUNK_CHARS - OVERLAP_CHARS
    assert chunks[1] == long_line[:CHUNK_CHARS]
    assert chunks[2] == long_line[step : step + CHUNK_CHARS]
    assert all(len(c) <= CHUNK_CHARS for c in chunks)
    rebuilt = chunks[1] + "".join(c[OVERLAP_CHARS:] for c in chunks[2:])
    assert rebuilt == long_line


@pytest.mark.parametrize("n_rows", range(3, 31))
def test_every_chunk_holding_table_rows_repeats_the_header(n_rows):
    # Many table lengths, so the chunk boundary falls at every point: mid-table, just after the header, and just
    # after the last row (where the next chunk opens with that row as overlap, then prose).
    rows = [f"DBS Card {i:02d} | S$196.20 | S$98.10 | 1 Year Annual Fee Waiver, then a little more" for i in range(n_rows)]
    after = [f"Paragraph {i} after the table, which says nothing about columns at all." for i in range(8)]
    text = "\n".join(["## DBS Credit Card Annual Fee", "Annual fees are charged up-front.", FEE_HEADER, *rows, *after])
    chunks = chunk(text)
    _rows_have_their_header(chunks, FEE_HEADER, rows)
    assert {line for c in chunks for line in c.splitlines()} == set(text.splitlines())  # nothing lost or added


def test_a_header_carried_over_does_not_follow_the_table_into_prose():
    rows = [f"DBS Card {i:02d} | S$196.20 | S$98.10 | 1 Year Annual Fee Waiver, then a little more" for i in range(30)]
    after = [f"Paragraph {i} after the table, which says nothing about columns at all." for i in range(30)]
    chunks = chunk("\n".join([FEE_HEADER, *rows, *after]))
    prose_only = [c for c in chunks if not any(r in c for r in rows)]
    assert prose_only and not any(FEE_HEADER in c for c in prose_only)


def test_a_long_row_inside_a_table_keeps_the_header_on_each_piece():
    rows = ["DBS Vantage Card | S$599.50 | N.A. | " + "Waived after the first year. " * 40, "POSB Card | S$196.20 | S$98.10 | 1 Year"]
    chunks = chunk("\n".join(["Some words before the table, long enough to be worth keeping as a chunk.", FEE_HEADER, *rows]))
    with_rows = [c for c in chunks if "S$599.50" in c or "Waived after" in c or "POSB Card" in c]
    assert len(with_rows) >= 2
    assert all(FEE_HEADER in c for c in with_rows)


def test_a_label_and_value_table_gets_no_header():
    # Two columns whose first row is data ("Age | Must be 21 ..."): there are no column names to repeat.
    rows = ["Age | Must be 21 to 75 years old"] + [
        f"Benefit {i} | Earn {i}% cash rebate on selected spend, capped at S$20 a month" for i in range(30)
    ]
    chunks = chunk("\n".join(["## Eligibility & Fees", *rows]))
    assert len(chunks) > 2
    assert sum(c.count("Age | Must be 21 to 75 years old") for c in chunks) == 1


def test_a_two_column_table_with_column_names_repeats_them():
    header = "Type of property loan | Does the TDSR threshold/MSR limit apply at the time of refinancing?"
    rows = [f"Property loan type {i} with a longer description of who holds it | Yes, with conditions" for i in range(25)]
    chunks = chunk("\n".join(["Borrowers may refinance in some cases.", header, *rows]))
    assert len(chunks) > 2
    _rows_have_their_header(chunks, header, rows)


@pytest.mark.parametrize(
    "first,second,expected",
    [
        (FEE_HEADER, "DBS Live Fresh Card | S$196.20 | S$98.10 | 1 Year", True),
        ("Reprice with DBS | Refinance elsewhere", "Administration fee | About S$800 | -", True),  # lost corner cell
        ("Existing property | Borrower needs to provide", "A HDB flat | A letter from the HDB", True),
        ("Annual Income | Credit Limit", "S$30,000 to S$120,000 | Up to 4X monthly income", True),
        ("Age | Must be 21 to 75 years old", "Minimum income | S$30,000 p.a.", False),
        ("What it is | Loyalty programme for Esso customers.", "How it works | Earn points.", False),
    ],
)
def test_header_rows_are_told_apart_from_data_rows(first, second, expected):
    assert ingest._names_columns(first, second) is expected


# ---------------------------------------------------------------- html_to_text() tables


FEE_TABLE_HTML = """<html><body>
<nav>Home | Cards | Loans</nav>
<h2>Annual fees</h2>
<p>Annual Fee amounts shown are inclusive of GST.</p>
<table>
  <thead><tr><th>Card Name</th><th>Principal Card's Annual Fee</th><th>Supplementary Card's Annual Fee</th></tr></thead>
  <tbody>
    <tr><td>DBS Live Fresh Card</td><td> S$196.20 </td><td>S$98.10</td></tr>
    <tr><td>DBS yuu<br>Visa Card</td><td>S$196.20</td><td>N.A.</td></tr>
    <tr><td></td><td></td><td></td></tr>
  </tbody>
</table>
<p>Fees are charged on your card approval date.</p>
<script>var x = "Card | Script";</script>
</body></html>"""


def test_each_table_row_becomes_one_line():
    lines = html_to_text(FEE_TABLE_HTML.encode("utf-8")).splitlines()
    header = "Card Name | Principal Card's Annual Fee | Supplementary Card's Annual Fee"
    assert lines == [
        "## Annual fees",
        "Annual Fee amounts shown are inclusive of GST.",
        header,
        "DBS Live Fresh Card | S$196.20 | S$98.10",
        "DBS yuu Visa Card | S$196.20 | N.A.",  # a <br> inside a cell stays on the row's line
        "Fees are charged on your card approval date.",
    ]  # the empty row, the menu and the script are gone


def test_a_flattened_table_keeps_its_header_through_chunking():
    rows = "".join(
        f"<tr><td>DBS Card {i:02d}</td><td>S$196.20</td><td>S$98.10</td><td>1 Year Annual Fee Waiver</td></tr>"
        for i in range(40)
    )
    html = (
        "<html><body><p>Annual fees for every card.</p><table><tr><th>Card Name</th><th>Principal Card's Annual Fee"
        "</th><th>Supplementary Card's Annual Fee</th><th>Fee Waiver</th></tr>" + rows + "</table></body></html>"
    )
    text = html_to_text(html)
    chunks = chunk(text)
    assert len(chunks) > 2
    table_rows = [line for line in text.splitlines() if line.startswith("DBS Card")]
    _rows_have_their_header(chunks, FEE_HEADER, table_rows)


# ---------------------------------------------------------------- atomic writes


def test_atomic_write_replaces_the_whole_file_and_leaves_nothing_behind(tmp_path):
    path = tmp_path / "kb.json"
    path.write_text("old contents", encoding="utf-8")
    ingest._write_atomic(path, '{"text": "Woman’s World MasterCard® — S$196.20"}')
    assert json.loads(path.read_text(encoding="utf-8")) == {"text": "Woman’s World MasterCard® — S$196.20"}
    assert [p.name for p in tmp_path.iterdir()] == ["kb.json"]
    assert path.stat().st_mode & 0o777 == 0o644


def test_a_failed_atomic_write_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text('{"ok": true}', encoding="utf-8")

    def boom(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(ingest.os, "replace", boom)
    with pytest.raises(OSError):
        ingest._write_atomic(path, '{"ok": false}')
    assert path.read_text(encoding="utf-8") == '{"ok": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["manifest.json"]  # the temp file was cleaned up


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway knowledge repository: every path ingest writes to points into tmp_path."""
    sources = tmp_path / "sources"
    monkeypatch.setattr(ingest, "SOURCES_DIR", sources)
    monkeypatch.setattr(ingest, "TEXT_DIR", sources / "text")
    monkeypatch.setattr(ingest, "RAW_SNAPSHOT_DIR", sources / "raw")
    monkeypatch.setattr(ingest, "MANIFEST_PATH", sources / "manifest.json")
    monkeypatch.setattr(ingest, "HISTORY_PATH", sources / "history.jsonl")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "KB_PATH", tmp_path / "kb.json")
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(ingest, "SOURCES", [])

    def no_network(*_args, **_kwargs):
        raise AssertionError("tests must not fetch")

    monkeypatch.setattr(ingest.requests, "get", no_network)
    return tmp_path


def _entry(did: str, kind: str = "html", **extra) -> dict:
    return {
        "id": did,
        "url": f"https://www.dbs.com.sg/{did}",
        "title": did.replace("-", " ").title(),
        "publisher": "DBS",
        "products": ["credit_card"],
        "kind": kind,
        "origin": "web",
        "status": "ok",
        "fetched_at": "2026-10-27T17:56:54",
        "last_good_fetch": "2026-10-27T17:56:54",
        "sha256": "old",
        "chars": 10,
        **extra,
    }


def test_build_writes_kb_json_whole_and_keeps_the_as_of_dates(repo):
    entry = _entry("dbs-annual-fee")
    rows = [f"DBS Woman’s Card {i:02d} | S$196.20 | S$98.10 | 1 Year Annual Fee Waiver" for i in range(30)]
    ingest._write_snapshot(entry, "\n".join(["## DBS Credit Card Annual Fee", FEE_HEADER, *rows]))
    ingest._save_manifest({entry["id"]: entry})
    ingest.build(verbose=False)
    kb = json.loads(config.KB_PATH.read_text(encoding="utf-8"))
    assert len(kb["chunks"]) > 2
    assert {c["fetched_at"] for c in kb["chunks"]} == {"2026-10-27T17:56:54"}
    assert "Woman’s" in kb["chunks"][0]["text"]
    _rows_have_their_header([c["text"] for c in kb["chunks"]], FEE_HEADER, rows)
    assert not [p for p in repo.rglob("*.tmp")]


# ---------------------------------------------------------------- reextract() and PDFs


def _pdf(user_password: str) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.encrypt(user_password=user_password, owner_password="owner", algorithm="RC4-128")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_an_owner_locked_pdf_opens_but_a_password_protected_one_is_refused():
    assert ingest.pdf_to_text(_pdf("")) == ""  # DBS T&Cs: locked against editing, readable by anyone
    with pytest.raises(ValueError, match="password"):
        ingest.pdf_to_text(_pdf("secret"))


def test_reextract_skips_an_unreadable_document_and_still_saves_the_manifest(repo):
    good, bad, locked = _entry("good-page"), _entry("bad-terms", kind="pdf"), _entry("locked-terms", kind="pdf")
    for e in (good, bad, locked):
        ingest._write_snapshot(e, "the old text")
    ingest._save_manifest({e["id"]: e for e in (good, bad, locked)})
    ingest.RAW_SNAPSHOT_DIR.mkdir(parents=True)
    (ingest.RAW_SNAPSHOT_DIR / "bad-terms.pdf").write_bytes(b"this is not a pdf")
    (ingest.RAW_SNAPSHOT_DIR / "good-page.html").write_bytes(b"<html><body><p>The new text</p></body></html>")
    (ingest.RAW_SNAPSHOT_DIR / "locked-terms.pdf").write_bytes(_pdf("secret"))

    assert ingest.reextract(verbose=False) == 1

    manifest = ingest.load_manifest()
    assert manifest["good-page"]["sha256"] == ingest._sha("The new text")
    assert ingest.read_snapshot("good-page") == "The new text"
    assert f"sha256: {manifest['good-page']['sha256']}" in (ingest.TEXT_DIR / "good-page.md").read_text("utf-8")
    for skipped in (bad, locked):  # entry and snapshot exactly as they were
        assert manifest[skipped["id"]] == skipped
        assert ingest.read_snapshot(skipped["id"]) == "the old text"
    events = [json.loads(line) for line in ingest.HISTORY_PATH.read_text("utf-8").splitlines()]
    assert {(e["event"], e["id"]) for e in events} == {
        ("reextracted", "good-page"),
        ("reextract_failed", "bad-terms"),
        ("reextract_failed", "locked-terms"),
    }


# ---------------------------------------------------------------- hand-saved pages


def _saved_page(repo: Path, text: str, when: datetime, meta: dict | None = None) -> Path:
    raw = repo / "raw"
    raw.mkdir(exist_ok=True)
    meta = meta or {"url": "https://www.dbs.com.sg/saved", "title": "Saved Calculator", "products": ["home_loan"]}
    (raw / "sources.json").write_text(json.dumps({"saved-calculator.txt": meta}), encoding="utf-8")
    page = raw / "saved-calculator.txt"
    page.write_text(text, encoding="utf-8")
    os.utime(page, (when.timestamp(), when.timestamp()))
    return page


def test_a_saved_page_is_dated_by_its_file_and_keeps_that_date_until_it_changes(repo):
    did = "dbs-saved-calculator"
    _saved_page(repo, "Rates as published on the calculator page.", datetime(2026, 10, 1, 9, 0))
    assert ingest.fetch(verbose=False)[did]["last_good_fetch"] == "2026-10-01T09:00:00"

    # Same text, file touched later (a git checkout or an iCloud sync): the date stays.
    _saved_page(repo, "Rates as published on the calculator page.", datetime(2026, 11, 5, 12, 0))
    assert ingest.fetch(verbose=False)[did]["last_good_fetch"] == "2026-10-01T09:00:00"

    # New text: the page was saved again, so it takes the new file's date.
    _saved_page(repo, "Rates as published on the calculator page, revised.", datetime(2026, 11, 6, 8, 30))
    manifest = ingest.fetch(verbose=False)
    assert manifest[did]["last_good_fetch"] == "2026-11-06T08:30:00"

    ingest.build(verbose=False)
    kb = json.loads(config.KB_PATH.read_text(encoding="utf-8"))
    assert {d["fetched_at"] for d in kb["documents"]} == {"2026-11-06T08:30:00"}


def test_saved_on_in_sources_json_wins_over_the_file_date(repo):
    meta = {"url": "https://www.dbs.com.sg/saved", "title": "Saved Calculator", "saved_on": "2026-09-15"}
    _saved_page(repo, "Rates as published on the calculator page.", datetime(2026, 11, 5, 12, 0), meta)
    assert ingest.fetch(verbose=False)["dbs-saved-calculator"]["last_good_fetch"] == "2026-09-15"
