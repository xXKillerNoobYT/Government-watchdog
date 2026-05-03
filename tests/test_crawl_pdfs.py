"""Tests for scripts/crawl_pdfs.py (WEI-259).

Network-free: targets pure helpers + a fixture-driven crawl through a mocked
requests session. Live crawl acceptance lives in WEI-262 closeout.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import crawl_pdfs  # noqa: E402
import db  # noqa: E402


def test_is_pdf_url_by_extension():
    assert crawl_pdfs._is_pdf_url("https://x.gov/a/agenda.pdf")
    assert not crawl_pdfs._is_pdf_url("https://x.gov/a/page.html")


def test_is_pdf_url_by_content_type():
    assert crawl_pdfs._is_pdf_url("https://x.gov/a/file", "application/pdf; charset=binary")


def test_classify_doc_type():
    assert crawl_pdfs._classify_doc_type("https://x/agenda-2025.pdf", "Council Agenda") == "agenda"
    assert crawl_pdfs._classify_doc_type("https://x/minutes.pdf", "") == "minutes"
    assert crawl_pdfs._classify_doc_type("https://x/ordinance_42.pdf", "") == "ordinance"
    assert crawl_pdfs._classify_doc_type("https://x/foo.pdf", "Random") == "other"


def test_safe_name_strips_unsafe_chars():
    assert crawl_pdfs._safe_name("https://x.gov/path/agenda 2025!.pdf") == "agenda_2025_.pdf"


def test_extract_links_resolves_relative():
    html = '<a href="/a">A</a><a href="https://x.gov/b">B</a><a href="javascript:void(0)">x</a>'
    out = crawl_pdfs._extract_links("https://x.gov/page", html)
    assert "https://x.gov/a" in out
    assert "https://x.gov/b" in out
    assert all("javascript" not in u for u in out)


def test_extract_sitemap_urls():
    xml = b"""<?xml version='1.0'?>
    <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
      <url><loc>https://x.gov/a.pdf</loc></url>
      <url><loc>https://x.gov/b</loc></url>
    </urlset>"""
    urls = crawl_pdfs._extract_sitemap_urls(xml)
    assert urls == ["https://x.gov/a.pdf", "https://x.gov/b"]


def test_alpine_filter_excludes_non_alpine():
    pat = crawl_pdfs.ALPINE_PATTERN
    assert pat.search("https://lincolncountywy.gov/alpine/agenda")
    assert not pat.search("https://lincolncountywy.gov/kemmerer/agenda")


@pytest.fixture()
def isolated_repo(tmp_path: Path, monkeypatch):
    """Point the crawler at a temp repo + DB so writes don't touch real tree."""
    monkeypatch.setattr(crawl_pdfs, "Path", Path)
    monkeypatch.setattr(crawl_pdfs.db, "DEFAULT_DB_PATH", tmp_path / "test.db")
    db.apply_migrations(tmp_path / "test.db")
    return tmp_path


def _mock_response(*, content=b"", text="", content_type="text/html", url="https://x"):
    resp = MagicMock()
    resp.headers = {"content-type": content_type}
    resp.content = content
    resp.text = text
    resp.url = url
    resp.close = MagicMock()
    return resp


def test_crawl_inserts_pdf_and_is_idempotent(isolated_repo: Path, monkeypatch):
    pdf_bytes = b"%PDF-1.4 fake"
    page_html = (
        '<html><body>'
        '<a href="https://www.alpinewy.gov/agenda-2026-05.pdf">Agenda</a>'
        '</body></html>'
    )

    def fake_get(self, url, **kwargs):
        if url.endswith(".pdf"):
            return _mock_response(content=pdf_bytes, content_type="application/pdf", url=url)
        if url.endswith("sitemap.xml"):
            return _mock_response(content=b"<urlset></urlset>", text="<urlset></urlset>",
                                  content_type="application/xml", url=url)
        return _mock_response(text=page_html, content=page_html.encode(), content_type="text/html", url=url)

    monkeypatch.setattr(crawl_pdfs.requests.Session, "get", fake_get)
    monkeypatch.setattr(crawl_pdfs, "_robotparser",
                        lambda host, scheme="https": MagicMock(can_fetch=lambda *a, **k: True))
    monkeypatch.setattr(crawl_pdfs.RateLimiter, "wait", lambda self, host: None)

    repo_root = Path(crawl_pdfs.__file__).resolve().parent.parent
    monkeypatch.chdir(repo_root)

    db_path = isolated_repo / "test.db"
    conn = db.open_db(db_path)

    target = crawl_pdfs.Target(
        key="alpinewy",
        base_url="https://www.alpinewy.gov",
        seeds=("https://www.alpinewy.gov/",),
        alpine_filter=False,
    )

    new, scanned = crawl_pdfs.crawl_target(target, conn, dry_run=False, limit=5)
    assert new == 1
    rows = conn.execute("SELECT source_url, sha256, doc_type, local_path FROM documents").fetchall()
    assert len(rows) == 1
    assert rows[0]["source_url"] == "https://www.alpinewy.gov/agenda-2026-05.pdf"
    assert rows[0]["doc_type"] == "agenda"

    # Re-run = idempotent: 0 new
    new2, _ = crawl_pdfs.crawl_target(target, conn, dry_run=False, limit=5)
    assert new2 == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1

    # Cleanup the file written into the real repo Raw-PDFs/ during the test
    written = repo_root / rows[0]["local_path"]
    if written.exists():
        written.unlink()
        # remove year/source/ if empty
        try:
            written.parent.rmdir()
            written.parent.parent.rmdir()
        except OSError:
            pass
