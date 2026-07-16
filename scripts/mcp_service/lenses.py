"""Interpretive lens packs — data, not code (PLAN-2026-AI §3.6, D5/D6).

Three v1 lens packs register as ``kind='lens'`` rows in the existing
``mcp_policy_packs`` surface (write-once per version). A lens is a *reading* of
already-verified, already-cited evidence; it can never mutate a canonical record
because the only write path a lens run has is ``submit_output`` into staging
(D5, structural).

**Fairness by construction (D6).** Every pack shares the *identical*
:data:`SHARED_REQUIREMENTS` and :data:`SHARED_PROHIBITIONS`. The three differ in
exactly one field — the interpretive ``frame`` (and its owner-visible disclosure
label). The fairness regression asserts these constraint sets are symmetric, so
no lens can be given looser rules than another.

**Required labels (fail-closed).** The lens output schema (:data:`LENS_OUTPUT_SCHEMA_ID`)
requires ``lens_id``, ``lens_version``, and ``uncertainty_summary`` among others;
a missing label is ``denied:schema`` at ``submit_output`` time, never a silent
pass. Importing this module registers that schema.

The pack disclosures and prohibitions here are part of what the owner accepted
with PLAN-2026-AI v1.0; changing them post-freeze needs a version bump + fresh
owner visibility (plan §9).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from . import schemas

LENS_VERSION = "1.0.0"
LENS_OUTPUT_SCHEMA_ID = "gov.lens.output"

# Identical for every lens (D6). These are the process guarantees a reader must
# honour regardless of viewpoint.
SHARED_REQUIREMENTS: tuple[str, ...] = (
    "Every claim must be anchored to a specific source in the job's authorized "
    "evidence set (source_anchor required).",
    "Separate interpretation from evidence: the 'interpretation' field is opinion "
    "about the cited facts; it must not restate a fact as if newly established.",
    "State uncertainty explicitly for every claim and summarize it "
    "(uncertainty_summary required).",
    "Include a neutral-comparison note describing how a differently-aligned reader "
    "might weigh the same evidence.",
)

# Identical for every lens (D6). Hard no-gos, enforced additionally by the frozen
# ai_risk_gate.scan_text sweep in the runner.
SHARED_PROHIBITIONS: tuple[str, ...] = (
    "No stereotyping of any group, party, or individual.",
    "No campaigning, persuasion, or get-out-the-vote framing.",
    "No claim to represent all members of a party or movement.",
    "No altering, disputing, or re-characterizing any statement's verification or "
    "publication state.",
)

# The ONLY thing that differs between packs (D6): the interpretive frame + its
# owner-visible disclosure label.
LENSES: dict[str, dict[str, str]] = {
    "lens.libertarian": {
        "label": "Libertarian interpretive lens",
        "frame": "Read the cited evidence through a libertarian frame that "
                 "emphasizes individual liberty, limited government, and "
                 "market-based trade-offs.",
    },
    "lens.republican-historical": {
        "label": "Original / historical-Republican interpretive lens",
        "frame": "Read the cited evidence through an original / historical "
                 "Republican frame that emphasizes constitutional order, civic "
                 "institutions, and continuity with founding-era principles.",
    },
    "lens.liberal-progressive": {
        "label": "Liberal / progressive interpretive lens",
        "frame": "Read the cited evidence through a liberal / progressive frame "
                 "that emphasizes equity, collective provision, and reform of "
                 "existing institutions.",
    },
}

# §3.6 lens output schema. Required labels (lens_id/lens_version/uncertainty_*)
# make an unlabelled output fail closed at submit_output.
LENS_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "lens_id", "lens_version", "disclosure", "interpretation", "claims",
        "evidence_refs", "uncertainty_summary", "neutral_comparison_note",
    ],
    "properties": {
        "lens_id": {"type": "string"},
        "lens_version": {"type": "string"},
        "disclosure": {"type": "string"},
        "interpretation": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "source_anchor", "confidence", "uncertainty"],
                "properties": {
                    "text": {"type": "string"},
                    "source_anchor": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "uncertainty": {"type": "string"},
                },
            },
        },
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "uncertainty_summary": {"type": "string"},
        "neutral_comparison_note": {"type": "string"},
    },
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def register_output_schema() -> None:
    """Register the lens output schema (idempotent). Called at import."""
    if LENS_OUTPUT_SCHEMA_ID not in schemas.registered_ids():
        schemas.register(LENS_OUTPUT_SCHEMA_ID, LENS_VERSION, LENS_OUTPUT_SCHEMA)


def pack_constraints(pack_id: str) -> dict[str, list[str]]:
    """The (requirements, prohibitions) constraint set for a lens.

    Identical across all three lenses by construction — the fairness test asserts
    this symmetry (D6).
    """
    if pack_id not in LENSES:
        raise KeyError(f"unknown lens {pack_id!r}")
    return {
        "requirements": list(SHARED_REQUIREMENTS),
        "prohibitions": list(SHARED_PROHIBITIONS),
    }


def disclosure(pack_id: str) -> dict[str, str]:
    """Owner-visible disclosure block for a lens pack."""
    meta = LENSES[pack_id]
    return {
        "label": meta["label"],
        "statement": (
            f"This is a {meta['label']}. It is one labelled interpretation of "
            "already-verified, source-cited evidence, not a neutral summary and "
            "not a claim of fact. It cannot change any verification or publication "
            "state."
        ),
    }


def rules_template(pack_id: str) -> str:
    """The rendered rules prompt for a lens: shared requirements + prohibitions +
    the pack's interpretive frame. Deterministic (stable ordering)."""
    meta = LENSES[pack_id]
    lines = [meta["frame"], "", "Requirements:"]
    lines += [f"- {r}" for r in SHARED_REQUIREMENTS]
    lines += ["", "Prohibitions:"]
    lines += [f"- {p}" for p in SHARED_PROHIBITIONS]
    return "\n".join(lines)


def content_hash(pack_id: str) -> str:
    """Stable content hash over the pack's disclosure, rules, and schema id."""
    payload = json.dumps(
        {
            "pack_id": pack_id,
            "version": LENS_VERSION,
            "disclosure": disclosure(pack_id),
            "rules_template": rules_template(pack_id),
            "required_output_schema_id": LENS_OUTPUT_SCHEMA_ID,
        },
        sort_keys=True, separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def seed_lens_packs(conn: sqlite3.Connection) -> list[str]:
    """Write the three v1 lens packs write-once into ``mcp_policy_packs``.

    ``INSERT OR IGNORE`` on the (pack_id, version) primary key: re-seeding is a
    no-op and can never overwrite a landed pack (write-once, D5). Returns the
    pack ids seeded (present after the call)."""
    register_output_schema()
    now = _utcnow()
    for pack_id in LENSES:
        conn.execute(
            "INSERT OR IGNORE INTO mcp_policy_packs "
            "(pack_id, version, kind, disclosure, rules_template, "
            " required_output_schema_id, content_hash, created_utc) "
            "VALUES (?, ?, 'lens', ?, ?, ?, ?, ?)",
            (
                pack_id, LENS_VERSION,
                json.dumps(disclosure(pack_id), separators=(",", ":")),
                rules_template(pack_id), LENS_OUTPUT_SCHEMA_ID,
                content_hash(pack_id), now,
            ),
        )
    conn.commit()
    return list(LENSES)


# Registering the schema at import mirrors contracts.py: importing the module
# makes the lens output shape known to submit_output's validation.
register_output_schema()
