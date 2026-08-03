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


def test_foreign_keys_are_actually_in_effect_not_merely_present(tmp_path):
    """INV-3, upgraded from a source-text count to a behavioural check.

    The previous version asserted `db.py` *contains* the string
    "PRAGMA foreign_keys = ON" at least twice. **Presence is not effect**, and
    SQLite makes that gap dangerous: the pragma is documented as a NO-OP inside
    a transaction, and it fails **silently** — no error, no warning, just a
    connection that stops enforcing every REFERENCES clause in the schema.

    Measured: `conn.execute("PRAGMA foreign_keys = ON")` after a DML statement
    leaves the value at **0**. So a refactor that moved the pragma below the
    first write in `open_db` would keep the old text-matching guard green while
    turning off referential integrity for every caller.

    This asserts what actually matters — the value on a live connection, and a
    real violation being rejected.
    """
    import sqlite3

    import db as db_mod

    db_path = tmp_path / "fk.db"
    db_mod.apply_migrations(db_path)

    conn = db_mod.open_db(db_path)
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1, (
            "open_db returned a connection with foreign_keys OFF — every "
            "REFERENCES clause in the schema is unenforced on it")
        # The end-to-end proof: a row pointing at a user that does not exist.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO access_grants (grant_id, user_id, tier, granted_utc)"
                " VALUES ('g1', 'NO-SUCH-USER', 'none', '2026-01-01')")
            conn.commit()
    finally:
        conn.rollback()
        conn.close()


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


# --- GOV-1681 (C4): `_statements` is used by every apply and had no test ------
#
# C4 audited `scripts/db.py`: two public callables, both exercised by ~90 test
# files, so the public surface is genuinely healthy. The gap was one level down —
# `_statements` splits every migration file and had **no test of its own
# behaviour**; existing references use it as a helper for 0025 assertions.
#
# Its docstring rests on an assumption: "Adequate for the project's simple,
# trigger-free migration files." Triggers really are 0. But the assumption is
# broader than the docstring says, and NOTHING enforced it.

def test_statements_strips_full_line_comments_and_splits_on_semicolons():
    """The behaviour the migration runner depends on, pinned directly."""
    import db as db_mod

    out = db_mod._statements(
        "-- a comment; with a semicolon\n"
        "    -- an indented one too\n"
        "CREATE TABLE t (id TEXT);\n\n;\n"
        "CREATE TABLE u (id TEXT);\n")
    assert out == ["CREATE TABLE t (id TEXT)", "CREATE TABLE u (id TEXT)"], out


def test_statements_is_fragile_in_two_documented_ways(request):
    """Documents the REAL behaviour, so the corpus guard below has a reason.

    Measured, not assumed — both produce errors that surface on the *following*
    statement, far from the line that caused them:

      inline comment with ';'  -> ['CREATE TABLE t (id TEXT)', '-- note',
                                   'caveat\\nCREATE TABLE u (id TEXT)']
                                  the NEXT statement is corrupted by a prefix
      ';' inside a literal     -> ["CREATE TABLE t (c TEXT DEFAULT 'a", "b')"]
                                  split mid-literal, both halves invalid

    This test asserts the fragility rather than pretending it away. If someone
    hardens the splitter, this test failing is the SIGNAL to delete it and the
    corpus guard with it — not a regression.
    """
    import db as db_mod

    inline = db_mod._statements(
        "CREATE TABLE t (id TEXT);  -- note; caveat\nCREATE TABLE u (id TEXT);")
    assert len(inline) == 3 and inline[1] == "-- note", (
        f"splitter behaviour changed: {inline}. If it was hardened on purpose, "
        "delete this test AND test_no_migration_defeats_the_naive_splitter.")

    literal = db_mod._statements("CREATE TABLE t (c TEXT DEFAULT 'a;b');")
    assert len(literal) == 2, f"splitter behaviour changed: {literal}"


