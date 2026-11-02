"""Knowledge repository: fetch -> snapshot -> chunk.

    python scripts/ingest.py                 # fetch every source, then rebuild data/kb.json
    python scripts/ingest.py --build-only    # rebuild kb.json from saved snapshots (no network)
    python scripts/ingest.py --only dbs-personal-loan,dbs-cashline

Everything the assistant can ever cite lives in data/sources/:

  manifest.json      one entry per document: url, title, publisher, products, fetched_at,
                     sha256 of the extracted text, chars, status, last_changed
  text/<id>.md       the extracted text the model reads (before chunking), with a provenance
                     header. Plain text and diffable — commit these.
  raw/<id>.html|pdf  the bytes exactly as fetched, for audit (git-ignored; large)
  history.jsonl      one line per document added / changed / failed / retired, so you can
                     see when a rate or fee on a DBS page changed

Fetch and build are separate on purpose: a fetch failure never wipes a good snapshot
(the document is marked `stale` and its last good text stays in the KB), and kb.json can
be rebuilt offline from the snapshots.

Pages that block scripted fetches: save them from your browser into data/raw/ (html, pdf,
txt) and describe them in data/raw/sources.json:
  {"my-file.pdf": {"url": "https://...", "title": "...", "publisher": "DBS",
                   "products": ["home_loan"]}}
(a bare URL string as the value also works).
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from html import unescape

import requests
from bs4 import BeautifulSoup, NavigableString

from .. import config
from .sources import SOURCES, SUPERSEDED_LINES

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DAISY-kb-builder/0.2; hackathon prototype)"}
CHUNK_CHARS = 900
OVERLAP_CHARS = 150
THIN_DOC_CHARS = 800  # below this a page is probably JS-rendered or a soft 404

SOURCES_DIR = config.DATA_DIR / "sources"
TEXT_DIR = SOURCES_DIR / "text"
RAW_SNAPSHOT_DIR = SOURCES_DIR / "raw"
MANIFEST_PATH = SOURCES_DIR / "manifest.json"
HISTORY_PATH = SOURCES_DIR / "history.jsonl"

_BLOCKS = ["p", "div", "li", "tr", "section", "article", "ul", "ol", "dd", "dt", "table", "caption"]
_HEADINGS = ["h1", "h2", "h3", "h4"]


# ---------------------------------------------------------------- extraction

def doc_id(src: dict) -> str:
    title = src["title"].lower().replace("&", " and ").replace("t and cs", "tncs")
    slug = re.sub(r"[^a-z0-9]+", "-", re.sub(r"['’]", "", title)).strip("-")
    prefix = src.get("publisher", "").lower()
    return slug if not prefix or slug.startswith(prefix) else f"{prefix}-{slug}"


def html_to_text(content: bytes | str) -> str:
    """Readable text with line structure kept. Pass bytes so the page's own charset is used."""
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form",
                     "aside", "button", "iframe"]):
        tag.decompose()
    body = soup.body or soup
    main = soup.find("main")
    root = main if main is not None and len(main.get_text(strip=True)) >= 500 else body

    for br in root.find_all("br"):
        br.replace_with(NavigableString("\n"))
    # Each table row becomes one line ("1 | 45% or 25% | 25%"), so a value stays next to its row label.
    # (Cells used to come out one per line, and the model couldn't tell which limit went with which row.)
    for tr in root.find_all("tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
        if any(cells):
            tr.replace_with(NavigableString("\n" + " | ".join(cells) + "\n"))
    for h in root.find_all(_HEADINGS):
        h.insert_before(NavigableString("\n\n## "))
        h.append(NavigableString("\n"))
    for el in root.find_all(_BLOCKS):
        el.append(NavigableString("\n"))

    lines = []
    for line in root.get_text("").splitlines():
        line = re.sub(r"[ \t ​]+", " ", unescape(line)).strip(" |")
        if line.strip("# "):
            lines.append(line)
    return "\n".join(lines).strip()


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        reader.decrypt("")  # DBS T&C PDFs are owner-locked with an empty user password
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = "\n".join(pages)
    text = re.sub(r"[ \t ]+", " ", text)
    return "\n".join(l.strip() for l in text.splitlines() if l.strip())


def chunk(text: str) -> list[str]:
    """Pack whole lines into ~CHUNK_CHARS chunks, carrying a short overlap tail forward."""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        while len(line) > CHUNK_CHARS:  # one very long line (common in PDFs): hard split
            head, line = line[:CHUNK_CHARS], line[CHUNK_CHARS - OVERLAP_CHARS:]
            if buf:
                chunks.append("\n".join(buf))
                buf, size = [], 0
            chunks.append(head)
        starts_section = line.startswith("## ") and size > CHUNK_CHARS // 2
        if buf and (size + len(line) + 1 > CHUNK_CHARS or starts_section):
            chunks.append("\n".join(buf))
            tail: list[str] = []
            for prev in reversed(buf):  # overlap: last lines up to OVERLAP_CHARS
                if sum(len(t) for t in tail) + len(prev) > OVERLAP_CHARS:
                    break
                tail.insert(0, prev)
            buf, size = tail, sum(len(t) + 1 for t in tail)
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return [c for c in chunks if len(c) > 80]


# ---------------------------------------------------------------- repository I/O

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text()) if MANIFEST_PATH.exists() else {}


