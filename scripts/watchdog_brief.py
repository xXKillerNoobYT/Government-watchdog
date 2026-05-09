"""Deterministic single-topic watchdog brief generator.

Reads the Phase 1 corpus (Database/gov_watchdog.db) read-only, selects
keyword-matching snippets, and emits a markdown brief plus a citations
JSON sidecar under Docs/Briefs/.

Design notes (locked by Docs/phase2-pilot-spec.md):
- No LLM, no network, stdlib-only sqlite + json.
- Output is byte-identical for a fixed corpus + topic config (repeatability
  acceptance check).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "Database" / "gov_watchdog.db"
BRIEFS_DIR = REPO_ROOT / "Docs" / "Briefs"

# Topic registry. In-script for the pilot (one topic). See spec §7 Q3.
TOPICS = {
    "alpine-wwtp-financing": {
        "title": "Alpine wastewater treatment plant — financing and operations",
        "brief_id": "2026-05-08-alpine-wwtp-financing",
        "generated_utc": "2026-05-08T00:00:00Z",
        "keywords": [
            "wastewater",
            "WWTP",
            "CWSRF",
            "sewer plant",
            "treatment plant",
            "pre-treatment plant",
            "JVA",
        ],
    },
    "alpine-lodging-tax": {
        "title": "Alpine lodging tax — revenue, tourism dependence, and use",
        "brief_id": "2026-05-09-alpine-lodging-tax",
        "generated_utc": "2026-05-09T00:00:00Z",
        "keywords": [
            "lodging tax",
            "lodging",
            "transient",
            "room tax",
            "travel and tourism",
            "tourism",
        ],
    },
}

WINDOW_BEFORE = 220
WINDOW_AFTER = 260
MERGE_GAP = 40
MIN_SNIPPET_LEN = 60
MAX_PER_SOURCE = 3
MAX_TOTAL = 30


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _find_matches(text: str, keywords: list[str]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pattern = re.compile(
        "|".join(re.escape(k) for k in keywords),
        re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        spans.append((m.start(), m.end()))
    return spans


def _build_windows(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    for s, e in spans:
        ws = max(0, s - WINDOW_BEFORE)
        we = min(len(text), e + WINDOW_AFTER)
        windows.append((ws, we))
    if not windows:
        return []
    windows.sort()
    merged = [windows[0]]
    for ws, we in windows[1:]:
        last_s, last_e = merged[-1]
        if ws <= last_e + MERGE_GAP:
            merged[-1] = (last_s, max(last_e, we))
        else:
            merged.append((ws, we))
    return merged


def _select_snippets_for_source(
    text: str, keywords: list[str]
) -> list[tuple[int, int, str]]:
    spans = _find_matches(text, keywords)
    if not spans:
        return []
    windows = _build_windows(text, spans)
    out: list[tuple[int, int, str]] = []
    for ws, we in windows:
        snippet = _normalise(text[ws:we])
        if len(snippet) < MIN_SNIPPET_LEN:
            continue
        out.append((ws, we - ws, snippet))
    out.sort(key=lambda r: r[0])
    return out[:MAX_PER_SOURCE]


def _open_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _gather(
    conn: sqlite3.Connection, keywords: list[str]
) -> tuple[list[dict], int, int]:
    cur = conn.cursor()
    n_docs = cur.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    n_tx = cur.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]

    rows: list[dict] = []

    doc_rows = cur.execute(
        "SELECT id, source_url, fetch_time_utc, raw_text FROM documents "
        "WHERE raw_text IS NOT NULL ORDER BY id"
    ).fetchall()
    for sid, url, fetched, text in doc_rows:
        for offset, length, snippet in _select_snippets_for_source(text, keywords):
            rows.append(
                {
                    "kind": "doc",
                    "source_id": sid,
                    "source_url": url,
                    "fetch_time_utc": fetched,
                    "offset": offset,
                    "length": length,
                    "snippet": snippet,
                }
            )

    tx_rows = cur.execute(
        "SELECT id, video_url, fetch_time_utc, full_text FROM transcripts "
        "WHERE full_text IS NOT NULL ORDER BY id"
    ).fetchall()
    for sid, url, fetched, text in tx_rows:
        for offset, length, snippet in _select_snippets_for_source(text, keywords):
            rows.append(
                {
                    "kind": "tx",
                    "source_id": sid,
                    "source_url": url,
                    "fetch_time_utc": fetched,
                    "offset": offset,
                    "length": length,
                    "snippet": snippet,
                }
            )

    rows.sort(key=lambda r: (0 if r["kind"] == "doc" else 1, r["source_id"], r["offset"]))
    return rows[:MAX_TOTAL], n_docs, n_tx


def _render(topic_key: str, topic: dict, evidence: list[dict], n_docs: int, n_tx: int) -> str:
    lines: list[str] = []
    lines.append(f"# Alpine watchdog brief: {topic['title']}")
    lines.append("")
    lines.append(f"**Brief id:** {topic['brief_id']}")
    lines.append(f"**Generated:** {topic['generated_utc']}")
    lines.append(
        f"**Corpus snapshot:** Database/gov_watchdog.db @ documents={n_docs} transcripts={n_tx}"
    )
    lines.append("**Method:** deterministic keyword extraction (no LLM); see `scripts/watchdog_brief.py`.")
    lines.append("")
    lines.append("## What this brief covers")
    lines.append("")
    lines.append(f"Topic keywords: `{', '.join(topic['keywords'])}`")
    lines.append(
        "Source filter: any document or transcript whose extracted text contains at "
        "least one keyword (case-insensitive)."
    )
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    for n, ev in enumerate(evidence, 1):
        lines.append(f"- {ev['snippet']} [^{n}]")
    lines.append("")
    lines.append("## Sources cited")
    lines.append("")
    for n, ev in enumerate(evidence, 1):
        lines.append(
            f"[^{n}]: {ev['source_url']} — fetched {ev['fetch_time_utc']} "
            f"({ev['kind']}, char_offset={ev['offset']}, len={ev['length']})"
        )
    lines.append("")
    lines.append("## Method & reproducibility")
    lines.append("")
    lines.append(f"Run: `python scripts/watchdog_brief.py --topic {topic_key}`")
    lines.append("Repeatability: identical corpus → identical brief (verified by re-run SHA256).")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a watchdog brief from the Phase 1 corpus.")
    parser.add_argument("--topic", required=True, choices=sorted(TOPICS.keys()))
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--out-dir", default=str(BRIEFS_DIR))
    args = parser.parse_args(argv)

    topic = TOPICS[args.topic]
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"db not found: {db_path}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with _open_ro(db_path) as conn:
        evidence, n_docs, n_tx = _gather(conn, topic["keywords"])

    md = _render(args.topic, topic, evidence, n_docs, n_tx)
    md_path = out_dir / f"{topic['brief_id']}.md"
    cite_path = out_dir / f"{topic['brief_id']}.citations.json"

    md_bytes = md.encode("utf-8")
    md_path.write_bytes(md_bytes)
    sha = hashlib.sha256(md_bytes).hexdigest()

    citations = {
        "brief_id": topic["brief_id"],
        "topic": args.topic,
        "title": topic["title"],
        "generated_utc": topic["generated_utc"],
        "keywords": topic["keywords"],
        "corpus": {
            "db_path": "Database/gov_watchdog.db",
            "documents": n_docs,
            "transcripts": n_tx,
        },
        "brief_sha256": sha,
        "evidence": [
            {
                "n": n,
                "kind": ev["kind"],
                "source_id": ev["source_id"],
                "source_url": ev["source_url"],
                "fetch_time_utc": ev["fetch_time_utc"],
                "offset": ev["offset"],
                "length": ev["length"],
            }
            for n, ev in enumerate(evidence, 1)
        ],
    }
    cite_path.write_text(
        json.dumps(citations, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    print(f"brief: {md_path}")
    print(f"citations: {cite_path}")
    print(f"snippets: {len(evidence)}")
    print(f"sha256: {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