def test_no_migration_defeats_the_naive_splitter():
    """The corpus assumption `_statements` rests on, made enforceable.

    Measured when written: **zero** violations across all 31 migrations — every
    `;`-bearing quote in the tree sits inside a full-line `--` comment, which the
    splitter strips. Zero is when a ratchet is free.

    Chose to guard the corpus rather than harden the splitter deliberately: this
    function runs on every migration apply, so it is the highest-blast-radius
    code in the repo, and a simple obviously-correct splitter plus an enforced
    precondition beats a clever one. See Docs/data-model-contract.md INV-7.
    """
    import re

    offenders = []
    for path in MIGRATIONS:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("--"):
                continue                      # stripped before splitting
            if "--" in line and ";" in line.split("--", 1)[1]:
                offenders.append(f"{path.name}:{lineno} inline comment contains ';'")
            if re.search(r"'[^'\n]*;[^'\n]*'", line):
                offenders.append(f"{path.name}:{lineno} string literal contains ';'")
    assert not offenders, (
        "these defeat db._statements' naive split, and the error will surface on "
        f"the FOLLOWING statement rather than this line: {offenders}")


def test_column_exists_resolves_only_a_real_table_name(tmp_path):
    """GOV-1683: a malformed identifier must answer False, not probe another table.

    `_column_exists` drives ADD COLUMN idempotency, so a wrong answer is a
    silently-missing column rather than an error. Measured on the previous
    interpolated form, `PRAGMA table_info({name})`:

        't) --'  -> resolved to table `t` and returned True   (WRONG table)
        't;x'    -> OperationalError                          (crash)

    Both now return False, which is the truthful answer. This is a correctness
    guard; it is not patching a live vulnerability — the name comes from
    repo-authored migration text and Python's driver refuses stacked statements.
    """
    import db as db_mod

    db_path = tmp_path / "colcheck.db"
    db_mod.apply_migrations(db_path)
    conn = db_mod.open_db(db_path)
    try:
        # Truthful positives and negatives on real names.
        assert db_mod._column_exists(conn, "users", "email") is True
        assert db_mod._column_exists(conn, "users", "no_such_column") is False
        assert db_mod._column_exists(conn, "no_such_table", "email") is False
        # The two shapes the interpolated form got wrong.
        assert db_mod._column_exists(conn, "users) --", "email") is False, (
            "a malformed identifier resolved to a real table — the name is "
            "being interpolated into SQL again rather than bound")
        assert db_mod._column_exists(conn, "users;x", "email") is False, (
            "a malformed identifier raised or matched instead of answering False")
    finally:
        conn.close()


# --- GOV-1684 (C9, data-model): unindexed foreign keys are a LATENT cost -------
#
# SQLite auto-indexes a UNIQUE constraint (`sqlite_autoindex_*`) but gives a
# REFERENCES clause **nothing**. An unindexed FK child column makes every parent
# DELETE (and every ON DELETE CASCADE) full-scan the child table.
#
# Measured 2026-07-31: **22 of 70** FK child columns are unindexed. That sounds
# alarming and is currently **free**, because the data model is append-only in
# practice — shipped code under `scripts/` contains exactly **one** `DELETE FROM`,
# against `supplied_file_links`, which is not a parent of any unindexed FK.
#
# So the answer is NOT "add 22 indexes". Each index costs write amplification and
# another schema object, to solve a problem no code has. What is worth pinning is
# the **precondition that makes the absence safe** — the same call INV-7 makes
# about the statement splitter: guard the corpus, do not harden the code.

#: Directories whose `.py` files are shipped behaviour (tests may delete freely).
_SHIPPED = (Path(__file__).resolve().parents[1] / "scripts",)
_DELETE_FROM = re.compile(r"DELETE\s+FROM\s+[\"']?(\w+)", re.I)


def _leftmost_indexed_columns(conn, table: str) -> set[str]:
    """Columns that are the FIRST column of some index on ``table``.

    Leftmost is the part that matters: an index on ``(a, b)`` serves a lookup on
    ``a`` but not one on ``b``. ``PRAGMA index_list`` includes the autoindexes
    SQLite creates for UNIQUE, which is exactly why this asks the schema rather
    than grepping for `CREATE INDEX` — `CLAUDE.md` records that under-reporting
    as a live trap.
    """
    out: set[str] = set()
    for _, name, *_ in conn.execute(f"PRAGMA index_list('{table}')"):
        info = conn.execute(f"PRAGMA index_info('{name}')").fetchall()
        if info:
            out.add(info[0][2])
    return out


