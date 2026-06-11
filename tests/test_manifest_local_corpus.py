"""Tests for the GOV-124 local-corpus selection manifest generator.

Covers the properties the SourceArchivist sign-off depends on:
- only YYYY-MM-DD folders are included; every other top-level entry is excluded;
- meeting folders are ordered oldest->newest (the ingest order);
- file-type classification is deterministic (pdf/txt = source-of-record,
  md/json = derived) and the SAME function the ingest adapter will import;
- `mayor-investigation/` is excluded with its risk reason;
- output is sanitized counts only and the generator is read-only (no writes to
  the corpus, no file contents read).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import manifest_local_corpus as mlc  # noqa: E402


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    """A miniature corpus: 3 dated meeting folders + the scratch/excluded dirs."""
    root = tmp_path / "TownOfAlpine"
    # Out of order on purpose — the generator must sort oldest->newest.
    (root / "2025-03-18").mkdir(parents=True)
    (root / "2023-04-26").mkdir(parents=True)
    (root / "2024-10-01").mkdir(parents=True)

    (root / "2023-04-26" / "agenda.pdf").write_bytes(b"%PDF-1.4 fake")
    (root / "2023-04-26" / "transcript.txt").write_text("meeting transcript")
    (root / "2023-04-26" / "notes.md").write_text("# derived notes")
    (root / "2024-10-01" / "minutes.pdf").write_bytes(b"%PDF-1.4 also fake")
    (root / "2024-10-01" / "meta.json").write_text("{}")
    (root / "2025-03-18" / "packet.pdf").write_bytes(b"%PDF small")
    # Unclassified files that must be surfaced (extensionless + .err), like the
    # real corpus has inside meeting folders.
    (root / "2025-03-18" / "scan").write_bytes(b"raw")
    (root / "2025-03-18" / "convert.err").write_text("boom")

    # Excluded top-level entries.
    (root / "directives").mkdir()
    (root / "mayor-investigation").mkdir()
    (root / "reports").mkdir()
    (root / ".memory").mkdir()
    (root / ".DS_Store").write_text("x")
    (root / "loose-readme.md").write_text("not in a meeting folder")
    return root


def test_only_date_folders_included_and_ordered(corpus: Path) -> None:
    folders = mlc.iter_meeting_folders(corpus)
    names = [name for name, _ in folders]
    assert names == ["2023-04-26", "2024-10-01", "2025-03-18"], "must be oldest->newest"


def test_classification_is_deterministic_source_of_record(tmp_path: Path) -> None:
    assert mlc.classify_file(Path("x.pdf")).source_of_record is True
    assert mlc.classify_file(Path("x.PDF")).source_of_record is True  # case-insensitive
    assert mlc.classify_file(Path("x.txt")).source_of_record is True
    assert mlc.classify_file(Path("x.md")).source_of_record is False
    assert mlc.classify_file(Path("x.json")).source_of_record is False
    assert mlc.classify_file(Path("x.png")).source_type == "other"


def test_manifest_counts_and_footprint(corpus: Path) -> None:
    m = mlc.build_manifest(corpus)
    assert m["meeting_folders"]["count"] == 3
    assert m["meeting_folders"]["oldest"] == "2023-04-26"
    assert m["meeting_folders"]["newest"] == "2025-03-18"
    # 3 pdf + 1 txt are source-of-record; md + json are not.
    assert m["totals"]["files_by_type"][".pdf"] == 3
    assert m["totals"]["files_by_type"][".txt"] == 1
    assert m["totals"]["files_by_type"][".md"] == 1
    # Footprint counts only source-of-record bytes (pdf+txt), never md/json.
    sor_bytes = (
        len(b"%PDF-1.4 fake") + len(b"%PDF-1.4 also fake") + len(b"%PDF small")
        + len("meeting transcript")
    )
    assert m["totals"]["source_of_record_footprint_bytes"] == sor_bytes


def test_excluded_top_level_includes_mayor_investigation_with_risk(corpus: Path) -> None:
    m = mlc.build_manifest(corpus)
    excluded = {e["name"]: e["reason"] for e in m["excluded_top_level"]}
    for name in ("directives", "mayor-investigation", "reports", ".memory",
                 ".DS_Store", "loose-readme.md"):
        assert name in excluded, f"{name} must appear in exclusion list"
    assert "RISK" in excluded["mayor-investigation"]
    # No date folder leaked into the exclusion list.
    assert not any(mlc.MEETING_DIR_RE.match(n) for n in excluded)


def test_read_only_does_not_mutate_corpus(corpus: Path) -> None:
    before = sorted(p.name for p in corpus.rglob("*"))
    mlc.build_manifest(corpus)
    after = sorted(p.name for p in corpus.rglob("*"))
    assert before == after, "manifest generation must not create/delete files"


def test_open_questions_flag_md_txt_and_storage(corpus: Path) -> None:
    m = mlc.build_manifest(corpus)
    blob = " ".join(m["open_questions"]).lower()
    assert ".txt" in blob and ".md" in blob
    assert "raw storage" in blob and "source granularity" in blob


def test_unclassified_files_surfaced(corpus: Path) -> None:
    m = mlc.build_manifest(corpus)
    unc = m["unclassified_in_meeting_folders"]
    # extensionless -> "<none>", and ".err" both surfaced, neither source-of-record.
    assert "<none>" in unc and ".err" in unc
    assert unc["<none>"]["files"] == 1 and unc[".err"]["files"] == 1
    assert "unclassified" in " ".join(m["open_questions"]).lower()
