"""GOV-1526 (Phase 1b of GOV-1523): build the self-contained web artifact.

Implements ``Docs/gov1523-artifact-contract-spec.md`` (the 1a contract). Produces
one versioned tarball per pinned backend ref:

    gw-web-artifact-<short_sha>.tar.gz
    ├── manifest.json
    ├── data/
    │   ├── published.json            # public lane  <- read_api.published_records
    │   └── reviewer_internal.json    # gated lane   <- read_api.reviewer_internal_records
    └── service/
        ├── run.py                    # loopback-only auth/notification service + health check
        └── <auth/notification import closure, copied verbatim from scripts/>

Design note (build-time vs. run-time, resolves a §1/§2 tension):
``read_api`` runs HERE, at BUILD time, in the backend repo, to produce the two
JSON lanes. It never ships in the artifact — §2 clause 1 forbids shipping any
file containing ``/Users/``/``/home/``/``/var/``/``/private/``, and
``read_api.py`` contains exactly those strings as its own raw-path leak-detector
constants. The running service serves the pre-built ``data/reviewer_internal.json``
after session auth (``accounts.gate.guard_civic_request``); it does not re-run
``read_api``. The runtime service subset is therefore the auth/notification
import closure only (accounts/, notifications/, email_gateway/, db.py), computed
deterministically below and verified path-clean by the deny-list tests.

Zero AI, zero network. Deterministic file order + normalized tar metadata so the
tarball and its ``artifact_sha256`` are reproducible from the same inputs.

Fail-closed CLI: ``--check`` (default on) runs the deny-list scan against the
staged artifact; ANY violation removes the tarball and exits non-zero, so a
leaking artifact is never attached to a release (fail closed by absence, §2/§6).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent

# --- contract constants (mirror Docs/gov1523-artifact-contract-spec.md §1) ----

SCHEMA_VERSION = 1
GATE_FUNCTIONS = [
    "read_api.published_records",
    "read_api.reviewer_internal_records",
]
#: Package roots whose import closure is packaged into the running service.
#: read_api is deliberately NOT here (see module docstring).
SERVICE_ENTRY_PACKAGES = ("accounts", "notifications", "email_gateway", "beta")

ARTIFACT_PREFIX = "gw-web-artifact-"

# ---------------------------------------------------------------------------
# Deny-list scanner (§2). Shared by the builder's --check and by the 1b tests.
# ---------------------------------------------------------------------------

#: Clause 1 — absolute local paths / vault prefixes that must never ride the wire.
ABSOLUTE_PATH_RE = re.compile(r"/Users/|/home/|/var/|/private/|Obsidian Vault|TownOfAlpine")

#: Clause 3 — an RFC-5322-shaped address. Account/audit rows carry email_hash only.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: Clause 4 — reviewer notes / Lane-5 free-text keys. Decisions surface as
#: labels/status only; a raw note key anywhere in a data lane is a leak.
DENIED_DATA_KEYS = frozenset(
    {"reviewer_note", "note", "reason", "decision_reason", "reviewer_reason", "free_text"}
)

DATA_DIR = "data"
SERVICE_DIR = "service"
MANIFEST_NAME = "manifest.json"
PUBLISHED_NAME = "data/published.json"
REVIEWER_INTERNAL_NAME = "data/reviewer_internal.json"


def _iter_files(root: Path):
    """Yield (posix-relative-path, Path) for every regular file under ``root``."""
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path.relative_to(root).as_posix(), path


def _walk_json_keys(obj):
    """Yield every dict key appearing anywhere in a decoded JSON structure."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _walk_json_keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json_keys(item)


def _walk_json_strings(obj):
    """Yield decoded JSON string keys and values recursively."""

    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                yield key
            yield from _walk_json_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json_strings(item)


