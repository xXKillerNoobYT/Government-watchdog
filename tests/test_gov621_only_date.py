"""GOV-621 Option-C pilot: the `--only-date` narrowing filter.

The pilot must run the crawler->ingest->structure pipeline against exactly ONE
Alpine meeting date (2026-06-23 in production) — not the whole corpus. These tests
pin the contract of the post-walk, exclude-only date filter added to both
`ingest_local_corpus.py` and `structure_real_corpus.py`:

- narrowing keeps ONLY files whose meeting date matches the window;
- it is exclude-only: it never adds or reclassifies a file — the signed GOV-133
  walk + classification are unchanged, so out-of-window items (incl. the binding
  out-of-folder allowlist notice) simply drop;
- coverage denominators scope to the window (no phantom "0/124" reporting);
- a non-matching date yields 0 selected files (failure pattern (d): a filter that
  matches nothing is a defect, caught here rather than in a live run);
- structuring under the window creates exactly one meeting spine + only
  in-window completeness gaps, and never flips publication state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import ingest_local_corpus as ing  # noqa: E402
import structure_real_corpus as sr  # noqa: E402


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    """Two dated meeting folders with a primary source each, plus one md-only
    folder and one out-of-folder allowlist notice (date 2026-03-24)."""
    root = tmp_path / "corpus"
    (root / "2026-06-23").mkdir(parents=True)
    (root / "2024-10-09").mkdir(parents=True)
    (root / "2023-04-26").mkdir(parents=True)  # md-only (no primary)
    (root / "master").mkdir(parents=True)

    (root / "2026-06-23" / "MEET-Agenda_jun23_council.pdf").write_bytes(b"%PDF agenda")
    (root / "2026-06-23" / "Council_Packet.pdf").write_bytes(b"%PDF packet")
    (root / "2024-10-09" / "Council_Meeting_Packet.pdf").write_bytes(b"%PDF other")
    (root / "2023-04-26" / "notes.md").write_text("# derived only")
    (root / "master" / "PRESERVED_media12251_turley_postponement_notice_2026-03-24.pdf").write_bytes(
        b"%PDF turley notice"
    )
    return root


@pytest.fixture()
def patched_repo(tmp_path: Path, monkeypatch) -> Path:
    """Redirect BOTH modules' managed raw store into tmp (never touch the repo)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(ing, "REPO_ROOT", repo)
    monkeypatch.setattr(sr, "REPO_ROOT", repo)
    return repo


# --- ingest-level filter -------------------------------------------------------

def test_only_date_narrows_to_one_folder(corpus: Path, patched_repo: Path,
                                         tmp_path: Path) -> None:
    summary = ing.ingest(corpus, tmp_path / "g.db", only_date="2026-06-23")
    # only the two 2026-06-23 PDFs; the other folder + the allowlist notice drop.
    assert summary["selected"] == 2
    assert summary["new_documents"] == 2
    assert summary["orphans"] == 0
    with db.open_db(tmp_path / "g.db") as conn:
        dates = [r[0] for r in conn.execute("SELECT DISTINCT doc_date FROM documents")]
    assert dates == ["2026-06-23"]


def test_only_date_is_exclude_only_drops_allowlist_notice(corpus: Path,
                                                          patched_repo: Path,
                                                          tmp_path: Path) -> None:
    """The 2026-03-24 allowlist notice is NOT reclassified or pulled in — it is
    simply outside the window and excluded."""
    summary = ing.ingest(corpus, tmp_path / "g.db", only_date="2026-06-23")
    assert "notice" not in summary["by_doc_type"]
    with db.open_db(tmp_path / "g.db") as conn:
        notices = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE doc_type = 'notice'"
        ).fetchone()[0]
    assert notices == 0


def test_only_date_scopes_coverage_denominators(corpus: Path, patched_repo: Path,
                                                tmp_path: Path) -> None:
    summary = ing.ingest(corpus, tmp_path / "g.db", only_date="2026-06-23")
    cov = summary["coverage"]
    assert cov["meeting_folders_total"] == 1     # scoped, not the full corpus
    assert cov["with_primary_source"] == 1
    assert cov["earliest_primary"] == "2026-06-23"


def test_only_date_no_match_yields_zero_selected(corpus: Path, patched_repo: Path,
                                                 tmp_path: Path) -> None:
    """Failure pattern (d): a window that matches nothing selects 0 files and
    writes no rows — surfaced as a defect, never a silent empty success."""
    summary = ing.ingest(corpus, tmp_path / "g.db", only_date="2099-01-01")
    assert summary["selected"] == 0
    assert summary["new_documents"] == 0
    with db.open_db(tmp_path / "g.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_only_date_none_ingests_full_corpus(corpus: Path, patched_repo: Path,
                                            tmp_path: Path) -> None:
    """Default (no window) is unchanged: full signed selection ingests."""
    summary = ing.ingest(corpus, tmp_path / "g.db")
    # 2 PDFs (2026-06-23) + 1 PDF (2024-10-09) + 1 allowlist notice = 4.
    assert summary["selected"] == 4
    # all three date-named folders count (incl. the md-only 2023-04-26).
    assert summary["coverage"]["meeting_folders_total"] == 3


def test_only_date_dry_run_writes_nothing(corpus: Path, patched_repo: Path,
                                          tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    summary = ing.ingest(corpus, db_path, dry_run=True, only_date="2026-06-23")
    assert summary["dry_run"] is True
    assert summary["selected"] == 2
    assert not db_path.exists()  # dry-run never opens/creates the DB


# --- structure-level filter ----------------------------------------------------

def test_structure_only_date_one_meeting_scoped_gaps(corpus: Path, patched_repo: Path,
                                                     tmp_path: Path) -> None:
    db_path = tmp_path / "g.db"
    summary = sr.structure(db_path=db_path, corpus_root=corpus, only_date="2026-06-23")
    # exactly one meeting spine (the windowed folder), not the whole corpus.
    assert summary["rows"]["meetings"] == 1
    assert summary["meeting_folders"] == 1
    # deterministic pass binds zero names and never flips publication state.
    assert summary["rows"]["speaker_attributions"] == 0
    assert summary["no_orphan_statements"] == 0
    # completeness gaps are all for the in-window meeting/documents.
    with db.open_db(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT m.meeting_date FROM meetings m"
        ).fetchall()
        assert [r[0] for r in rows] == ["2026-06-23"]
        # PDF gaps only for in-window docs.
        offdate = conn.execute(
            "SELECT COUNT(*) FROM completeness_gaps g JOIN documents d "
            "ON g.subject_node_id = CAST(d.id AS TEXT) "
            "WHERE g.gap_type = 'pdf_text_unextracted' AND d.doc_date != '2026-06-23'"
        ).fetchone()[0]
        assert offdate == 0
