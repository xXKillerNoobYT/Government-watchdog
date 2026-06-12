"""Lane-2 AI extraction adapter + gateway-run ledger (GOV-89, Slice 3 B).

Maps the GOV-88 (Slice 3 A) interface design
(``Docs/stage3-ai-gateway-gap-analysis.md`` §2/§3) onto runtime code, against
contract 1.09 (automation-vs-AI boundary), 1.11 (publication gates), and
``AI_GATEWAY_PROCESSING_WORKFLOW.md`` (the six processing lanes + the five
run-log requirements).

What this module does (Lane 2 only — "AI-assisted extraction"):

* Proposes ``statements`` (+ their ``evidence_links``) from **already-preserved**
  Lane-1 output (transcript segments / registry sources). It never fetches,
  transcribes, or expands source material — it reads what deterministic ingest
  already stored. The actual model call is injected as a ``proposer`` callable so
  the adapter is offline-deterministic in tests/CI; the real provider is loaded
  from local (gitignored) config and refuses to run without it.
* Writes each accepted claim through the **same** :func:`statements.insert_statement`
  path Slice 2 uses, binding the AI-specific fields and **overriding every gating
  field** so a buggy proposer cannot smuggle a publishable row:
  ``produced_by='ai'``, ``verification_status='machine_extracted_unreviewed'``,
  ``review_state='unreviewed'``, ``publication_state='not_publishable'``,
  ``layer='ai_thought_then'`` (paraphrase ``is_verbatim=0``), the model's
  ``confidence`` label, and the gateway ``ai_extraction_run_id``.
* Inherits **no-orphan-claims** unchanged: a proposed claim with no resolving
  ``evidence_link`` pointer (and no segment edge) is rejected by
  ``insert_statement`` (1.07 §2.3); the adapter counts the rejection and never
  writes it.
* Enforces **attribution safety**: a proposed speaker is always routed through
  :func:`speakers.attribute_speaker` with ``person_confirmed=False`` — AI can
  never confirm an identity from official records, so an uncertain speaker
  *drops the name* (no bound ``person_id``, no ``made_statement`` edge, a
  name-free label; the guess survives only as the vault-only
  ``candidate_person_id`` reviewer hint). Never a wrong name (1.07 §3; 1.09 step 9).
* Records a **gateway-run ledger** row (``ai_extraction_runs``) capturing the
  input source/segment set, model/tool/prompt version, produced artifact ids,
  error status, reviewer state, and the forward-only retry chain (AI_GATEWAY §17).

Fail-closed posture:

* AI output enters at ``machine_extracted_unreviewed`` and ``not_publishable`` and
  stays there until a human promotes it AND the deterministic publication gate
  allows it (the single AI->public path, GOV-88 §2.3). This module authorizes no
  publication.
* :func:`outputs_publication_blocked` returns True (downstream blocked) unless the
  run finished ``error_status='ok'`` AND a reviewer ``approved`` it — so a failed
  run, a partial run, or an unreviewed run can never feed a downstream surface
  (AI_GATEWAY "failed gateway processing must block downstream").

Data boundary (1.11 §2.1; AI_GATEWAY §7.1): the ledger, its ``error_detail``, the
local provider config, and the produced AI rows are local/vault-only.
``ai_extraction_runs`` is deliberately NOT on
``publication.WEB_SAFE_FIELD_ALLOWLIST``; only summary counts belong in a
Paperclip comment.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Reuse the SSOT enums + the guarded write paths — never re-type or re-implement.
import publication as pub
import speakers as spk
import statements as st
# GOV-105 positive PII guard, enforced at THIS write boundary (GOV-137): an AI
# claim whose text/quote carries private-individual PII is dropped fail-closed.
from concept_map import assert_no_pii, PiiGuardError

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- gateway-run vocabularies (mirror the 0009 CHECK literals; parity-tested) --

ALLOWED_LANES = frozenset({"2_extraction", "3_verification", "4_risk"})
ALLOWED_RUN_ERROR_STATUS = frozenset({"ok", "partial", "failed"})
ALLOWED_RUN_REVIEWER_STATE = frozenset({"unreviewed", "in_review", "approved", "rejected"})

# AI-lane fixed bindings (1.09 §6.1 / §2.1; 1.11 §5). These are NON-NEGOTIABLE —
# the adapter overrides whatever the proposer returns for these fields.
AI_PRODUCED_BY = "ai"
AI_ENTRY_VERIFICATION_STATUS = "machine_extracted_unreviewed"
AI_REVIEW_STATE = "unreviewed"
AI_PUBLICATION_STATE = "not_publishable"
AI_LAYER = "ai_thought_then"

# The only reviewer state that, paired with error_status='ok', unblocks downstream.
PROMOTABLE_REVIEWER_STATE = "approved"

# --- the source-grounded prompt (1.09 §16; AI_GATEWAY rule "prompts must require
# source-grounded output, uncertainty labels, no unsupported allegations"). The
# prompt is versioned; the version id is recorded on every run for reproducibility.
PROMPT_ID = "alpine-lane2-source-grounded.v1"
SOURCE_GROUNDED_PROMPT = """\
You are a Lane-2 extraction assistant for a government-records watchdog. You read
ONLY the provided, already-preserved Alpine source segments. Rules you MUST follow:

