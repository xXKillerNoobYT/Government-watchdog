"""Signed-webhook ingress: stdlib HTTP server, 127.0.0.1 only.

GOV-733 (implements GOV-719 plan CTRL-2026, rev c4d03918 §3.2). Leaf module —
imports ONLY stdlib + ``db`` / ``event_envelope`` / ``job_queue`` (enforced by
the AC-7 import-graph test). It never imports crawler/AI/provider modules and
never runs a worker (RED-2: dispatch happens only from ``job_worker`` CLI).

Per request (POST):

1. ``X-GW-Source`` names a registered, active ``webhook_sources`` row.
2. ``X-GW-Timestamp`` (unix seconds) is within the freshness window (300 s).
3. ``X-GW-Signature`` = hex HMAC-SHA256 over the raw body with the source's
   secret, compared in constant time.

All three must pass. Then the body is canonicalised into a WRITE-ONCE envelope
and — only on a first sighting — one job is enqueued, returning ``202`` with the
``envelope_id``. Any failure ⇒ ``401``, a JSONL rejection log line, and **zero
rows** (the first DB write is downstream of every auth check, AC-2).

Bind guard: :func:`serve` refuses any host other than 127.0.0.1/localhost
(GATE-PUB / INV-4 — no public exposure, ever).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
import event_envelope  # noqa: E402
import job_queue  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO_ROOT / "Logs" / "control-plane"
ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "localhost"})
DEFAULT_FRESHNESS_S = 300
DEFAULT_LANE = "noop_synthetic"

HDR_SOURCE = "X-GW-Source"
HDR_SIGNATURE = "X-GW-Signature"
HDR_TIMESTAMP = "X-GW-Timestamp"


class BindError(Exception):
    """Raised on an attempt to bind anywhere but the loopback interface."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def compute_signature(secret: str, raw_body: bytes) -> str:
    """Hex HMAC-SHA256 over the raw request body. The shared signing recipe."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def resolve_secret(secret_ref: str) -> str | None:
    """The HMAC secret lives in a local env var named by ``secret_ref`` (INV-7)."""
    return os.environ.get(secret_ref)


class Rejected(Exception):
    """A request that failed verification. Carries a machine reason code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _verify(
    conn,
    *,
    source_key: str | None,
    signature: str | None,
    timestamp: str | None,
    raw_body: bytes,
    now_epoch: float,
    freshness_s: int,
    secret_lookup,
) -> None:
    """Raise :class:`Rejected` unless the request is authentic and fresh."""
    if not source_key:
        raise Rejected("missing_source")
    row = conn.execute(
        "SELECT secret_ref, active FROM webhook_sources WHERE source_key = ?",
        (source_key,),
    ).fetchone()
    if row is None:
        raise Rejected("unknown_source")
    if not int(row["active"]):
        raise Rejected("inactive_source")
    if not signature:
        raise Rejected("missing_signature")
    if not timestamp:
        raise Rejected("missing_timestamp")
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        raise Rejected("bad_timestamp")
    if abs(now_epoch - ts) > freshness_s:
        raise Rejected("stale_timestamp")
    secret = secret_lookup(row["secret_ref"])
    if not secret:
        # Misconfiguration: no secret to verify against. Fail closed, no rows.
        raise Rejected("secret_unavailable")
    expected = compute_signature(secret, raw_body)
    if not hmac.compare_digest(expected, signature):
        raise Rejected("bad_signature")


