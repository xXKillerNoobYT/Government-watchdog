"""Typed read-only resource builders (CONTRACT-2026-MCP §3.1).

Each builder (1) proves the requested id is authorized by the job's input
selector — context minimization, a valid grant for job J cannot read a statement
outside J's selector — then (2) reads the backing table(s) and (3) projects
through the deny-by-default allowlist (:mod:`.allowlists`). Builders never run
the redaction scan themselves; the service choke-point does that once over the
finished payload. Builders also never write.

URI scheme: ``gov-evidence://job/<job_id>/<type>/<id>``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import allowlists
from .errors import DENY_NOT_FOUND, MCPDenied

_EXCERPT_LIMIT = 500


def uri(job_id: str, rtype: str, rid: str) -> str:
    return f"gov-evidence://job/{job_id}/{rtype}/{rid}"


def _excerpt(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= _EXCERPT_LIMIT else text[: _EXCERPT_LIMIT - 1] + "…"


def _selector(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT input_selector FROM mcp_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise MCPDenied(DENY_NOT_FOUND, f"job {job_id!r} not found")
    try:
        sel = json.loads(row["input_selector"] or "{}")
    except Exception:  # noqa: BLE001
        sel = {}
    return sel if isinstance(sel, dict) else {}


def _authorized_source_ids(conn: sqlite3.Connection, job_id: str) -> set[str]:
    """Sources the job may see: those explicitly listed plus those cited by the
    job's authorized statements (a statement's provenance is in-scope)."""
    sel = _selector(conn, job_id)
    allowed = set(sel.get("source_ids", []) or [])
    stmt_ids = sel.get("statement_ids", []) or []
    if stmt_ids:
        marks = ",".join("?" * len(stmt_ids))
        rows = conn.execute(
            f"SELECT DISTINCT to_source_id FROM evidence_links "
            f"WHERE from_node_type = 'statement' AND from_node_id IN ({marks})",
            tuple(stmt_ids),
        ).fetchall()
        allowed.update(r[0] for r in rows if r[0])
    return allowed


def _require(conn: sqlite3.Connection, job_id: str, key: str, rid: str) -> None:
    sel = _selector(conn, job_id)
    if rid not in set(sel.get(key, []) or []):
        raise MCPDenied(DENY_NOT_FOUND, f"{rid!r} not in job {job_id!r} selector")


# ---------------------------------------------------------------------------
# Builders. Each returns an allowlist-projected dict (or raises MCPDenied).
# ---------------------------------------------------------------------------


def job_spec(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM mcp_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise MCPDenied(DENY_NOT_FOUND, f"job {job_id!r} not found")
    row = dict(row)
    sel = _selector(conn, job_id)
    input_uris = [uri(job_id, "evidence.statement", s) for s in sel.get("statement_ids", []) or []]
    input_uris += [uri(job_id, "evidence.segment", s) for s in sel.get("segment_ids", []) or []]
    candidate = {
        "job_id": row["job_id"],
        "area_id": row["area_id"],
        "job_kind": row["job_kind"],
        "input_uris": input_uris,
        "policy_pack_id": row["policy_pack_id"],
        "policy_pack_version": row["policy_pack_version"],
    }
    return allowlists.project("job.spec", candidate)


def _evidence_links_for(conn: sqlite3.Connection, statement_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM evidence_links WHERE from_node_id = ? AND from_node_type = 'statement' "
        "ORDER BY evidence_link_id",
        (statement_id,),
    ).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        candidate = {
            "source_id": r.get("to_source_id"),
            "locator_kind": r.get("locator_kind"),
            "page": r.get("page"),
            "timestamp_seconds": r.get("timestamp_seconds"),
            "section": r.get("section"),
            "paragraph": r.get("paragraph"),
        }
        out.append(allowlists.project("_evidence_link_ref", candidate))
    return out


def statement(conn: sqlite3.Connection, job_id: str, statement_id: str) -> dict[str, Any]:
    _require(conn, job_id, "statement_ids", statement_id)
    row = conn.execute(
        "SELECT s.*, seg.timestamp_seconds AS seg_ts, seg.timestamp_human AS seg_tsh "
        "FROM statements s LEFT JOIN transcript_segments seg ON seg.segment_id = s.segment_id "
        "WHERE s.statement_id = ?",
        (statement_id,),
    ).fetchone()
    if row is None:
        raise MCPDenied(DENY_NOT_FOUND, f"statement {statement_id!r} not found")
    row = dict(row)
    candidate = {
        "statement_id": row["statement_id"],
        "text": row["statement_text"],
        "segment_id": row["segment_id"],
        "agenda_item_id": row["agenda_item_id"],
        "timestamp_seconds": row.get("seg_ts"),
        "timestamp_human": row.get("seg_tsh"),
        "verification_status": row["verification_status"],
        "publication_state": row["publication_state"],
        "evidence_links": _evidence_links_for(conn, statement_id),
    }
    return allowlists.project("evidence.statement", candidate)


def segment(conn: sqlite3.Connection, job_id: str, segment_id: str) -> dict[str, Any]:
    _require(conn, job_id, "segment_ids", segment_id)
    row = conn.execute(
        "SELECT * FROM transcript_segments WHERE segment_id = ?", (segment_id,)
    ).fetchone()
    if row is None:
        raise MCPDenied(DENY_NOT_FOUND, f"segment {segment_id!r} not found")
    row = dict(row)
    # A speaker label crosses only via a resolved attribution's display_label;
    # an unresolved/uncertain attribution yields no label (no wrong attribution).
    attr = conn.execute(
        "SELECT display_label FROM speaker_attributions "
        "WHERE statement_id IN (SELECT statement_id FROM statements WHERE segment_id = ?) "
        "AND attribution_state = 'attributed' AND display_label IS NOT NULL LIMIT 1",
        (segment_id,),
    ).fetchone()
    candidate = {
        "segment_id": row["segment_id"],
        "transcript_id": row["transcript_id"],
        "timestamp_seconds": row["timestamp_seconds"],
        "timestamp_human": row["timestamp_human"],
        "text": _excerpt(row["segment_text"]),
        "speaker_label": attr["display_label"] if attr else None,
    }
    return allowlists.project("evidence.segment", candidate)


def provenance(conn: sqlite3.Connection, job_id: str, source_id: str) -> dict[str, Any]:
    if source_id not in _authorized_source_ids(conn, job_id):
        raise MCPDenied(DENY_NOT_FOUND, f"source {source_id!r} not authorized for job {job_id!r}")
    row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    if row is None:
        raise MCPDenied(DENY_NOT_FOUND, f"source {source_id!r} not found")
    row = dict(row)
    candidate = {
        "source_id": row["source_id"],
        "source_class": row["source_class"],
        "area_id": row["jurisdiction"],
        "captured_at": row["scan_date"],
        "archive_url": row["archive_url"],
        "content_hash": row["raw_sha256"],
        "version": row["verification_status"],
    }
    return allowlists.project("evidence.provenance", candidate)


def policy_pack(conn: sqlite3.Connection, pack_id: str, version: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM mcp_policy_packs WHERE pack_id = ? AND version = ?",
        (pack_id, version),
    ).fetchone()
    if row is None:
        raise MCPDenied(DENY_NOT_FOUND, f"policy pack {pack_id!r}@{version!r} not found")
    row = dict(row)
    try:
        disclosure = json.loads(row["disclosure"] or "{}")
    except Exception:  # noqa: BLE001
        disclosure = {}
    candidate = {
        "pack_id": row["pack_id"],
        "kind": row["kind"],
        "version": row["version"],
        "disclosure": disclosure,
        "rules_template": row["rules_template"],
        "required_output_schema_id": row["required_output_schema_id"],
        "content_hash": row["content_hash"],
    }
    return allowlists.project("policy.pack", candidate)
