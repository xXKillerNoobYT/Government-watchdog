"""GOV-1655: the magic-token single-use RACE guard — the branch no test reached.

`scripts/beta/tokens.py`'s module docstring asserts the property outright:

    Consumption is atomic: a conditional UPDATE stamps ``consumed_utc`` only if
    it is still NULL, so two concurrent verifies of the same token can never
    both win -- the loser sees ``rowcount == 0`` and is rejected.

That mechanism is two cooperating halves — `AND consumed_utc IS NULL` on the
UPDATE (`tokens.py:95`, `:137`) and `if cur.rowcount != 1: return None`
(`:97`, `:139`) — and **neither half had a test**. Every pre-existing test
redeems *sequentially*, so the earlier `SELECT`'s `consumed_utc is not None`
check rejects the second attempt and execution never reaches the atomic branch
at all. The guard could have been deleted wholesale with a green suite.

Why that matters here specifically: a magic link IS the credential. Two winners
on one row means two live sessions minted from a single one-time invite — a
replay, not a cosmetic bug. And a `gw_beta_session` lives 7 days.

**These tests are deterministic, not probabilistic.** Iteration 4's rule for
this repo is that a race needs a deterministic test, because replacing a flaky
test with a flaky test proves nothing. The seam: `consume` and `consume_code`
each call `common.iso()` exactly once, positioned *between* their SELECT and
their UPDATE (`tokens.py:90` and `:124`). Gating that one call on a
`threading.Barrier` guarantees every racer has finished its SELECT — and so
believes the row is unconsumed — before any racer attempts its UPDATE. The
contended window is forced open every run, not sampled.

Mutation-proved: each of the four guard halves was deleted individually and the
named test confirmed red. See the PR body for the matrix.
"""

from __future__ import annotations

import threading

import pytest

import db
from beta import common, tokens

EMAIL = "racer@example.com"


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "gov1655.db"
    db.apply_migrations(path)
    return path