def _log(event: str, did: str, **extra) -> None:
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "event": event,
                            "id": did, **extra}, ensure_ascii=False) + "\n")


def _write_snapshot(entry: dict, text: str) -> None:
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    header = "\n".join([
        "---",
        f"id: {entry['id']}",
        f"title: {entry['title']}",
        f"publisher: {entry['publisher']}",
        f"url: {entry['url']}",
        f"products: {', '.join(entry['products'])}",
        f"fetched_at: {entry['fetched_at']}",
        f"sha256: {entry['sha256']}",
        "---",
    ])
    (TEXT_DIR / f"{entry['id']}.md").write_text(f"{header}\n\n{text}\n", encoding="utf-8")


def read_snapshot(did: str) -> str:
    """The document text without its provenance header."""
    path = TEXT_DIR / f"{did}.md"
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8")
    if raw.startswith("---\n"):
        end = raw.find("\n---\n", 4)
        if end != -1:
            return raw[end + 5:].strip()
    return raw.strip()


# ---------------------------------------------------------------- fetch

def _fetch_one(src: dict) -> dict:
    did = doc_id(src)
    try:
        resp = requests.get(src["url"], headers=HEADERS, timeout=45)
        resp.raise_for_status()
        if "404" in resp.url.rsplit("/", 1)[-1]:
            raise RuntimeError(f"soft 404 (redirected to {resp.url})")
        is_pdf = src["kind"] == "pdf" or resp.headers.get("content-type", "").startswith("application/pdf")
        text = pdf_to_text(resp.content) if is_pdf else html_to_text(resp.content)
        return {"id": did, "src": src, "ok": True, "text": text, "raw": resp.content,
                "ext": "pdf" if is_pdf else "html"}
    except Exception as exc:  # noqa: BLE001
        return {"id": did, "src": src, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def _local_sources() -> list[dict]:
    """Files saved by hand into data/raw/, described by data/raw/sources.json."""
    mapping_path = config.RAW_DIR / "sources.json"
    mapping = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
    out = []
    if not config.RAW_DIR.exists():
        return out
    for path in sorted(config.RAW_DIR.iterdir()):
        suffix = path.suffix.lower()
        if path.name.startswith(".") or path.name == "sources.json" or suffix not in (
                ".pdf", ".html", ".htm", ".txt", ".md"):
            continue
        meta = mapping.get(path.name, {})
        if isinstance(meta, str):
            meta = {"url": meta}
        out.append({
            "url": meta.get("url", f"local:{path.name}"),
            "title": meta.get("title", path.stem.replace("_", " ").replace("-", " ")),
            "publisher": meta.get("publisher", "DBS"),
            "products": meta.get("products", ["general"]),
            "kind": "pdf" if suffix == ".pdf" else "html" if suffix in (".html", ".htm") else "txt",
            "local_path": str(path),
        })
    return out


def _read_local(src: dict) -> dict:
    from pathlib import Path

    did = doc_id(src)
    path = Path(src["local_path"])
    try:
        data = path.read_bytes()
        if src["kind"] == "pdf":
            text = pdf_to_text(data)
        elif src["kind"] == "html":
            text = html_to_text(data)
        else:
            text = data.decode("utf-8", errors="ignore")
        return {"id": did, "src": src, "ok": True, "text": text, "raw": data, "ext": path.suffix.lstrip(".")}
    except Exception as exc:  # noqa: BLE001
        return {"id": did, "src": src, "ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


def fetch(only: set[str] | None = None, verbose: bool = True) -> dict:
    """Fetch sources into the repository. Returns the updated manifest."""
    manifest = load_manifest()
    web = [s for s in SOURCES if not only or doc_id(s) in only]
    local = [s for s in _local_sources() if not only or doc_id(s) in only]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_fetch_one, web))
    results += [_read_local(s) for s in local]

    now = datetime.now().isoformat(timespec="seconds")
    RAW_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for res in results:
        did, src = res["id"], res["src"]
        prev = manifest.get(did, {})
        entry = {
            "id": did, "url": src["url"], "title": src["title"], "publisher": src.get("publisher", "DBS"),
            "products": src.get("products", ["general"]), "kind": src["kind"],
            "origin": "manual" if "local_path" in src else "web",
        }
        if res["ok"]:
            text = res["text"]
            sha = _sha(text)
            entry.update({
                "status": "thin" if len(text) < THIN_DOC_CHARS else "ok",
                "fetched_at": now, "last_good_fetch": now, "sha256": sha, "chars": len(text),
                "last_changed": prev.get("last_changed", now) if prev.get("sha256") == sha else now,
                "error": None,
            })
            (RAW_SNAPSHOT_DIR / f"{did}.{res['ext']}").write_bytes(res["raw"])
            _write_snapshot(entry, text)
            if not prev.get("sha256"):
                _log("added", did, chars=len(text), url=src["url"])
            elif prev["sha256"] != sha:
                _log("changed", did, chars=len(text), previous_chars=prev.get("chars"), url=src["url"])
            label = "THIN" if entry["status"] == "thin" else "ok  "
            if verbose:
                print(f"  {label} {did:<55} {len(text):>8,} chars")
        else:
            has_snapshot = (TEXT_DIR / f"{did}.md").exists()
            entry.update({k: prev.get(k) for k in ("sha256", "chars", "last_changed", "last_good_fetch")})
            entry.update({"status": "stale" if has_snapshot else "failed", "fetched_at": now,
                          "error": res["error"]})
            _log("failed", did, error=res["error"], kept_previous_snapshot=has_snapshot)
            if verbose:
                note = "keeping last good snapshot" if has_snapshot else "no snapshot"
                print(f"  FAIL {did:<55} {res['error'][:70]}  ({note})")
        manifest[did] = entry

    # Anything in the manifest that is no longer listed gets retired (kept for the record).
    if not only:
        live = {doc_id(s) for s in SOURCES} | {doc_id(s) for s in local}
        for did, entry in manifest.items():
            if did not in live and entry.get("status") != "retired":
                entry["status"] = "retired"
                _log("retired", did, url=entry.get("url"))

    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(dict(sorted(manifest.items())), indent=1, ensure_ascii=False))
    return manifest