def _unindexed_foreign_keys(conn) -> list[tuple[str, str, str, str]]:
    """(child_table, child_column, parent_table, on_delete) for unindexed FKs."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    found = []
    for t in tables:
        indexed = _leftmost_indexed_columns(conn, t)
        for fk in conn.execute(f"PRAGMA foreign_key_list('{t}')"):
            # (id, seq, table, from, to, on_update, on_delete, match) — the two
            # action columns are ADJACENT, and taking the wrong one is silent.
            _, _, parent, child_col, _, _on_update, on_del, *_ = fk
            if child_col not in indexed:
                found.append((t, child_col, parent, on_del))
    return found


def test_no_shipped_delete_targets_a_parent_with_unindexed_children(tmp_path):
    """The precondition that makes 22 unindexed FKs safe: nothing deletes.

    This is the guard that has teeth, because it fires on the change that turns
    a latent cost into a real one — someone adding the first `DELETE FROM` against
    a table other rows point at. At that moment the delete is O(child rows), the
    author has no reason to suspect it, and **SQLite reports nothing**: the delete
    simply gets slower as the child table grows.

    It deliberately does NOT assert "zero unindexed FKs". That assertion would be
    red today for 22 columns nobody needs indexed, and a guard that is red on
    arrival gets suppressed rather than obeyed.
    """
    import db as db_mod

    db_path = tmp_path / "fk-perf.db"
    db_mod.apply_migrations(db_path)
    conn = db_mod.open_db(db_path)
    try:
        risky = {}
        for child_t, child_c, parent, _ in _unindexed_foreign_keys(conn):
            risky.setdefault(parent, []).append(f"{child_t}.{child_c}")

        offenders = []
        for root in _SHIPPED:
            for py in sorted(root.rglob("*.py")):
                for target in set(_DELETE_FROM.findall(py.read_text(encoding="utf-8"))):
                    if target in risky:
                        rel = py.relative_to(root.parents[0])
                        offenders.append(
                            f"{rel} deletes from `{target}`, whose children "
                            f"{sorted(risky[target])} have NO index on the "
                            f"referencing column — that delete is O(child rows)")
        assert not offenders, (
            "A shipped DELETE now targets a table with unindexed foreign-key "
            "children. Add an index on each referencing column in a new "
            "migration (next free slot), or route the removal through a "
            "soft-delete flag:\n  " + "\n  ".join(offenders))
    finally:
        conn.close()


#: The one CASCADE edge whose child column is unindexed, with its blocker.
#:
#: Named rather than omitted: an allowlist you can read is a known gap, an
#: absent assertion is an unknown one. Deleting this entry is the definition of
#: done, and it cannot happen until the 0032 migration-slot collision clears
#: (#199 vs #132) — the same blocker as the `email_outbox` index (#217).
_KNOWN_UNINDEXED_CASCADES = {("meeting_documents", "document_id")}


def test_every_on_delete_cascade_child_column_is_indexed(tmp_path):
    """A CASCADE is a delete the SCHEMA designs to happen.

    "Nothing deletes today" is the argument that makes the other 21 unindexed
    FKs acceptable, and it is precisely the argument a CASCADE edge refuses:
    the schema itself declares that deleting the parent deletes these children.
    So the four CASCADE edges get the stricter rule.

    Three of the four are already indexed. The fourth,
    `meeting_documents.document_id`, is not — measured cost of deleting **one**
    `documents` row: **5.00 ms** against 100k rows, versus **0.33 ms** with the
    index, scaling linearly (0.65 ms at 10k). It is allowlisted above rather
    than hidden, because the fix needs a migration slot that is currently
    contested.
    """
    import db as db_mod

    db_path = tmp_path / "cascade.db"
    db_mod.apply_migrations(db_path)
    conn = db_mod.open_db(db_path)
    try:
        gaps = {(t, c) for t, c, _, on_del in _unindexed_foreign_keys(conn)
                if on_del.upper() == "CASCADE"}

        new = gaps - _KNOWN_UNINDEXED_CASCADES
        assert not new, (
            "A new ON DELETE CASCADE has an unindexed referencing column: "
            f"{sorted(new)}. The schema says this delete WILL happen, so the "
            "'nothing deletes today' argument does not cover it — add "
            "`CREATE INDEX IF NOT EXISTS ... ON <child>(<column>)` in the same "
            "migration that adds the CASCADE.")

        stale = _KNOWN_UNINDEXED_CASCADES - gaps
        assert not stale, (
            f"{sorted(stale)} is allowlisted as an unindexed CASCADE but is now "
            "indexed. Delete the entry from _KNOWN_UNINDEXED_CASCADES — a stale "
            "allowlist silently widens what the guard permits.")
    finally:
        conn.close()
