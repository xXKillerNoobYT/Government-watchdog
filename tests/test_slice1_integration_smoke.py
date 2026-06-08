"""First-slice integration smoke tests (GOV-77, Stage 1 Slice 1 Issue E).

Integration of contracts 1.02 / 1.03 / 1.04 / 1.05. Source: GOV-71 §2.E.

Two halves, matching the Issue E success/failure definition:

* SUCCESS half — the full slice (migrate -> seed -> ingest sanitized fixture ->
  verify) runs clean and all three contract invariants hold:
    1. raw preserved + reproducibility hash matches,
    2. provenance present (source_id, sha256, fetch_time, archive URL),
    3. every record defaults not-publishable.

* FAILURE half (the anti-"green smoke that asserts nothing" guard) — when an
  invariant is deliberately broken, the smoke must go RED, not silently pass.
  These tests prove each of the three checks actually fails loudly on regression.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import db  # noqa: E402
import slice1_smoke as smoke  # noqa: E402


def _check(result: dict, name: str) -> dict:
    return next(c for c in result["checks"] if c["name"] == name)


# --- SUCCESS half: clean slice, all three invariants hold ------------------

def test_smoke_passes_on_clean_slice(tmp_path: Path) -> None:
    result = smoke.run_smoke(sandbox=tmp_path)
    assert result["ok"], result["checks"]
    assert {c["name"] for c in result["checks"]} == {
        "raw_preserved_and_reproducible",
        "provenance_present",
        "default_not_publishable",
    }
    assert all(c["passed"] for c in result["checks"])


def test_invariant_raw_preserved_and_reproducible(tmp_path: Path) -> None:
    result = smoke.run_smoke(sandbox=tmp_path)
    raw = _check(result, "raw_preserved_and_reproducible")
    assert raw["passed"]
    assert raw["reproducibility"]["checked"] == 1
    assert raw["reproducibility"]["ok"] == 1
    assert raw["reproducibility"]["missing"] == []
    assert raw["reproducibility"]["mismatch"] == []
    assert len(raw["recorded_sha256"]) == 64


def test_invariant_provenance_present(tmp_path: Path) -> None:
    result = smoke.run_smoke(sandbox=tmp_path)
    prov = _check(result, "provenance_present")
    assert prov["passed"]
    # the ingested fixture reconciled to the registered Alpine source...
    assert prov["fields"]["source_id"] == "alpinewy_gov"
    # ...and carries every required provenance field.
    assert prov["fields"]["sha256"]
    assert prov["fields"]["fetch_time"]
    assert prov["fields"]["archive_url"]


def test_invariant_default_not_publishable(tmp_path: Path) -> None:
    result = smoke.run_smoke(sandbox=tmp_path)
    nopub = _check(result, "default_not_publishable")
    assert nopub["passed"], nopub.get("offenders")
    assert nopub["rows"] >= 4  # the four Alpine seeds


def test_strict_mode_returns_ok(tmp_path: Path) -> None:
    # strict=True must NOT raise on a clean slice.
    result = smoke.run_smoke(sandbox=tmp_path, strict=True)
    assert result["ok"]


# --- FAILURE half: each invariant must fail loudly on regression -----------

def test_raw_invariant_fails_loudly_on_tamper(tmp_path: Path) -> None:
    """Corrupting the stored raw artifact must flip the raw invariant to FAIL."""
    result = smoke.run_smoke(sandbox=tmp_path)
    assert result["ok"]
    # tamper with the preserved raw bytes after ingest+hash...
    raw_file = tmp_path / smoke._FIXTURE_REL_PATH
    raw_file.write_bytes(b"corrupted bytes that do not match the recorded hash")
    # ...re-running the reproducibility verifier against the same DB must catch it.
    with db.open_db(Path(result["db_path"])) as conn:
        repro = smoke.rp.verify_reproducibility(conn, repo_root=tmp_path)
    assert repro["mismatch"], "tamper went undetected — smoke would pass green falsely"


def test_provenance_invariant_fails_loudly_when_unlinked(tmp_path: Path) -> None:
    """A record that does not resolve to a source_id must FAIL the provenance check."""
    result = smoke.run_smoke(sandbox=tmp_path)
    with db.open_db(Path(result["db_path"])) as conn:
        conn.execute(
            "UPDATE documents SET source_id = NULL WHERE id = ?", (result["doc_id"],)
        )
        conn.commit()
        prov = smoke._check_provenance(conn, result["doc_id"])
    assert not prov["passed"]
    assert "source_id" in prov["error"]


def test_not_publishable_invariant_fails_loudly_on_leak(tmp_path: Path) -> None:
    """A source flipped to publishable must FAIL the default-not-publishable check."""
    result = smoke.run_smoke(sandbox=tmp_path)
    with db.open_db(Path(result["db_path"])) as conn:
        conn.execute(
            "UPDATE sources SET publication_state = 'publishable' "
            "WHERE source_id = 'alpinewy_gov'"
        )
        conn.commit()
        nopub = smoke._check_default_not_publishable(conn)
    assert not nopub["passed"]
    assert nopub["offenders"]
    assert nopub["offenders"][0]["source_id"] == "alpinewy_gov"


def test_strict_mode_raises_on_regression(tmp_path: Path, monkeypatch) -> None:
    """strict=True must raise SmokeFailure when an invariant regresses."""
    # Force the not-publishable check to report a failure, proving strict raises.
    def _broken_check(conn):  # noqa: ANN001
        return {"name": "default_not_publishable", "passed": False,
                "error": "injected regression"}

    monkeypatch.setattr(smoke, "_check_default_not_publishable", _broken_check)
    with pytest.raises(smoke.SmokeFailure, match="default_not_publishable"):
        smoke.run_smoke(sandbox=tmp_path, strict=True)


# --- data boundary: smoke uses only sanitized fixtures, no real data -------

def test_smoke_never_touches_real_db_or_raw_store(tmp_path: Path) -> None:
    """The smoke must operate entirely inside its sandbox (no real DB / Raw-PDFs)."""
    result = smoke.run_smoke(sandbox=tmp_path)
    assert str(tmp_path) in result["db_path"]
    assert not (smoke.REPO_ROOT / "Database" / "slice1_smoke.db").exists()


def test_default_sandbox_is_removed() -> None:
    """With no sandbox arg, the TemporaryDirectory is cleaned up before return."""
    result = smoke.run_smoke()
    assert not Path(result["db_path"]).exists()