1. SOURCE-GROUNDED: every claim you output MUST cite an exact source pointer
   (segment timestamp or document page/section) into the provided material. If you
   cannot ground a claim in the provided source, DO NOT output it.
2. NO UNSUPPORTED ALLEGATIONS: never output an accusation, motive, legal
   conclusion, or characterization that the source does not literally support.
   Extract what was said/decided; do not infer wrongdoing.
3. UNCERTAINTY LABELS: mark each claim's confidence (high/medium/low). If you are
   unsure who spoke, DO NOT name them — leave the speaker uncertain. No name is
   better than a wrong name.
4. NO NEW FACTS: do not add information beyond the provided source. Paraphrases
   must be marked non-verbatim.
5. You propose drafts only. You never decide verification, publication, or that a
   claim is true. A human reviewer does that.
"""


class ProviderNotConfigured(RuntimeError):
    """No local AI provider is configured and no proposer was injected."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --- provider config (local/vault-only; never committed) --------------------

DEFAULT_PROVIDER_CONFIG = REPO_ROOT / "Database" / "ai_provider.local.json"


def load_provider_config(path: Path | None = None) -> dict[str, Any]:
    """Load the local AI provider config (model/version/provider).

    Resolution order, fail-closed to a disabled offline provider:

    1. Environment: ``GOV_AI_PROVIDER`` / ``GOV_AI_MODEL`` / ``GOV_AI_MODEL_VERSION``.
    2. A local JSON file (default ``Database/ai_provider.local.json``, gitignored).
    3. Otherwise ``{"provider": "offline-disabled", ...}`` — :func:`run_extraction`
       refuses to call a live model with this config (a proposer must be injected).

    The config is reviewer/vault-only and never web-projected; only ``model_name``
    / ``model_version`` reach the ledger (no secrets/keys are read or stored).
    """
    env_provider = os.environ.get("GOV_AI_PROVIDER")
    if env_provider:
        return {
            "provider": env_provider,
            "model_name": os.environ.get("GOV_AI_MODEL"),
            "model_version": os.environ.get("GOV_AI_MODEL_VERSION"),
        }
    cfg_path = Path(path) if path is not None else DEFAULT_PROVIDER_CONFIG
    if cfg_path.exists():
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return {
            "provider": data.get("provider", "offline-disabled"),
            "model_name": data.get("model_name"),
            "model_version": data.get("model_version"),
        }
    return {"provider": "offline-disabled", "model_name": None, "model_version": None}


# A proposer: given (conn, input_source_ids, input_segment_ids) returns a list of
# proposed-claim dicts. Injected so the adapter is deterministic/offline in tests.
Proposer = Callable[[sqlite3.Connection, list[str], list[str]], list[dict[str, Any]]]


# --- ledger writes ----------------------------------------------------------