def _reject_json_constant(value: str):
    raise ValueError(f"non-finite JSON constant {value!r}")


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def allowed_file_set(root: Path) -> set[str]:
    """The closed allowlist for a staged artifact (§2 clause 5).

    Exactly: the manifest, the two lane files, ``service/run.py``, the
    seedless ``service/schema.sql`` (GOV-1544), and the packaged service
    import closure — nothing else.
    """
    allowed = {MANIFEST_NAME, PUBLISHED_NAME, REVIEWER_INTERNAL_NAME,
               "service/run.py", "service/schema.sql"}
    for rel in compute_service_closure(SCRIPTS_DIR):
        allowed.add(f"{SERVICE_DIR}/{rel.as_posix()}")
    return allowed


def deny_list_violations(root: Path) -> list[str]:
    """Return every deny-list / allowlist violation for a staged artifact tree.

    Empty list == clean. A non-empty result is a BUILD FAILURE (§2): the
    artifact is not attached, so nothing downstream can consume it.
    """
    violations: list[str] = []
    present = {rel for rel, _ in _iter_files(root)}

    # Clause 5 — closed file allowlist (subsumes "no registry / raw-corpus files").
    allowed = allowed_file_set(root)
    for rel in sorted(present - allowed):
        violations.append(f"clause5 unexpected file in artifact: {rel}")
    for rel in sorted(allowed - present):
        violations.append(f"clause5 missing required file: {rel}")

    # Clause 1 — no absolute local paths in ANY file of the tarball.
    for rel, path in _iter_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in ABSOLUTE_PATH_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            violations.append(f"clause1 absolute path in {rel}:{line}: {match.group(0)!r}")

    # Clauses 2/3/4 apply to the data lanes.
    published = _load_json(root / PUBLISHED_NAME)
    reviewer_internal = _load_json(root / REVIEWER_INTERNAL_NAME)

    # Clause 2 — no non-publishable rows in the public lane.
    if isinstance(published, list):
        for i, row in enumerate(published):
            state = row.get("publication_state") if isinstance(row, dict) else None
            if state != "publishable":
                violations.append(
                    f"clause2 published.json[{i}] publication_state={state!r} (must be 'publishable')"
                )
    else:
        violations.append("clause2 published.json is not a JSON array")

    # Clauses 1/3/4 on decoded data prevent JSON escapes from hiding a path,
    # plaintext email address, or reviewer-note field from the raw-text scan.
    for name, lane in ((PUBLISHED_NAME, published), (REVIEWER_INTERNAL_NAME, reviewer_internal)):
        for value in _walk_json_strings(lane):
            for match in ABSOLUTE_PATH_RE.finditer(value):
                violations.append(
                    f"clause1 decoded absolute path in {name}: "
                    f"{match.group(0)!r}"
                )
            for match in EMAIL_RE.finditer(value):
                violations.append(
                    f"clause3 plaintext email in {name}: {match.group(0)!r}"
                )
        for key in _walk_json_keys(lane):
            if key in DENIED_DATA_KEYS:
                violations.append(f"clause4 reviewer-note key {key!r} in {name}")

    return violations


def _load_json(path: Path):
    if not path.exists():
        return None
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_json_keys,
    )


# ---------------------------------------------------------------------------
# Service import-closure computation (runtime subset only — no read_api).
# ---------------------------------------------------------------------------


