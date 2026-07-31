"""GOV-1526 (P1b of GOV-1523): web-artifact builder + deny-list + service tests.

These run in CI against a *seeded* DB — the real registry is local/vault-only and
never present on a runner, so the suite builds its own gated fixture (a public
publishable row and a promoted reviewer-internal row, both carrying raw vault
paths that the frozen gate must strip). The deny-list tests unpack a freshly
built artifact and assert every §2 clause; negative-control tests poison the
staged tree to prove each clause is a real BUILD FAILURE, not a vacuous pass.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as gate  # noqa: E402
import db  # noqa: E402
import export_web_artifact as ewa  # noqa: E402
import statements as st  # noqa: E402
from accounts import service as accounts_service  # noqa: E402
from accounts import sessions  # noqa: E402
from beta import allowlist as beta_allowlist  # noqa: E402
from beta import http_api as beta_http_api  # noqa: E402
from beta import sessions as beta_sessions  # noqa: E402
from email_gateway import flags  # noqa: E402

# A fake but well-formed 40-char SHA + fixed timestamp => deterministic manifest.
FAKE_COMMIT = "a" * 40
FIXED_TS = "2026-07-21T00:00:00+00:00"


def _seed(conn: sqlite3.Connection) -> None:
    """One public publishable row + one promoted reviewer-internal row.

    Both reference raw vault locators (``/Users/...`` transcript_path, a
    ``file:///Users/...`` provenance URI) so a passing deny-list proves the gate
    stripped them — the lanes are real gated output, not empty stubs.
    """
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class,"
        " original_url) VALUES ('alpine_packet', 'Agenda Packet', 'alpine',"
        " 'document', 'official', 'https://alpinewy.gov/packet.pdf')"
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc)"
        " VALUES (1, '2026-05-08', 'Town Council', '2026-05-08T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title)"
        " VALUES ('alpine:2026-05-08:item-7', 1, 7, 'Fireworks ban — adoption')"
    )
    conn.commit()

    # Public lane: reviewed + evidence + flipped publishable; raw locators MUST strip.
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-public",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "The council adopted the fireworks ban.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
        [
            {
                "to_source_id": "alpine_packet",
                "relation": "substantiates",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "final_url": "https://alpinewy.gov/packet.pdf",
                "archive_url": "https://web.archive.org/web/2026/https://alpinewy.gov/packet.pdf",
                "archive_status": "available",
                "scan_date": "2026-05-09",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "page",
                "page": 3,
                "verification_status": "human_verified",
                "confidence": "high",
                "transcript_path": "/Users/IA/Obsidian Vault/Source-Data/raw.txt",
                "deep_link": "/Users/IA/Raw-PDFs/packet.pdf#page=3",
            }
        ],
    )

    # Reviewer-internal lane: an AI row promoted under reviewer:isaac, still
    # not_publishable, with a file:// vault provenance URI that must strip.
    run_id = "ewa:ai-run"
    ai.create_run(conn, run_id=run_id, input_source_ids=[])
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-reviewer-internal",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "A Town Council special meeting was convened.",
            "produced_by": "ai",
            "layer": "ai_thought_then",
            "ai_extraction_run_id": run_id,
        },
        [
            {
                "to_source_id": "alpine_packet",
                "relation": "references",
                "original_url": "file:///Users/IA/Documents/TownOfAlpine/2024-10-09/x.txt",
                "archive_status": "not_checked",
                "scan_date": "2026-06-12",
                "captured_at_utc": "2026-06-12T03:23:24Z",
                "locator_kind": "page",
                "page": 1,
                "verification_status": "machine_extracted_unreviewed",
                "confidence": "high",
            }
        ],
    )
    gate.register_reviewer(
        conn, "reviewer:isaac", display_name="Isaac",
        registered_by="owner:isaac", note="GOV-1526 artifact test seed",
    )
    gate.promote_statement(
        conn, "stmt-reviewer-internal", reviewer_id="reviewer:isaac",
        decision="approved", reason="source-grounded civic announcement",
        to_verification_status="reviewed_source_linked",
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "registry.db"
    db.apply_migrations(path)
    conn = db.open_db(path)
    _seed(conn)
    conn.close()
    return path


@pytest.fixture()
def staged(db_path: Path) -> Path:
    """A built + extracted artifact tree (deny-list scan runs against this)."""
    files = ewa.stage_files(
        db_path, backend_commit=FAKE_COMMIT, generated_at_utc=FIXED_TS
    )
    root = db_path.parent / "artifact"
    ewa.extract_to(files, root)
    return root


# --- manifest + lanes (§1) --------------------------------------------------


def test_manifest_shape(staged: Path) -> None:
    manifest = json.loads((staged / "manifest.json").read_text())
    assert manifest["backend_commit"] == FAKE_COMMIT
    assert manifest["schema_version"] == ewa.SCHEMA_VERSION
    assert manifest["gate_functions"] == [
        "read_api.published_records", "read_api.reviewer_internal_records"
    ]
    assert manifest["generated_at_utc"] == FIXED_TS
    assert len(manifest["artifact_sha256"]) == 64
    # Honest, real gated counts — both lanes non-empty from the seed.
    assert manifest["row_counts"] == {"published": 1, "reviewer_internal": 1}


def test_manifest_sha_is_reproducible(db_path: Path) -> None:
    a = ewa.stage_files(db_path, backend_commit=FAKE_COMMIT, generated_at_utc=FIXED_TS)
    b = ewa.stage_files(db_path, backend_commit=FAKE_COMMIT, generated_at_utc=FIXED_TS)
    assert json.loads(a["manifest.json"])["artifact_sha256"] == \
        json.loads(b["manifest.json"])["artifact_sha256"]


def test_read_api_not_shipped(staged: Path) -> None:
    """read_api runs at build time only; shipping it would trip clause 1."""
    shipped = {p.name for p in (staged / "service").rglob("*.py")}
    assert "read_api.py" not in shipped
    assert not (staged / "service" / "read_api.py").exists()


# --- deny-list clauses on real gated output (§2) ----------------------------


def test_deny_list_passes_on_real_gated_artifact(staged: Path) -> None:
    assert ewa.deny_list_violations(staged) == []


def test_lanes_are_present_and_stripped(staged: Path) -> None:
    pub_text = (staged / "data/published.json").read_text()
    rev_text = (staged / "data/reviewer_internal.json").read_text()
    # The seeded raw locators do not survive into either lane.
    for needle in ("/Users/", "Obsidian Vault", "TownOfAlpine", "file://"):
        assert needle not in pub_text
        assert needle not in rev_text
    published = json.loads(pub_text)
    assert [r["statement_id"] for r in published] == ["stmt-public"]
    assert all(r["publication_state"] == "publishable" for r in published)


# --- negative controls: each clause is a real build failure -----------------


def test_clause1_absolute_path_fails(staged: Path) -> None:
    (staged / "data/published.json").write_text(
        json.dumps([{"publication_state": "publishable",
                     "src": "/Users/IA/vault/leak.txt"}])
    )
    assert any(v.startswith("clause1") for v in ewa.deny_list_violations(staged))


def test_clause2_not_publishable_in_public_lane_fails(staged: Path) -> None:
    (staged / "data/published.json").write_text(
        json.dumps([{"statement_id": "x", "publication_state": "not_publishable"}])
    )
    assert any(v.startswith("clause2") for v in ewa.deny_list_violations(staged))


def test_clause3_plaintext_email_fails(staged: Path) -> None:
    (staged / "data/reviewer_internal.json").write_text(
        json.dumps([{"contact": "resident@example.com"}])
    )
    assert any(v.startswith("clause3") for v in ewa.deny_list_violations(staged))


def test_clause4_reviewer_note_key_fails(staged: Path) -> None:
    (staged / "data/reviewer_internal.json").write_text(
        json.dumps([{"statement_id": "x", "reviewer_note": "looks fine to me"}])
    )
    assert any(v.startswith("clause4") for v in ewa.deny_list_violations(staged))


def test_clause5_extra_file_fails(staged: Path) -> None:
    (staged / "data/registry_dump.csv").write_text("source_id,raw_local_path\n")
    assert any(v.startswith("clause5") for v in ewa.deny_list_violations(staged))


def test_clause5_missing_required_file_fails(staged: Path) -> None:
    (staged / "data/published.json").unlink()
    assert any("missing required file" in v for v in ewa.deny_list_violations(staged))


# --- build_artifact fail-closed contract ------------------------------------


def test_build_writes_tarball_and_checks(db_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "dist"
    record = ewa.build_artifact(
        db_path, out, backend_commit=FAKE_COMMIT, generated_at_utc=FIXED_TS
    )
    tarball = Path(record["tarball"])
    assert tarball.exists()
    assert tarball.name == f"gw-web-artifact-{FAKE_COMMIT[:12]}.tar.gz"
    with tarfile.open(tarball) as tar:
        members = sorted(m.name for m in tar.getmembers())
    assert "manifest.json" in members
    assert "service/run.py" in members
    assert "service/read_api.py" not in members


def test_build_fails_closed_on_violation(monkeypatch, db_path: Path, tmp_path: Path) -> None:
    """A deny-list hit raises and writes no tarball (fail closed by absence)."""
    real_stage = ewa.stage_files

    def poisoned(*args, **kwargs):
        files = real_stage(*args, **kwargs)
        files["data/published.json"] = b'[{"publication_state": "not_publishable"}]\n'
        return files

    monkeypatch.setattr(ewa, "stage_files", poisoned)
    out = tmp_path / "dist"
    with pytest.raises(ValueError, match="deny-list check failed"):
        ewa.build_artifact(db_path, out, backend_commit=FAKE_COMMIT,
                           generated_at_utc=FIXED_TS)
    assert not out.exists() or not list(out.glob("*.tar.gz"))


# --- service entrypoint: health, routing, gate, bind guard ------------------


def _load_run_module(staged: Path):
    """Import the artifact's service/run.py so health/routing can be exercised."""
    run_path = staged / "service" / "run.py"
    spec = importlib.util.spec_from_file_location("gw_artifact_run", run_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_health_payload_has_no_civic_data(staged: Path) -> None:
    run = _load_run_module(staged)
    payload = run.health_payload()
    assert payload["status"] == "ok"
    assert payload["backend_commit"] == FAKE_COMMIT
    assert payload["schema_version"] == ewa.SCHEMA_VERSION
    # Liveness carries only commit/version/counts — no statements, no records.
    assert set(payload) == {"status", "backend_commit", "schema_version", "row_counts"}


def test_route_health_and_gated_states(staged: Path, db_path: Path) -> None:
    run = _load_run_module(staged)
    conn = db.open_db(db_path)
    try:
        # Health: 200, no DB dependence beyond none.
        status, _ = run.process_request(conn, path="/api/health", authorization=None)
        assert status == 200
        # Notifications: flag off (no row) => constant 404, endpoint invisible.
        status, body = run.process_request(conn, path="/api/notifications", authorization=None)
        assert status == 404 and body == {"error": "not_found"}
        # Reviewer-internal: no session => constant 403 access_denied.
        status, body = run.process_request(conn, path="/api/reviewer-internal", authorization=None)
        assert status == 403 and body == {"error": "access_denied"}
        # Unknown route => 404.
        status, _ = run.process_request(conn, path="/api/nope", authorization=None)
        assert status == 404
    finally:
        conn.close()


def test_reviewer_internal_opens_for_approved_session(staged: Path, db_path: Path) -> None:
    run = _load_run_module(staged)
    conn = db.open_db(db_path)
    try:
        user_id = accounts_service.create_user(conn, email="approved@example.com")
        accounts_service.approve(conn, user_id, owner_decision_ref="GOV-1526-test")
        _, raw_token = sessions.issue_session(conn, user_id)
        status, body = run.process_request(
            conn, path="/api/reviewer-internal",
            authorization=f"Bearer {raw_token}")
        assert status == 200
        records = body["reviewer_internal_records"]
        assert [r["statement_id"] for r in records] == ["stmt-reviewer-internal"]
    finally:
        conn.close()


def test_reviewer_internal_opens_for_beta_cookie_over_http(
    staged: Path,
    db_path: Path,
) -> None:
    """The staged artifact forwards Cookie and serves only after live beta gates."""
    run = _load_run_module(staged)
    conn = db.open_db(db_path)
    try:
        email = "artifact-beta@local.test"
        beta_allowlist.add(
            conn,
            email,
            owner_decision_ref="GOV-1526-beta-cookie-test",
        )
        flags.set_flag(
            conn,
            beta_http_api.BETA_GATE_FLAG,
            enabled=True,
            owner_decision_ref="GOV-1526-beta-cookie-on",
        )
        _, raw_token = beta_sessions.issue(conn, email)

        user_id = accounts_service.create_user(
            conn,
            email="artifact-approved@local.test",
        )
        accounts_service.approve(
            conn,
            user_id,
            owner_decision_ref="GOV-1526-duplicate-authorization-test",
        )
        _, bearer_token = sessions.issue_session(conn, user_id)
    finally:
        conn.close()

    server = run.serve(db_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection(
        server.server_address[0],
        server.server_address[1],
        timeout=5,
    )
    try:
        client.request(
            "GET",
            "/api/reviewer-internal",
            headers={
                "Cookie": f"{beta_http_api.COOKIE_NAME}={raw_token}",
            },
        )
        response = client.getresponse()
        body = json.loads(response.read())
        assert response.status == 200
        assert [
            row["statement_id"]
            for row in body["reviewer_internal_records"]
        ] == ["stmt-reviewer-internal"]

        client.putrequest("GET", "/api/reviewer-internal")
        client.putheader(
            "Cookie",
            f"{beta_http_api.COOKIE_NAME}={raw_token}",
        )
        client.putheader(
            "Cookie",
            f"{beta_http_api.COOKIE_NAME}={raw_token}",
        )
        client.endheaders()
        duplicate_response = client.getresponse()
        duplicate_body = json.loads(duplicate_response.read())
        assert duplicate_response.status == 403
        assert duplicate_body == {"error": "access_denied"}

        client.putrequest("GET", "/api/reviewer-internal")
        client.putheader("Authorization", "")
        client.putheader("Authorization", "Basic unsupported")
        client.putheader(
            "Cookie",
            f"{beta_http_api.COOKIE_NAME}={raw_token}",
        )
        client.endheaders()
        ambiguous_response = client.getresponse()
        ambiguous_body = json.loads(ambiguous_response.read())
        assert ambiguous_response.status == 403
        assert ambiguous_body == {"error": "access_denied"}

        client.putrequest("GET", "/api/reviewer-internal")
        client.putheader("Authorization", f"Bearer {bearer_token}")
        client.putheader("Authorization", "Basic unsupported")
        client.endheaders()
        duplicate_auth_response = client.getresponse()
        duplicate_auth_body = json.loads(duplicate_auth_response.read())
        assert duplicate_auth_response.status == 403
        assert duplicate_auth_body == {"error": "access_denied"}
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _serve_and_get(staged: Path, db_file: Path, routes: list[str]) -> dict[str, int]:
    """Start run.py on loopback, return {route: http_status} for each route."""
    import socket
    import time
    import urllib.error
    import urllib.request

    # Pick a free loopback port up front to avoid collisions.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, str(staged / "service" / "run.py"),
         "--db", str(db_file), "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    statuses: dict[str, int] = {}
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 15
        while time.time() < deadline:  # wait for liveness
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.2)
        else:
            raise AssertionError("service never became live")
        for route in routes:
            try:
                with urllib.request.urlopen(base + route, timeout=5) as r:
                    statuses[route] = r.status
            except urllib.error.HTTPError as exc:
                statuses[route] = exc.code
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return statuses


def test_service_fails_closed_on_unmigrated_db(staged: Path, tmp_path: Path) -> None:
    """An empty (schema-less) DB => health still 200, other routes clean 500.

    A per-request fault (missing table) must degrade to a constant 500, never a
    dropped connection or a leaked traceback.
    """
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()  # exists, no schema
    statuses = _serve_and_get(
        staged, empty, ["/api/health", "/api/notifications"]
    )
    assert statuses["/api/health"] == 200
    assert statuses["/api/notifications"] == 500


def test_bind_guard_refuses_non_loopback(staged: Path, db_path: Path) -> None:
    """The entrypoint refuses any non-loopback bind (loopback-only posture)."""
    proc = subprocess.run(
        [sys.executable, str(staged / "service" / "run.py"),
         "--db", str(db_path), "--host", "0.0.0.0", "--port", "8791"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0
    assert "loopback only" in (proc.stderr + proc.stdout)
