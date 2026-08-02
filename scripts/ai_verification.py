"""Lane-3 verification layer — compare AI draft to source, label, flag uncertainty.

GOV-90, Stage 1 Slice 3 C. Maps the GOV-88 (Slice 3 A) interface design
(``Docs/stage3-ai-gateway-gap-analysis.md`` §4.2 — Lane 3 L3-1/L3-5/L3-6) onto
runtime code, against contract 1.09 (automation-vs-AI boundary, step 11 prep),
1.11 (publication gates §5), and ``AI_GATEWAY_PROCESSING_WORKFLOW.md`` lane 3
("compare AI output to primary source, assign a verification label, flag
uncertainty").

What this module does (Lane 3 only — "verification layer"):

* For each AI-produced ``statements`` row (Lane-2 output), **deterministically**
  compares the AI draft text to the *primary source* at its anchored pointer —
  the ``transcript_segments`` text the statement/evidence_link points into — using
  a token-grounding overlap (no model, no network; reproducible in tests/CI).
* Assigns a **verification label** (the ``verdict``) and surfaces an
  **uncertainty/confidence flag**, recording both to the new append-only
  :data:`RESULTS_TABLE` side table keyed to the statement and the Lane-3 run.
* On mismatch / low-confidence / unresolvable source, marks the AI row
  **contested** (``contested=1``) so a reviewer sees it must be checked.

Fail-closed posture — the load-bearing invariant:

* Lane 3 writes **NO gating field**. It never touches ``statements`` or
  ``evidence_links`` — not ``verification_status``, not ``publication_state``,
  not ``correction_status``, not ``review_state`` (gap analysis §4.2 L3-1; 1.09
  §2.3). The verdict lives *beside* the claim, never *on* it. Because the claim
  row is untouched, an AI claim stays ``machine_extracted_unreviewed`` +
  ``not_publishable`` by construction — Lane 3 can flag a claim contested but can
  **never promote** it.
* A ``source_match`` verdict means only "the draft is grounded in its source,
  ready for a HUMAN reviewer" — it does **not** make the claim publishable. The
  only promotion is the human G2 gate (1.09 step 11 / G2, 1.11 §5).
* A **low-confidence** AI claim is never auto-validated: the verdict is downgraded
  to at most ``uncertain`` even on a strong text overlap (1.09 §5 low-confidence
  → reviewer, never auto-promote).
* :func:`verification_blocks_publication` returns True for every verdict except a
  ``source_match`` that a human has separately approved — so a mismatch /
  uncertain / unverifiable / unreviewed verdict can never feed a downstream
  surface (AI_GATEWAY "failed gateway processing must block downstream").

Run ledger reuse (no new run-log plumbing): the Lane-3 run is recorded in the
existing ``ai_extraction_runs`` ledger with ``lane='3_verification'`` via
:mod:`ai_extraction`'s ``create_run`` / ``finalize_run`` / ``set_reviewer_state``
(AI_GATEWAY §17: input set, model/tool version, errors, reviewer state, retry).
Lane 3 is deterministic, so ``model_name`` is None and a ``tool_version`` is
recorded; the per-statement findings live in :data:`RESULTS_TABLE`.

Data boundary (1.11 §2.1; AI_GATEWAY §7.1): :data:`RESULTS_TABLE`, its
``source_excerpt`` and ``detail`` are local/vault-only — deliberately NOT on
``publication.WEB_SAFE_FIELD_ALLOWLIST``. Only summary counts belong in a
Paperclip comment.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

# Reuse the gateway-run ledger + the AI-lane bindings — never re-implement them.
import ai_extraction as ai

RESULTS_TABLE = "ai_verification_results"

# --- Lane-3 vocabularies (mirror the 0010 CHECK literals; parity-tested) -----

# The verification label. A FLAG, never a promotion (gap analysis §4.2 L3-2).
ALLOWED_VERDICTS = frozenset(
    {"source_match", "source_mismatch", "unverifiable", "uncertain"}
)
# The only verdict that is NOT contested. Even this one does not publish on its
# own — a human still has to promote the claim (1.09 step 11 / G2).
NON_CONTESTED_VERDICT = "source_match"

ALLOWED_UNCERTAINTY_FLAGS = frozenset({"high", "medium", "low"})

# The Lane this module runs on the shared ai_extraction_runs ledger.
LANE = "3_verification"

# Deterministic compare method id (versioned for reproducibility/audit).
MATCH_METHOD = "token_containment.v1"

# Verdict bands over the source-grounding containment score (fraction of the AI
# draft's content tokens that appear in the source text at the pointer).
MATCH_HIGH = 0.60      # >= this and not low-confidence -> source_match
MISMATCH_LOW = 0.20    # <= this -> source_mismatch; in-between -> uncertain

#: The ONLY confidence values that permit an automatic ``source_match`` (GOV-1708).
#:
#: An explicit allowlist, deliberately **not** ``statements.ALLOWED_CONFIDENCE -
#: {"low"}``. Derivation-by-subtraction is what makes ``file_read_api``'s
#: ``WEB_SAFE_DIFF_FIELDS`` the one fail-OPEN surface in a fail-closed repo
#: (GOV-1705): a value added to the SSOT would become auto-matchable with nobody
#: deciding it should be. Here a new vocabulary value denies until someone opts
#: it in, and ``test_auto_matchable_confidences_are_real_vocabulary`` fails if
#: these names stop existing upstream.
_AUTO_MATCHABLE_CONFIDENCES = frozenset({"high", "medium"})

# Lane-2 paraphrase rows carry a literal "AI paraphrase:" lead-in (see
# ai_extraction); strip it so it does not inflate the overlap with itself.
_AI_PREFIX_RE = re.compile(r"^\s*ai\s+(?:paraphrase|summary)\s*:\s*", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Content-word stoplist: function words carry no grounding signal, so including
# them would let an ungrounded paraphrase score high on shared "the/of/and".
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "it", "its", "this", "that", "these", "those", "they", "them", "their",
    "he", "she", "his", "her", "we", "our", "you", "your", "i", "me", "my",
    "will", "would", "should", "could", "can", "may", "might", "must", "do",
    "does", "did", "has", "have", "had", "not", "no", "so", "than", "then",
    "there", "here", "which", "who", "whom", "what", "when", "where", "how",
    "about", "into", "over", "under", "up", "down", "out", "off", "also",
})


class VerificationError(RuntimeError):
    """A Lane-3 run failed in a way that must fail closed (recorded as failed)."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _content_tokens(text: str | None) -> set[str]:
    """Lowercased content tokens (AI lead-in + stopwords removed)."""
    if not text:
        return set()
    stripped = _AI_PREFIX_RE.sub("", text)
    return {t for t in _TOKEN_RE.findall(stripped.lower()) if t not in _STOPWORDS}


