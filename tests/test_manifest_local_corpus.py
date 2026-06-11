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
    # The binding out-of-folder allowlist file (GOV-133): one real notice in master/.
    (root / "master").mkdir()
    (root / "master" / "PRESERVED_media12251_turley_postponement_notice_2026-03-24.pdf").write_bytes(
        b"%PDF notice"
    )
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
    # Footprint counts source-of-record bytes (pdf+txt in folders + allowlist),
    # never md/json.
    sor_bytes = (
        len(b"%PDF-1.4 fake") + len(b"%PDF-1.4 also fake") + len(b"%PDF small")
        + len("meeting transcript") + len(b"%PDF notice")  # allowlisted notice
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


def test_sign_off_block_records_settled_selection(corpus: Path) -> None:
    m = mlc.build_manifest(corpus)
    so = m["sign_off"]
    assert so["review_issue"] == "GOV-133"
    blob = " ".join(so["settled"]).lower()
    assert "copy bytes" in blob  # C2: copy, not reference-in-place
    assert "alpine_local_corpus" in blob  # one corpus-level sources row
    assert "34/124" in so["coverage_reality"]


def test_allowlisted_out_of_folder_is_source_of_record(corpus: Path) -> None:
    m = mlc.build_manifest(corpus)
    allow = m["allowlisted_out_of_folder"]
    assert len(allow) == 1
    a = allow[0]
    assert a["rel_path"].endswith("turley_postponement_notice_2026-03-24.pdf")
    assert a["meeting_date"] == "2026-03-24" and a["source_type"] == "notice"


def test_iter_source_of_record_selection_order_and_allowlist(corpus: Path) -> None:
    selected = mlc.iter_source_of_record_files(corpus)
    # 3 pdf + 1 txt in folders + 1 allowlisted notice = 5; no .md/.json/unclassified.
    assert len(selected) == 5
    # Meeting-folder files come first (oldest->newest), allowlist last.
    assert selected[0].meeting_date == "2023-04-26"
    assert selected[-1].origin == "allowlist"
    assert all(sf.file_class.source_of_record for sf in selected)
    # No derived/unclassified leaked into the ingest set.
    assert not any(sf.path.suffix == ".md" for sf in selected)


def test_unclassified_files_surfaced(corpus: Path) -> None:
    m = mlc.build_manifest(corpus)
    unc = m["unclassified_in_meeting_folders"]
    # extensionless -> "<none>", and ".err" both surfaced, neither source-of-record.
    assert "<none>" in unc and ".err" in unc
    assert unc["<none>"]["files"] == 1 and unc[".err"]["files"] == 1
    assert "not source-bearing" in " ".join(m["resolved_notes"]).lower()