def process_request(
    conn,
    *,
    source_key: str | None,
    signature: str | None,
    timestamp: str | None,
    raw_body: bytes,
    now_epoch: float | None = None,
    freshness_s: int = DEFAULT_FRESHNESS_S,
    secret_lookup=resolve_secret,
    lane_router=None,
) -> tuple[int, dict]:
    """Pure request core (no sockets) — the unit-testable heart of ingress.

    Returns ``(status, response_dict)``. ``202`` on accept (envelope +, if new,
    one job); ``401`` on any verification failure with zero rows; ``400`` on an
    authenticated-but-malformed body. The caller owns nothing here — this
    function opens and commits its own write only after verification passes.
    """
    now_epoch = time.time() if now_epoch is None else now_epoch
    try:
        _verify(
            conn,
            source_key=source_key,
            signature=signature,
            timestamp=timestamp,
            raw_body=raw_body,
            now_epoch=now_epoch,
            freshness_s=freshness_s,
            secret_lookup=secret_lookup,
        )
    except Rejected as rej:
        return 401, {"status": "rejected", "reason": rej.reason}

    # Authenticated. Parse the body; a malformed authenticated body is a 400.
    try:
        body = json.loads(raw_body.decode("utf-8"))
        event_kind = body["event_kind"]
        source_ref = body["source_ref"]
        policy_version = body["policy_version"]
    except (ValueError, KeyError, TypeError) as exc:
        return 400, {"status": "bad_request", "reason": f"malformed_body:{exc}"}

    area_id = body.get("area_id")
    content_sha256 = hashlib.sha256(raw_body).hexdigest()
    lane = (lane_router or (lambda ek, b: DEFAULT_LANE))(event_kind, body)

    result = event_envelope.insert_envelope(
        conn,
        source_key=source_key,
        event_kind=event_kind,
        source_ref=source_ref,
        content_sha256=content_sha256,
        policy_version=policy_version,
        payload=body,
        area_id=area_id,
    )
    job_id = None
    if result.is_new:
        job_id = job_queue.enqueue_job(
            conn,
            envelope_id=result.envelope_id,
            lane=lane,
            area_id=area_id,
            policy_version=policy_version,
        )
    conn.commit()
    return 202, {
        "status": "accepted",
        "envelope_id": result.envelope_id,
        "is_new": result.is_new,
        "job_id": job_id,
    }


def _log_line(log_dir: Path, record: dict) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).date().isoformat()
    path = log_dir / f"ingress-{day}.jsonl"
    record = {"at": _utcnow(), **record}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def make_handler(db_path: Path, *, freshness_s: int = DEFAULT_FRESHNESS_S,
                 log_dir: Path = DEFAULT_LOG_DIR, secret_lookup=resolve_secret):
    """Build a request-handler class bound to one DB path."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr spam
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            conn = db.open_db(db_path)
            try:
                status, payload = process_request(
                    conn,
                    source_key=self.headers.get(HDR_SOURCE),
                    signature=self.headers.get(HDR_SIGNATURE),
                    timestamp=self.headers.get(HDR_TIMESTAMP),
                    raw_body=raw,
                    freshness_s=freshness_s,
                    secret_lookup=secret_lookup,
                )
            finally:
                conn.close()
            _log_line(log_dir, {
                "source": self.headers.get(HDR_SOURCE),
                "status": status,
                "result": payload.get("status"),
                "reason": payload.get("reason"),
                "envelope_id": payload.get("envelope_id"),
            })
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(db_path: Path, *, host: str = "127.0.0.1", port: int = 8719,
          freshness_s: int = DEFAULT_FRESHNESS_S,
          log_dir: Path = DEFAULT_LOG_DIR) -> HTTPServer:
    """Create (do not yet serve) an HTTPServer bound to loopback only.

    Refuses any non-loopback host — GATE-PUB / INV-4. Returns the server so a
    caller/test can ``serve_forever`` or ``handle_request`` then shut down.
    """
    if host not in ALLOWED_BIND_HOSTS:
        raise BindError(
            f"refusing to bind ingress to {host!r}; loopback only (127.0.0.1)"
        )
    handler = make_handler(db_path, freshness_s=freshness_s, log_dir=log_dir)
    return HTTPServer((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Signed-webhook ingress (loopback only)")
    p.add_argument("--db", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8719)
    p.add_argument("--freshness-s", type=int, default=DEFAULT_FRESHNESS_S)
    args = p.parse_args(argv)
    server = serve(Path(args.db), host=args.host, port=args.port,
                   freshness_s=args.freshness_s)
    print(f"ingress listening on http://{args.host}:{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
