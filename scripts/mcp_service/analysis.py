"""Multi-lens analysis runner (PLAN-2026-AI §3.7, D5/D6, AM-3).

The orchestration that ties routing, lenses, and the frozen risk gate together
while guaranteeing a lens run cannot touch a canonical fact:

1. **Assemble evidence ONCE (D6).** :func:`assemble_evidence` reads the job's
   authorized statements/segments through the same allowlist-projecting resource
   builders the MCP boundary uses, runs the frozen redaction scan over the
   result, and content-hashes it. Every lens consumes that identical context, so
   fairness is structural — the fairness test asserts the hash is equal across
   all three lenses.
2. **Per lens: route → schema-validate → risk sweep → submit (D5).** Generation
   goes through :func:`routing.route_and_generate` (the single chokepoint, budget
   + health + audit enforced). The output body is validated against the lens
   schema (missing label ⇒ ``denied:schema``), swept with the frozen
   ``ai_risk_gate.scan_text`` (any finding ⇒ ``validation:rejected``, never
   usable), and written through ``submit_output`` into ``mcp_job_outputs``
   staging.

The ONLY persistence this module performs is the ``submit_output`` staging write
plus a review_state bookkeeping UPDATE on that same staging row. It never
INSERTs/UPDATEs a canonical table — the gate-bypass test proves the canonical
tables are byte-identical before and after a full run (AM-3).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import capability, lenses, redaction, resources, routing, schemas, service
from .errors import DENY_NOT_FOUND, MCPDenied
from .providers.base import ProviderAdapter

DEFAULT_JOB_KIND = "lens_analysis"
OUTPUT_KIND = "lens_analysis"
_MAX_CLAIMS = 8


@dataclass(frozen=True)
class EvidenceContext:
    parts: list[str]
    evidence_hash: str
    evidence_refs: list[str]
    authorized_anchors: list[str]
    # (source_anchor, statement excerpt) pairs the runner turns into claims.
    claim_seeds: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class LensRun:
    lens_id: str
    lens_version: str
    validation: str            # 'accepted' | 'rejected'
    output_id: str | None
    provider_id: str | None
    evidence_hash: str
    findings: list[dict[str, Any]]


def assemble_evidence(conn: sqlite3.Connection, job_id: str) -> EvidenceContext:
    """Assemble + hash the job's authorized evidence ONCE (D6).

    Uses the allowlist-projecting resource builders (so no raw path/PII can be
    in the context) and re-runs the frozen redaction scan as defense-in-depth.
    The hash covers only the evidence — never any lens frame — so it is identical
    for every lens over the same job.
    """
    selector = resources._selector(conn, job_id)
    statement_ids = [str(s) for s in selector.get("statement_ids", []) or []]
    segment_ids = [str(s) for s in selector.get("segment_ids", []) or []]

    projected: list[dict[str, Any]] = []
    parts: list[str] = []
    refs: list[str] = []
    anchors: set[str] = set()
    claim_seeds: list[dict[str, str]] = []

    for sid in statement_ids:
        stmt = resources.statement(conn, job_id, sid)
        projected.append({"kind": "statement", "data": stmt})
        refs.append(resources.uri(job_id, "evidence.statement", sid))
        anchors.add(sid)
        parts.append(f"[statement {sid}] {stmt.get('text', '')}")
        for link in stmt.get("evidence_links", []):
            src = link.get("source_id")
            if src:
                anchors.add(src)
                claim_seeds.append({"source_anchor": src,
                                    "excerpt": stmt.get("text", "")[:200]})
    for seg in segment_ids:
        segment = resources.segment(conn, job_id, seg)
        projected.append({"kind": "segment", "data": segment})
        refs.append(resources.uri(job_id, "evidence.segment", seg))
        parts.append(f"[segment {seg}] {segment.get('text', '')}")

    # Defense-in-depth: the boundary already stripped raw columns; re-scan the
    # assembled context so a marker embedded in a value fails closed here too.
    redaction.assert_clean(projected)

    canonical = json.dumps(projected, sort_keys=True, separators=(",", ":"))
    evidence_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # If nothing was cited, seed one anchor from the first authorized statement so
    # a claim can still resolve inside the authorized set.
    if not claim_seeds and statement_ids:
        claim_seeds.append({"source_anchor": statement_ids[0], "excerpt": ""})
    return EvidenceContext(
        parts=parts, evidence_hash=evidence_hash, evidence_refs=refs,
        authorized_anchors=sorted(anchors), claim_seeds=claim_seeds[:_MAX_CLAIMS],
    )


def _build_lens_body(
    pack_id: str, interpretation: str, evidence: EvidenceContext
) -> dict[str, Any]:
    """Assemble a schema-valid lens output body labelled with lens/version."""
    disclosure = lenses.disclosure(pack_id)
    claims = [
        {
            "text": f"Under {pack_id}, the cited record supports: "
                    f"{seed['excerpt'] or interpretation[:80]}",
            "source_anchor": seed["source_anchor"],
            "confidence": "low",
            "uncertainty": "Interpretive reading; reasonable readers may weigh "
                           "this evidence differently.",
        }
        for seed in evidence.claim_seeds
    ]
    return {
        "lens_id": pack_id,
        "lens_version": lenses.LENS_VERSION,
        "disclosure": disclosure["statement"],
        "interpretation": interpretation,
        "claims": claims,
        "evidence_refs": list(evidence.evidence_refs),
        "uncertainty_summary": "All claims are low-confidence interpretations of "
                               "verified, source-cited evidence.",
        "neutral_comparison_note": "A differently-aligned reader could emphasize "
                                   "other cited facts; verification and publication "
                                   "state are unchanged by this reading.",
    }


def run_multi_lens(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    adapters: Mapping[str, ProviderAdapter],
    token: str,
    lens_ids: list[str] | None = None,
    job_kind: str = DEFAULT_JOB_KIND,
    context_class: str = routing.CONTEXT_LOCAL_ONLY,
    policy_id: str | None = None,
    degrade_threshold: int = routing.health.DEFAULT_DEGRADE_THRESHOLD,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Run every lens over one shared evidence context; return a run summary.

    ``token`` is a capability token carrying ``tool:submit_output`` scope for
    ``job_id`` (the caller/CLI is the composition root that mints it). Each lens's
    output is validated, risk-swept, and staged; the return value never contains
    raw evidence — only ids, hashes, and validation verdicts.
    """
    lenses.register_output_schema()
    lens_ids = lens_ids if lens_ids is not None else list(lenses.LENSES)
    area_id = _area_id(conn, job_id)
    evidence = assemble_evidence(conn, job_id)

    runs: list[LensRun] = []
    for pack_id in lens_ids:
        pack = _load_pack(conn, pack_id)
        # Context = shared evidence + this lens's rules. The evidence hash covers
        # only the evidence (identical across lenses); the rules are instruction.
        context_parts = list(evidence.parts) + [pack["rules_template"]]
        route = routing.route_and_generate(
            conn, job_kind=job_kind, context_class=context_class, adapters=adapters,
            context_parts=context_parts, policy_id=policy_id, job_id=job_id,
            area_id=area_id, lens_version=lenses.LENS_VERSION,
            degrade_threshold=degrade_threshold, now_utc=now_utc,
        )
        body = _build_lens_body(pack_id, route.result.text, evidence)
        # Fail-closed schema gate: a missing label raises denied:schema here.
        schemas.validate(body, lenses.LENS_OUTPUT_SCHEMA_ID, lenses.LENS_VERSION)

        # Frozen risk sweep over the generated body (AM-3 content guard).
        findings = redaction.scan_findings(body)

        args = {
            "job_id": job_id, "output_kind": OUTPUT_KIND, "body": body,
            "claims": [
                {"source_anchor": c["source_anchor"], "confidence": c["confidence"],
                 "uncertainty": c["uncertainty"]}
                for c in body["claims"]
            ],
            "policy_pack_id": pack_id, "policy_pack_version": lenses.LENS_VERSION,
        }
        res = service.call_tool(conn, "submit_output", args, token, job_id=job_id)
        output_id = res["output_id"]
        validation = "accepted"
        if findings:
            # Staging bookkeeping only: mark the draft rejected so it is never
            # usable. Never touches a canonical table.
            conn.execute(
                "UPDATE mcp_job_outputs SET review_state = 'rejected' WHERE output_id = ?",
                (output_id,),
            )
            conn.commit()
            validation = "rejected"

        runs.append(LensRun(
            lens_id=pack_id, lens_version=lenses.LENS_VERSION, validation=validation,
            output_id=output_id, provider_id=route.provider_id,
            evidence_hash=evidence.evidence_hash, findings=findings,
        ))

    return {
        "job_id": job_id,
        "evidence_hash": evidence.evidence_hash,
        "lens_count": len(runs),
        "runs": [
            {"lens_id": r.lens_id, "lens_version": r.lens_version,
             "validation": r.validation, "output_id": r.output_id,
             "provider_id": r.provider_id, "evidence_hash": r.evidence_hash,
             "finding_categories": sorted({f.get("category", "?") for f in r.findings})}
            for r in runs
        ],
    }


def mint_submit_token(conn: sqlite3.Connection, job_id: str, *, ttl_seconds: int = 3600) -> str:
    """Mint a minimal-scope submit_output grant for the runner (composition-root helper)."""
    _, token = capability.mint_grant(
        conn, job_id=job_id, scopes=["tool:submit_output"], ttl_seconds=ttl_seconds)
    return token


def _area_id(conn: sqlite3.Connection, job_id: str) -> str | None:
    row = conn.execute("SELECT area_id FROM mcp_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row["area_id"] if row else None


def _load_pack(conn: sqlite3.Connection, pack_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM mcp_policy_packs WHERE pack_id = ? AND version = ? AND kind = 'lens'",
        (pack_id, lenses.LENS_VERSION),
    ).fetchone()
    if row is None:
        raise MCPDenied(
            DENY_NOT_FOUND, f"lens pack {pack_id!r}@{lenses.LENS_VERSION} not seeded")
    return dict(row)