def containment_score(claim_text: str | None, source_text: str | None) -> float:
    """Fraction of the claim's content tokens grounded in the source text.

    Containment (not symmetric Jaccard) is the right measure for a *paraphrase*:
    a faithful AI summary is shorter than the source, so we ask "is every salient
    word of the claim present in the source?" — not "do the two texts have equal
    vocabulary". Returns 0.0 when the claim has no content tokens (nothing to
    ground -> cannot be matched).
    """
    claim = _content_tokens(claim_text)
    if not claim:
        return 0.0
    source = _content_tokens(source_text)
    return len(claim & source) / len(claim)


# --- source resolution at the pointer ---------------------------------------

def _resolve_source_text(
    conn: sqlite3.Connection, statement: dict[str, Any]
) -> tuple[str | None, str | None]:
    """Resolve the primary-source text a statement should be grounded in.

    Order (gap analysis §4.2 L3-1 "compare draft to source at the pointer"):

    1. The ``statement_from_segment`` anchor — ``statements.segment_id`` ->
       ``transcript_segments.segment_text`` (the timestamped primary span).
    2. Else the first timestamp ``evidence_link`` whose ``timestamp_seconds``
       resolves to a ``transcript_segments`` row for the same source.

    Returns ``(source_text, evidence_link_id)``. ``source_text`` is None when no
    primary span resolves — the caller records ``unverifiable`` (fail-closed).
    """
    segment_id = statement.get("segment_id")
    if segment_id:
        row = conn.execute(
            "SELECT segment_text FROM transcript_segments WHERE segment_id = ?",
            (segment_id,),
        ).fetchone()
        if row is not None:
            return row["segment_text"], None

    # Fall back to a timestamp evidence_link -> the segment at that offset.
    links = conn.execute(
        "SELECT evidence_link_id, to_source_id, locator_kind, timestamp_seconds "
        "FROM evidence_links WHERE from_node_id = ? AND from_node_type = 'statement'",
        (statement["statement_id"],),
    ).fetchall()
    for link in links:
        if link["locator_kind"] != "timestamp" or link["timestamp_seconds"] is None:
            continue
        seg = conn.execute(
            "SELECT segment_text FROM transcript_segments "
            "WHERE source_id = ? AND timestamp_seconds = ?",
            (link["to_source_id"], link["timestamp_seconds"]),
        ).fetchone()
        if seg is not None:
            return seg["segment_text"], link["evidence_link_id"]

    return None, None