def create_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    lane: str = "2_extraction",
    input_source_ids: list[str] | None = None,
    input_segment_ids: list[str] | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
    tool_version: str | None = None,
    prompt_id: str = PROMPT_ID,
    retry_of_run_id: str | None = None,
    retry_count: int = 0,
    dry_run: bool = True,
    commit: bool = True,
) -> str:
    """Open a gateway-run ledger row (``error_status='ok'``, ``reviewer_state='unreviewed'``)."""
    if lane not in ALLOWED_LANES:
        raise ValueError(f"lane {lane!r} not in {sorted(ALLOWED_LANES)}")
    if st._is_missing(run_id):
        raise ValueError("run requires a non-empty run_id")
    conn.execute(
        "INSERT INTO ai_extraction_runs ("
        "run_id, lane, input_source_ids, input_segment_ids, model_name, "
        "model_version, tool_version, prompt_id, retry_of_run_id, retry_count, "
        "dry_run, started_utc"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            lane,
            json.dumps(list(input_source_ids or [])),
            json.dumps(list(input_segment_ids)) if input_segment_ids is not None else None,
            model_name,
            model_version,
            tool_version,
            prompt_id,
            retry_of_run_id,
            int(retry_count),
            1 if dry_run else 0,
            _now_utc_iso(),
        ),
    )
    if commit:
        conn.commit()
    return run_id


def finalize_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    output_statement_ids: list[str],
    output_evidence_link_ids: list[str],
    orphan_rejected_count: int,
    error_status: str,
    error_detail: str | None = None,
    commit: bool = True,
) -> None:
    """Close a run: write the produced artifact ids, counts, and error status."""
    if error_status not in ALLOWED_RUN_ERROR_STATUS:
        raise ValueError(f"error_status {error_status!r} not in {sorted(ALLOWED_RUN_ERROR_STATUS)}")
    conn.execute(
        "UPDATE ai_extraction_runs SET "
        "output_statement_ids = ?, output_evidence_link_ids = ?, output_count = ?, "
        "orphan_rejected_count = ?, error_status = ?, error_detail = ?, finished_utc = ? "
        "WHERE run_id = ?",
        (
            json.dumps(list(output_statement_ids)),
            json.dumps(list(output_evidence_link_ids)),
            len(output_statement_ids),
            int(orphan_rejected_count),
            error_status,
            error_detail,
            _now_utc_iso(),
            run_id,
        ),
    )
    if commit:
        conn.commit()


def set_reviewer_state(
    conn: sqlite3.Connection, run_id: str, reviewer_state: str, *, commit: bool = True
) -> None:
    """Record a human reviewer decision on a run (Lane 5 gate hook).

    This is the ONLY way a run becomes promotable. Automation/AI never calls it
    with ``approved`` (1.09 step 11 / G2 — only a human promotes).
    """
    if reviewer_state not in ALLOWED_RUN_REVIEWER_STATE:
        raise ValueError(
            f"reviewer_state {reviewer_state!r} not in {sorted(ALLOWED_RUN_REVIEWER_STATE)}"
        )
    conn.execute(
        "UPDATE ai_extraction_runs SET reviewer_state = ? WHERE run_id = ?",
        (reviewer_state, run_id),
    )
    if commit:
        conn.commit()


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ai_extraction_runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"run_id {run_id!r} not found")
    return dict(row)


def outputs_publication_blocked(run: dict[str, Any]) -> bool:
    """Fail-closed downstream gate: True unless the run is OK *and* reviewer-approved.

    A failed/partial run, or any run not yet ``approved`` by a human, blocks its
    outputs from any downstream/public surface (AI_GATEWAY "failed gateway
    processing must block downstream"; 1.11 §5 reviewer gate). The produced rows
    are ALSO ``not_publishable`` at the DB layer regardless — this is the
    belt-and-braces run-level block.
    """
    return not (
        run.get("error_status") == "ok"
        and run.get("reviewer_state") == PROMOTABLE_REVIEWER_STATE
    )


# --- attribution safety (AI never names) ------------------------------------

def _apply_ai_speaker(
    conn: sqlite3.Connection, statement_id: str, speaker: dict[str, Any]
) -> dict[str, Any]:
    """Route a proposed AI speaker through the §3 safety gate, AI-never-confirmed.

    The adapter forces ``person_confirmed=False`` and never passes
    ``ceo_approved_public`` — so :func:`speakers.derive_attribution_state` can only
    ever resolve to ``uncertain``/``unattributed`` (an AI guess drops the name and
    survives only as the private ``candidate_person_id``). The bound name path is
    structurally unreachable from here.
    """
    attribution = {
        "speaker_attribution_id": speaker.get(
            "speaker_attribution_id", f"{statement_id}:ai-attr"
        ),
        "statement_id": statement_id,
        # An AI proposal is never a confirmed identity. Request 'uncertain' so the
        # candidate is retained as a reviewer hint, never bound.
        "attribution_state": "uncertain",
        "speaker_class": speaker.get("speaker_class", "unidentified"),
        "confidence": speaker.get("confidence", "low"),
        "candidate_person_id": speaker.get("candidate_person_id"),
        "person_confirmed": False,  # AI can never confirm — hard rule.
        "role_title": speaker.get("role_title"),
        "role_only_label": speaker.get("role_only_label"),
        "reviewer_state": "unreviewed",
    }
    return spk.attribute_speaker(conn, attribution, ceo_approved_public=False, commit=False)


