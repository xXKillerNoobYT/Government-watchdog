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
