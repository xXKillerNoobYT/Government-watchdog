"""GOV-1654: the owner's beta control surface — ``scripts/beta/admin.py`` + the
``valid_email`` fail-closed guard.

Both surfaces had **zero** tests before this file (AUTO GO C4 audit of the
access-gate area): ``grep -rl "beta.*admin" tests/`` returned nothing, and
``common.valid_email`` was referenced only from ``scripts/``, never from a test.

Why that mattered more than an ordinary coverage gap:

* ``admin.py`` is the ONLY lever that flips the fail-closed ``beta_gate_enabled``
  flag and the only one that admits or locks out an email. Swapping
  ``enabled=True``/``enabled=False`` between its ``enable`` and ``disable``
  branches inverts the kill switch, and nothing would have failed.
* ``valid_email`` is the FIRST gate on both public entry points. Deleting it is
  invisible to the obvious assertion — see :func:`test_request_magic_link_...`
  below, which documents why "no token was issued" is the wrong thing to check.

Every test here was mutation-proved: the guard it names was individually
weakened in the source and this file was confirmed to go red. The mutation that
each test catches is recorded in its docstring.
"""

from __future__ import annotations

import contextlib

import pytest

import db
from beta import admin, allowlist, common, http_api, service
from email_gateway import flags

OWNER_REF = "test-card-gov1654"


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "gov1654.db"
    db.apply_migrations(path)
    return path


@pytest.fixture()
def conn(db_path):
    c = db.open_db(db_path)
    yield c
    c.close()


@contextlib.contextmanager
def reopened(db_path):
    """A FRESH connection, because ``admin.main`` opens and closes its own.

    Asserting through the test's long-lived ``conn`` could read a snapshot taken
    before the CLI committed; opening after the call removes that ambiguity.
    """
    c = db.open_db(db_path)
    try:
        yield c
    finally:
        c.close()


# --- admin CLI: the fail-closed gate flag ------------------------------------

def test_enable_turns_the_gate_on(db_path):
    """Mutation caught: ``enable`` passing ``enabled=False`` (inverted switch)."""
    assert admin.main(["--db", str(db_path), "enable",
                       "--owner-decision-ref", OWNER_REF]) == 0
    with reopened(db_path) as c:
        assert flags.is_enabled(c, http_api.BETA_GATE_FLAG) is True


def test_disable_turns_the_gate_off(db_path):
    """Mutation caught: ``disable`` passing ``enabled=True`` (inverted switch).

    Runs ``enable`` first so the assertion cannot be satisfied by the
    fail-closed default — ``is_enabled`` returns False when no row exists, so
    disabling a never-enabled gate would pass against a no-op ``disable``.
    """
    admin.main(["--db", str(db_path), "enable",
                "--owner-decision-ref", OWNER_REF])
    with reopened(db_path) as c:
        assert flags.is_enabled(c, http_api.BETA_GATE_FLAG) is True

    assert admin.main(["--db", str(db_path), "disable",
                       "--owner-decision-ref", OWNER_REF]) == 0
    with reopened(db_path) as c:
        assert flags.is_enabled(c, http_api.BETA_GATE_FLAG) is False


def test_gate_flag_history_is_append_only_and_last_write_wins(db_path):
    """``feature_flags`` keeps both rows; the read resolves to the later one.

    Locks in that the CLI never rewrites history — the owner's decision trail
    survives a flip. Also pins that the resolution is deterministic even when
    both rows land in the same millisecond: ``flag_seq`` is a monotonic
    autoincrement, so the ``at_utc DESC, flag_seq DESC`` tie-break is stable
    (unlike this repo's ``token_hex`` id columns — see GOV-1652/#177).
    """
    admin.main(["--db", str(db_path), "enable",
                "--owner-decision-ref", "card-on"])
    admin.main(["--db", str(db_path), "disable",
                "--owner-decision-ref", "card-off"])
    with reopened(db_path) as c:
        rows = c.execute(
            "SELECT enabled, owner_decision_ref FROM feature_flags"
            " WHERE flag_name = ? ORDER BY flag_seq",
            (http_api.BETA_GATE_FLAG,)).fetchall()
        assert [(r["enabled"], r["owner_decision_ref"]) for r in rows] == [
            (1, "card-on"), (0, "card-off")]
        assert flags.is_enabled(c, http_api.BETA_GATE_FLAG) is False