def compute_service_closure(scripts_dir: Path) -> list[Path]:
    """Deterministic local-import closure of the auth/notification service.

    Starts from the entry packages and follows only *local* module imports
    (top-level ``scripts/*.py`` files and ``scripts/*/`` packages). Returns a
    sorted list of paths relative to ``scripts_dir``. If a future edit adds an
    import that pulls a path-dirty module (e.g. read_api) into the closure, the
    deny-list scan fails the build — fail closed, by construction.
    """
    local_mods: set[str] = set()
    for path in scripts_dir.iterdir():
        if path.suffix == ".py":
            local_mods.add(path.stem)
        elif path.is_dir() and (path / "__init__.py").exists():
            local_mods.add(path.name)

    def module_file(mod: str) -> Path | None:
        parts = mod.split(".")
        for cand in (
            scripts_dir.joinpath(*parts).with_suffix(".py"),
            scripts_dir.joinpath(*parts) / "__init__.py",
            scripts_dir / (parts[0] + ".py"),
            scripts_dir / parts[0] / "__init__.py",
        ):
            if cand.exists():
                return cand
        return None

    def imported_top_levels(path: Path) -> set[str]:
        out: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    out.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                out.add(node.module.split(".")[0])
        return out

    queue: list[Path] = []
    for pkg in SERVICE_ENTRY_PACKAGES:
        queue.extend((scripts_dir / pkg).glob("*.py"))

    files: set[Path] = set()
    while queue:
        current = queue.pop()
        if current in files:
            continue
        files.add(current)
        for imp in imported_top_levels(current):
            if imp in local_mods:
                target = module_file(imp)
                if target and target not in files:
                    queue.append(target)
                    if target.name == "__init__.py":
                        queue.extend(target.parent.glob("*.py"))

    return sorted(p.relative_to(scripts_dir) for p in files)


# ---------------------------------------------------------------------------
# service/run.py — the loopback-only entrypoint written into every artifact.
# ---------------------------------------------------------------------------

