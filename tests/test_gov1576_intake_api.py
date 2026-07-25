"""GOV-1576 B3: gated supplied-file intake API (fail-closed).

Covers the AC end to end:

  * Flag off returns 404 for the whole surface (negative test) — the surface is
    indistinguishable from routes that do not exist.
  * Rejects disallowed mime + over-size; accepts an allow-listed type in-limit.
  * Success persists raw (B1) + record (B2) in ``pending`` with full provenance.
  * No unauthenticated / public path — no session, a bad session, or a
    de-allowlisted email are all a single neutral 401.

Plus the belt-and-suspenders behaviours the AC implies: known-bad-hash 422,
store-unavailable 503, dedupe, server-derived ``supplied_by`` (un-forgeable
provenance), and the loopback-only bind guard.
"""

from __future__ import annotations

import base64
import http.client
import json
import threading

import pytest

import db
import file_records
import raw_object_store
from beta import allowlist, common, http_api, intake_api, sessions
from email_gateway import flags


# --- fixtures ----------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "gov1576.db"
    db.apply_migrations(path)
    return path


@pytest.fixture()
def conn(db_path):
    c = db.open_db(db_path)
    yield c
    c.close()


@pytest.fixture()
def store(tmp_path):
    """A real B1 store with a generated (never-committed) key."""
    return raw_object_store.RawObjectStore(
        tmp_path / "raw-store", key=raw_object_store.generate_key())


def _enable_gate(conn):
    flags.set_flag(conn, http_api.BETA_GATE_FLAG, enabled=True,
                   owner_decision_ref="test-card-gov1576")


ALLOWED_EMAIL = "supplier@example.com"


def _session_cookie(conn, email=ALLOWED_EMAIL, *, allowlisted=True):
    """Mint a session and return its Cookie header value."""
    if allowlisted:
        allowlist.add(conn, email, owner_decision_ref="GOV-1576-test")
    _, raw = sessions.issue(conn, email)
    return f"{intake_api.COOKIE_NAME}={raw}"


