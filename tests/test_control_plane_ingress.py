"""GOV-733 CTRL-2026 — signed ingress (AC-1 re-notify, AC-2 reject, AC-7 imports).

Includes a real loopback HTTP round-trip and the bind guard, plus the
import-graph test proving webhook_ingress touches no crawler/AI/provider module.
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import db  # noqa: E402
import webhook_ingress as wi  # noqa: E402

SECRET = "s3cr3t-synthetic"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "ctrl.db"
    db.apply_migrations(p)
    c = db.open_db(p)
    c.execute(
        "INSERT INTO webhook_sources (source_key, secret_ref, active, created_at) "
        "VALUES ('toa', 'GW_SECRET_TOA', 1, '2026-07-15T00:00:00.000+00:00')"
    )
    c.execute(
        "INSERT INTO webhook_sources (source_key, secret_ref, active, created_at) "
        "VALUES ('off', 'GW_SECRET_OFF', 0, '2026-07-15T00:00:00.000+00:00')"
    )
    c.commit()
    c.close()
    return p


def _lookup(ref):
    return {"GW_SECRET_TOA": SECRET, "GW_SECRET_OFF": SECRET}.get(ref)


def _body(**over) -> bytes:
    payload = dict(event_kind="agenda.published", source_ref="meeting/129",
                   policy_version="2026-COMM-v1", area_id="AREA-1", data={"n": 1})
    payload.update(over)
    return json.dumps(payload).encode("utf-8")


def _call(db_path, raw, *, source="toa", sign_secret=SECRET, ts=None,
          now=1000.0, freshness=300):
    ts = now if ts is None else ts
    sig = wi.compute_signature(sign_secret, raw) if sign_secret else None
    c = db.open_db(db_path)
    try:
        return wi.process_request(
            c, source_key=source, signature=sig, timestamp=str(ts),
            raw_body=raw, now_epoch=now, freshness_s=freshness,
            secret_lookup=_lookup,
        )
    finally:
        c.close()


def _counts(db_path):
    c = db.open_db(db_path)
    try:
        e = c.execute("SELECT COUNT(*) FROM event_envelopes").fetchone()[0]
        j = c.execute("SELECT COUNT(*) FROM event_jobs").fetchone()[0]
        return e, j
    finally:
        c.close()


# --- AC-1: valid + replay ----------------------------------------------------

def test_valid_signed_request_accepts_and_enqueues(db_path):
    status, resp = _call(db_path, _body())
    assert status == 202
    assert resp["is_new"] is True
    assert resp["job_id"] is not None
    assert _counts(db_path) == (1, 1)


def test_replayed_request_enqueues_zero_new_jobs(db_path):
    raw = _body()
    _call(db_path, raw)
    status, resp = _call(db_path, raw)  # identical signed replay
    assert status == 202
    assert resp["is_new"] is False
    assert resp["job_id"] is None
    assert _counts(db_path) == (1, 1)  # still exactly one envelope + one job


# --- AC-2: rejection ⇒ 401 + zero rows --------------------------------------

def test_unsigned_rejected_no_rows(db_path):
    status, resp = _call(db_path, _body(), sign_secret=None)
    assert status == 401
    assert resp["reason"] == "missing_signature"
    assert _counts(db_path) == (0, 0)


def test_bad_signature_rejected_no_rows(db_path):
    status, resp = _call(db_path, _body(), sign_secret="wrong-secret")
    assert status == 401
    assert resp["reason"] == "bad_signature"
    assert _counts(db_path) == (0, 0)


def test_stale_timestamp_rejected_no_rows(db_path):
    status, resp = _call(db_path, _body(), ts=1000.0, now=2000.0, freshness=300)
    assert status == 401
    assert resp["reason"] == "stale_timestamp"
    assert _counts(db_path) == (0, 0)


def test_unknown_source_rejected_no_rows(db_path):
    status, resp = _call(db_path, _body(), source="nope")
    assert status == 401
    assert resp["reason"] == "unknown_source"
    assert _counts(db_path) == (0, 0)


def test_inactive_source_rejected_no_rows(db_path):
    status, resp = _call(db_path, _body(), source="off")
    assert status == 401
    assert resp["reason"] == "inactive_source"
    assert _counts(db_path) == (0, 0)


def test_authenticated_but_malformed_body_is_400_no_rows(db_path):
    raw = b"not json at all"
    status, resp = _call(db_path, raw)
    assert status == 400
    assert _counts(db_path) == (0, 0)


# --- bind guard --------------------------------------------------------------

def test_serve_refuses_non_loopback_bind(db_path):
    with pytest.raises(wi.BindError):
        wi.serve(db_path, host="0.0.0.0", port=0)


def test_serve_allows_loopback(db_path):
    server = wi.serve(db_path, host="127.0.0.1", port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


# --- real HTTP round-trip ----------------------------------------------------

def test_real_loopback_round_trip(db_path, monkeypatch, tmp_path):
    monkeypatch.setenv("GW_SECRET_TOA", SECRET)
    log_dir = tmp_path / "logs"
    handler = wi.make_handler(db_path, log_dir=log_dir)
    from http.server import HTTPServer

    server = HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        raw = _body()
        ts = str(time.time())
        sig = wi.compute_signature(SECRET, raw)
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/", data=raw, method="POST",
            headers={wi.HDR_SOURCE: "toa", wi.HDR_SIGNATURE: sig, wi.HDR_TIMESTAMP: ts},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 202
            body = json.loads(r.read())
        assert body["is_new"] is True
    finally:
        server.shutdown()
        server.server_close()
    assert _counts(db_path) == (1, 1)
    # a JSONL ingress log line was written (accepts + rejects, plan §7)
    logs = list(log_dir.glob("ingress-*.jsonl"))
    assert logs and logs[0].read_text().strip()


# --- AC-7: import-graph ------------------------------------------------------

def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_ingress_imports_only_stdlib_and_allowed_leaves():
    imports = _imported_modules(SCRIPTS / "webhook_ingress.py")
    allowed_local = {"db", "event_envelope", "job_queue"}
    stdlib = {
        "__future__", "argparse", "hashlib", "hmac", "json", "os", "sys",
        "time", "datetime", "http", "pathlib",
    }
    unexpected = imports - allowed_local - stdlib
    assert not unexpected, f"webhook_ingress has unexpected imports: {unexpected}"


def test_ingress_imports_nothing_from_crawler_ai_provider():
    imports = _imported_modules(SCRIPTS / "webhook_ingress.py")
    forbidden_substrings = ("crawl", "ai_", "gateway", "provider", "model",
                            "read_api", "risk", "ingest", "structure", "embed")
    for name in imports:
        assert not any(s in name for s in forbidden_substrings), \
            f"ingress must not import {name}"
