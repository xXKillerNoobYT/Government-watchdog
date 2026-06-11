"""Tests for the GOV-124 on-disk corpus ingest adapter.

Covers the acceptance criteria + the GOV-133 sign-off conditions:
- selection drives off the shared signed walk (C1 no drift): only .pdf/.txt +
  the binding allowlist become `documents`; .md never does;
- one corpus-level `sources` row (C4); no orphan documents;
- COPY semantics (C2): bytes land in a managed raw store and re-hash to the
  recorded sha256 (reproducibility);
- idempotent: a second run creates no new rows and copies nothing;
- coverage report counts primary-vs-derived folders.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import ingest_local_corpus as ing  # noqa: E402
from raw_preservation import verify_reproducibility  # noqa: E402


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    (root / "2024-10-09").mkdir(parents=True)
    (root / "2023-04-26").mkdir(parents=True)  # md-only folder (no primary)
    (root / "master").mkdir(parents=True)

    (root / "2024-10-09" / "Council_Meeting_Packet.pdf").write_bytes(b"%PDF packet")
    (root / "2024-10-09" / "youtube_transcript_abc.txt").write_text("hello transcript")
    (root / "2024-10-09" / "summary.md").write_text("# derived — must NOT ingest")
    (root / "2023-04-26" / "notes.md").write_text("# derived only")
    (root / "master" / "PRESERVED_media12251_turley_postponement_notice_2026-03-24.pdf").write_bytes(
        b"%PDF turley notice"
    )
    return root


@pytest.fixture()
def patched_repo(tmp_path: Path, monkeypatch) -> Path:
    """Redirect the managed raw store into tmp so tests never write into the repo."""
    monkeypatch.setattr(ing, "REPO_ROOT", tmp_path / "repo")
    (tmp_path / "repo").mkdir()
    return tmp_path / "repo"


def _doc_rows(db_path: Path):
    with db.open_db(db_path) as conn:
        return conn.execute(
            "SELECT source_url, doc_type, doc_date, local_path, sha256 FROM documents "
            "ORDER BY doc_date"
        ).fetchall()


def test_ingest_selection_excludes_md_and_links_source(corpus: Path, patched_repo: Path,
                                                       tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    summary = ing.ingest(corpus, db_path)
    # 1 pdf + 1 txt (2024-10-09) + 1 allowlist notice = 3 source-of-record; no .md.
    assert summary["selected"] == 3
    assert summary["new_documents"] == 3
    assert summary["orphans"] == 0
    rows = _doc_rows(db_path)
    assert len(rows) == 3
    assert not any(r["source_url"].endswith(".md") for r in rows)
    # exactly one corpus-level sources row, and every doc links to it
    with db.open_db(db_path) as conn:
        srcs = conn.execute("SELECT source_id FROM sources").fetchall()
        assert [s["source_id"] for s in srcs] == [ing.CORPUS_SOURCE_ID]


def test_allowlist_notice_ingested_as_notice(corpus: Path, patched_repo: Path,
                                             tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    ing.ingest(corpus, db_path)
    rows = _doc_rows(db_path)
    notice = [r for r in rows if r["doc_type"] == "notice"]
    assert len(notice) == 1
    assert notice[0]["source_url"].endswith("turley_postponement_notice_2026-03-24.pdf")
    assert notice[0]["doc_date"] == "2026-03-24"


def test_copy_then_reproducibility_clean(corpus: Path, patched_repo: Path,
                                         tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    summary = ing.ingest(corpus, db_path)
    assert summary["copied_to_raw_store"] == 3
    # Every stored raw file re-hashes to the recorded sha256 (C2 / reproducibility).
    with db.open_db(db_path) as conn:
        result = verify_reproducibility(conn, repo_root=patched_repo)
    assert result["missing"] == [] and result["mismatch"] == []
    assert result["checked"] == 3 and result["ok"] == 3


def test_idempotent_second_run(corpus: Path, patched_repo: Path, tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    ing.ingest(corpus, db_path)
    before = _doc_rows(db_path)
    second = ing.ingest(corpus, db_path)
    after = _doc_rows(db_path)
    assert second["new_documents"] == 0
    assert second["copied_to_raw_store"] == 0  # sha-addressed store already populated
    assert [tuple(r) for r in before] == [tuple(r) for r in after]  # no row drift


def test_coverage_counts_primary_vs_derived(corpus: Path, patched_repo: Path,
                                            tmp_path: Path) -> None:
    summary = ing.ingest(corpus, tmp_path / "g.db")
    cov = summary["coverage"]
    assert cov["meeting_folders_total"] == 2
    assert cov["with_primary_source"] == 1   # only 2024-10-09 has primary
    assert cov["derived_md_only"] == 1       # 2023-04-26 is md-only
    assert cov["earliest_primary"] == "2024-10-09"