# ---------------------------------------------------------------- build

def _boilerplate_lines(docs: list[tuple[dict, str]]) -> set[tuple[str, str]]:
    """Lines repeated across most pages of one publisher (menus, footers, 'Apply now')."""
    by_pub: dict[str, Counter] = {}
    n_docs: Counter = Counter()
    for entry, text in docs:
        if entry["kind"] != "html":
            continue
        pub = entry["publisher"]
        n_docs[pub] += 1
        by_pub.setdefault(pub, Counter()).update({l.strip() for l in text.splitlines() if l.strip()})
    noise = set()
    for pub, counts in by_pub.items():
        threshold = max(4, int(0.4 * n_docs[pub]))
        noise |= {(pub, line) for line, c in counts.items() if c >= threshold and len(line) < 160}
    return noise


def build(verbose: bool = True) -> dict:
    """Chunk the current snapshots into data/kb.json."""
    manifest = load_manifest()
    usable = [e for e in manifest.values() if e.get("status") in ("ok", "thin", "stale")]
    docs = [(e, read_snapshot(e["id"])) for e in usable]
    docs = [(e, t) for e, t in docs if t]
    noise = _boilerplate_lines(docs)

    chunks, seen = [], set()
    dropped_dupes = 0
    for entry, text in docs:
        if entry["kind"] == "html":
            text = "\n".join(l for l in text.splitlines() if (entry["publisher"], l.strip()) not in noise)
        superseded = SUPERSEDED_LINES.get(entry["id"], [])
        text = "\n".join(l for l in text.splitlines() if not any(s in l for s in superseded))
        url_key = hashlib.sha1(entry["url"].encode()).hexdigest()[:10]
        for i, piece in enumerate(chunk(text)):
            fingerprint = _sha(re.sub(r"\s+", " ", piece.lower()))
            if fingerprint in seen:
                dropped_dupes += 1
                continue
            seen.add(fingerprint)
            chunks.append({
                "id": f"{url_key}-{i}",
                "doc_id": entry["id"],
                "url": entry["url"],
                "title": entry["title"],
                "publisher": entry["publisher"],
                "products": entry["products"],
                "fetched_at": entry.get("last_good_fetch"),
                "text": piece,
            })

    per_doc = Counter(c["doc_id"] for c in chunks)
    kb = {
        "built_on": date.today().isoformat(),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "chunks": chunks,
        "documents": [{"id": e["id"], "url": e["url"], "title": e["title"], "publisher": e["publisher"],
                       "products": e["products"], "status": e["status"], "chars": e.get("chars"),
                       "chunks": per_doc.get(e["id"], 0), "fetched_at": e.get("last_good_fetch")}
                      for e, _ in docs],
        "failures": [{"id": e["id"], "url": e["url"], "error": e.get("error")}
                     for e in manifest.values() if e.get("status") in ("failed", "stale")],
        "boilerplate_lines_removed": len(noise),
        "duplicate_chunks_removed": dropped_dupes,
    }
    config.DATA_DIR.mkdir(exist_ok=True)
    config.KB_PATH.write_text(json.dumps(kb, ensure_ascii=False, indent=1))
    if verbose:
        print(f"\n{len(chunks)} chunks from {len(docs)} documents -> {config.KB_PATH}")
        print(f"  removed {len(noise)} boilerplate lines, {dropped_dupes} duplicate chunks")
        thin = [e["id"] for e, _ in docs if e["status"] == "thin"]
        if thin:
            print(f"  thin documents (check them): {', '.join(thin)}")
        if kb["failures"]:
            print(f"  {len(kb['failures'])} source(s) failed or stale — see {MANIFEST_PATH}. "
                  f"Save them from your browser into {config.RAW_DIR} and re-run.")
    return kb


