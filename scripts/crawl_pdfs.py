"""PDF crawler for the Government Watchdog Phase 1 (WEI-259).

Targets:
- alpinewy.gov
- lincolncountywy.gov (Alpine-relevant pages only — links *to* alpine in path/text)
- library.municode.com (Alpine entry)

Behaviour (per Docs/phase1-spec.md §2.1, §6, §7):
- Static HTTP + sitemap parsing only (no Playwright in Phase 1).
- Respects robots.txt by default; jittered 3-12s delay; ≤20 req/min/domain cap.
- Idempotent: skip if (source_url) already in `documents`; SHA256 dedupe before insert.
- Writes provenance (sha256, fetch_time_utc ISO-8601 ms, robots_status, cms_signature).
- Files land at Raw-PDFs/<YYYY>/<source>/<safe_name>.pdf.

CLI:
    python scripts/crawl_pdfs.py [--dry-run] [--limit N] [--target alpinewy|lincoln|municode]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.robotparser
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    print("Missing deps; pip install -r requirements.txt", file=sys.stderr)
    raise

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402

logger = logging.getLogger("crawl_pdfs")

USER_AGENT = "GovernmentWatchdogBot/0.1 (+contact: weirdtoocompany@gmail.com)"
MIN_DELAY = 3.0
MAX_DELAY = 12.0
PER_MINUTE_CAP = 20
REQUEST_TIMEOUT = 30
MAX_PAGES_PER_TARGET = 500  # safety cap on page traversal


@dataclass(frozen=True)
class Target:
    key: str
    base_url: str
    seeds: tuple[str, ...]
    alpine_filter: bool  # True => only follow links matching alpine_pattern

    @property
    def host(self) -> str:
        return urllib.parse.urlparse(self.base_url).netloc.lower()


TARGETS: dict[str, Target] = {
    "alpinewy": Target(
        key="alpinewy",
        base_url="https://www.alpinewy.gov",
        seeds=(
            "https://www.alpinewy.gov/",
            "https://www.alpinewy.gov/sitemap.xml",
        ),
        alpine_filter=False,
    ),
    "lincoln": Target(
        key="lincoln",
        base_url="https://www.lincolncountywy.gov",
        seeds=(
            "https://www.lincolncountywy.gov/",
            "https://www.lincolncountywy.gov/sitemap.xml",
        ),
        alpine_filter=True,
    ),
    "municode": Target(
        key="municode",
        base_url="https://library.municode.com",
        seeds=(
            "https://library.municode.com/wy/alpine",
        ),
        alpine_filter=False,
    ),
}

ALPINE_PATTERN = re.compile(r"alpine", re.IGNORECASE)
PDF_EXTENSIONS = (".pdf",)


class RateLimiter:
    """Per-domain RPM + jittered delay between requests."""

    def __init__(self, per_minute_cap: int = PER_MINUTE_CAP) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._last: dict[str, float] = {}
        self._cap = per_minute_cap

    def wait(self, host: str) -> None:
        now = time.monotonic()
        bucket = self._buckets[host]
        while bucket and now - bucket[0] > 60:
            bucket.popleft()
        if len(bucket) >= self._cap:
            sleep_for = 60 - (now - bucket[0]) + 0.1
            logger.info("rate-cap %s: sleeping %.1fs", host, sleep_for)
            time.sleep(sleep_for)
            now = time.monotonic()
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
        last = self._last.get(host)
        if last is not None:
            elapsed = now - last
            jitter = random.uniform(MIN_DELAY, MAX_DELAY)
            if elapsed < jitter:
                time.sleep(jitter - elapsed)
        self._last[host] = time.monotonic()
        bucket.append(time.monotonic())


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_name(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    tail = Path(parsed.path).name or "index"
    tail = re.sub(r"[^A-Za-z0-9._-]+", "_", tail)
    return tail[:120] or "index.pdf"


def _detect_cms(headers: dict[str, str], html: str) -> str:
    text = (html or "").lower()
    server = headers.get("server", "").lower()
    if "civicplus" in text or "civicplus" in server:
        return "civicplus"
    if "granicus" in text or "granicus" in server:
        return "granicus"
    if "wp-content" in text or "wordpress" in text:
        return "wordpress"
    if "municode" in text:
        return "municode"
    return "static"


def _robotparser(host: str, scheme: str = "https") -> urllib.robotparser.RobotFileParser:
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{scheme}://{host}/robots.txt")
    try:
        rp.read()
    except Exception as exc:
        logger.warning("robots.txt fetch failed for %s: %s", host, exc)
    return rp


def _is_allowed(rp: urllib.robotparser.RobotFileParser, url: str) -> bool:
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def _http_get(session: requests.Session, url: str, *, allow_binary: bool = False):
    return session.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        allow_redirects=True,
        stream=allow_binary,
    )


def _extract_links(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        out.append(urllib.parse.urljoin(base_url, href))
    return out


def _extract_sitemap_urls(xml_bytes: bytes) -> list[str]:
    soup = BeautifulSoup(xml_bytes, "xml")
    return [loc.get_text(strip=True) for loc in soup.find_all("loc") if loc.get_text(strip=True)]


def _is_pdf_url(url: str, content_type: str | None = None) -> bool:
    path = urllib.parse.urlparse(url).path.lower()
    if path.endswith(PDF_EXTENSIONS):
        return True
    if content_type and "application/pdf" in content_type.lower():
        return True
    return False


def _classify_doc_type(url: str, title: str) -> str:
    blob = f"{url} {title}".lower()
    if "agenda" in blob:
        return "agenda"
    if "minute" in blob:
        return "minutes"
    if "ordinance" in blob:
        return "ordinance"
    if "resolution" in blob:
        return "resolution"
    if "code" in blob:
        return "code"
    if "packet" in blob:
        return "packet"
    return "other"


def _existing_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT source_url FROM documents").fetchall()
    return {r[0] for r in rows}


def _existing_hashes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT sha256 FROM documents").fetchall()
    return {r[0] for r in rows}


def _insert_document(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    referer_url: str | None,
    title: str | None,
    doc_type: str,
    local_path: str,
    sha256: str,
    size_bytes: int,
    fetch_time_utc: str,
    cms_signature: str,
    robots_status: str,
) -> bool:
    try:
        conn.execute(
            "INSERT INTO documents (source_url, referer_url, title, doc_type, local_path, "
            "sha256, size_bytes, fetch_time_utc, cms_signature, robots_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_url, referer_url, title, doc_type, local_path, sha256, size_bytes,
             fetch_time_utc, cms_signature, robots_status),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def crawl_target(
    target: Target,
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    rate: RateLimiter | None = None,
) -> tuple[int, int]:
    """Crawl a single target. Returns (new_pdfs, scanned_pages)."""
    rate = rate or RateLimiter()
    rp = _robotparser(target.host)
    session = requests.Session()
    seen_urls: set[str] = set()
    queue: deque[tuple[str, str | None]] = deque((s, None) for s in target.seeds)
    new_count = 0
    scanned = 0
    existing_urls = _existing_urls(conn)
    existing_hashes = _existing_hashes(conn)
    repo_root = Path(__file__).resolve().parent.parent
    year = datetime.now(timezone.utc).strftime("%Y")
    out_dir = repo_root / "Raw-PDFs" / year / target.key
    out_dir.mkdir(parents=True, exist_ok=True)

    while queue and scanned < MAX_PAGES_PER_TARGET:
        url, referer = queue.popleft()
        if url in seen_urls:
            continue
        seen_urls.add(url)

        host = urllib.parse.urlparse(url).netloc.lower()
        if host != target.host:
            continue

        allowed = _is_allowed(rp, url)
        if not allowed:
            logger.info("robots-skip %s", url)
            continue

        rate.wait(host)
        scanned += 1
        try:
            head_or_get = _http_get(session, url, allow_binary=True)
        except requests.RequestException as exc:
            logger.warning("fetch fail %s: %s", url, exc)
            continue

        ctype = head_or_get.headers.get("content-type", "")
        is_pdf = _is_pdf_url(url, ctype)

        if is_pdf:
            if url in existing_urls:
                head_or_get.close()
                continue
            content = head_or_get.content
            head_or_get.close()
            sha = _sha256(content)
            if sha in existing_hashes:
                logger.info("sha-dedupe skip %s", url)
                continue
            safe = _safe_name(url)
            local_path = out_dir / safe
            if dry_run:
                logger.info("[dry-run] would write %s (%d bytes)", local_path, len(content))
            else:
                local_path.write_bytes(content)
                rel_path = local_path.relative_to(repo_root).as_posix()
                title = re.sub(r"\.pdf$", "", safe, flags=re.IGNORECASE)
                doc_type = _classify_doc_type(url, title)
                inserted = _insert_document(
                    conn,
                    source_url=url,
                    referer_url=referer,
                    title=title,
                    doc_type=doc_type,
                    local_path=rel_path,
                    sha256=sha,
                    size_bytes=len(content),
                    fetch_time_utc=_now_utc_iso(),
                    cms_signature="static",
                    robots_status="allowed",
                )
                if inserted:
                    new_count += 1
                    existing_urls.add(url)
                    existing_hashes.add(sha)
            if limit is not None and new_count >= limit:
                break
            continue

        body = head_or_get.text
        head_or_get.close()
        cms = _detect_cms(dict(head_or_get.headers), body)

        if "xml" in ctype.lower() or url.lower().endswith(".xml"):
            for child_url in _extract_sitemap_urls(body.encode("utf-8", errors="ignore")):
                if target.alpine_filter and not ALPINE_PATTERN.search(child_url):
                    continue
                if child_url not in seen_urls:
                    queue.append((child_url, url))
            continue

        for link in _extract_links(url, body):
            if urllib.parse.urlparse(link).netloc.lower() != target.host:
                continue
            if target.alpine_filter and not (
                ALPINE_PATTERN.search(link) or ALPINE_PATTERN.search(body[:4000])
            ):
                continue
            if link in seen_urls:
                continue
            queue.append((link, url))

        logger.debug("scanned %s (cms=%s)", url, cms)

    return new_count, scanned


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap new PDFs per target (test runs)")
    parser.add_argument("--target", choices=list(TARGETS), action="append",
                        help="restrict to one or more targets")
    parser.add_argument("--db", default=str(db.DEFAULT_DB_PATH))
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.apply_migrations(Path(args.db))
    conn = db.open_db(Path(args.db))

    targets = [TARGETS[k] for k in (args.target or list(TARGETS))]

    started = _now_utc_iso()
    rate = RateLimiter()
    total_new = 0
    total_scanned = 0
    notes: list[str] = []
    for t in targets:
        try:
            new, scanned = crawl_target(t, conn, dry_run=args.dry_run, limit=args.limit, rate=rate)
            total_new += new
            total_scanned += scanned
            notes.append(f"{t.key}: +{new} pdfs, {scanned} pages")
        except Exception as exc:
            logger.exception("crawl failed for %s", t.key)
            notes.append(f"{t.key}: ERROR {exc}")

    if not args.dry_run:
        conn.execute(
            "INSERT INTO crawl_runs (started_utc, finished_utc, status, targets, "
            "new_documents, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (started, _now_utc_iso(), "ok", json.dumps([t.key for t in targets]),
             total_new, "; ".join(notes)),
        )
        conn.commit()

    logger.info("DONE: %d new pdfs across %d pages", total_new, total_scanned)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