RUN_PY = r'''"""Loopback-only auth/notification service + health check (GOV-1526 artifact).

Single documented entrypoint for the pinned backend web artifact:

    run.py --db <path> --port <port> [--host 127.0.0.1]

Routes:
  GET    /api/health            liveness for the website build/proxy; zero civic data.
  GET    /api/notifications     the existing GOV-771 endpoint (flag-gated, session-auth).
  GET    /api/reviewer-internal approved-tier civic data (accounts.gate) -> the pre-built
                                data/reviewer_internal.json lane; 403 otherwise.
  POST   /api/beta/magic-link/request  gated-beta front door (GOV-801/GOV-1544 wiring);
  GET    /api/beta/magic-link/verify   every /api/beta/* route answers a constant 404
  POST   /api/beta/waitlist            until the owner-gated ``beta_gate_enabled``
  DELETE /api/beta/sessions/current    flag row is enabled (fail closed, D1).

Bind guard: refuses any host outside ALLOWED_BIND_HOSTS (127.0.0.1/localhost) —
the service is never publicly addressable. All frozen serving logic is reused
(notifications.http_api, accounts.gate); nothing is re-implemented here.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

SERVICE_DIR = Path(__file__).resolve().parent
ARTIFACT_ROOT = SERVICE_DIR.parent
sys.path.insert(0, str(SERVICE_DIR))

ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "localhost"})

HEALTH_ROUTE = "/api/health"
NOTIFICATIONS_ROUTE = "/api/notifications"
REVIEWER_INTERNAL_ROUTE = "/api/reviewer-internal"
BETA_ROUTE_PREFIX = "/api/beta/"

BODY_404 = {"error": "not_found"}
BODY_500 = {"error": "internal_error"}
DENIED_BODY = {"error": "access_denied"}


class BindError(Exception):
    """Raised on an attempt to bind anywhere but the loopback interface."""


def _manifest() -> dict:
    path = ARTIFACT_ROOT / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def health_payload() -> dict:
    """Zero-civic-data liveness body (backend_commit + schema_version only)."""
    manifest = _manifest()
    return {
        "status": "ok",
        "backend_commit": manifest.get("backend_commit"),
        "schema_version": manifest.get("schema_version"),
        "row_counts": manifest.get("row_counts"),
    }


def reviewer_internal_payload(conn: sqlite3.Connection, authorization: str | None) -> tuple[int, dict]:
    """Approved-tier gate -> the pre-built reviewer_internal lane, or 403.

    The civic query is a static-file read that lives BELOW the gate, so no code
    path reaches the data unauthorized (same shape as accounts.gate's worked
    example). Reuses the single fail-closed authorization path; no new logic.
    """
    from accounts import gate

    token = authorization[len("Bearer "):] if authorization and authorization.startswith("Bearer ") else None
    status, principal_or_body = gate.guard_civic_request(conn, token)
    if status != 200:
        return status, dict(DENIED_BODY)
    lane = ARTIFACT_ROOT / "data" / "reviewer_internal.json"
    records = json.loads(lane.read_text(encoding="utf-8")) if lane.exists() else []
    return 200, {"reviewer_internal_records": records}


def beta_request(conn: sqlite3.Connection, *, method: str, path: str,
                 raw_body: bytes, cookie_header: str | None,
                 ip_hint: str | None,
                 verify_base_url: str | None = None) -> tuple[int, dict, dict]:
    """Bridge to the gated-beta front door (GOV-1544 wiring; no new logic).

    Delegates to ``beta.http_api.process_request`` — flag check FIRST, so the
    whole /api/beta/* surface is a constant 404 until ``beta_gate_enabled`` is
    appended by an owner-gated decision. Returns (status, body, headers);
    headers carry the Set-Cookie for verify/sign-out.
    """
    from beta import http_api as beta_http_api
    from beta import service as beta_service

    return beta_http_api.process_request(
        conn, method=method, path=path, raw_body=raw_body,
        cookie_header=cookie_header, ip_hint=ip_hint,
        verify_base_url=verify_base_url or beta_service.DEFAULT_VERIFY_BASE_URL)


def process_request(conn: sqlite3.Connection, *, path: str, authorization: str | None) -> tuple[int, dict]:
    """Pure request core (no sockets) — the unit-testable router."""
    route = urlsplit(path).path
    if route == HEALTH_ROUTE:
        return 200, health_payload()
    if route == NOTIFICATIONS_ROUTE:
        from notifications import http_api
        return http_api.process_request(conn, path=path, authorization=authorization)
    if route == REVIEWER_INTERNAL_ROUTE:
        return reviewer_internal_payload(conn, authorization)
    return 404, dict(BODY_404)


def _open(db_path: Path) -> sqlite3.Connection:
    import db
    return db.open_db(db_path)


def make_handler(db_path: Path, *, verify_base_url: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence default stderr spam
            pass

        def _dispatch(self, method: str) -> None:
            route = urlsplit(self.path).path
            headers: dict = {}
            # Always drain the request body so HTTP framing stays correct even
            # for routes that ignore it (e.g. flag-off constant 404s).
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw_body = self.rfile.read(length) if length else b""
            # /api/health needs no DB, so it answers even if the DB is absent.
            if method == "GET" and route == HEALTH_ROUTE:
                status, payload = 200, health_payload()
            else:
                # Fail closed on ANY handler error: a constant 500 body, never a
                # leaked traceback and never a dropped connection. The proxy maps
                # a down service to 502 (§6); a per-request fault must not crash
                # the loop or expose internals.
                try:
                    conn = _open(db_path)
                    try:
                        if route.startswith(BETA_ROUTE_PREFIX):
                            from beta import common as beta_common

                            # ip_hint is computed at the boundary — a raw IP
                            # never crosses into the service or audit layers.
                            ip_hint = beta_common.ip_hint(
                                self.client_address[0]
                                if self.client_address else None)
                            status, payload, headers = beta_request(
                                conn, method=method, path=self.path,
                                raw_body=raw_body,
                                cookie_header=self.headers.get("Cookie"),
                                ip_hint=ip_hint,
                                verify_base_url=verify_base_url)
                        elif method == "GET":
                            status, payload = process_request(
                                conn, path=self.path,
                                authorization=self.headers.get("Authorization"))
                        else:
                            status, payload = 404, dict(BODY_404)
                    finally:
                        conn.close()
                except Exception:  # noqa: BLE001 — defense in depth at the boundary
                    status, payload, headers = 500, dict(BODY_500), {}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return Handler


def serve(db_path: Path, *, host: str = "127.0.0.1", port: int = 8791,
          verify_base_url: str | None = None) -> HTTPServer:
    """Create (do not yet serve) an HTTPServer bound to loopback only."""
    if host not in ALLOWED_BIND_HOSTS:
        raise BindError(
            f"refusing to bind web-artifact service to {host!r}; loopback only (127.0.0.1)"
        )
    return HTTPServer((host, port),
                      make_handler(db_path, verify_base_url=verify_base_url))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Loopback-only auth/notification service + health check (GOV-1526)")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--verify-base-url", default=None,
                        help="public origin baked into magic-link emails "
                             "(default: the beta service's loopback default)")
    args = parser.parse_args(argv)
    # INFO logs to stderr: the hash-only send audit trail (GOV-1544 F2) must
    # reach platform logs. No log line anywhere in the service may carry a
    # plaintext email address — enforced by tests and the e2e log sweep.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    # GOV-1544 F2: env supplies SMTP *credentials only*; complete+valid env
    # registers the adapter, but every send still resolves through the
    # owner-gated email_adapter_enabled flag (fail closed either way).
    from email_gateway import adapters as email_adapters

    email_adapters.register_smtp_from_env()
    server = serve(args.db, host=args.host, port=args.port,
                   verify_base_url=args.verify_base_url)
    print(f"web-artifact service listening on http://{args.host}:{args.port}{HEALTH_ROUTE}",
          file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _json_bytes(obj) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _resolve_backend_commit(explicit: str | None) -> str:
    if explicit:
        commit = explicit.strip()
    else:
        commit = subprocess.check_output(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
    if len(commit) != 40 or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"backend_commit must be a full 40-char SHA, got {commit!r}")
    return commit


def _content_digest(files: dict[str, bytes]) -> str:
    """sha256 over a deterministic (path, bytes) ordering, excluding manifest.json.

    Hashing file *contents* (not the gzip stream) keeps the digest stable across
    machines: gzip embeds a timestamp, so the tarball bytes are not reproducible,
    but the file contents are. A verifier recomputes with the same rule.
    """
    hasher = hashlib.sha256()
    for rel in sorted(files):
        if rel == MANIFEST_NAME:
            continue
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(files[rel])
        hasher.update(b"\0")
    return hasher.hexdigest()


def schema_sql_bytes(scripts_dir: Path = SCRIPTS_DIR) -> bytes:
    """Seedless DB schema (GOV-1543 §2 build-stage output; GOV-1544 wiring).

    Migrations never ship in the artifact, but the deployed unit must be able
    to initialize an EMPTY ``/data/gw.db`` (accounts/flags/waitlist/outbox
    only — zero rows, zero civic data). Apply the repo's migrations to a
    throwaway DB and dump ``sqlite_master`` DDL in creation order.
    """
    import tempfile

    sys.path.insert(0, str(scripts_dir))
    import db  # noqa: E402

    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "schema.db"
        db.apply_migrations(tmp_db)
        import sqlite3

        conn = sqlite3.connect(tmp_db)
        try:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master"
                " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        finally:
            conn.close()
    return ("\n\n".join(sql + ";" for (sql,) in rows) + "\n").encode("utf-8")


def stage_files(
    db_path: Path,
    *,
    backend_commit: str,
    generated_at_utc: str,
    scripts_dir: Path = SCRIPTS_DIR,
) -> dict[str, bytes]:
    """Produce the full in-memory {relpath: bytes} map of the artifact.

    ``read_api`` is imported and executed HERE (build time) to project the two
    lanes; it is never copied into ``service/``.
    """
    sys.path.insert(0, str(scripts_dir))
    import db  # noqa: E402  (scripts/db.py, resolvable via the sys.path insert)
    import read_api  # noqa: E402

    with db.open_db(db_path) as conn:
        published = read_api.published_records(conn)
        reviewer_internal = read_api.reviewer_internal_records(conn)

    files: dict[str, bytes] = {}
    files[PUBLISHED_NAME] = _json_bytes(published)
    files[REVIEWER_INTERNAL_NAME] = _json_bytes(reviewer_internal)

    # Service code: the entrypoint + the deterministic import closure.
    files["service/run.py"] = RUN_PY.encode("utf-8")
    files["service/schema.sql"] = schema_sql_bytes(scripts_dir)
    for rel in compute_service_closure(scripts_dir):
        files[f"{SERVICE_DIR}/{rel.as_posix()}"] = (scripts_dir / rel).read_bytes()

    manifest = {
        "backend_commit": backend_commit,
        "generated_at_utc": generated_at_utc,
        "schema_version": SCHEMA_VERSION,
        "gate_functions": GATE_FUNCTIONS,
        "row_counts": {
            "published": len(published),
            "reviewer_internal": len(reviewer_internal),
        },
    }
    manifest["artifact_sha256"] = _content_digest(files)
    files[MANIFEST_NAME] = _json_bytes(manifest)
    return files


def write_tarball(files: dict[str, bytes], out_path: Path) -> Path:
    """Write ``files`` to a reproducible gzip tarball (normalized member metadata)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 in the gzip header + normalized tar members => byte-stable given
    # identical inputs (the artifact_sha256 already covers content regardless).
    with open(out_path, "wb") as raw:
        import gzip

        # Empty filename avoids embedding the output path in the gzip header.
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:  # type: ignore[arg-type]
                for rel in sorted(files):
                    data = files[rel]
                    info = tarfile.TarInfo(name=rel)
                    info.size = len(data)
                    info.mtime = 0
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
    return out_path


def extract_to(files: dict[str, bytes], root: Path) -> Path:
    """Materialize the in-memory file map to a directory (for the deny-list scan)."""
    for rel, data in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return root


def build_artifact(
    db_path: Path,
    out_dir: Path,
    *,
    backend_commit: str | None = None,
    generated_at_utc: str | None = None,
    check: bool = True,
    scripts_dir: Path = SCRIPTS_DIR,
) -> dict:
    """Build + (optionally) deny-list-check the artifact. Returns a run record.

    On a deny-list violation the tarball is removed and ``ValueError`` is raised
    (fail closed — a leaking artifact is never left on disk for a release job).
    """
    commit = _resolve_backend_commit(backend_commit)
    generated = generated_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")
    files = stage_files(
        db_path, backend_commit=commit, generated_at_utc=generated, scripts_dir=scripts_dir
    )

    if check:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = extract_to(files, Path(tmp))
            violations = deny_list_violations(root)
        if violations:
            raise ValueError(
                "deny-list check failed; artifact NOT written:\n  "
                + "\n  ".join(violations)
            )

    manifest = json.loads(files[MANIFEST_NAME])
    tarball = out_dir / f"{ARTIFACT_PREFIX}{commit[:12]}.tar.gz"
    write_tarball(files, tarball)
    return {
        "tarball": str(tarball),
        "manifest": manifest,
        "file_count": len(files),
        "files": sorted(files),
    }


def _main(argv: list[str] | None = None) -> int:
    import db

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH,
                        help="registry DB to project the two lanes from")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "dist",
                        help="directory the tarball is written to (gitignored)")
    parser.add_argument("--backend-commit", default=None,
                        help="full 40-char SHA (default: git HEAD of this repo)")
    parser.add_argument("--generated-at", default=None,
                        help="ISO-8601 UTC build timestamp (default: now)")
    parser.add_argument("--no-check", dest="check", action="store_false",
                        help="skip the deny-list scan (NOT for release builds)")
    args = parser.parse_args(argv)

    record = build_artifact(
        args.db, args.out_dir,
        backend_commit=args.backend_commit,
        generated_at_utc=args.generated_at,
        check=args.check,
    )
    print(json.dumps(record["manifest"], indent=2, sort_keys=True))
    print(f"\nwrote {record['tarball']} ({record['file_count']} files)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
