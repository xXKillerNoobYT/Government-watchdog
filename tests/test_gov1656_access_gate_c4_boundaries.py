"""GOV-1656 [C4]: the last four uncovered ``access-gate`` surfaces.

Closes the coverage audit AUTO GO opened on ``scripts/beta/`` — the surfaces the
suite exercised only *incidentally*, so their guards were deletable with a green
suite. Each test names the mutation it exists to catch.

Four surfaces, in the audit's priority order:

  * ``ratelimit.count_in_window`` — the window's lower bound. Every prior test
    uses wall-clock times that never land on the cutoff, so ``>= cutoff`` could
    become ``> cutoff`` untouched. That is the fail-OPEN direction on the rate
    limit guarding credential issuance.
  * ``waitlist.add`` — the returned ``request_id`` and the ``ip_hint`` /
    ``area_interest`` columns were asserted nowhere. Both are TEXT and nullable,
    so transposing them in the INSERT is silent.
  * ``mailer.send_waitlist_confirmation`` — ran only through the null adapter,
    so the template id was never observed. A wrong id sends the wrong email.
  * ``intake_api.load_known_bad`` / ``build_store`` — the denylist's env parsing
    and the store's two fail-closed branches. ``load_known_bad``'s ``.lower()``
    and comma handling are the only things that make operator input match a
    computed digest; without them the denylist denies nothing, silently.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

import pytest

import db
import raw_object_store
from beta import common, intake_api, mailer, ratelimit, waitlist
from email_gateway import adapters, flags, templates


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "gov1656.db"
    db.apply_migrations(path)
    c = db.open_db(path)
    yield c
    c.close()


@pytest.fixture()
def capture(conn):
    """Register a real adapter that records sends, so bodies are observable.

    Without this the mailer resolves to the null adapter and nothing about the
    rendered email can be asserted — which is exactly why the waitlist template
    id had no coverage.
    """
    sink: list[dict] = []

    class _Capturing:
        name = "capture"

        def send(self, *, to_email, subject, body_text, body_html):
            sink.append({"to": to_email, "subject": subject, "body": body_text})
            return "capture-ref"

    adapters.register_adapter("capture", _Capturing)
    flags.set_flag(conn, flags.EMAIL_ADAPTER_FLAG, enabled=True,
                   owner_decision_ref="test-card-gov1656")
    try:
        yield sink
    finally:
        adapters.unregister_adapter("capture")


def _insert_token(conn, email, created_utc):
    """One ``beta_magic_tokens`` row with a caller-chosen ``created_utc``."""
    conn.execute(
        "INSERT INTO beta_magic_tokens (token_id, email, token_hash,"
        " created_utc, expires_utc) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), email, common.token_hash(str(uuid.uuid4())),
         created_utc, created_utc))
    conn.commit()


# --- 1. ratelimit: the window's lower bound ----------------------------------

def test_count_in_window_includes_a_row_exactly_at_the_cutoff(conn):
    """Mutation: ``{column} >= ?`` -> ``> ?``.

    A row timestamped exactly ``window_seconds`` ago is INSIDE the trailing
    window. Pinned explicitly because a caller pacing requests onto the boundary
    is precisely how an off-by-one here would be exploited, and because wall
    clocks never produce this timestamp by accident.
    """
    now = common.utcnow()
    _insert_token(conn, "edge@example.com",
                  common.iso(now - timedelta(seconds=3600)))
    assert ratelimit.count_in_window(
        conn, "beta_magic_tokens", "edge@example.com",
        now=now, window_seconds=3600) == 1


def test_count_in_window_excludes_a_row_one_millisecond_outside(conn):
    """Mutation: drop the ``{column} >= ?`` predicate (or widen the window).

    The complement of the test above: one millisecond older than the cutoff is
    OUTSIDE. Together the two pin the boundary to a single instant, so neither
    direction can drift without a failure.
    """
    now = common.utcnow()
    _insert_token(conn, "stale@example.com",
                  common.iso(now - timedelta(seconds=3600, milliseconds=1)))
    assert ratelimit.count_in_window(
        conn, "beta_magic_tokens", "stale@example.com",
        now=now, window_seconds=3600) == 0


def test_count_in_window_counts_by_normalized_email(conn):
    """Mutation: drop ``common.normalize_email`` from the count query.

    Defense-in-depth, stated honestly: ``service.request_magic_link`` normalizes
    before it calls here, so this is not a live bypass today. It is the contract
    that makes ``count_in_window`` safe for a caller that does NOT pre-normalize
    — and rows are always stored normalized, so a raw-email query would count 0
    and the limit would vanish.
    """
    _insert_token(conn, "case@example.com", common.iso(common.utcnow()))
    assert ratelimit.count_in_window(
        conn, "beta_magic_tokens", "  CASE@Example.COM  ") == 1


def test_over_limit_is_false_one_below_the_limit(conn):
    """Mutation: ``count >= limit`` -> ``count >= limit - 1`` (or ``True``).

    The suite already pins that AT the limit is over; nothing pinned that BELOW
    it is not. Without this, a mutation that rejects one request early passes.
    """
    now = common.utcnow()
    for _ in range(4):
        _insert_token(conn, "under@example.com", common.iso(now))
    assert ratelimit.over_limit(conn, "beta_magic_tokens", "under@example.com",
                                limit=5, now=now) is False
    _insert_token(conn, "under@example.com", common.iso(now))
    assert ratelimit.over_limit(conn, "beta_magic_tokens", "under@example.com",
                                limit=5, now=now) is True


# --- 2. waitlist.add: the return value and the columns -----------------------

def test_waitlist_add_returns_the_id_it_actually_stored(conn):
    """Mutation: return a freshly minted id instead of the persisted one.

    The caller's only handle on the row is this return value, so an id that does
    not match the row is worse than no id at all.
    """
    request_id = waitlist.add(conn, "wl@example.com")
    stored = conn.execute("SELECT request_id FROM beta_waitlist").fetchone()[0]
    assert request_id == stored
    assert uuid.UUID(request_id).version == 4


def test_waitlist_add_puts_area_interest_and_ip_hint_in_their_own_columns(conn):
    """Mutation: transpose ``area_interest`` and ``ip_hint`` in the INSERT.

    Both columns are TEXT and nullable, so a transposition raises nothing and
    corrupts abuse forensics silently. Distinct sentinels are the only way to
    see it.
    """
    waitlist.add(conn, "cols@example.com",
                 area_interest="AREA-SENTINEL", ip_hint="IPHINT-SENTINEL")
    row = conn.execute(
        "SELECT area_interest, ip_hint FROM beta_waitlist").fetchone()
    assert row["area_interest"] == "AREA-SENTINEL"
    assert row["ip_hint"] == "IPHINT-SENTINEL"


def test_waitlist_add_stores_the_normalized_email(conn):
    """Mutation: drop ``common.normalize_email`` from the INSERT.

    The waitlist rate limit counts rows by normalized email. If ``add`` stored
    the raw address, ``Wl@Example.com`` would write a row the limit could never
    see — an unbounded-submission bypass, not merely untidy data.
    """
    waitlist.add(conn, "  WL@Example.COM  ")
    assert conn.execute(
        "SELECT email FROM beta_waitlist").fetchone()[0] == "wl@example.com"


# --- 3. mailer: the waitlist confirmation actually renders ITS template ------

def test_waitlist_confirmation_renders_the_waitlist_template(conn, capture):
    """Mutation: ``render("waitlist_confirmation")`` -> ``render("magic_link")``.

    Both ids exist in the registry, so a swap raises nothing. Asserted against
    the registry's own output rather than a copied literal, so the test pins the
    binding and not the wording.
    """
    subject, body_text = templates.render("waitlist_confirmation")
    mailer.send_waitlist_confirmation(conn, "conf@example.com")
    assert len(capture) == 1
    assert capture[0]["subject"] == subject
    assert capture[0]["body"] == body_text


def test_waitlist_confirmation_addresses_the_caller_and_returns_the_ref(conn,
                                                                       capture):
    """Mutation: drop the ``return``, or address a different variable.

    The adapter's reference is the only delivery evidence the caller ever gets;
    swallowing it turns a failed send into a silent success.
    """
    assert mailer.send_waitlist_confirmation(
        conn, "ref@example.com") == "capture-ref"
    assert capture[0]["to"] == "ref@example.com"


# --- 4. intake_api: the denylist's env parsing (fail-OPEN if it drifts) ------

def test_load_known_bad_lowercases_operator_supplied_hashes():
    """Mutation: drop ``.lower()``.

    ``hashlib.sha256(...).hexdigest()`` is lowercase, and plenty of hashing
    tools print uppercase. Without the fold, an operator who pastes an uppercase
    digest gets a denylist that denies NOTHING and reports no error. Compared
    against a real computed digest so the test proves the match, not the case.
    """
    digest = hashlib.sha256(b"known-bad-bytes").hexdigest()
    loaded = intake_api.load_known_bad(
        {"GOV_INTAKE_KNOWN_BAD_SHA256": digest.upper()})
    assert digest in loaded


def test_load_known_bad_accepts_comma_separated_hashes():
    """Mutation: drop ``.replace(",", " ")``.

    Comma separation is the natural way to write a list into one env var. Without
    the replace, ``"aaa...,bbb..."`` parses as ONE 129-character token that
    matches no digest — the whole denylist silently disappears.
    """
    a = hashlib.sha256(b"a").hexdigest()
    b = hashlib.sha256(b"b").hexdigest()
    loaded = intake_api.load_known_bad(
        {"GOV_INTAKE_KNOWN_BAD_SHA256": f"{a},{b}"})
    assert loaded == frozenset({a, b})
    # whitespace-and-comma mixed is the realistic hand-edited shape
    assert intake_api.load_known_bad(
        {"GOV_INTAKE_KNOWN_BAD_SHA256": f" {a} , {b} "}) == frozenset({a, b})


def test_load_known_bad_accepts_whitespace_separated_hashes():
    """Mutation: ``raw.replace(",", " ").split()`` -> ``raw.split(",")``.

    The obvious "simplification" — why replace commas with spaces just to split
    on whitespace? — passes every comma test above while silently collapsing a
    space-separated list into ONE unmatchable token. Both separators are part of
    the contract, so both are pinned. Nothing tested this before.
    """
    a = hashlib.sha256(b"a").hexdigest()
    b = hashlib.sha256(b"b").hexdigest()
    assert intake_api.load_known_bad(
        {"GOV_INTAKE_KNOWN_BAD_SHA256": f"{a} {b}"}) == frozenset({a, b})


@pytest.mark.parametrize("raw", ["", "   ", ",", " , , "])
def test_load_known_bad_is_empty_and_holds_no_blank_member(raw):
    """Contract pin, NOT a mutation catcher — recorded honestly.

    Dropping the ``if p.strip()`` filter does **not** fail this test, and that is
    the correct result: ``str.split()`` with no argument splits on runs of
    whitespace and discards empties, so ``parts`` can never contain a blank for
    ANY input (verified over ``""``, ``"   "``, ``","``, ``" , , "``, ``"a,,b"``).
    The filter is unreachable today — the same redundancy class as the
    ``bool(email)`` clause in ``common.valid_email``.

    It is still worth keeping in the source and pinning here, because it stops
    being redundant the moment someone applies the refactor the test above
    guards: ``raw.split(",")`` turns ``","`` into ``["", ""]``. What this test
    asserts is the caller-visible contract — an absent or blank var yields a
    truly EMPTY denylist, never one holding ``""`` that a missing digest could
    accidentally match.
    """
    assert intake_api.load_known_bad({"GOV_INTAKE_KNOWN_BAD_SHA256": raw}) == frozenset()
    assert intake_api.load_known_bad({}) == frozenset()


# --- 4b. intake_api.build_store: both fail-closed branches ------------------

def test_build_store_returns_none_when_the_key_is_unprovisioned(monkeypatch):
    """Mutation: let ``RawObjectStoreError`` propagate instead of returning None.

    ``build_store`` promises it never raises, because the endpoint turns None
    into a clean 503. A propagating error is a 500 on a path whose whole purpose
    is refusing cleanly. Driven through the real ``key_from_env`` by unsetting
    the env var — the branch is reached, not stubbed.
    """
    monkeypatch.delenv("GOV_RAWSTORE_KEY_HEX", raising=False)
    assert intake_api.build_store() is None


def test_build_store_returns_none_when_the_store_root_is_unusable(monkeypatch,
                                                                  tmp_path):
    """Mutation: drop ``OSError`` from the second ``except`` tuple.

    A valid key with an unusable root must still refuse. Rooting the store
    beneath a regular FILE makes the real constructor's mkdir raise
    ``NotADirectoryError`` (an ``OSError``), so this exercises the branch
    without patching the store.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("this is a file, so nothing can be created under it")
    monkeypatch.setenv("GOV_RAWSTORE_KEY_HEX",
                       raw_object_store.generate_key().hex())
    monkeypatch.setenv("GOV_RAWSTORE_ROOT", str(blocker / "store"))
    assert intake_api.build_store() is None
