"""Tests for SSOT fields + uiStatus publication allowlist (GOV-76, Issue D).

Covers the binding acceptance criteria:

- D-1: explicit 11-value registry -> 6-value record mapping with parity, the
  load-bearing ``changed_needs_review -> sourceChanged=True`` assertion, no
  silent fallthrough, and fail-closed on unknown input.
- D-2: web-safe field allowlist excludes raw paths/hashes, owner_agent, reviewer
  notes, and all reviewer-state fields; fail-closed (unknown fields dropped).
- Default publication state = not-publishable (migration 0005), asserted on a
  fresh DB.
- Enum validator fail-closed: unknown/unreviewed inputs never resolve to a
  publishable uiStatus.
- Migration 0005 idempotent (re-run safe).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import publication as pub  # noqa: E402


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


# --- enum-of-record + drift guards (import-time) ---------------------------

def test_six_value_enum_is_authoritative() -> None:
    assert pub.ALLOWED_VERIFICATION_STATUSES == {
        "source_recorded",
        "machine_extracted_unreviewed",
        "reviewed_source_linked",
        "human_verified",
        "disputed",
        "do_not_publish",
    }


def test_uistatus_mapping_inputs_match_enum() -> None:
    # 1.05-g structural drift guard, expressed as a test too (it also fires at
    # import time inside publication.py).
    assert set(pub._VERIFICATION_STATUS_ROLES) == pub.ALLOWED_VERIFICATION_STATUSES


# --- D-1: 11 -> 6 registry mapping + parity --------------------------------

def test_registry_vocabulary_has_eleven_values() -> None:
    assert len(pub.REGISTRY_VERIFICATION_STATUSES) == 11


def test_mapping_domain_equals_registry_vocabulary() -> None:
    # No silent fallthrough: every registry value has an explicit mapping and
    # the mapping invents no value outside the registry vocabulary.
    assert set(pub.VERIFICATION_STATUS_MAP) == pub.REGISTRY_VERIFICATION_STATUSES


def test_every_mapped_status_is_a_six_value_record_status() -> None:
    for registry_value, (record_status, _changed) in pub.VERIFICATION_STATUS_MAP.items():
        assert record_status in pub.ALLOWED_VERIFICATION_STATUSES, (
            f"{registry_value!r} maps to non-record status {record_status!r}"
        )


def test_changed_needs_review_preserves_source_changed() -> None:
    # The load-bearing D-1 parity case: the "source changed / needs review"
    # signal must survive reconciliation as sourceChanged=True.
    record_status, source_changed = pub.map_registry_verification("changed_needs_review")
    assert source_changed is True
    assert record_status == "source_recorded"


def test_only_changed_needs_review_sets_source_changed() -> None:
    # sourceChanged must not leak onto any other registry value.
    changed = {
        value
        for value, (_status, flag) in pub.VERIFICATION_STATUS_MAP.items()
        if flag
    }
    assert changed == {"changed_needs_review"}


def test_changed_needs_review_round_trips_to_source_changed_uistatus() -> None:
    # End-to-end: a changed source with otherwise-present evidence computes the
    # gated "source-changed" uiStatus (rule #4), never a reassuring state.
    record_status, source_changed = pub.map_registry_verification("changed_needs_review")
    ui = pub.compute_ui_status({
        "verificationStatus": record_status,
        "sourceChanged": source_changed,
        "sourcePresent": True,
        "rawPreserved": True,
    })
    assert ui == "source-changed"


def test_none_registry_status_is_pre_review() -> None:
    assert pub.map_registry_verification(None) == (None, False)


def test_unknown_registry_status_fails_closed() -> None:
    with pytest.raises(pub.UnknownRegistryStatus):
        pub.map_registry_verification("totally_made_up_status")


# --- enum validator fail-closed (uiStatus) ---------------------------------

def test_compute_ui_status_fails_closed_on_unknown() -> None:
    # An unknown verificationStatus with no evidence signals must not resolve to
    # a publishable state.
    ui = pub.compute_ui_status({
        "verificationStatus": "bogus",
        "sourcePresent": True,
    })
    assert ui == "pending-review"
    assert ui not in pub.PUBLICATION_ELIGIBLE_UI_STATUSES


def test_unreviewed_record_is_not_publication_eligible() -> None:
    record = {
        "verificationStatus": "machine_extracted_unreviewed",
        "sourcePresent": True,
    }
    assert pub.compute_ui_status(record) == "unverified"
    assert pub.is_publication_eligible(record) is False


def test_do_not_publish_is_never_eligible() -> None:
    record = {"verificationStatus": "do_not_publish", "sourcePresent": True}
    assert pub.is_publication_eligible(record) is False


def test_reviewed_source_backed_is_eligible() -> None:
    # The one path that DOES reach the allowlist: an explicitly reviewed record
    # with a present source. (publication_state is still a separate gate.)
    record = {"verificationStatus": "human_verified", "sourcePresent": True}
    assert pub.compute_ui_status(record) == "source-backed"
    assert pub.is_publication_eligible(record) is True


# --- D-2: web-safe field allowlist (fail-closed) ---------------------------

_FORBIDDEN = ["raw_local_path", "raw_sha256", "owner_agent", "notes", "review_state"]


def test_web_safe_excludes_forbidden_fields() -> None:
    record = {
        "source_id": "alpinewy_gov",
        "name": "Town of Alpine",
        "url": "https://example.gov",
        "ui_status": "source-backed",
        # forbidden:
        "raw_local_path": "/Users/IA/Raw-PDFs/x.pdf",
        "raw_sha256": "deadbeef",
        "owner_agent": "BackendCrawlerEngineer",
        "notes": "reviewer note: looks off",
        "review_state": "in_review",
    }
    safe = pub.to_web_safe(record)
    for field in _FORBIDDEN:
        assert field not in safe, f"{field} leaked across the web boundary"
    # safe fields survive
    assert safe["source_id"] == "alpinewy_gov"
    assert safe["ui_status"] == "source-backed"


def test_web_safe_is_fail_closed_for_unknown_fields() -> None:
    # A new/unknown column is dropped by default — publishable only if
    # explicitly allowlisted.
    safe = pub.to_web_safe({"source_id": "x", "some_future_secret_column": "leak"})
    assert "some_future_secret_column" not in safe
    assert safe == {"source_id": "x"}


def test_named_unsafe_set_disjoint_from_allowlist() -> None:
    assert not (pub.WEB_SAFE_FIELD_ALLOWLIST & pub.WEB_UNSAFE_FIELDS)
    for field in _FORBIDDEN:
        assert field not in pub.WEB_SAFE_FIELD_ALLOWLIST


# --- migration 0005: default-not-publishable + idempotency -----------------

def test_migration_adds_publication_columns(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    with db.open_db(fresh_db) as conn:
        cols = _columns(conn, "sources")
    for required in ("produced_by", "review_state", "publication_state",
                     "source_changed", "ui_status"):
        assert required in cols, f"sources.{required} missing"


def test_default_publication_state_is_not_publishable(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    with db.open_db(fresh_db) as conn:
        conn.execute(
            "INSERT INTO sources (source_id, name, scope) VALUES (?, ?, 'alpine')",
            ("seed1", "Seed Source"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT produced_by, review_state, publication_state, source_changed, ui_status "
            "FROM sources WHERE source_id = 'seed1'"
        ).fetchone()
    assert row["publication_state"] == pub.DEFAULT_PUBLICATION_STATE == "not_publishable"
    assert row["produced_by"] == "automation"
    assert row["review_state"] == "unreviewed"
    assert row["source_changed"] == 0
    assert row["ui_status"] is None  # NULL = not computed = fail-closed


def test_produced_by_check_rejects_bad_value(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    with db.open_db(fresh_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sources (source_id, name, scope, produced_by) "
                "VALUES ('bad', 'X', 'alpine', 'martian')"
            )


def test_publication_state_check_rejects_bad_value(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    with db.open_db(fresh_db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sources (source_id, name, scope, publication_state) "
                "VALUES ('bad', 'X', 'alpine', 'definitely_publish_it')"
            )


def test_migration_0005_idempotent(fresh_db: Path) -> None:
    db.apply_migrations(fresh_db)
    db.apply_migrations(fresh_db)  # second run must not raise (duplicate column)
    with db.open_db(fresh_db) as conn:
        cols = _columns(conn, "sources")
    assert "publication_state" in cols


# --- GOV-1687 (C1b, ingest-provenance): a web-safe allowlist may not be an `assert`

import ast as _ast  # noqa: E402  (local to this guard; the file's own imports are above)

#: Names that mark a check as enforcing a *web-safe / publication* allowlist.
_ALLOWLIST_MARKERS = ("WEB_SAFE", "ALLOWLIST")

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

#: Modules that carry this defect but CANNOT be fixed without an unfreeze decision.
#:
#: `publication.py` is **byte-frozen against `origin/main`** — not by
#: `test_deploy_frozen_surface.py` (which freezes four *other* surfaces and is what
#: `CLAUDE.md` documents), but by `git diff origin/main == ""` assertions in SEVEN
#: separate test files enforcing "extend the SSOT, do not fork it". Its line 403
#: guard has exactly the `-O` defect this test exists to catch, and fixing it takes
#: an explicit unfreeze card. Named here rather than omitted: an allowlist you can
#: read is a known gap; a narrowed scan is an invisible one. See GOV-1687 / #229.
_FROZEN_BLOCKED = frozenset({"publication.py"})


def _allowlist_asserts(path: Path) -> list[tuple[int, str]]:
    """`assert` statements whose test mentions a web-safe/allowlist name."""
    try:
        tree = _ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:                                    # pragma: no cover
        return []
    out = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Assert):
            continue
        names = {n.id for n in _ast.walk(node.test) if isinstance(n, _ast.Name)}
        names |= {n.attr for n in _ast.walk(node.test) if isinstance(n, _ast.Attribute)}
        if any(mk in n.upper() for n in names for mk in _ALLOWLIST_MARKERS):
            out.append((node.lineno, sorted(names)))
    return out


def test_no_web_safe_allowlist_is_enforced_by_a_bare_assert():
    """`python -O` DELETES `assert`. A stripped allowlist is a silent leak.

    Measured 2026-07-31: under `-O` a dict carrying a non-allowlisted key passes
    the assert and the process exits **0**. Two modules relied on this shape —
    `stage3_source_inventory.py` (whose comment claimed the module would "refuse
    to import") and `file_read_api.py`, *the sole Backend→Website crossing*,
    whose sibling half already raised `FieldLeak` while the links half did not.

    **Latent, not live**: no `-O` or `PYTHONOPTIMIZE` appears anywhere in CI or
    config, so nothing strips these today. The point is that a defense-in-depth
    check exists for a *future* edit, and `-O` is exactly the condition under
    which that future edit would go unguarded.

    Ratchet at **zero** — the cheapest moment to install it.
    """
    offenders = []
    for py in sorted(_SCRIPTS.rglob("*.py")):
        for lineno, names in _allowlist_asserts(py):
            rel = py.relative_to(_SCRIPTS.parent)
            if rel.name in _FROZEN_BLOCKED:
                continue
            offenders.append(f"{rel}:{lineno} asserts on {names}")
    assert not offenders, (
        "A web-safe/publication allowlist is enforced by a bare `assert`, which "
        "`python -O` removes entirely — the check silently becomes a no-op and a "
        "non-allowlisted field is projected. Raise an explicit exception "
        "instead (see `file_read_api._assert_file_keys` for the shape):\n  "
        + "\n  ".join(offenders))
