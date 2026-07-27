"""Gated supplied-file intake API (GOV-1576 B3, GOV-1566 chain).

An authenticated upload endpoint that lands a *supplied* file into the raw
object store (B1, :mod:`raw_object_store`) and the file record + provenance
model (B2, :mod:`file_records`) with ``review_state='pending'`` — review before
AI, review before display. It is a thin, testable transport layer: all bytes
persistence lives in B1, all record/provenance rules in B2.

Fail-closed & private-by-default, in the same idiom as
:mod:`beta.http_api`:

  * The owner-gated ``beta_gate_enabled`` feature flag is checked FIRST. While
    the flag is off — or absent, the shipped state — the *whole* surface is a
    constant 404, indistinguishable from routes that do not exist. Merging this
    module activates nothing (INV-5 / D1).
  * Loopback-only bind guard (:func:`serve` refuses any non-loopback host —
    GATE-PUB / INV-4). There is no public path, ever.
  * Authentication is the beta session cookie (HttpOnly+Secure+SameSite=Strict,
    minted by the magic-link flow). The cookie resolves to a normalized email
    which must *still* be on the allowlist; a missing/expired/revoked session or
    a de-allowlisted email is a single neutral 401 (no allowlist signal).

Validation, in order, once authenticated:

  * mime must be on :data:`ALLOWED_MIMES` (web-safe document/image types) → 415;
  * base64 must decode → 400; empty → 400; decoded size over
    :data:`MAX_UPLOAD_BYTES` → 413;
  * the content SHA-256 must not be on the known-bad denylist → 422.

On success the bytes are stored (B1, encrypted at rest, content-addressed) and a
``pending`` record with full provenance is inserted (B2). ``supplied_by`` is the
*authenticated* session email — server-derived, never taken from the body, so a
caller cannot forge who supplied a file. The 201 body is the supplier's receipt
(``file_id``, ``sha256``, ``review_state``, ``deduped``); no vault path, no other
caller's data, nothing that could leak the allowlist.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sqlite3
import sys
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import file_records  # noqa: E402
import raw_object_store  # noqa: E402
from beta import allowlist, common, sessions  # noqa: E402
from beta.http_api import BETA_GATE_FLAG, COOKIE_NAME  # noqa: E402
from email_gateway import flags  # noqa: E402

INTAKE_UPLOAD_ROUTE = "/api/beta/intake/upload"
ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "localhost"})

#: Web-safe supplied-file types. Deliberately small and explicit — anything not
#: on this allow-list is rejected (415). Not an AI/model label; an operator
#: transport policy, mirroring the export deny-list posture.
ALLOWED_MIMES = frozenset({
    "application/pdf",
    "text/plain",
    "text/csv",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
})

#: Hard ceiling on the decoded file size (25 MiB) — civic documents are small;
#: this bounds memory and the raw store. Over-size is a 413, not a silent trim.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: Base64 inflates ~4/3 and rides inside a JSON envelope; cap the raw request a
#: little above that so an over-size body is refused at the socket, never buffered.
_READ_CAP = MAX_UPLOAD_BYTES * 2 + 8192

#: Default raw-object-store root (gitignored vault). Overridable via env for tests
#: / alternate deployments. The AES-256 key is provisioned out-of-band
#: ($GOV_RAWSTORE_KEY_HEX), never committed and never defaulted here.
_STORE_ROOT_ENV = "GOV_RAWSTORE_ROOT"
_DEFAULT_STORE_ROOT = "Vault/raw-object-store"

#: Optional denylist of known-bad content hashes (lowercase sha256 hex), loaded
#: from env as a comma/space separated list. Empty by default; a match is a 422.
_KNOWN_BAD_ENV = "GOV_INTAKE_KNOWN_BAD_SHA256"

# Constant, request-independent bodies — nothing caller-derived may appear here.
BODY_404 = {"error": "not_found"}
BODY_400 = {"error": "bad_request"}
BODY_401 = {"error": "unauthorized"}
BODY_413 = {"error": "payload_too_large"}
BODY_415 = {"error": "unsupported_media_type"}
BODY_422 = {"error": "rejected_known_bad"}
BODY_503 = {"error": "store_unavailable"}

#: Body fields the caller must supply. ``supplied_by`` is intentionally NOT here —
#: it is the authenticated session email, server-derived (provenance integrity).
_REQUIRED_FIELDS = ("area", "source_type", "original_filename", "mime",
                    "content_base64")


class BindError(Exception):
    """Raised on an attempt to bind anywhere but the loopback interface."""


# --- helpers -----------------------------------------------------------------

def cookie_token(cookie_header: str | None) -> str | None:
    """Extract the raw beta session token from a Cookie header, or None."""
    if not cookie_header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(cookie_header)
    except CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel else None


def authenticate(conn: sqlite3.Connection, cookie_header: str | None) -> str | None:
    """Resolve the caller to an allowlisted email, or None (fail-closed).

    None for every failure mode — no cookie, unknown/expired/revoked session, or
    an email that has since been de-allowlisted — so the caller cannot tell them
    apart (no allowlist enumeration signal).
    """
    email = sessions.verify(conn, cookie_token(cookie_header) or "")
    if email is None:
        return None
    if not allowlist.is_allowed(conn, email):
        return None
    return email


def _json_body(raw_body: bytes) -> dict | None:
    """Parse a JSON object body, or None if absent/malformed/not-an-object."""
    if not raw_body:
        return None
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_http_url(value: str) -> bool:
    """True only for an absolute ``http(s)://`` URL with a host.

    This is the exact shape ``origin_url`` (the validated locator column) is
    allowed to hold; anything else a supplier types is free-text prose that
    belongs in ``provenance_note``.
    """
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _route_provenance(origin_url: str | None, provenance_note: str | None,
                      ) -> tuple[str | None, str | None]:
    """Split a supplier's locator + note into ``(origin_url, provenance_note)``.

    Contract (GOV-1625 / GOV-1624): ``origin_url`` may hold ONLY a validated
    ``http(s)`` URL. Interim compatibility — the pre-split frontend still puts
    free-text prose ("handed to me at the June meeting") in ``origin_url``;
    rather than reject it (400) or store prose as a "link", that prose is ROUTED
    into ``provenance_note``. A split-frontend caller may also send
    ``provenance_note`` explicitly. Nothing is dropped: if both a prose
    ``origin_url`` and a distinct explicit note arrive, they are joined so no
    supplier text is lost. Deterministic, no model.
    """
    if origin_url is not None and not _is_http_url(origin_url):
        prose, origin_url = origin_url, None  # prose in the locator slot is a note
        if provenance_note is None:
            provenance_note = prose
        elif prose != provenance_note:
            provenance_note = f"{provenance_note}\n{prose}"
    return origin_url, provenance_note


def load_known_bad(env: dict | None = None) -> frozenset[str]:
    """Read the known-bad sha256 denylist from the environment (lowercased)."""
    import os

    raw = (env or os.environ).get(_KNOWN_BAD_ENV, "") or ""
    parts = raw.replace(",", " ").split()
    return frozenset(p.strip().lower() for p in parts if p.strip())


def build_store():
    """Construct the raw object store from env, or return None (fail-closed).

    Returns None when the AES key or store root is unavailable/invalid, so the
    endpoint answers a clean 503 rather than accepting bytes it cannot preserve.
    Never raises — the caller treats None as "store unavailable".
    """
    import os

    try:
        key = raw_object_store.key_from_env()
    except raw_object_store.RawObjectStoreError:
        return None
    root = Path(os.environ.get(_STORE_ROOT_ENV, _DEFAULT_STORE_ROOT))
    try:
        return raw_object_store.RawObjectStore(root, key=key)
    except (raw_object_store.RawObjectStoreError, OSError):
        return None


# --- pure request core (no sockets) ------------------------------------------

def process_request(conn: sqlite3.Connection, store, *, method: str, path: str,
                    raw_body: bytes = b"", cookie_header: str | None = None,
                    known_bad: frozenset[str] | None = None,
                    ) -> tuple[int, dict, dict]:
    """The unit-testable heart of the intake surface: ``(status, body, headers)``.

    The feature flag is checked before the method/route even matter, so a
    disabled or absent flag yields a constant 404 for every request (fail-closed).
    """
    if not flags.is_enabled(conn, BETA_GATE_FLAG):
        return 404, dict(BODY_404), {}

    route = urlsplit(path).path
    if not (method == "POST" and route == INTAKE_UPLOAD_ROUTE):
        return 404, dict(BODY_404), {}

    # No unauthenticated / public path: a valid allowlisted session is required.
    supplied_by = authenticate(conn, cookie_header)
    if supplied_by is None:
        return 401, dict(BODY_401), {}

    # The store must be configured before we accept any bytes (fail-closed).
    if store is None:
        return 503, dict(BODY_503), {}

    body = _json_body(raw_body)
    if body is None or any(not isinstance(body.get(f), str) or not body[f].strip()
                           for f in _REQUIRED_FIELDS):
        return 400, dict(BODY_400), {}

    mime = body["mime"].strip()
    if mime not in ALLOWED_MIMES:
        return 415, dict(BODY_415), {}

    try:
        data = base64.b64decode(body["content_base64"], validate=True)
    except (binascii.Error, ValueError):
        return 400, dict(BODY_400), {}
    if not data:
        return 400, dict(BODY_400), {}
    if len(data) > MAX_UPLOAD_BYTES:
        return 413, dict(BODY_413), {}

    import hashlib

    sha256 = hashlib.sha256(data).hexdigest()
    if sha256 in (known_bad if known_bad is not None else frozenset()):
        return 422, dict(BODY_422), {}

    # Optional provenance the supplier asserts; captured_at defaults to now.
    captured_at = body.get("captured_at")
    if not (isinstance(captured_at, str) and captured_at.strip()):
        captured_at = common.iso(common.utcnow())
    origin_url = body.get("origin_url")
    origin_url = origin_url.strip() if isinstance(origin_url, str) and origin_url.strip() else None
    provenance_note = body.get("provenance_note")
    provenance_note = (provenance_note.strip()
                       if isinstance(provenance_note, str) and provenance_note.strip()
                       else None)
    # origin_url holds ONLY a validated http(s) URL; non-URL prose (from the
    # pre-split frontend) is routed into provenance_note, never stored as a link.
    origin_url, provenance_note = _route_provenance(origin_url, provenance_note)

    # Persist bytes (B1: content-addressed, immutable, encrypted at rest) then
    # the record (B2: always inserted 'pending', full provenance). supplied_by is
    # the authenticated email, never the request body.
    put = store.put(data, supplied_by=supplied_by,
                    original_filename=body["original_filename"].strip(),
                    captured_at=captured_at)
    try:
        record = file_records.insert_file_record(
            conn,
            area=body["area"].strip(),
            source_type=body["source_type"].strip(),
            original_filename=body["original_filename"].strip(),
            sha256=put.sha256,
            mime=mime,
            byte_size=put.size_bytes,
            supplied_by=supplied_by,
            captured_at=captured_at,
            origin_url=origin_url,
            provenance_note=provenance_note,
        )
    except file_records.FileRecordError:
        # Bytes are already preserved in B1 (immutable); a record failure means
        # bad provenance from the caller — surface a 400, never a partial claim.
        return 400, dict(BODY_400), {}

    return 201, {
        "file_id": record.file_id,
        "sha256": record.sha256,
        "review_state": record.review_state,
        "deduped": put.deduped,
    }, {}


# --- socket layer ------------------------------------------------------------

def make_handler(db_path: Path):
    """Build a request-handler class bound to one DB path.

    The store and denylist are resolved once from the environment; a missing key
    yields ``store=None`` (a clean 503 behind the gate) rather than a start-up
    crash, so the flag-off 404 surface never depends on the key being present.
    """
    store = build_store()
    known_bad = load_known_bad()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr spam
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > _READ_CAP:
                # Refuse an over-size body at the socket — never buffer it.
                return self._send(413, dict(BODY_413))
            raw = self.rfile.read(length) if length else b""
            conn = _open(db_path)
            try:
                status, payload, headers = process_request(
                    conn, store, method="POST", path=self.path, raw_body=raw,
                    cookie_header=self.headers.get("Cookie"), known_bad=known_bad)
            finally:
                conn.close()
            self._send(status, payload, headers)

        def _send(self, status: int, payload: dict, headers: dict | None = None) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _open(db_path: Path) -> sqlite3.Connection:
    import db  # deferred: scripts/db.py, resolvable via the sys.path insert

    return db.open_db(db_path)


def serve(db_path: Path, *, host: str = "127.0.0.1", port: int = 8802) -> HTTPServer:
    """Create (do not yet serve) an HTTPServer bound to loopback only.

    Refuses any non-loopback host — GATE-PUB / INV-4. Returns the server so a
    caller/test can ``serve_forever`` or ``handle_request`` then shut down.
    """
    if host not in ALLOWED_BIND_HOSTS:
        raise BindError(
            f"refusing to bind intake API to {host!r}; loopback only (127.0.0.1)")
    return HTTPServer((host, port), make_handler(db_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Loopback-only gated supplied-file intake API (GOV-1576 B3)")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8802)
    args = parser.parse_args(argv)
    server = serve(args.db, host=args.host, port=args.port)
    print(f"intake API listening on http://{args.host}:{args.port}",
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