@pytest.mark.parametrize("cmd", ["enable", "disable"])
def test_gate_flip_requires_an_owner_decision_ref(db_path, cmd):
    """Mutation caught: dropping ``required=True`` from ``--owner-decision-ref``.

    argparse exits 2 before any DB work, so the flag table stays empty — the
    owner gate is not merely recorded, it is a precondition.
    """
    with pytest.raises(SystemExit) as exc:
        admin.main(["--db", str(db_path), cmd])
    assert exc.value.code == 2
    with reopened(db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM feature_flags").fetchone()[0] == 0


# --- admin CLI: the allowlist ------------------------------------------------

def test_allow_admits_an_email(db_path):
    """Mutation caught: ``allow`` not calling ``allowlist.add``."""
    assert admin.main(["--db", str(db_path), "allow", "--email", "A@Example.com",
                       "--owner-decision-ref", OWNER_REF]) == 0
    with reopened(db_path) as c:
        # Normalized on the way in: the CLI stores exactly one shape.
        assert allowlist.is_allowed(c, "a@example.com") is True


def test_revoke_withdraws_admission(db_path, capsys):
    """Mutation caught: ``revoke`` not calling ``allowlist.revoke``.

    Admits first so the assertion distinguishes "revoked" from "never admitted".
    """
    admin.main(["--db", str(db_path), "allow", "--email", "a@example.com",
                "--owner-decision-ref", OWNER_REF])
    capsys.readouterr()

    assert admin.main(["--db", str(db_path), "revoke", "--email",
                       "a@example.com", "--owner-decision-ref", OWNER_REF]) == 0
    assert "revoked" in capsys.readouterr().out
    with reopened(db_path) as c:
        assert allowlist.is_allowed(c, "a@example.com") is False


def test_revoke_reports_when_there_is_no_active_row(db_path, capsys):
    """A revoke against an unknown email says so and still exits 0.

    Mutation caught: printing the success branch unconditionally. The owner
    needs to be able to tell "locked out" from "was never admitted"; a CLI that
    prints ``revoked`` either way silently confirms a lockout that never happened.
    """
    assert admin.main(["--db", str(db_path), "revoke", "--email",
                       "nobody@example.com", "--owner-decision-ref",
                       OWNER_REF]) == 0
    assert "no active row to revoke" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["allow", "--email", "a@example.com"],
    ["revoke", "--email", "a@example.com"],
])
def test_allowlist_change_requires_an_owner_decision_ref(db_path, argv):
    """Mutation caught: dropping ``required=True`` on the allowlist subcommands.

    Asserts the table is untouched, so this cannot pass merely because argparse
    printed something.
    """
    with pytest.raises(SystemExit) as exc:
        admin.main(["--db", str(db_path)] + argv)
    assert exc.value.code == 2
    with reopened(db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM beta_allowlist").fetchone()[0] == 0


def test_allow_refuses_a_malformed_email(db_path):
    """Mutation caught: removing ``allowlist.add``'s ``valid_email`` guard.

    The CLI does not pre-validate, so this pins that the service-layer guard is
    what protects the table from the owner's typo.
    """
    with pytest.raises(ValueError, match="invalid email"):
        admin.main(["--db", str(db_path), "allow", "--email", "not an email",
                    "--owner-decision-ref", OWNER_REF])
    with reopened(db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM beta_allowlist").fetchone()[0] == 0


def test_list_never_prints_a_raw_email(db_path, capsys):
    """The module docstring promises "no emails printed raw" — this locks it.

    Mutation caught: adding ``email`` to ``list``'s SELECT and print. The repo
    is PUBLIC and CLI output lands in terminal scrollback and CI logs, so the
    address staying out of stdout is a disclosure boundary, not cosmetics.
    Asserts on the local part too, since printing ``a@example.com`` split or
    partially would still be a leak.
    """
    admin.main(["--db", str(db_path), "allow", "--email",
                "leaky.person@example.com", "--owner-decision-ref", OWNER_REF])
    capsys.readouterr()

    assert admin.main(["--db", str(db_path), "list"]) == 0
    out = capsys.readouterr().out
    assert "1 allowlist row(s)" in out
    assert "status=active" in out
    assert "leaky.person@example.com" not in out
    assert "leaky.person" not in out


def test_list_does_not_mutate(db_path):
    """"Reads never mutate" (module docstring) — pinned against the audit log.

    Mutation caught: a ``list`` branch that recorded an audit row or touched
    ``beta_allowlist``. The audit log is append-only with no delete path, so an
    accidental write there is unrecoverable noise in a provenance trail.
    """
    admin.main(["--db", str(db_path), "allow", "--email", "a@example.com",
                "--owner-decision-ref", OWNER_REF])
    with reopened(db_path) as c:
        before = (
            c.execute("SELECT COUNT(*) FROM beta_audit_log").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM beta_allowlist").fetchone()[0],
        )

    admin.main(["--db", str(db_path), "list"])
    with reopened(db_path) as c:
        after = (
            c.execute("SELECT COUNT(*) FROM beta_audit_log").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM beta_allowlist").fetchone()[0],
        )
    assert after == before


# --- the valid_email fail-closed guard ---------------------------------------

def test_request_magic_link_rejects_a_malformed_email_before_recording_identity(
        conn):
    """Mutation caught: deleting ``request_magic_link``'s ``valid_email`` guard.

    **Why this asserts on the audit trail and not on the token table.**
    ``request_magic_link`` returns None on every path so the HTTP layer can
    answer a constant 200 and never leak allowlist membership. A malformed
    address is therefore stopped TWICE — once by this guard, and again by
    ``allowlist.is_allowed`` further down. So "no token was issued" passes with
    the guard deleted and proves nothing.

    What actually separates the two worlds is what reaches the append-only
    audit log:

    * guard present -> ``magic_link_rejected`` / ``invalid_email``, and NO
      identity recorded (``email_hash`` is NULL, because the reject call passes
      no email).
    * guard deleted -> ``magic_link_requested`` carrying ``sha256`` of the
      garbage input, and no reject row at all.
    """
    service.request_magic_link(conn, "not an email")

    rows = conn.execute(
        "SELECT event, email_hash, detail FROM beta_audit_log"
        " ORDER BY rowid").fetchall()
    assert [r["event"] for r in rows] == ["magic_link_rejected"]
    assert rows[0]["detail"] == "invalid_email"
    # The guard's real product: garbage never becomes an audit identity.
    assert rows[0]["email_hash"] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM beta_magic_tokens").fetchone()[0] == 0


def test_join_waitlist_rejects_a_malformed_email_and_writes_no_row(conn):
    """Mutation caught: deleting ``join_waitlist``'s ``valid_email`` guard.

    Here the row table IS the discriminator, unlike the magic-link path above:
    the waitlist is public (no allowlist check downstream) and
    ``waitlist.add`` does no validation of its own, so with the guard removed
    the malformed address lands in ``beta_waitlist`` verbatim.
    """
    service.join_waitlist(conn, "not an email", area_interest="alpine")

    assert conn.execute(
        "SELECT COUNT(*) FROM beta_waitlist").fetchone()[0] == 0
    # Silent by design: the neutral 200 path records nothing for a bad address.
    assert conn.execute(
        "SELECT COUNT(*) FROM beta_audit_log").fetchone()[0] == 0


def test_join_waitlist_accepts_a_wellformed_email(conn):
    """The over-reach lock for the test above.

    Without this, mutating the guard to reject EVERYTHING would still leave the
    previous test green — a guard that denies all input is not fail-closed, it
    is broken. Pins that the happy path still writes its row.
    """
    service.join_waitlist(conn, "Someone@Example.com", area_interest="alpine")

    row = conn.execute(
        "SELECT email, area_interest FROM beta_waitlist").fetchone()
    assert row["email"] == "someone@example.com"
    assert row["area_interest"] == "alpine"


@pytest.mark.parametrize("email,expected", [
    ("a@example.com", True),
    ("someone+tag@sub.example.co.uk", True),
    ("", False),                    # empty
    ("no-at-sign", False),          # missing @
    ("has space@example.com", False),   # space before the @
    ("a@exa mple.com", False),          # space after the @
    (" a@example.com", False),      # untrimmed: normalize_email runs first in
                                    # callers, but the predicate itself denies
])
def test_valid_email_predicate(email, expected):
    """The rules ``valid_email`` actually enforces, pinned individually.

    Mutation caught: dropping either the ``"@" in`` or the ``" " not in``
    clause. Deliberately minimal — this is a denial floor, not an RFC 5322
    parser, and the docstring says so.
    """
    assert common.valid_email(email) is expected


def test_valid_email_is_total_over_none():
    """``bool(email)`` exists for ``None``, not for the empty string.

    Found by mutation: deleting ``bool(email)`` from the predicate leaves EVERY
    string case above unchanged, because ``"@" in ""`` is already False. The
    clause is redundant for ``str`` and load-bearing only here — without it
    ``"@" in None`` raises ``TypeError`` instead of denying.

    Callers normalize first (``normalize_email`` maps None -> ``""``), so None
    should never arrive in practice; this pins the predicate's totality anyway,
    because a guard that raises instead of denying is not fail-closed — an
    unhandled TypeError on the public intake path is a 500, not a refusal.
    """
    assert common.valid_email(None) is False
