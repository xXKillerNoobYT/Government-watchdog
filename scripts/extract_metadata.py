"""Phase 1.5 — backfill structured metadata for documents and transcripts.

Idempotent. Reads:
  - Transcripts/2026/*.json (local cache from fetch_transcripts.py) for
    transcripts: meeting_date, upload_date, title.
  - documents.raw_text for PDFs: doc_date, doc_type, derived title.

Writes only when the existing column is NULL or a placeholder
(doc_type='other', title equal to the URL slug). Never touches raw_text,
source_url, fetch_time_utc, or sha256.

Run: python scripts/extract_metadata.py
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "Database" / "gov_watchdog.db"
TRANSCRIPTS_DIR = REPO_ROOT / "Transcripts" / "2026"


_MONTH = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


def _date_from_pdf_text(text: str) -> str | None:
    if not text:
        return None
    t = text.replace("\n", " ")

    m = re.search(r"FOR\s*FISCAL\s*YEAR\s*ENDED\s*JUNE\s*30,?\s*(\d{4})", t, re.I)
    if m:
        return f"{m.group(1)}-06-30"

    m = re.search(r"FYE\s*6[-/]30[-/](\d{4})", t, re.I)
    if m:
        return f"{m.group(1)}-06-30"

    m = re.search(r"As\s*of\s*June\s*30,?\s*(\d{4})", t, re.I)
    if m:
        return f"{m.group(1)}-06-30"

    m = re.search(
        r"DUE\s*DATE:\s*([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})", t, re.I
    )
    if m and m.group(1).lower() in _MONTH:
        return f"{int(m.group(3)):04d}-{_MONTH[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"

    m = re.search(r"REPORT\s*YEAR:?\s*(\d{4})", t, re.I)
    if m:
        return f"{m.group(1)}-12-31"

    return None


def _type_from_pdf_text(text: str) -> str | None:
    if not text:
        return None
    t = text.replace("\n", " ")
    if re.search(r"PUBLIC\s*RECORDS\s*REQUEST", t, re.I):
        return "form"
    if re.search(r"ORDINANCE\s*NO\.", t, re.I):
        return "ordinance"
    if re.search(r"INTERNAL\s*CONTROL\s*EVALUATION", t, re.I):
        return "internal_control"
    if re.search(r"BALANCE\s*SHEET", t, re.I):
        return "balance_sheet"
    if re.search(r"F-66\(WY-2\)|Annual\s*City\s*and\s*Town\s*Financial\s*Report|Local\s*Government\s*Annual\s*Report", t, re.I):
        return "annual_report"
    return None


_PRETTY_TYPE = {
    "form": "Form",
    "ordinance": "Ordinance",
    "balance_sheet": "Balance sheet",
    "internal_control": "Internal control evaluation",
    "annual_report": "Annual financial report",
}


def _title_for_pdf(doc_type: str | None, doc_date: str | None) -> str | None:
    if not doc_type or doc_type not in _PRETTY_TYPE:
        return None
    pretty = _PRETTY_TYPE[doc_type]
    if doc_date:
        return f"{pretty} ({doc_date})"
    return pretty


_DATE_IN_TITLE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _meeting_date_from_title(title: str | None) -> str | None:
    if not title:
        return None
    m = _DATE_IN_TITLE.search(title)
    if not m:
        return None
    mo, dy, yr = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return f"{yr:04d}-{mo:02d}-{dy:02d}"


def _upload_date_iso(raw: str | None) -> str | None:
    if not raw:
        return None
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def update_transcripts(con: sqlite3.Connection) -> dict:
    cur = con.cursor()
    stats = {"meeting_date": 0, "upload_date": 0, "title_meta_used": 0}
    rows = cur.execute(
        "SELECT id, video_id, title, meeting_date, upload_date FROM transcripts"
    ).fetchall()
    for sid, vid, cur_title, cur_meet, cur_upload in rows:
        meta_path = TRANSCRIPTS_DIR / f"{vid}.json"
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8")).get("meta", {})
        new_title = meta.get("title")
        if new_title and new_title != cur_title:
            cur.execute("UPDATE transcripts SET title=? WHERE id=?", (new_title, sid))
            stats["title_meta_used"] += 1
        if cur_meet is None:
            md = _meeting_date_from_title(new_title or cur_title)
            if md:
                cur.execute(
                    "UPDATE transcripts SET meeting_date=? WHERE id=?", (md, sid)
                )
                stats["meeting_date"] += 1
        if cur_upload is None:
            ud = _upload_date_iso(meta.get("upload_date"))
            if ud:
                cur.execute(
                    "UPDATE transcripts SET upload_date=? WHERE id=?", (ud, sid)
                )
                stats["upload_date"] += 1
    return stats


def update_documents(con: sqlite3.Connection) -> dict:
    cur = con.cursor()
    stats = {"doc_date": 0, "doc_type": 0, "title": 0}
    rows = cur.execute(
        "SELECT id, source_url, title, doc_type, doc_date, raw_text FROM documents"
    ).fetchall()
    for sid, url, cur_title, cur_type, cur_date, text in rows:
        new_type: str | None = cur_type
        if cur_type in (None, "other"):
            t = _type_from_pdf_text(text)
            if t:
                cur.execute("UPDATE documents SET doc_type=? WHERE id=?", (t, sid))
                new_type = t
                stats["doc_type"] += 1
        new_date: str | None = cur_date
        if cur_date is None:
            d = _date_from_pdf_text(text)
            if d:
                cur.execute("UPDATE documents SET doc_date=? WHERE id=?", (d, sid))
                new_date = d
                stats["doc_date"] += 1

        url_slug = url.rsplit("/", 1)[-1] if url else None
        if cur_title is None or cur_title == url_slug:
            new_title = _title_for_pdf(new_type, new_date)
            if new_title:
                cur.execute(
                    "UPDATE documents SET title=? WHERE id=?", (new_title, sid)
                )
                stats["title"] += 1
    return stats


def main() -> None:
    con = sqlite3.connect(DB_PATH)
    try:
        with con:
            tx_stats = update_transcripts(con)
            doc_stats = update_documents(con)
        print("transcripts updated:", tx_stats)
        print("documents updated:  ", doc_stats)
    finally:
        con.close()


if __name__ == "__main__":
    main()
