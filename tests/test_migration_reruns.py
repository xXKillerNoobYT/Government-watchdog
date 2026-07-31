"""GOV-1679 (C1, data-model): migrations must stay re-runnable.

`scripts/db.py:_apply_statement` special-cases `ADD COLUMN` — SQLite has no
`ADD COLUMN IF NOT EXISTS`, so it probes the column and skips. Its docstring
states the obligation that falls on everything else:

    Only special-cases ``ADD COLUMN`` (not re-runnable in SQLite); everything
    else is expected to use ``IF NOT EXISTS`` and is passed through.

That expectation was **documented in a docstring and enforced by nothing**.
Measured at the moment this guard was written: 75 of 75 `CREATE TABLE` and 99 of
99 `CREATE INDEX` already comply — **zero violations**. That is precisely why it
goes in now: a ratchet costs nothing while the count is zero, and costs a
cleanup project afterwards.

The contract is `Docs/data-model-contract.md` INV-2.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MIGRATIONS = sorted((Path(__file__).resolve().parents[1]
                     / "Database" / "migrations").glob("*.sql"))

#: `CREATE TABLE [IF NOT EXISTS] <name>` — group 1 is the guard, group 2 the name.
_CREATE_TABLE = re.compile(r"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I)
#: `CREATE [UNIQUE] INDEX [IF NOT EXISTS] <name>`.
_CREATE_INDEX = re.compile(r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I)


def _sql_without_comments(path: Path) -> str:
    """Drop `--` comment lines before matching.

    Not cosmetic. A first pass at this measurement counted a **comment** in
    `0021_control_plane.sql` that says "six new tables via CREATE TABLE IF NOT
    EXISTS" as a statement, and reported one more table than exists. Matching
    prose that describes the rule is the recurring trap on this repo; strip the
    prose and match SQL.
    """
    return "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                     if not l.strip().startswith("--"))


def _offenders(pattern: re.Pattern[str], sql: str) -> list[str]:
    return [m.group(2) for m in pattern.finditer(sql) if not m.group(1)]


@pytest.mark.parametrize("path", MIGRATIONS, ids=lambda p: p.name)
def test_every_create_is_if_not_exists(path):
    sql = _sql_without_comments(path)
    bad = _offenders(_CREATE_TABLE, sql) + _offenders(_CREATE_INDEX, sql)
    assert not bad, (
        f"{path.name} creates {bad} without IF NOT EXISTS. "
        "`db._apply_statement` only makes ADD COLUMN idempotent; everything else "
        "must be IF NOT EXISTS or a re-apply raises. See "
        "Docs/data-model-contract.md INV-2.")


def test_the_corpus_is_actually_being_scanned():
    """A parametrized test over an empty list passes silently.

    If the glob ever stops matching — directory renamed, test moved a level —
    every case above vanishes and the suite still goes green. That is the
    "property holds by absence" shape this repo has hit four times, so the
    corpus size is asserted rather than assumed.
    """
    assert len(MIGRATIONS) >= 31, (
        f"expected at least the 31 migrations present at GOV-1679; found "
        f"{len(MIGRATIONS)} — has the directory moved?")
    total = sum(len(_CREATE_TABLE.findall(_sql_without_comments(p))) for p in MIGRATIONS)
    assert total >= 75, f"expected >=75 CREATE TABLE statements, found {total}"


def test_the_whole_corpus_actually_re_applies_against_a_populated_schema(tmp_path):
    """INV-2 end-to-end: every statement re-runs against a schema that already has it.

    RED-PROOF NOTE — this replaces a source-string check that SURVIVED its
    mutation. The first version asserted `"_ADD_COLUMN_RE" in db_src`; renaming
    the *assignment* left the name at its call site, so the substring was still
    found and the test passed with the idempotency probe gone. **A name is not a
    behaviour**, and this repo's recurring trap is matching vocabulary instead of
    the thing itself.

    Simply calling `apply_migrations` twice proves nothing either: the second
    call reads `schema_migrations` and skips every version. Clearing that ledger
    is what forces all 31 files to execute against an already-populated
    database — which is exactly the situation INV-2 exists for.
    """
    import db as db_mod

    db_path = tmp_path / "rerun.db"
    db_mod.apply_migrations(db_path)

    conn = db_mod.open_db(db_path)
    try:
        applied = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert applied == len(MIGRATIONS), (
            f"first apply recorded {applied} versions for {len(MIGRATIONS)} files")
        tables_before = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        conn.execute("DELETE FROM schema_migrations")   # force a full re-apply
        conn.commit()
    finally:
        conn.close()

    db_mod.apply_migrations(db_path)   # must not raise: every statement re-runs

    conn = db_mod.open_db(db_path)
    try:
        tables_after = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    finally:
        conn.close()
    assert tables_after == tables_before, (
        "a re-apply changed the table count; a migration is not idempotent")


def test_foreign_keys_are_enabled_on_both_connection_paths():
    """INV-3. SQLite defaults `foreign_keys` OFF *per connection*.

    A connection opened without it silently stops enforcing every REFERENCES
    clause in the schema — no error, just unenforced integrity.
    """
    db_src = (Path(__file__).resolve().parents[1] / "scripts" / "db.py").read_text(
        encoding="utf-8")
    assert db_src.count("PRAGMA foreign_keys = ON") >= 2, (
        "expected PRAGMA foreign_keys = ON in BOTH apply_migrations and open_db")


# --- GOV-1680 (C1b): the three invariants C1 wrote but nothing pinned ----------
#
# C1b mapped `Docs/data-model-contract.md`'s six invariants to the suite and
# found three with nothing behind them — INV-4, INV-5, INV-6. Notably the three
# that WERE pinned are the ones written a day earlier alongside the contract;
# the inherited ones had no guard at all. Same shape as access-gate's C1b, where
# 4 of 8 invariants turned out to be assertions nobody could fail.

#: A rebuild migration is one that DROPs a table it is replacing.
_REBUILDS = [p for p in MIGRATIONS if "DROP TABLE" in p.read_text(encoding="utf-8")]


def test_rebuild_migrations_pair_legacy_alter_table_on_and_off():
    """INV-4. The pragma is load-bearing and silent when omitted.

    SQLite cannot ALTER a CHECK, a foreign key, or a column type, so changing one
    means: create `<t>_new`, copy, `DROP TABLE <t>`, `RENAME <t>_new TO <t>`.

    With `legacy_alter_table` OFF (the default), the RENAME "helpfully" rewrites
    `REFERENCES` clauses in **other** tables to follow the rename — so tables
    that deliberately point at `statements` get silently repointed at the scratch
    table. Nothing errors. `0009`'s own comment records this, and until now
    nothing enforced it.
    """
    assert _REBUILDS, "no rebuild migrations found — has the corpus moved?"
    bad = []
    for path in _REBUILDS:
        sql = path.read_text(encoding="utf-8")
        if sql.count("PRAGMA legacy_alter_table = ON") < 1 or \
           sql.count("PRAGMA legacy_alter_table = OFF") < 1:
            bad.append(path.name)
    assert not bad, (
        f"{bad} DROP a table without pairing PRAGMA legacy_alter_table ON/OFF. "
        "Without it the RENAME rewrites REFERENCES clauses in OTHER tables. "
        "See Docs/data-model-contract.md INV-4.")


def test_a_failed_run_is_partially_applied_and_recoverable(tmp_path, monkeypatch):
    """INV-5, as MEASURED — this test disproved the contract's original claim.

    The contract first said a run was "all-or-nothing", written from code
    structure (`with sqlite3.connect(...)`, one commit after the loop) and never
    tested. It is false: `sqlite3` opens its implicit transaction before **DML
    only**, so `CREATE TABLE` runs in autocommit. The first migration's DDL lands
    durably; the first `INSERT INTO schema_migrations` then opens the transaction,
    and everything after it rolls back.

    What this pins is the property that actually matters: a failed run is
    **recoverable**, because the ledger is empty and INV-2 makes re-running the
    already-applied migration a no-op. Moving the commit inside the loop would
    break that — every migration would become independently durable and a genuine
    half-applied schema (with a ledger agreeing) becomes possible.
    """
    import sqlite3

    import db as db_mod

    staging = tmp_path / "migrations"
    staging.mkdir()
    for src in MIGRATIONS[:3]:
        (staging / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    (staging / "9999_deliberately_broken.sql").write_text(
        "CREATE TABLE IF NOT EXISTS ok_before_the_error (id TEXT);\n"
        "THIS IS NOT SQL;\n", encoding="utf-8")
    monkeypatch.setattr(db_mod, "MIGRATIONS_DIR", staging)

    db_path = tmp_path / "partial.db"
    with pytest.raises(sqlite3.OperationalError):
        db_mod.apply_migrations(db_path)

    assert db_path.exists(), "no database file — the test proved nothing"
    conn = sqlite3.connect(db_path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        ledger = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()

    # The autocommitted half: 0001 ran before any transaction existed.
    assert "documents" in names, (
        "0001's DDL did not survive — if apply_migrations now wraps DDL in a "
        "transaction the contract's INV-5 needs updating again, in the other "
        "direction")
    # The transactional half: everything after the first ledger INSERT rolled back.
    assert "sources" not in names, "0003's table survived; the rollback boundary moved"
    assert "ok_before_the_error" not in names, "the broken migration's table survived"
    # The recovery property — this is the one worth protecting.
    assert ledger == 0, (
        f"schema_migrations has {ledger} rows after a failed run. An empty "
        "ledger plus INV-2 is what makes the retry safe; a populated one would "
        "make the retry SKIP migrations whose DDL was rolled back")


#: Tables the byte-frozen serving surfaces read. Extracted once, listed here on
#: purpose: two hand-maintained copies of one fact fail loudly, a derived list
#: that silently returns nothing does not.
FROZEN_SURFACE_TABLES = (
    "agenda_items", "agenda_threads", "completeness_gaps", "concept_edges",
    "evidence_links", "meetings", "speaker_attributions", "statements",
    "topics", "transcript_segments", "transcripts",
)


def test_every_table_a_frozen_surface_reads_still_exists(tmp_path):
    """INV-6. The frozen-surface guard freezes FILE BYTES, not the schema.

    `test_frozen_surfaces_byte0_vs_origin_main` proves `read_api.py` and friends
    have not changed. It cannot notice that a migration renamed a table they
    SELECT from — the file is byte-identical and now broken. **And the surface
    cannot be edited to adapt, because it is frozen**, so this class of breakage
    has no cheap fix once shipped.
    """
    import db as db_mod

    db_path = tmp_path / "frozen.db"
    db_mod.apply_migrations(db_path)
    conn = db_mod.open_db(db_path)
    try:
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()
    missing = sorted(t for t in FROZEN_SURFACE_TABLES if t not in present)
    assert not missing, (
        f"tables {missing} are read by a BYTE-FROZEN serving surface but no "
        "longer exist after migrations. The surface cannot be edited to adapt "
        "(tests/test_deploy_frozen_surface.py). See data-model-contract INV-6.")