# --- the verdict (deterministic; a flag, never a promotion) ------------------

def classify(
    *, source_text: str | None, claim_text: str | None, claim_confidence: str
) -> tuple[str, float | None, str]:
    """Return ``(verdict, score, uncertainty_flag)`` for one claim vs its source.

    Pure + deterministic. Fail-closed bands:

    * source unresolved -> ``unverifiable`` (highest uncertainty).
    * score >= :data:`MATCH_HIGH` AND the claim is not low-confidence ->
      ``source_match`` (low uncertainty). A claim is auto-matched **only** when
      its confidence is in :data:`_AUTO_MATCHABLE_CONFIDENCES`; low-confidence
      claims and **unrecognised** ones alike are capped at ``uncertain`` no
      matter how high the overlap (1.09 §5 low-confidence -> reviewer).
    * score <= :data:`MISMATCH_LOW` -> ``source_mismatch`` (high uncertainty).
    * otherwise -> ``uncertain`` (medium uncertainty).
    """
    if source_text is None:
        return "unverifiable", None, "high"

    score = containment_score(claim_text, source_text)
    # Fail-closed: only a RECOGNISED non-low confidence may auto-match. The
    # previous form was `== "low"`, which denied on the literal and let every
    # other value through — including " low", "", None and "unknown" (GOV-1708).
    conf = (claim_confidence or "").strip().lower()
    auto_matchable = conf in _AUTO_MATCHABLE_CONFIDENCES

    if score >= MATCH_HIGH and auto_matchable:
        return "source_match", score, "low"
    if score <= MISMATCH_LOW:
        return "source_mismatch", score, "high"
    # Mid-band, or a high-overlap-but-low-confidence claim: needs a human.
    return "uncertain", score, "medium"


