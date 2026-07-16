"""Registered contract schemas + the typed tool table (CONTRACT-2026-MCP §3.2).

Importing this module registers every request/response JSON Schema under a
stable ``{schema_id, semver}`` and defines :data:`TOOLS` — the closed set of six
tools, each with its capability scope, the resource type it serves, and whether
it reads or writes. There is deliberately **no** exec/eval/shell/filesystem tool
in this table; the no-shell static guard test asserts the package never imports a
subprocess primitive.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import schemas

SEMVER = "1.0.0"

# --- resource / response schemas ------------------------------------------------

_EVIDENCE_LINK_REF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source_id"],
    "properties": {
        "source_id": {"type": "string"},
        "locator_kind": {"type": "string"},
        "page": {"type": "integer"},
        "timestamp_seconds": {"type": "integer"},
        "section": {"type": "string"},
        "paragraph": {"type": "integer"},
    },
}

_SCHEMAS: dict[str, dict] = {
    "gov.job.spec": {
        "type": "object",
        "additionalProperties": False,
        "required": ["job_id", "job_kind", "input_uris"],
        "properties": {
            "job_id": {"type": "string"},
            "area_id": {"type": "string"},
            "job_kind": {"type": "string"},
            "input_uris": {"type": "array", "items": {"type": "string"}},
            "policy_pack_id": {"type": "string"},
            "policy_pack_version": {"type": "string"},
        },
    },
    "gov.evidence.statement": {
        "type": "object",
        "additionalProperties": False,
        "required": ["statement_id", "text", "verification_status", "publication_state",
                     "evidence_links"],
        "properties": {
            "statement_id": {"type": "string"},
            "text": {"type": "string"},
            "segment_id": {"type": "string"},
            "agenda_item_id": {"type": "string"},
            "timestamp_seconds": {"type": "integer"},
            "timestamp_human": {"type": "string"},
            "verification_status": {"type": "string"},
            "publication_state": {"type": "string"},
            "evidence_links": {"type": "array", "items": _EVIDENCE_LINK_REF},
        },
    },
    "gov.evidence.segment": {
        "type": "object",
        "additionalProperties": False,
        "required": ["segment_id", "transcript_id", "text"],
        "properties": {
            "segment_id": {"type": "string"},
            "transcript_id": {"type": "integer"},
            "char_start": {"type": "integer"},
            "char_end": {"type": "integer"},
            "timestamp_seconds": {"type": "integer"},
            "timestamp_human": {"type": "string"},
            "text": {"type": "string"},
            "speaker_label": {"type": "string"},
        },
    },
    "gov.evidence.provenance": {
        "type": "object",
        "additionalProperties": False,
        "required": ["source_id"],
        "properties": {
            "source_id": {"type": "string"},
            "source_class": {"type": "string"},
            "area_id": {"type": "string"},
            "captured_at": {"type": "string"},
            "archive_url": {"type": "string"},
            "content_hash": {"type": "string"},
            "version": {"type": "string"},
        },
    },
    "gov.policy.pack": {
        "type": "object",
        "additionalProperties": False,
        "required": ["pack_id", "kind", "version", "required_output_schema_id", "content_hash"],
        "properties": {
            "pack_id": {"type": "string"},
            "kind": {"type": "string", "enum": ["lens", "processing"]},
            "version": {"type": "string"},
            "disclosure": {"type": "object", "additionalProperties": True},
            "rules_template": {"type": "string"},
            "required_output_schema_id": {"type": "string"},
            "content_hash": {"type": "string"},
        },
    },
}

# --- tool request schemas -------------------------------------------------------

_REQ_SCHEMAS: dict[str, dict] = {
    "gov.tool.list_job_inputs.req": {
        "type": "object", "additionalProperties": False,
        "required": ["job_id"], "properties": {"job_id": {"type": "string"}},
    },
    "gov.tool.get_statement.req": {
        "type": "object", "additionalProperties": False,
        "required": ["job_id", "statement_id"],
        "properties": {"job_id": {"type": "string"}, "statement_id": {"type": "string"}},
    },
    "gov.tool.get_segment.req": {
        "type": "object", "additionalProperties": False,
        "required": ["job_id", "segment_id"],
        "properties": {"job_id": {"type": "string"}, "segment_id": {"type": "string"}},
    },
    "gov.tool.get_provenance.req": {
        "type": "object", "additionalProperties": False,
        "required": ["job_id", "source_id"],
        "properties": {"job_id": {"type": "string"}, "source_id": {"type": "string"}},
    },
    "gov.tool.get_policy_pack.req": {
        "type": "object", "additionalProperties": False,
        "required": ["pack_id", "version"],
        "properties": {"pack_id": {"type": "string"}, "version": {"type": "string"}},
    },
    "gov.tool.submit_output.req": {
        "type": "object", "additionalProperties": False,
        "required": ["job_id", "output_kind", "body", "policy_pack_id", "policy_pack_version"],
        "properties": {
            "job_id": {"type": "string"},
            "output_kind": {"type": "string"},
            "body": {"type": "object", "additionalProperties": True},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["source_anchor", "confidence", "uncertainty"],
                    "properties": {
                        "source_anchor": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "uncertainty": {"type": "string"},
                    },
                },
            },
            "policy_pack_id": {"type": "string"},
            "policy_pack_version": {"type": "string"},
        },
    },
    "gov.tool.list_job_inputs.res": {
        "type": "object", "additionalProperties": False,
        "required": ["job_id", "resource_uris"],
        "properties": {
            "job_id": {"type": "string"},
            "resource_uris": {"type": "array", "items": {"type": "string"}},
        },
    },
    "gov.tool.submit_output.res": {
        "type": "object", "additionalProperties": False,
        "required": ["output_id", "job_id", "review_state"],
        "properties": {
            "output_id": {"type": "string"},
            "job_id": {"type": "string"},
            "review_state": {"type": "string"},
        },
    },
}


def register_all() -> None:
    for sid, schema in {**_SCHEMAS, **_REQ_SCHEMAS}.items():
        schemas.register(sid, SEMVER, schema)


register_all()


# --- typed tool table (§3.2) ----------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    scope: str
    effect: str          # 'read' | 'write'
    req_schema_id: str
    res_schema_id: str


TOOLS: dict[str, ToolSpec] = {
    "list_job_inputs": ToolSpec(
        "list_job_inputs", "tool:list_job_inputs", "read",
        "gov.tool.list_job_inputs.req", "gov.tool.list_job_inputs.res"),
    "get_statement": ToolSpec(
        "get_statement", "tool:get_statement", "read",
        "gov.tool.get_statement.req", "gov.evidence.statement"),
    "get_segment": ToolSpec(
        "get_segment", "tool:get_segment", "read",
        "gov.tool.get_segment.req", "gov.evidence.segment"),
    "get_provenance": ToolSpec(
        "get_provenance", "tool:get_provenance", "read",
        "gov.tool.get_provenance.req", "gov.evidence.provenance"),
    "get_policy_pack": ToolSpec(
        "get_policy_pack", "tool:get_policy_pack", "read",
        "gov.tool.get_policy_pack.req", "gov.policy.pack"),
    "submit_output": ToolSpec(
        "submit_output", "tool:submit_output", "write",
        "gov.tool.submit_output.req", "gov.tool.submit_output.res"),
}

# Resource-type -> read scope string (exact match, no wildcards).
RESOURCE_SCOPES: dict[str, str] = {
    "job.spec": "resource:job.spec:read",
    "evidence.statement": "resource:evidence.statement:read",
    "evidence.segment": "resource:evidence.segment:read",
    "evidence.provenance": "resource:evidence.provenance:read",
    "policy.pack": "resource:policy.pack:read",
}