def reextract(verbose: bool = True) -> int:
    """Re-run text extraction over the saved raw snapshots (after an extraction fix), offline.
    Logged as `reextracted`, not `changed`: the page itself didn't change, our reading of it did."""
    manifest = load_manifest()
    count = 0
    for did, entry in manifest.items():
        raw = next(iter(RAW_SNAPSHOT_DIR.glob(f"{did}.*")), None)
        if entry.get("status") not in ("ok", "thin", "stale") or raw is None:
            continue
        data = raw.read_bytes()
        text = pdf_to_text(data) if raw.suffix == ".pdf" else (
            html_to_text(data) if raw.suffix in (".html", ".htm") else data.decode("utf-8", errors="ignore"))
        sha = _sha(text)
        if sha == entry.get("sha256"):
            continue
        entry.update({"sha256": sha, "chars": len(text)})
        _write_snapshot(entry, text)
        _log("reextracted", did, chars=len(text), reason="text extraction improved; source unchanged")
        count += 1
        if verbose:
            print(f"  re-extracted {did:<55} {len(text):>8,} chars")
    MANIFEST_PATH.write_text(json.dumps(dict(sorted(manifest.items())), indent=1, ensure_ascii=False))
    return count


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-only", action="store_true", help="rebuild kb.json from snapshots, no network")
    parser.add_argument("--only", default="", help="comma-separated document ids to re-fetch")
    parser.add_argument("--reextract", action="store_true",
                        help="re-run text extraction on the saved raw snapshots (offline), then rebuild")
    args = parser.parse_args(argv)
    if args.reextract:
        print(f"Re-extracting text from {RAW_SNAPSHOT_DIR} …")
        print(f"{reextract()} snapshot(s) re-extracted")
    elif not args.build_only:
        only = {s.strip() for s in args.only.split(",") if s.strip()} or None
        print(f"Fetching {'selected' if only else 'all'} sources into {SOURCES_DIR} …")
        fetch(only=only)
    kb = build()
    return 0 if kb["chunks"] else 1


if __name__ == "__main__":
    sys.exit(main())