def _race(db_path, consumers):
    """Run ``consumers`` concurrently, all SELECTing before any UPDATEs.

    Each consumer is ``fn(conn) -> str | None`` and gets its OWN connection,
    opened inside its own thread (sqlite3 forbids sharing one across threads).

    The barrier is installed on ``common.iso``, which each consume path calls
    exactly once between its SELECT and its UPDATE. Every thread therefore
    parks *after* reading ``consumed_utc IS NULL`` and is released only once
    all of them have — which is precisely the interleaving the guard exists for
    and the one that never happens by accident in a test.

    Returns the consumers' return values, positionally.
    """
    barrier = threading.Barrier(len(consumers), timeout=10)
    real_iso = common.iso

    def gated_iso(dt):
        out = real_iso(dt)
        barrier.wait()          # all SELECTs done; now let the UPDATEs collide
        return out

    results: list = [None] * len(consumers)
    errors: list = [None] * len(consumers)

    def worker(i, fn):
        try:
            conn = db.open_db(db_path)
            try:
                results[i] = fn(conn)
            finally:
                conn.close()
        except BaseException as exc:            # noqa: BLE001 - surfaced below
            errors[i] = exc

    common.iso = gated_iso
    try:
        threads = [threading.Thread(target=worker, args=(i, fn))
                   for i, fn in enumerate(consumers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert not any(t.is_alive() for t in threads), "a consumer thread hung"
    finally:
        common.iso = real_iso

    # A racer that raised (e.g. 'database is locked') is NOT a pass. Without
    # this the test could go green because both consumers died before UPDATE.
    assert errors == [None] * len(consumers), f"consumer(s) raised: {errors}"
    return results


def _race_ordered(db_path, consumers):
    """Like :func:`_race`, but the UPDATEs are additionally SERIALISED in order.

    Needed whenever the racers are *different* code paths. `_race` pins that
    every racer SELECTs before any racer UPDATEs, but it does NOT pin which
    UPDATE commits first — and for an asymmetric race the two directions are
    different assertions with different outcomes. Left un-pinned, such a test
    catches the bug only on the runs where the ordering happens to be
    unfavourable, i.e. it is flaky. (Found exactly that way: two identical
    mutations produced CAUGHT and SURVIVED on consecutive harness runs.)

    Guarantees, in order: (1) all racers complete their SELECT, (2) racer 0
    performs its UPDATE and commits and returns, (3) racer 1 does, and so on.
    """
    n = len(consumers)
    arrived = threading.Barrier(n, timeout=10)
    turns = [threading.Event() for _ in range(n)]
    real_iso = common.iso
    index_of = {}

    def gated_iso(dt):
        out = real_iso(dt)
        i = index_of[threading.current_thread().name]
        arrived.wait()          # (1) nobody proceeds until all have SELECTed
        turns[i].wait(timeout=10)   # (2)(3) then strictly one at a time
        return out

    results: list = [None] * n
    errors: list = [None] * n

    def worker(i, fn):
        try:
            conn = db.open_db(db_path)
            try:
                results[i] = fn(conn)
            finally:
                conn.close()
        except BaseException as exc:            # noqa: BLE001 - surfaced below
            errors[i] = exc

    common.iso = gated_iso
    try:
        threads = []
        for i, fn in enumerate(consumers):
            t = threading.Thread(target=worker, args=(i, fn), name=f"racer-{i}")
            index_of[t.name] = i
            threads.append(t)
        for t in threads:
            t.start()
        for i, t in enumerate(threads):
            turns[i].set()      # release racer i's UPDATE; wait for it to finish
            t.join(timeout=20)
            assert not t.is_alive(), f"racer-{i} hung"
    finally:
        common.iso = real_iso

    assert errors == [None] * n, f"consumer(s) raised: {errors}"
    return results


def _consumed_rows(db_path):
    conn = db.open_db(db_path)
    try:
        return conn.execute(
            "SELECT token_id, consumed_utc FROM beta_magic_tokens"
            " WHERE consumed_utc IS NOT NULL").fetchall()
    finally:
        conn.close()


def _issue_link_only(db_path):
    conn = db.open_db(db_path)
    try:
        return tokens.issue(conn, EMAIL)
    finally:
        conn.close()


def _issue_with_code(db_path):
    conn = db.open_db(db_path)
    try:
        return tokens.issue_with_code(conn, EMAIL)
    finally:
        conn.close()


# --- the race guard ----------------------------------------------------------

def test_two_concurrent_link_consumes_only_one_wins(db_path):
    """Mutations caught: dropping `AND consumed_utc IS NULL` (tokens.py:95), OR
    dropping `if cur.rowcount != 1: return None` (tokens.py:97).

    Both threads pass the pre-SELECT unconsumed check — that is the whole point
    of the barrier — so the ONLY thing separating one winner from two is the
    conditional UPDATE plus its rowcount verdict.
    """
    raw = _issue_link_only(db_path)

    results = _race(db_path, [lambda c: tokens.consume(c, raw),
                              lambda c: tokens.consume(c, raw)])

    winners = [r for r in results if r is not None]
    assert winners == [EMAIL], f"expected exactly one winner, got {results}"
    assert results.count(None) == 1, "the loser must be rejected, not served"
    # ...and the row was stamped exactly once.
    assert len(_consumed_rows(db_path)) == 1


def test_two_concurrent_code_consumes_only_one_wins(db_path):
    """Same guard on the numeric-code path (tokens.py:137/:139).

    The audit found this path's race branch equally unreached: every existing
    code test redeems sequentially or tests the attempt cap.
    """
    _, raw_code = _issue_with_code(db_path)

    results = _race(db_path, [lambda c: tokens.consume_code(c, EMAIL, raw_code),
                              lambda c: tokens.consume_code(c, EMAIL, raw_code)])

    winners = [r for r in results if r is not None]
    assert winners == [EMAIL], f"expected exactly one winner, got {results}"
    assert len(_consumed_rows(db_path)) == 1


def test_cross_credential_link_first_then_code_is_rejected(db_path):
    """The GOV-1538 cross-credential invariant, raced — LINK commits first.

    `issue_with_code` mints a link token AND a 6-digit code on the SAME row, and
    the docstring promises "either credential redeems that one row, so consuming
    one invalidates the other." Sequentially that is covered. **Concurrently it
    was not** — and this is the realistic user story, not a contrived one: the
    same person taps the emailed link on their phone while typing the code on
    their laptop. Two winners mints two sessions from one invite.

    This direction pins the CODE path's guard: both credentials read the row as
    unconsumed, the link redeems it, and `consume_code`'s conditional UPDATE
    must then find nothing to stamp.

    Mutation caught: `consume_code`'s `AND consumed_utc IS NULL` or its rowcount
    verdict. (The link path's guard is pinned by the mirror test below — the two
    directions are NOT interchangeable, which is why both exist.)
    """
    raw_token, raw_code = _issue_with_code(db_path)

    link, code = _race_ordered(
        db_path, [lambda c: tokens.consume(c, raw_token),
                  lambda c: tokens.consume_code(c, EMAIL, raw_code)])

    assert link == EMAIL, "the link redeemed first and must win"
    assert code is None, (
        "the code redeemed an already-consumed row -> two sessions, one invite")
    assert len(_consumed_rows(db_path)) == 1


def test_cross_credential_code_first_then_link_is_rejected(db_path):
    """Mirror of the above — CODE commits first, so the LINK must lose.

    Pins `consume`'s guard against a row already consumed *by the other
    credential*. Not redundant with
    :func:`test_two_concurrent_link_consumes_only_one_wins`: there both racers
    are `consume`, so a mutation to the shared UPDATE breaks both sides
    symmetrically. Here the winner is `consume_code`, so this is the only test
    that catches the link path being made unconditional while the code path
    stays correct.
    """
    raw_token, raw_code = _issue_with_code(db_path)

    code, link = _race_ordered(
        db_path, [lambda c: tokens.consume_code(c, EMAIL, raw_code),
                  lambda c: tokens.consume(c, raw_token)])

    assert code == EMAIL, "the code redeemed first and must win"
    assert link is None, (
        "the link redeemed an already-consumed row -> two sessions, one invite")
    assert len(_consumed_rows(db_path)) == 1


# --- over-reach locks --------------------------------------------------------
#
# Without these, a mutation that makes consume ALWAYS fail would leave every
# test above green: "exactly one winner" is satisfied by "nobody wins" only if
# nothing asserts the uncontended path still works. A guard that rejects
# everyone is broken, not strict.

def test_uncontended_link_consume_still_succeeds(db_path):
    """Over-reach lock for the link path (single caller, no race)."""
    raw = _issue_link_only(db_path)
    conn = db.open_db(db_path)
    try:
        assert tokens.consume(conn, raw) == EMAIL
    finally:
        conn.close()
    assert len(_consumed_rows(db_path)) == 1


def test_uncontended_code_consume_still_succeeds(db_path):
    """Over-reach lock for the code path (single caller, no race)."""
    _, raw_code = _issue_with_code(db_path)
    conn = db.open_db(db_path)
    try:
        assert tokens.consume_code(conn, EMAIL, raw_code) == EMAIL
    finally:
        conn.close()
    assert len(_consumed_rows(db_path)) == 1


def test_the_barrier_actually_forces_the_contended_interleaving(db_path):
    """Proves the HARNESS, not the code — the test above is only meaningful if
    both racers really did SELECT before either UPDATEd.

    Without this, a barrier that silently no-op'd (or a `common.iso` that was
    never called on the consume path, e.g. after a refactor moved it) would
    reduce every race test to a sequential redemption that passes for the wrong
    reason — the exact failure iteration 5 hit when its duplicate-cookie test
    went green via the wrong code path.

    Records the observed order: both `select` events must precede both
    `update` events.
    """
    raw = _issue_link_only(db_path)
    events: list[str] = []
    lock = threading.Lock()
    real_iso = common.iso
    barrier = threading.Barrier(2, timeout=10)

    def probe_iso(dt):
        out = real_iso(dt)
        with lock:
            events.append("select-done")     # SELECT is behind us, UPDATE ahead
        barrier.wait()
        with lock:
            events.append("released")
        return out

    def worker():
        conn = db.open_db(db_path)
        try:
            tokens.consume(conn, raw)
        finally:
            conn.close()

    common.iso = probe_iso
    try:
        ts = [threading.Thread(target=worker) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=20)
    finally:
        common.iso = real_iso

    assert events[:2] == ["select-done", "select-done"], (
        f"both SELECTs must complete before either is released, got {events}")
    assert events[2:] == ["released", "released"], f"unexpected order: {events}"
