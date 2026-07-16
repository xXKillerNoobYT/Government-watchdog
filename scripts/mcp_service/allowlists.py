"""Deny-by-default field allowlists per resource type (CONTRACT-2026-MCP §3.1, D3).

This is the *structural* half of the two-layer boundary. For each resource type
there is an exhaustive set of boundary field names; :func:`project` builds the
outgoing dict from ONLY those names. A field not listed here does not exist at
the boundary — ``local_path``, ``raw_local_path``, ``local_note_path``,
``transcript_path``, reviewer ids/notes, and raw registry columns are
structurally unreachable because no allowlist names them.

The redaction scan (:mod:`.redaction`, D2) runs *after* projection as an
independent backstop: if an allowlisted field's *value* were to embed a raw
marker, the scanner fail-closes even though the shape is legal.
"""

from __future__ import annotations

from typing import Any

# Boundary field names, per §3.1 "Allowlisted fields" column. Exhaustive.
ALLOWLISTS: dict[str, frozenset[str]] = {
    "job.spec": frozenset(
        {"job_id", "area_id", "job_kind", "input_uris", "policy_pack_id",
         "policy_pack_version"}
    ),
    "evidence.statement": frozenset(
        {"statement_id", "text", "segment_id", "agenda_item_id",
         "timestamp_seconds", "timestamp_human", "verification_status",
         "publication_state", "evidence_links"}
    ),
    "evidence.segment": frozenset(
        {"segment_id", "transcript_id", "char_start", "char_end",
         "timestamp_seconds", "timestamp_human", "text", "speaker_label"}
    ),
    "evidence.provenance": frozenset(
        {"source_id", "source_class", "area_id", "captured_at", "archive_url",
         "content_hash", "version"}
    ),
    "policy.pack": frozenset(
        {"pack_id", "kind", "version", "disclosure", "rules_template",
         "required_output_schema_id", "content_hash"}
    ),
    # Nested object inside evidence.statement.evidence_links[*].
    "_evidence_link_ref": frozenset(
        {"source_id", "locator_kind", "page", "timestamp_seconds", "section",
         "paragraph"}
    ),
}


class UnknownResourceType(KeyError):
    """A resource type with no registered allowlist (deny-by-default)."""


def allowed_fields(resource_type: str) -> frozenset[str]:
    try:
        return ALLOWLISTS[resource_type]
    except KeyError:
        raise UnknownResourceType(resource_type)


def project(resource_type: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict containing ONLY the allowlisted keys of ``candidate``.

    Deny-by-default: any key not in the type's allowlist is dropped, and ``None``
    values are omitted so an absent locator never fabricates a field. This is the
    single structural choke-point — resource builders pass a superset and trust
    projection to strip anything not explicitly named.
    """
    allow = allowed_fields(resource_type)
    return {k: v for k, v in candidate.items() if k in allow and v is not None}