def _body(content: bytes = b"%PDF-1.4 fake pdf bytes", *, mime="application/pdf",
          **overrides) -> bytes:
    payload = {
        "area": "alpine",
        "source_type": "minutes",
        "original_filename": "council-minutes.pdf",
        "mime": mime,
        "content_base64": base64.b64encode(content).decode("ascii"),
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


def _post(conn, store, body=None, *, cookie=None, known_bad=None, path=None):
    return intake_api.process_request(
        conn, store, method="POST",
        path=path or intake_api.INTAKE_UPLOAD_ROUTE,
        raw_body=body if body is not None else _body(),
        cookie_header=cookie, known_bad=known_bad)


# --- AC1: flag off => 404 for the whole surface ------------------------------

def test_flag_off_returns_404_for_upload(conn, store):
    status, payload, _ = _post(conn, store, cookie=_session_cookie(conn))
    assert status == 404
    assert payload == {"error": "not_found"}


def test_flag_off_404_even_with_everything_valid(conn, store):
    # A perfectly valid, authenticated request is STILL 404 while the flag is off
    # — the gate is checked before auth, method, or route matter (fail-closed).
    cookie = _session_cookie(conn)
    status, _, _ = _post(conn, store, cookie=cookie)
    assert status == 404


def test_flag_off_other_methods_and_routes_404(conn, store):
    for method, path in [("GET", intake_api.INTAKE_UPLOAD_ROUTE),
                         ("POST", "/api/beta/intake/other"),
                         ("DELETE", intake_api.INTAKE_UPLOAD_ROUTE)]:
        status, _, _ = intake_api.process_request(
            conn, store, method=method, path=path)
        assert status == 404


def test_flag_on_wrong_method_or_route_still_404(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    for method, path in [("GET", intake_api.INTAKE_UPLOAD_ROUTE),
                         ("POST", "/api/beta/intake/nope"),
                         ("PUT", intake_api.INTAKE_UPLOAD_ROUTE)]:
        status, _, _ = intake_api.process_request(
            conn, store, method=method, path=path, cookie_header=cookie)
        assert status == 404


# --- AC4: no unauthenticated / public path -----------------------------------

def test_no_cookie_is_401(conn, store):
    _enable_gate(conn)
    status, payload, _ = _post(conn, store, cookie=None)
    assert status == 401
    assert payload == {"error": "unauthorized"}


def test_garbage_cookie_is_401(conn, store):
    _enable_gate(conn)
    status, _, _ = _post(conn, store, cookie="gw_beta_session=not-a-real-token")
    assert status == 401


def test_session_for_non_allowlisted_email_is_401(conn, store):
    _enable_gate(conn)
    # A minted session whose email is not on the allowlist must not authenticate.
    cookie = _session_cookie(conn, "ghost@example.com", allowlisted=False)
    status, _, _ = _post(conn, store, cookie=cookie)
    assert status == 401


def test_deallowlisted_email_is_401(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)  # allowlisted + session
    allowlist.revoke(conn, ALLOWED_EMAIL, owner_decision_ref="GOV-1576-revoke")
    status, _, _ = _post(conn, store, cookie=cookie)
    assert status == 401


# --- store availability (fail-closed) ----------------------------------------

def test_store_unavailable_is_503(conn):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    status, payload, _ = intake_api.process_request(
        conn, None, method="POST", path=intake_api.INTAKE_UPLOAD_ROUTE,
        raw_body=_body(), cookie_header=cookie)
    assert status == 503
    assert payload == {"error": "store_unavailable"}


# --- AC2: mime allow-list + size limits --------------------------------------

def test_disallowed_mime_is_415(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    status, payload, _ = _post(
        conn, store, body=_body(mime="application/x-msdownload"), cookie=cookie)
    assert status == 415
    assert payload == {"error": "unsupported_media_type"}


def test_oversize_is_413(conn, store, monkeypatch):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    monkeypatch.setattr(intake_api, "MAX_UPLOAD_BYTES", 8)
    status, payload, _ = _post(
        conn, store, body=_body(b"way more than eight bytes"), cookie=cookie)
    assert status == 413
    assert payload == {"error": "payload_too_large"}


def test_allowlisted_mime_in_limit_accepted(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    for mime in sorted(intake_api.ALLOWED_MIMES):
        status, _, _ = _post(
            conn, store, body=_body(f"bytes for {mime}".encode(), mime=mime),
            cookie=cookie)
        assert status == 201, mime


# --- body / encoding validation ----------------------------------------------

def test_not_json_is_400(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    status, _, _ = _post(conn, store, body=b"not json", cookie=cookie)
    assert status == 400


def test_missing_required_field_is_400(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    good = json.loads(_body().decode())
    for field in ("area", "source_type", "original_filename", "mime",
                  "content_base64"):
        broken = dict(good)
        broken.pop(field)
        status, _, _ = _post(conn, store, body=json.dumps(broken).encode(),
                             cookie=cookie)
        assert status == 400, field


def test_blank_required_field_is_400(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    status, _, _ = _post(conn, store, body=_body(area="   "), cookie=cookie)
    assert status == 400


def test_bad_base64_is_400(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    good = json.loads(_body().decode())
    good["content_base64"] = "!!! not valid base64 !!!"
    status, _, _ = _post(conn, store, body=json.dumps(good).encode(), cookie=cookie)
    assert status == 400


def test_empty_content_is_400(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    status, _, _ = _post(conn, store, body=_body(content=b""), cookie=cookie)
    assert status == 400


# --- known-bad denylist ------------------------------------------------------

def test_known_bad_hash_is_422(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    import hashlib

    content = b"%PDF-1.4 malicious"
    bad = hashlib.sha256(content).hexdigest()
    status, payload, _ = _post(
        conn, store, body=_body(content=content), cookie=cookie,
        known_bad=frozenset({bad}))
    assert status == 422
    assert payload == {"error": "rejected_known_bad"}
    # And nothing was persisted.
    assert store.object_count() == 0
    assert conn.execute("SELECT COUNT(*) FROM supplied_files").fetchone()[0] == 0


# --- AC3: success persists raw + record, pending, full provenance ------------

def test_happy_path_persists_pending_with_full_provenance(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    content = b"%PDF-1.4 real council minutes"
    status, payload, _ = _post(
        conn, store,
        body=_body(content=content, origin_url="https://example.gov/min.pdf"),
        cookie=cookie)
    assert status == 201

    # Raw bytes are in B1, content-addressed + retrievable (decrypts + verifies).
    assert store.exists(payload["sha256"])
    assert store.get(payload["sha256"]) == content

    # The record is in B2, pending, with un-fabricated provenance.
    record = file_records.get_file_record(conn, payload["file_id"])
    assert record is not None
    assert record.review_state == "pending"
    assert record.sha256 == payload["sha256"]
    assert record.byte_size == len(content)
    assert record.area == "alpine"
    assert record.source_type == "minutes"
    assert record.original_filename == "council-minutes.pdf"
    assert record.mime == "application/pdf"
    assert record.supplied_by == ALLOWED_EMAIL
    assert record.origin_url == "https://example.gov/min.pdf"
    assert record.captured_at  # defaulted, non-empty
    assert payload["deduped"] is False


def test_supplied_by_is_session_email_not_body(conn, store):
    # A caller cannot forge who supplied the file: supplied_by is the session
    # email even if the body tries to set it.
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    status, payload, _ = _post(
        conn, store, body=_body(supplied_by="attacker@evil.example"),
        cookie=cookie)
    assert status == 201
    record = file_records.get_file_record(conn, payload["file_id"])
    assert record.supplied_by == ALLOWED_EMAIL


def test_dedupe_same_bytes_twice(conn, store):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    content = b"identical civic bytes"
    s1, p1, _ = _post(conn, store, body=_body(content=content), cookie=cookie)
    s2, p2, _ = _post(conn, store, body=_body(content=content), cookie=cookie)
    assert (s1, s2) == (201, 201)
    assert p1["deduped"] is False and p2["deduped"] is True
    assert p1["sha256"] == p2["sha256"]
    # One physical object, two distinct records (same bytes, own provenance rows).
    assert store.object_count() == 1
    assert p1["file_id"] != p2["file_id"]
    assert conn.execute("SELECT COUNT(*) FROM supplied_files").fetchone()[0] == 2


# --- bind guard (GATE-PUB / INV-4) -------------------------------------------

def test_serve_refuses_non_loopback(db_path):
    with pytest.raises(intake_api.BindError):
        intake_api.serve(db_path, host="0.0.0.0")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_serve_allows_loopback(db_path, host):
    server = intake_api.serve(db_path, host=host, port=0)
    try:
        assert server.server_address[0] in {"127.0.0.1"}
    finally:
        server.server_close()


# --- live socket round-trip (transport + Content-Length cap) -----------------

def test_live_upload_roundtrip(db_path, conn, monkeypatch, tmp_path):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    conn.close()
    # Point the handler's store at a real, keyed store via env.
    monkeypatch.setenv(raw_object_store._KEY_ENV,
                       raw_object_store.generate_key().hex())
    monkeypatch.setenv(intake_api._STORE_ROOT_ENV, str(tmp_path / "live-store"))

    server = intake_api.serve(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("POST", intake_api.INTAKE_UPLOAD_ROUTE, body=_body(),
                  headers={"Content-Type": "application/json", "Cookie": cookie})
        resp = c.getresponse()
        payload = json.loads(resp.read())
        assert resp.status == 201
        assert payload["review_state"] == "pending"
    finally:
        t.join(timeout=5)
        server.server_close()


def test_live_content_length_cap_is_413(db_path, conn, monkeypatch):
    _enable_gate(conn)
    cookie = _session_cookie(conn)
    conn.close()
    monkeypatch.setattr(intake_api, "_READ_CAP", 16)
    server = intake_api.serve(db_path, host="127.0.0.1", port=0)
    port = server.server_address[1]
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("POST", intake_api.INTAKE_UPLOAD_ROUTE,
                  body=b"x" * 1024,
                  headers={"Content-Type": "application/json", "Cookie": cookie})
        resp = c.getresponse()
        assert resp.status == 413
    finally:
        t.join(timeout=5)
        server.server_close()