def _excerpt(text: str | None, limit: int = 280) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def verify_statement(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    run_id: str,
    result_id: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Verify one AI statement against its primary source; write the verdict row.

    Loads the statement, resolves its source text at the pointer, classifies, and
    appends one :data:`RESULTS_TABLE` row. **Never mutates the statement or its
    evidence_links** (no gating field written — gap analysis §4.2 L3-1). Returns
    the verdict dict. Raises :class:`VerificationError` if the statement does not
    resolve or is not an AI row (Lane 3 verifies Lane-2 output only).
    """
    stmt_row = conn.execute(
        "SELECT statement_id, segment_id, statement_text, produced_by, confidence "
        "FROM statements WHERE statement_id = ?",
        (statement_id,),
    ).fetchone()
    if stmt_row is None:
        raise VerificationError(f"statement {statement_id!r} does not resolve")
    statement = dict(stmt_row)
    if statement["produced_by"] != ai.AI_PRODUCED_BY:
        raise VerificationError(
            f"statement {statement_id!r} produced_by={statement['produced_by']!r}; "
            "Lane 3 verifies AI-produced rows only"
        )

    source_text, evidence_link_id = _resolve_source_text(conn, statement)
    verdict, score, uncertainty = classify(
        source_text=source_text,
        claim_text=statement["statement_text"],
        claim_confidence=statement["confidence"],
    )
    contested = 0 if verdict == NON_CONTESTED_VERDICT else 1

    result_id = result_id or f"{statement_id}:verif:{run_id}"
    now = _now_utc_iso()
    conn.execute(
        f"INSERT INTO {RESULTS_TABLE} ("
        "result_id, run_id, statement_id, evidence_link_id, verdict, match_method, "
        "match_score, uncertainty_flag, contested, source_excerpt, detail, "
        "compared_utc, created_utc"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result_id,
            run_id,
            statement_id,
            evidence_link_id,
            verdict,
            MATCH_METHOD,
            score,
            uncertainty,
            contested,
            _excerpt(source_text),
            f"score={score!r} method={MATCH_METHOD}",
            now,
            now,
        ),
    )
    if commit:
        conn.commit()

    return {
        "result_id": result_id,
        "statement_id": statement_id,
        "verdict": verdict,
        "match_score": score,
        "uncertainty_flag": uncertainty,
        "contested": contested,
        "evidence_link_id": evidence_link_id,
    }


def run_verification(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    input_statement_ids: list[str],
    input_source_ids: list[str] | None = None,
    input_segment_ids: list[str] | None = None,
    tool_version: str | None = None,
    retry_of_run_id: str | None = None,
    retry_count: int = 0,
    dry_run: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    """Run one Lane-3 verification pass over a set of AI statements.

    Opens a ``lane='3_verification'`` row on the shared ``ai_extraction_runs``
    ledger, verifies each statement (writing one verdict row each), and finalizes
    the ledger with the produced result ids and an execution ``error_status``.

    ``error_status`` reflects *execution health*, not the verdicts: ``ok`` when
    every statement was compared, ``failed`` when a comparison raised (fail-closed
    — the verdicts of a failed run cannot be trusted). Note a clean run that emits
    only mismatches is still ``ok`` at the run level — the *block* comes from the
    per-statement verdicts (``contested``) + the claims' own ``not_publishable``
    default, never from this status (see :func:`verification_blocks_publication`).

    Lane 3 is deterministic: ``model_name``/``model_version`` are None, only a
    ``tool_version`` is recorded. Never raises on a per-statement failure — it is
    recorded and the run finalizes ``failed`` (auditable, fail-closed).
    """
    ai.create_run(
        conn,
        run_id=run_id,
        lane=LANE,
        input_source_ids=input_source_ids or [],
        input_segment_ids=input_segment_ids,
        model_name=None,
        model_version=None,
        tool_version=tool_version,
        prompt_id=None,  # deterministic compare — no grounded-generation prompt
        retry_of_run_id=retry_of_run_id,
        retry_count=retry_count,
        dry_run=dry_run,
        commit=False,
    )

    verdicts: list[dict[str, Any]] = []
    result_ids: list[str] = []
    verified_ids: list[str] = []
    errors: list[str] = []

    for statement_id in input_statement_ids:
        try:
            verdict = verify_statement(conn, statement_id, run_id=run_id, commit=False)
        except (VerificationError, sqlite3.Error) as exc:
            errors.append(f"{statement_id}={type(exc).__name__}: {exc}")
            continue
        verdicts.append(verdict)
        result_ids.append(verdict["result_id"])
        verified_ids.append(statement_id)

    if errors and not verdicts:
        error_status = "failed"
        error_detail = f"all {len(errors)} statement(s) failed verification: " + "; ".join(errors)
    elif errors:
        error_status = "partial"
        error_detail = f"{len(errors)} statement(s) failed: " + "; ".join(errors)
    else:
        error_status = "ok"
        error_detail = None

    ai.finalize_run(
        conn,
        run_id,
        # For a Lane-3 run: output_statement_ids = the verified statements;
        # output_evidence_link_ids carries the produced verdict result ids (the
        # run's output artifact ids — ledger column is generic, see gap §3.1).
        output_statement_ids=verified_ids,
        output_evidence_link_ids=result_ids,
        orphan_rejected_count=0,  # Lane 3 rejects nothing; unverifiable is a verdict
        error_status=error_status,
        error_detail=error_detail,
        commit=commit,
    )

    contested = [v for v in verdicts if v["contested"]]
    return {
        "run_id": run_id,
        "ok": error_status == "ok",
        "error_status": error_status,
        "verdicts": verdicts,
        "result_ids": result_ids,
        "verified_count": len(verified_ids),
        "contested_count": len(contested),
        "errors": errors,
    }


# --- reads / downstream gate -------------------------------------------------

def latest_verdict(conn: sqlite3.Connection, statement_id: str) -> dict[str, Any] | None:
    """Most recent Lane-3 verdict for a statement (the reviewer-queue view)."""
    row = conn.execute(
        f"SELECT * FROM {RESULTS_TABLE} WHERE statement_id = ? "
        "ORDER BY created_utc DESC, result_id DESC LIMIT 1",
        (statement_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def verification_blocks_publication(
    verdict: dict[str, Any] | None, *, human_approved: bool = False
) -> bool:
    """Fail-closed downstream gate from a Lane-3 verdict.

    Returns True (blocked) unless the verdict is ``source_match`` AND a human has
    separately ``human_approved`` it. So:

    * no verdict yet -> blocked (nothing verified);
    * mismatch / uncertain / unverifiable -> blocked (contested);
    * ``source_match`` but not human-approved -> STILL blocked — Lane 3 never
      promotes; only the human G2 gate does (1.09 step 11 / G2, 1.11 §5).

    This is belt-and-braces with the claim's ``not_publishable`` default: even a
    matched, human-approved verdict does not flip the DB ``publication_state`` —
    that is a separate explicit reviewed transition Lane 3 never performs.
    """
    if not verdict:
        return True
    return not (verdict.get("verdict") == NON_CONTESTED_VERDICT and human_approved)
