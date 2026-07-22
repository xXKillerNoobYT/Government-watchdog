"""GOV-1544 (P3b of GOV-1523) — beta front-door wiring into the web artifact.

Contract addendum to ``Docs/gov1523-artifact-contract-spec.md`` §1 (same shape
as the GOV-1538 accounts addendum, different module): ``scripts/beta/`` joins
the packaged service import closure, the generated ``service/run.py`` routes
``/api/beta/*`` (POST/GET/DELETE), and the artifact gains the seedless
``service/schema.sql`` so a deployed unit can initialize an empty accounts DB
without shipping migrations.

Proven here, against the REAL staged artifact over a REAL loopback socket:

  * flag off (the shipped state) ⇒ every /api/beta/* route is a constant 404 —
    deploying activates nothing (D1).
  * flag on ⇒ magic-link verify sets the session cookie with SameSite=Strict
    (F1, through the full HTTP stack), sign-out clears it.
  * deny-list stays green with beta packaged; schema.sql creates the
    accounts/flags/beta tables and zero rows of anything.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import socket
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import db  # noqa: E402
import export_web_artifact as ewa  # noqa: E402
from beta import allowlist, tokens  # noqa: E402
from email_gateway import flags  # noqa: E402

FAKE_COMMIT = "f" * 40
FIXED_TS = "2026-07-21T00:00:00+00:00"


# --- closure / allowlist / schema (no sockets) --------------------------------

def test_beta_is_in_the_service_entry_packages():
    assert "beta" in ewa.SERVICE_ENTRY_PACKAGES


def test_closure_packages_the_beta_front_door():
    rels = {rel.as_posix() for rel in ewa.compute_service_closure(ewa.SCRIPTS_DIR)}
    assert "beta/http_api.py" in rels
    assert "beta/service.py" in rels
    assert "read_api.py" not in rels  # still never shipped


def test_schema_sql_creates_seedless_accounts_tables(tmp_path):
    sql = ewa.schema_sql_bytes().decode("utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for required in ("feature_flags", "email_outbox", "beta_allowlist",
                     "beta_magic_tokens", "beta_sessions", "beta_waitlist"):
        assert required in tables, f"schema.sql missing {required}"
    for table in tables:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    conn.close()


# --- staged artifact fixtures -------------------------------------------------

@pytest.fixture(scope="module")
def service_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("gov1544") / "service.db"
    db.apply_migrations(path)
    return path


@pytest.fixture(scope="module")
def staged(tmp_path_factory, service_db) -> Path:
    files = ewa.stage_files(service_db, backend_commit=FAKE_COMMIT,
                            generated_at_utc=FIXED_TS)
    root = tmp_path_factory.mktemp("gov1544-artifact")
    return ewa.extract_to(files, root)


def test_staged_artifact_ships_beta_and_schema_and_stays_deny_clean(staged):
    assert (staged / "service/beta/http_api.py").exists()
    assert (staged / "service/schema.sql").exists()
    assert ewa.deny_list_violations(staged) == []


# --- the wired service over a real loopback socket ----------------------------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_service(staged, service_db):
    spec = importlib.util.spec_from_file_location(
        "gov1544_staged_run", staged / "service" / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)
    port = _free_port()
    server = run.serve(service_db, port=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    conn.request(method, path, body=payload, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    result = (response.status, data, dict(response.getheaders()))
    conn.close()
    return result


def test_health_still_answers(live_service):
    status, data, _ = _request(live_service, "GET", "/api/health")
    assert status == 200
    assert json.loads(data)["status"] == "ok"


def test_flag_off_is_constant_404_for_every_beta_route(live_service):
    for method, path in (
        ("POST", "/api/beta/magic-link/request"),
        ("GET", "/api/beta/magic-link/verify?token=x"),
        ("POST", "/api/beta/waitlist"),
        ("DELETE", "/api/beta/sessions/current"),
    ):
        status, data, _ = _request(
            live_service, method, path, body={"email": "a@example.com"}
            if method == "POST" else None)
        assert (status, json.loads(data)) == (404, {"error": "not_found"}), (
            f"{method} {path} must be a constant 404 while the flag is off")


def test_non_beta_post_is_404_not_500(live_service):
    status, data, _ = _request(live_service, "POST", "/api/notifications",
                               body={})
    assert (status, json.loads(data)) == (404, {"error": "not_found"})


def test_flag_on_full_magic_link_flow_sets_strict_cookie(live_service, service_db):
    conn = db.open_db(service_db)
    try:
        flags.set_flag(conn, "beta_gate_enabled", enabled=True,
                       owner_decision_ref="test:GOV-1544:wiring")
        allowlist.add(conn, "flow@example.com",
                      owner_decision_ref="test:GOV-1544:wiring")
        raw_token = tokens.issue(conn, "flow@example.com")
    finally:
        conn.close()

    # waitlist joins answer neutrally now that the gate is on
    status, data, _ = _request(live_service, "POST", "/api/beta/waitlist",
                               body={"email": "resident@example.com"})
    assert (status, json.loads(data)) == (200, {"status": "ok"})

    status, _, headers = _request(
        live_service, "GET", f"/api/beta/magic-link/verify?token={raw_token}")
    assert status == 302
    assert headers["Location"] == "/#/app"
    cookie = headers["Set-Cookie"]
    assert cookie.startswith("gw_beta_session=")
    assert "SameSite=Strict" in cookie and "SameSite=Lax" not in cookie
    assert "HttpOnly" in cookie and "Secure" in cookie

    raw_session = cookie.split(";", 1)[0].split("=", 1)[1]
    status, data, headers = _request(
        live_service, "DELETE", "/api/beta/sessions/current",
        headers={"Cookie": f"gw_beta_session={raw_session}"})
    assert (status, json.loads(data)) == (200, {"status": "ok"})
    assert "Max-Age=0" in headers["Set-Cookie"]