# --- the Lane-2 run ---------------------------------------------------------

def _bind_ai_fields(claim: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Project a proposed claim onto the fixed AI bindings (gating fields overridden)."""
    is_verbatim = 1 if claim.get("is_verbatim") else 0  # AI defaults to paraphrase (0)
    return {
        "statement_id": claim["statement_id"],
        "segment_id": claim.get("segment_id"),
        "agenda_item_id": claim.get("agenda_item_id"),
        "statement_text": claim.get("statement_text"),
        "is_verbatim": is_verbatim,
        "layer": claim.get("layer", AI_LAYER),
        "confidence": claim.get("confidence", "low"),
        # Gating fields — overridden, NOT taken from the proposer (1.09 §2.3).
        "produced_by": AI_PRODUCED_BY,
        "verification_status": AI_ENTRY_VERIFICATION_STATUS,
        "review_state": AI_REVIEW_STATE,
        "publication_state": AI_PUBLICATION_STATE,
        "ai_extraction_run_id": run_id,
    }


def _assert_claim_pii_free(ai_statement: dict[str, Any], links: list[dict[str, Any]]) -> None:
    """Fail-closed PII guard at the AI write boundary (GOV-105 / GOV-137).

    Raises :class:`PiiGuardError` if any evidence-link ``quoted_text`` matches a
    private-individual PII pattern (street address, voter/registration id, ...).
    ``quoted_text`` is the VERBATIM source span GOV-137 introduces — an AI proposer
    copies it character-for-character from the source, so a literal address/voter-id
    in the source would otherwise be written verbatim. The caller counts the raised
    claim as a rejection and never writes it (fail-closed; no name > wrong name).
    The guard message names only the pattern KIND, never the matched value, so the
    rejection that lands in the run ledger's ``error_detail`` cannot itself leak it.

    SCOPING (deliberate, flagged to VSR/SecurityPrivacy): this guards the NEW
    verbatim field only. It does NOT guard the paraphrased ``statement_text`` — a
    paraphrase that surfaces private PII remains the domain of the Lane-4 RISK layer
    (``ai_risk_gate`` privacy flag + Lane-5 block), the established contract. Adding
    a hard Lane-2 drop on ``statement_text`` would pre-empt and silence that
    risk-flagging path. Whether to ALSO hard-drop at Lane 2 is an owner/architecture
    call for the review lane, not a unilateral change here.
    """
    for index, link in enumerate(links):
        assert_no_pii(link.get("quoted_text"), f"ai evidence_link[{index}] quoted_text")


def run_extraction(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    input_source_ids: list[str],
    input_segment_ids: list[str] | None = None,
    proposer: Proposer | None = None,
    lane: str = "2_extraction",
    tool_version: str | None = None,
    model_name: str | None = None,
    model_version: str | None = None,
    provider_config: dict[str, Any] | None = None,
    retry_of_run_id: str | None = None,
    retry_count: int = 0,
    dry_run: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    """Run one Lane-2 AI extraction over already-preserved Alpine source.

    Opens a ledger row, runs the injected ``proposer`` (no live model call without
    one), writes each accepted claim via :func:`statements.insert_statement` with
    AI bindings + the run provenance, routes any proposed speaker through the
    attribution-safety gate, counts orphan/pointer rejections, and finalizes the
    ledger. Returns a structured result; never raises on a proposer error (it is
    recorded as ``error_status='failed'`` instead — fail-closed and auditable).

    The model/tool version recorded on the run comes from ``model_name`` /
    ``model_version`` (or the loaded ``provider_config``); no secret is read.
    """
    cfg = provider_config if provider_config is not None else load_provider_config()
    model_name = model_name or cfg.get("model_name")
    model_version = model_version or cfg.get("model_version")

    create_run(
        conn,
        run_id=run_id,
        lane=lane,
        input_source_ids=input_source_ids,
        input_segment_ids=input_segment_ids,
        model_name=model_name,
        model_version=model_version,
        tool_version=tool_version,
        prompt_id=PROMPT_ID,
        retry_of_run_id=retry_of_run_id,
        retry_count=retry_count,
        dry_run=dry_run,
        commit=False,
    )

    written_statements: list[str] = []
    written_links: list[str] = []
    rejected: list[dict[str, Any]] = []
    error_status = "ok"
    error_detail: str | None = None

    try:
        if proposer is None:
            # No live provider call is implemented here on purpose: a real model
            # call is configured locally and injected. Refuse fail-closed.
            raise ProviderNotConfigured(
                f"no proposer injected and provider={cfg.get('provider')!r}; "
                "Lane-2 needs an injected proposer (offline/deterministic) or a "
                "configured local provider adapter"
            )
        proposed = proposer(conn, list(input_source_ids), list(input_segment_ids or []))
    except Exception as exc:  # fail-closed: a proposer failure is a failed run.
        finalize_run(
            conn,
            run_id,
            output_statement_ids=[],
            output_evidence_link_ids=[],
            orphan_rejected_count=0,
            error_status="failed",
            error_detail=f"{type(exc).__name__}: {exc}",
            commit=commit,
        )
        return {
            "run_id": run_id,
            "ok": False,
            "error_status": "failed",
            "written_statements": [],
            "written_evidence_links": [],
            "rejected": [],
            "output_count": 0,
        }

    for claim in proposed:
        statement_id = claim.get("statement_id")
        links = list(claim.get("evidence_links") or [])
        for link in links:
            link.setdefault("ai_extraction_run_id", run_id)
        ai_statement = _bind_ai_fields(claim, run_id)
        try:
            # Write boundary, in order: PII guard (GOV-105/GOV-137) THEN the 1.07
            # orphan/pointer invariants. Both reject fail-closed — the claim is
            # dropped and counted, never written.
            _assert_claim_pii_free(ai_statement, links)
            result = st.insert_statement(conn, ai_statement, links, commit=False)
        except (st.OrphanClaimError, st.PointerError, PiiGuardError, ValueError) as exc:
            # No-orphan-claims (1.07 §2.3) + PII guard inherited fail-closed: an AI
            # claim with no resolving pointer/segment, or one carrying private PII,
            # is rejected and NOT written.
            rejected.append({"statement_id": statement_id, "error": f"{type(exc).__name__}: {exc}"})
            continue

        written_statements.append(result["statement_id"])
        link_ids = [
            r[0]
            for r in conn.execute(
                "SELECT evidence_link_id FROM evidence_links WHERE from_node_id = ?",
                (result["statement_id"],),
            ).fetchall()
        ]
        written_links.extend(link_ids)

        # Attribution safety: an AI speaker guess never names (uncertain -> no name).
        if claim.get("speaker"):
            _apply_ai_speaker(conn, result["statement_id"], claim["speaker"])

    if rejected and written_statements:
        error_status = "partial"
        error_detail = f"{len(rejected)} claim(s) rejected: " + "; ".join(
            f"{r['statement_id']}={r['error']}" for r in rejected
        )
    elif rejected and not written_statements:
        error_status = "failed"
        # Carry the per-claim rejection reasons into the ledger (auditable WHY).
        # Each reason names only an error type + contract text (the PII guard names
        # the pattern KIND, never the matched value), so this leaks no PII.
        error_detail = f"all {len(rejected)} claim(s) rejected; nothing written: " + "; ".join(
            f"{r['statement_id']}={r['error']}" for r in rejected
        )

    finalize_run(
        conn,
        run_id,
        output_statement_ids=written_statements,
        output_evidence_link_ids=written_links,
        orphan_rejected_count=len(rejected),
        error_status=error_status,
        error_detail=error_detail,
        commit=commit,
    )

    return {
        "run_id": run_id,
        "ok": error_status == "ok",
        "error_status": error_status,
        "written_statements": written_statements,
        "written_evidence_links": written_links,
        "rejected": rejected,
        "output_count": len(written_statements),
    }
