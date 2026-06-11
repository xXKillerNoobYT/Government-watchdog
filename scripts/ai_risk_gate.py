"""Lane-4 risk layer + Lane-5 runtime reviewer-gate (GOV-91, Slice 3 D).

Maps the GOV-88 (Slice 3 A) interface design
(``Docs/stage3-ai-gateway-gap-analysis.md`` §4.3 — Lane 4 L4-5; §4.4 — Lane 5
L5-1/L5-5) onto runtime code, against contract 1.09 (automation-vs-AI boundary,
step 11 / G2), 1.11 (publication/privacy/legal/moderation gates §1/§2/§4/§5/§6.5),
and ``AI_GATEWAY_PROCESSING_WORKFLOW.md`` lanes 4 ("identify privacy/legal/
publication/moderation risks and no-go conditions") and 5 ("approve, correct,
dispute, hold, or reject output before beta/public presentation").

Two lanes, one fail-closed module:

**Lane 4 — risk layer.** :func:`run_risk` deterministically screens each
AI-produced ``statements`` row (Lane-2 output) for the four 1.11 no-go families —
``privacy`` (PII never-collect/never-publish, §2.1), ``legal`` (accusation /
legal conclusion / motive / campaign framing about a named individual, §4.1),
``moderation`` (rumor / brigading / unsourced-validation markers, RISK_ASSESSMENT
cat 5), and ``publication`` (an AI/unreviewed row treated as ready before the
gates pass, §1/§5). Every finding lands in the new append-only
:data:`RISK_TABLE` side table keyed to the statement + the Lane-4 run. Lane 4
writes **NO gating field** — like Lane 3 it flags beside the claim, never on it
(gap analysis §4.3). A ``no_go`` flag sets ``blocks_downstream=1`` so the
reviewer-gate and the downstream read refuse to promote/publish the claim until a
reviewer resolves it.

**Lane 5 — runtime reviewer-gate.** :func:`promote_statement` is the **only**
sanctioned code path that moves a claim's ``verification_status`` to a reviewed
value (and is the runtime form of 1.09 step 11 / G2 "only a human promotes").
It is fail-closed:

* **Not a registered reviewer -> rejected (allowlist).** A ``reviewer_id`` that
  does not resolve to a registered, active human reviewer
  (:func:`is_registered_reviewer`) raises :class:`ReviewerGateError` before any
  write — default-deny, so an *unknown* actor id (not just a known automation
  sentinel) is rejected (1.09 §2.5; 1.11 §5 "the AI cannot promote itself"). This
  is the acceptance test "promoting an AI row without a reviewer decision is
  rejected" plus the GOV-93 hardening "a non-registered, non-sentinel id is
  rejected".
* **Failed gateway run blocks downstream.** If the producing
  ``ai_extraction_runs`` row is not ``error_status='ok'``, a promoting decision is
  refused (AI_GATEWAY "failed gateway processing must block downstream").
* **Open no-go risk flag blocks downstream.** An unresolved ``blocks_downstream``
  Lane-4 flag refuses promotion until a reviewer resolves it.
* **No auto-publish.** Promotion moves ``verification_status`` to a reviewed value
  and stamps ``review_state='reviewed'`` + a recomputed ``ui_status``; it NEVER
  flips ``publication_state`` to ``publishable`` — that is the separate owner
  decision (1.11 P8). So nothing AI-written is publishable by default even after
  promotion. Every call records a :data:`DECISION_TABLE` audit row first (who /
  what / from->to / why), satisfying the 1.11 §6.5 auditable-gate hook (L5-5).

Run ledger reuse (no new run-log plumbing): the Lane-4 run is recorded on the
existing ``ai_extraction_runs`` ledger with ``lane='4_risk'`` via
:mod:`ai_extraction`'s ``create_run`` / ``finalize_run`` (AI_GATEWAY §17). Lane 4
is deterministic, so ``model_name`` is None and a ``tool_version`` is recorded;
the per-statement findings live in :data:`RISK_TABLE`.

Data boundary (1.11 §2.1; AI_GATEWAY §7.1): :data:`RISK_TABLE`,
:data:`DECISION_TABLE`, their ``matched_signal`` / ``detail`` / ``reason`` are
local/vault-only — deliberately NOT on ``publication.WEB_SAFE_FIELD_ALLOWLIST``.
Only summary counts belong in a Paperclip comment.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

# Reuse the gateway-run ledger + the AI-lane bindings + the SSOT enums — never
# re-implement or re-type them.
import ai_extraction as ai
import publication as pub

RISK_TABLE = "ai_risk_flags"
DECISION_TABLE = "reviewer_decisions"
# The reviewer-identity registry (GOV-131; migration 0014): the source of truth
# the Lane-5 allowlist consumes via is_registered_reviewer(). Vault-only (ADR §5).
REVIEWER_REGISTRY_TABLE = "reviewer_identities"

# --- Lane-4 vocabularies (mirror the 0011 CHECK literals; parity-tested) -----

# The four 1.11 no-go families this lane screens for (AI_GATEWAY lane 4).
RISK_CATEGORIES = frozenset({"privacy", "legal", "publication", "moderation"})

# 'no_go' = hard block; 'review' = route to a human; 'clear' = screened, nothing
# found. Only 'clear' does not block downstream.
RISK_SEVERITIES = frozenset({"no_go", "review", "clear"})

# The Lane this module runs on the shared ai_extraction_runs ledger.
LANE = "4_risk"

# Deterministic detector id (versioned for reproducibility/audit).
DETECTOR = "rule_screen.v1"

# --- Lane-5 vocabularies (mirror the 0011 CHECK literals; parity-tested) -----

# The Lane-5 reviewer action set (approve / correct / dispute / hold / reject).
REVIEWER_DECISIONS = frozenset(
    {"approved", "corrected", "disputed", "hold", "rejected"}
)
# The two decisions that move a claim toward a reviewed status (a promotion).
PROMOTING_DECISIONS = frozenset({"approved", "corrected"})

# A reviewer_id that is empty or names an automation/AI actor can NEVER promote
# (1.09 §2.5 — AI/automation never self-promotes; 1.11 §5). Lower-cased compare.
FORBIDDEN_REVIEWER_IDS = frozenset(
    {"", "ai", "automation", "gateway", "system", "bot", ai.AI_PRODUCED_BY}
)


class RiskScanError(RuntimeError):
    """A Lane-4 scan failed in a way that must fail closed (recorded as failed)."""


class ReviewerGateError(RuntimeError):
    """A Lane-5 promotion was refused (fail-closed). Nothing was written."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ===========================================================================
# Reviewer-identity registry (GOV-131; migration 0014 — reviewer_identities)
# ===========================================================================
#
# The source of truth of REGISTERED, ACTIVE human reviewer identities. Builds
# subtask A of the GOV-130 ADR (§2 schema; §3 lookup; §5 boundary). The Lane-5
# gate is now an ALLOWLIST (default-deny): `promote_statement` gate 1 and
# `resolve_flag` admit a reviewer ONLY if is_registered_reviewer() returns True
# (GOV-93 flipped it from the former DENYLIST that rejected only
# FORBIDDEN_REVIEWER_IDS). This module provides that lookup + the vault-only admin
# helpers. The registry ships EMPTY: the safe fail-closed default is "nobody
# passes"; WHICH humans are authorized is the owner decision (GOV-130 subtask B /
# GOV-132), seeded there, not here. FORBIDDEN_REVIEWER_IDS survives as a
# fast-reject folded inside is_registered_reviewer() so an automation/AI actor can
# never be allowlisted even if mis-seeded (defense in depth; 1.09 §2.5 / 1.11 §5).


def is_registered_reviewer(conn: sqlite3.Connection, reviewer_id: str | None) -> bool:
    """True iff ``reviewer_id`` resolves to a registered, active human reviewer.

    The allowlist primitive the GOV-93 hardening will gate on. Fail-closed on
    EVERY uncertain path (ADR §3) — the default answer is always False:

    * empty / ``None`` ``reviewer_id`` => False;
    * a known automation/AI actor sentinel (:data:`FORBIDDEN_REVIEWER_IDS`) =>
      False, folded in as a fast-reject so an AI actor can never be allowlisted
      even if mis-seeded (defense in depth; 1.09 §2.5 / 1.11 §5);
    * no matching row => False (an unknown id never passes);
    * ``status='revoked'`` => False (revocation takes effect immediately);
    * empty registry => False (the safe default — nobody passes);
    * registry table absent (pre-0014, caught :class:`sqlite3.OperationalError`)
      => False, so the allowlist can never silently degrade to allow.
    """
    rid = (reviewer_id or "").strip()
    if not rid or rid.lower() in FORBIDDEN_REVIEWER_IDS:
        return False
    try:
        row = conn.execute(
            f"SELECT 1 FROM {REVIEWER_REGISTRY_TABLE} "
            "WHERE reviewer_id = ? AND status = 'active' LIMIT 1",
            (rid,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False  # table not yet migrated => nobody passes.
    return row is not None


def register_reviewer(
    conn: sqlite3.Connection,
    reviewer_id: str,
    *,
    display_name: str,
    registered_by: str,
    note: str | None = None,
    commit: bool = True,
) -> None:
    """Vault-only admin helper: add (or re-activate) a reviewer identity.

    NOT a web/API surface — this is an operational/seeding helper invoked from a
    trusted local context (and from tests). An owner-approved ``registered_by``
    must be named (the audit of who admitted this identity). Re-registering an
    existing id re-activates it and clears the revoked_* audit fields, recording
    the new registration; ``reviewer_id`` itself is immutable (PK).

    Refuses an empty id or a known automation/AI actor sentinel — an AI actor can
    never be seeded into the allowlist (1.09 §2.5 / 1.11 §5).
    """
    rid = (reviewer_id or "").strip()
    if not rid or rid.lower() in FORBIDDEN_REVIEWER_IDS:
        raise ReviewerGateError(
            f"reviewer_id {reviewer_id!r} is empty or an automation/AI actor; "
            "it may not be registered as a human reviewer"
        )
    if not (display_name or "").strip():
        raise ReviewerGateError("register_reviewer requires a non-empty display_name")
    if not (registered_by or "").strip():
        raise ReviewerGateError("register_reviewer requires a non-empty registered_by")
    now = _now_utc_iso()
    # Upsert: an explicit re-registration re-activates and clears the revoke audit.
    conn.execute(
        f"INSERT INTO {REVIEWER_REGISTRY_TABLE} ("
        "reviewer_id, display_name, status, registered_utc, registered_by, "
        "revoked_utc, revoked_by, revoked_reason, note"
        ") VALUES (?, ?, 'active', ?, ?, NULL, NULL, NULL, ?) "
        "ON CONFLICT(reviewer_id) DO UPDATE SET "
        "display_name = excluded.display_name, status = 'active', "
        "registered_utc = excluded.registered_utc, registered_by = excluded.registered_by, "
        "revoked_utc = NULL, revoked_by = NULL, revoked_reason = NULL, note = excluded.note",
        (rid, display_name, now, registered_by, note),
    )
    if commit:
        conn.commit()


def revoke_reviewer(
    conn: sqlite3.Connection,
    reviewer_id: str,
    *,
    revoked_by: str,
    reason: str,
    commit: bool = True,
) -> None:
    """Vault-only admin helper: revoke a reviewer identity (never DELETE).

    Sets ``status='revoked'`` and stamps the revoke audit (who/when/why). The row
    is retained so the registry stays its own audit trail (ADR §2); a revoked
    identity is excluded by :func:`is_registered_reviewer` immediately. Raises
    :class:`ReviewerGateError` if the id does not resolve, or with an empty
    ``revoked_by`` / ``reason`` (a revocation must be attributable + justified).
    """
    rid = (reviewer_id or "").strip()
    if not (revoked_by or "").strip():
        raise ReviewerGateError("revoke_reviewer requires a non-empty revoked_by")
    if not (reason or "").strip():
        raise ReviewerGateError("revoke_reviewer requires a non-empty reason")
    cur = conn.execute(
        f"UPDATE {REVIEWER_REGISTRY_TABLE} SET status = 'revoked', "
        "revoked_utc = ?, revoked_by = ?, revoked_reason = ? WHERE reviewer_id = ?",
        (_now_utc_iso(), revoked_by, reason, rid),
    )
    if cur.rowcount == 0:
        raise ReviewerGateError(f"reviewer_id {reviewer_id!r} is not registered")
    if commit:
        conn.commit()


# ===========================================================================
# Lane 4 — risk layer (deterministic screen; a flag, never a gating write)
# ===========================================================================

# Privacy / PII patterns (1.11 §2.1 never-collect/never-publish). Deterministic,
# conservative: a hit is a no_go that a reviewer must clear. These screen the AI
# DRAFT text (a Lane-2 paraphrase) — the real defence is privacy-by-schema-absence
# (no PII columns) + to_web_safe()'s allowlist; this catches PII that leaked into
# free text before it can be promoted.
_PRIVACY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # US phone number (varied separators).
    ("phone", re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")),
    # email address.
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # SSN-like (###-##-####).
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # street address (number + street word + a street-type suffix).
    (
        "street_address",
        re.compile(
            r"\b\d{1,6}\s+\w+(?:\s+\w+)*\s+"
            r"(?:st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|ct|court|"
            r"way|cir|circle|ter|terrace|pl|place|trl|trail|loop|pkwy|parkway|hwy|highway|"
            r"sq|square|pt|point)\b",
            re.IGNORECASE,
        ),
    ),
    # voter-roll / registration language.
    ("voter_data", re.compile(r"\bvoter\s+(?:registration|roll|id|file)\b", re.IGNORECASE)),
)

# Legal / defamation patterns (1.11 §4.1: accusation / legal conclusion / motive /
# campaign framing about a named individual). Word-boundary keyword screen.
_LEGAL_TERMS = (
    "committed fraud", "is liable", "violated the law", "broke the law", "guilty of",
    "embezzled", "embezzlement", "corruptly", "corruption", "bribe", "bribery",
    "kickback", "stole", "theft of", "criminal", "illegally", "illegal",
    "conspired", "conspiracy", "perjury", "lied to", "cover-up", "coverup",
    "should be charged", "should resign", "vote against", "vote out", "vote for",
    "must be removed", "is a liar", "is a crook",
)
_LEGAL_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(t) for t in _LEGAL_TERMS) + r")(?!\w)",
    re.IGNORECASE,
)

# Moderation / community patterns (RISK_ASSESSMENT cat 5: rumor amplification,
# brigading, unsourced validation, allegation phrasing).
_MODERATION_TERMS = (
    "rumor has it", "rumour has it", "everyone knows", "people are saying",
    "word on the street", "allegedly", "supposedly", "it is rumored", "it is rumoured",
    "some say", "sources say", "unconfirmed reports", "many believe", "we all know",
)
_MODERATION_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(t) for t in _MODERATION_TERMS) + r")(?!\w)",
    re.IGNORECASE,
)


def _excerpt(text: str | None, limit: int = 160) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def scan_text(text: str | None) -> list[dict[str, Any]]:
    """Deterministically screen claim text for privacy/legal/moderation no-gos.

    Returns a list of ``{category, severity, matched_signal}`` findings (a hit per
    category, not per pattern). Pure + deterministic — no model, no network.
    ``publication`` is NOT screened here: it depends on the row's gating *state*,
    not its text (see :func:`scan_statement`). All text hits are ``no_go`` — a
    PII/accusation/rumor leak in an AI draft must be cleared by a reviewer, never
    auto-promoted (1.11 default-deny).
    """
    findings: list[dict[str, Any]] = []
    body = text or ""

    for kind, pattern in _PRIVACY_PATTERNS:
        match = pattern.search(body)
        if match:
            findings.append(
                {"category": "privacy", "severity": "no_go",
                 "matched_signal": f"{kind}:{_excerpt(match.group(0))}"}
            )
            break  # one privacy flag per statement is enough to block.

    legal = _LEGAL_RE.search(body)
    if legal:
        findings.append(
            {"category": "legal", "severity": "no_go",
             "matched_signal": _excerpt(legal.group(0))}
        )

    moderation = _MODERATION_RE.search(body)
    if moderation:
        findings.append(
            {"category": "moderation", "severity": "no_go",
             "matched_signal": _excerpt(moderation.group(0))}
        )

    return findings


def scan_statement(
    conn: sqlite3.Connection, statement_id: str
) -> list[dict[str, Any]]:
    """Screen one statement; return the list of flag findings (not yet written).

    Combines the text screen (:func:`scan_text`) with a state screen for the
    ``publication`` family: any AI-produced row whose ``verification_status`` is
    not a reviewed value is a publication ``review`` flag — it is "not ready"
    (1.11 §1/§5), a reminder that the reviewer-gate must run before anything
    downstream. Raises :class:`RiskScanError` if the statement does not resolve.
    """
    row = conn.execute(
        "SELECT statement_id, statement_text, produced_by, verification_status "
        "FROM statements WHERE statement_id = ?",
        (statement_id,),
    ).fetchone()
    if row is None:
        raise RiskScanError(f"statement {statement_id!r} does not resolve")
    statement = dict(row)

    findings = scan_text(statement["statement_text"])

    # publication readiness: an AI / unreviewed row is not ready by default.
    if (
        statement["produced_by"] == ai.AI_PRODUCED_BY
        and statement["verification_status"] not in pub.REVIEWED_VERIFICATION_STATUSES
    ):
        findings.append(
            {"category": "publication", "severity": "review",
             "matched_signal": f"produced_by=ai/verification={statement['verification_status']}"}
        )

    return findings


def _record_flag(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    statement_id: str,
    category: str,
    severity: str,
    matched_signal: str | None,
    flag_id: str | None = None,
) -> str:
    """Append one Lane-4 flag row (never mutates the claim)."""
    if category not in RISK_CATEGORIES:
        raise ValueError(f"risk_category {category!r} not in {sorted(RISK_CATEGORIES)}")
    if severity not in RISK_SEVERITIES:
        raise ValueError(f"severity {severity!r} not in {sorted(RISK_SEVERITIES)}")
    # Only a 'no_go' hard-blocks downstream. 'review' routes to a human (the
    # reviewer-gate still promotes over it); 'clear' is an audit-only record.
    blocks = 1 if severity == "no_go" else 0
    flag_id = flag_id or f"{statement_id}:risk:{category}:{run_id}"
    now = _now_utc_iso()
    conn.execute(
        f"INSERT INTO {RISK_TABLE} ("
        "flag_id, run_id, statement_id, risk_category, severity, blocks_downstream, "
        "detector, matched_signal, detail, scanned_utc, created_utc"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            flag_id, run_id, statement_id, category, severity, blocks,
            DETECTOR, matched_signal, f"detector={DETECTOR}", now, now,
        ),
    )
    return flag_id


def run_risk(
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
    """Run one Lane-4 risk pass over a set of statements.

    Opens a ``lane='4_risk'`` row on the shared ``ai_extraction_runs`` ledger,
    screens each statement (writing a flag row per finding), and finalizes the
    ledger with the produced flag ids and an execution ``error_status``.

    ``error_status`` reflects *execution health*, not the findings: ``ok`` when
    every statement was screened, ``failed`` when a screen raised (fail-closed —
    the findings of a failed run cannot be trusted). A clean run that flags many
    no-gos is still ``ok`` at the run level — the *block* comes from the
    per-statement ``blocks_downstream`` flags + the claims' ``not_publishable``
    default, never from this status. Never raises on a per-statement failure.
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
        prompt_id=None,  # deterministic screen — no grounded-generation prompt
        retry_of_run_id=retry_of_run_id,
        retry_count=retry_count,
        dry_run=dry_run,
        commit=False,
    )

    flag_ids: list[str] = []
    scanned_ids: list[str] = []
    blocking_ids: set[str] = set()
    errors: list[str] = []

    for statement_id in input_statement_ids:
        try:
            findings = scan_statement(conn, statement_id)
        except (RiskScanError, sqlite3.Error) as exc:
            errors.append(f"{statement_id}={type(exc).__name__}: {exc}")
            continue
        scanned_ids.append(statement_id)
        for finding in findings:
            fid = _record_flag(
                conn,
                run_id=run_id,
                statement_id=statement_id,
                category=finding["category"],
                severity=finding["severity"],
                matched_signal=finding.get("matched_signal"),
            )
            flag_ids.append(fid)
            if finding["severity"] == "no_go":
                blocking_ids.add(statement_id)

    if errors and not scanned_ids:
        error_status = "failed"
        error_detail = f"all {len(errors)} statement(s) failed risk scan: " + "; ".join(errors)
    elif errors:
        error_status = "partial"
        error_detail = f"{len(errors)} statement(s) failed: " + "; ".join(errors)
    else:
        error_status = "ok"
        error_detail = None

    ai.finalize_run(
        conn,
        run_id,
        output_statement_ids=scanned_ids,
        output_evidence_link_ids=flag_ids,  # the run's output artifact ids = flags
        orphan_rejected_count=0,
        error_status=error_status,
        error_detail=error_detail,
        commit=commit,
    )

    return {
        "run_id": run_id,
        "ok": error_status == "ok",
        "error_status": error_status,
        "scanned_count": len(scanned_ids),
        "flag_ids": flag_ids,
        "flag_count": len(flag_ids),
        "blocked_statement_ids": sorted(blocking_ids),
        "errors": errors,
    }


# --- Lane-4 reads ------------------------------------------------------------

def open_risk_flags(conn: sqlite3.Connection, statement_id: str) -> list[dict[str, Any]]:
    """Unresolved blocking Lane-4 flags for a statement (the gate's no-go set)."""
    rows = conn.execute(
        f"SELECT * FROM {RISK_TABLE} WHERE statement_id = ? "
        "AND blocks_downstream = 1 AND resolved = 0 "
        "ORDER BY created_utc DESC, flag_id DESC",
        (statement_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_flag(
    conn: sqlite3.Connection,
    flag_id: str,
    *,
    reviewer_id: str,
    reason: str,
    commit: bool = True,
) -> None:
    """A reviewer clears a Lane-4 flag (audited). Allowlist: registered humans only."""
    if not is_registered_reviewer(conn, reviewer_id):
        raise ReviewerGateError(
            f"reviewer_id {reviewer_id!r} is not a registered, active human reviewer; "
            "may not resolve a risk flag (human-only allowlist gate, GOV-93)"
        )
    if not (reason or "").strip():
        raise ReviewerGateError("resolving a risk flag requires a non-empty reason")
    conn.execute(
        f"UPDATE {RISK_TABLE} SET resolved = 1, resolved_by = ?, resolved_reason = ?, "
        "resolved_utc = ? WHERE flag_id = ?",
        (reviewer_id, reason, _now_utc_iso(), flag_id),
    )
    if commit:
        conn.commit()


# ===========================================================================
# Lane 5 — runtime reviewer-gate (the ONLY sanctioned promotion path)
# ===========================================================================

def _load_statement(conn: sqlite3.Connection, statement_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT statement_id, statement_text, produced_by, verification_status, "
        "review_state, publication_state, correction_status, source_changed, "
        "segment_id, ai_extraction_run_id FROM statements WHERE statement_id = ?",
        (statement_id,),
    ).fetchone()
    if row is None:
        raise ReviewerGateError(f"statement {statement_id!r} does not resolve")
    return dict(row)


def _producing_run_blocks(conn: sqlite3.Connection, statement: dict[str, Any]) -> str | None:
    """Return a block reason if the producing gateway run is not healthy, else None.

    Fail-closed: a failed/partial producing run blocks promotion (AI_GATEWAY
    "failed gateway processing must block downstream"). A row with no AI run
    (human/automation origin) has nothing to block on here.
    """
    run_id = statement.get("ai_extraction_run_id")
    if not run_id:
        return None
    try:
        run = ai.get_run(conn, run_id)
    except ValueError:
        return f"producing run {run_id!r} not found"
    if run.get("error_status") != "ok":
        return f"producing run {run_id!r} error_status={run.get('error_status')!r}"
    return None


def _recompute_ui_status(conn: sqlite3.Connection, statement: dict[str, Any], verification_status: str, correction_status: str) -> str:
    """Recompute ui_status from the new verification state via the SSOT mapping."""
    source_present = not pub_is_missing(statement.get("segment_id")) or bool(
        conn.execute(
            "SELECT 1 FROM evidence_links WHERE from_node_id = ? AND from_node_type = 'statement' LIMIT 1",
            (statement["statement_id"],),
        ).fetchone()
    )
    archive_present = bool(
        conn.execute(
            "SELECT 1 FROM evidence_links WHERE from_node_id = ? AND from_node_type = 'statement' "
            "AND archive_status = 'available' LIMIT 1",
            (statement["statement_id"],),
        ).fetchone()
    )
    return pub.compute_ui_status(
        {
            "verificationStatus": verification_status,
            "correctionStatus": correction_status,
            "sourceChanged": bool(statement.get("source_changed")),
            "sourcePresent": source_present,
            "archivePresent": archive_present,
            "rawPreserved": False,
        }
    )


def pub_is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


# The verificationStatus each decision drives the claim to (the from->to chain).
# approve/correct must name an explicit reviewed target (validated below); the
# terminal decisions are fixed by 1.11 §4.3.
_TERMINAL_STATUS = {
    "disputed": "disputed",
    "rejected": "do_not_publish",
}


def promote_statement(
    conn: sqlite3.Connection,
    statement_id: str,
    *,
    reviewer_id: str,
    decision: str,
    reason: str,
    to_verification_status: str | None = None,
    reason_category: str | None = None,
    run_id: str | None = None,
    decision_id: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """The Lane-5 runtime reviewer-gate — the ONLY sanctioned promotion path.

    Records one :data:`DECISION_TABLE` audit row, then (and only then) applies the
    decision to the claim's ``verification_status`` / ``review_state`` /
    ``ui_status``. Fail-closed; raises :class:`ReviewerGateError` (writing
    nothing) when:

    * ``reviewer_id`` is not a registered, active human reviewer
      (:func:`is_registered_reviewer` — allowlist, default-deny; covers empty,
      automation/AI sentinels, AND any unknown/unregistered id) — "promoting an AI
      row without a reviewer decision is rejected" (1.09 step 11 / G2; acceptance);
    * ``decision`` is not a Lane-5 action, or ``reason`` is empty;
    * a *promoting* decision (approve/correct) names a ``to_verification_status``
      that is not one of the reviewed record statuses;
    * a *promoting* decision is attempted while the producing gateway run is not
      ``ok`` (failed-run downstream block) or an unresolved no-go risk flag exists.

    NEVER flips ``publication_state`` to ``publishable`` — that is the separate
    owner decision (1.11 P8); nothing AI-written is publishable by default even
    after this promotion. Returns the decision + the claim's new state.
    """
    statement = _load_statement(conn, statement_id)

    # --- gate 1: the reviewer must be a registered, active human (allowlist) ---
    rid = (reviewer_id or "").strip()
    if not is_registered_reviewer(conn, rid):
        raise ReviewerGateError(
            f"reviewer_id {reviewer_id!r} is not a registered, active human "
            "reviewer; only a registered reviewer may promote (1.09 step 11 / G2, "
            "GOV-93 allowlist). Empty, automation/AI sentinel, unknown, and revoked "
            "ids are all rejected by default"
        )
    if decision not in REVIEWER_DECISIONS:
        raise ReviewerGateError(
            f"decision {decision!r} not in {sorted(REVIEWER_DECISIONS)}"
        )
    if not (reason or "").strip():
        raise ReviewerGateError("a reviewer decision requires a non-empty reason")

    promoting = decision in PROMOTING_DECISIONS
    from_status = statement["verification_status"]
    target_status = from_status  # default: unchanged (hold)

    if promoting:
        # --- gate 2: a promotion names a real reviewed target ---
        if to_verification_status not in pub.REVIEWED_VERIFICATION_STATUSES:
            raise ReviewerGateError(
                f"to_verification_status {to_verification_status!r} is not a reviewed "
                f"status {sorted(pub.REVIEWED_VERIFICATION_STATUSES)}; a promotion must "
                "land on a reviewed value"
            )
        # --- gate 3: failed producing run blocks downstream ---
        run_block = _producing_run_blocks(conn, statement)
        if run_block is not None:
            raise ReviewerGateError(f"promotion blocked: {run_block}")
        # --- gate 4: an open no-go risk flag blocks downstream ---
        open_flags = open_risk_flags(conn, statement_id)
        if open_flags:
            cats = sorted({f["risk_category"] for f in open_flags})
            raise ReviewerGateError(
                f"promotion blocked: {len(open_flags)} unresolved no-go risk flag(s) "
                f"{cats}; a reviewer must resolve them first"
            )
        target_status = to_verification_status
    elif decision in _TERMINAL_STATUS:
        target_status = _TERMINAL_STATUS[decision]

    # correction_status moves to 'corrected' only on a 'corrected' decision.
    correction_status = "corrected" if decision == "corrected" else statement["correction_status"]
    # review_state reflects the human action.
    review_state = "in_review" if decision == "hold" else "reviewed"

    # --- write the audit row FIRST (the who/when/reason hook, 1.11 §6.5) ---
    decision_id = decision_id or f"{statement_id}:dec:{rid}:{from_status}->{target_status}"
    now = _now_utc_iso()
    conn.execute(
        f"INSERT INTO {DECISION_TABLE} ("
        "decision_id, statement_id, run_id, reviewer_id, decision, "
        "from_verification_status, to_verification_status, reason, reason_category, "
        "promoted, decided_utc, created_utc"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id, statement_id, run_id or statement.get("ai_extraction_run_id"),
            rid, decision, from_status, target_status, reason, reason_category,
            1 if promoting else 0, now, now,
        ),
    )

    # --- then apply the transition to the claim (no publication_state flip) ---
    new_ui = _recompute_ui_status(conn, statement, target_status, correction_status)
    conn.execute(
        "UPDATE statements SET verification_status = ?, review_state = ?, "
        "correction_status = ?, ui_status = ? WHERE statement_id = ?",
        (target_status, review_state, correction_status, new_ui, statement_id),
    )
    if commit:
        conn.commit()

    return {
        "decision_id": decision_id,
        "statement_id": statement_id,
        "decision": decision,
        "promoted": promoting,
        "from_verification_status": from_status,
        "to_verification_status": target_status,
        "review_state": review_state,
        "ui_status": new_ui,
        # never publishable from here — owner gate (P8).
        "publication_state": statement["publication_state"],
    }


# --- Lane-5 reads / downstream gate -----------------------------------------

def latest_decision(conn: sqlite3.Connection, statement_id: str) -> dict[str, Any] | None:
    """Most recent reviewer decision for a statement (the audit-trail view)."""
    row = conn.execute(
        f"SELECT * FROM {DECISION_TABLE} WHERE statement_id = ? "
        "ORDER BY created_utc DESC, decision_id DESC LIMIT 1",
        (statement_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def statement_publication_blocked(conn: sqlite3.Connection, statement_id: str) -> bool:
    """Fail-closed downstream/publication gate for a statement.

    Returns True (blocked) unless EVERY condition holds:

    * the claim resolves and is at a reviewed ``verification_status``;
    * a promoting reviewer decision exists in the audit ledger (a human moved it);
    * no unresolved no-go Lane-4 risk flag remains;
    * the producing gateway run (if any) is ``error_status='ok'``;
    * the computed ``ui_status`` is in ``PUBLICATION_ELIGIBLE_UI_STATUSES``;
    * the DB ``publication_state`` is explicitly ``publishable``.

    The last clause is the belt-and-braces owner gate (1.11 P8): because
    :func:`promote_statement` never flips ``publication_state``, an AI claim stays
    blocked here even once reviewed — exactly the "nothing AI-written is
    publishable by default" invariant. (The first five clauses are what a future
    reviewed-but-owner-approved surface would additionally require.)
    """
    row = conn.execute(
        "SELECT statement_id, verification_status, publication_state, ui_status, "
        "correction_status, source_changed, segment_id, ai_extraction_run_id "
        "FROM statements WHERE statement_id = ?",
        (statement_id,),
    ).fetchone()
    if row is None:
        return True
    statement = dict(row)

    if statement["verification_status"] not in pub.REVIEWED_VERIFICATION_STATUSES:
        return True

    decision = latest_decision(conn, statement_id)
    if not decision or not decision.get("promoted"):
        return True

    if open_risk_flags(conn, statement_id):
        return True

    if _producing_run_blocks(conn, statement) is not None:
        return True

    ui = _recompute_ui_status(
        conn, statement, statement["verification_status"], statement["correction_status"]
    )
    if ui not in pub.PUBLICATION_ELIGIBLE_UI_STATUSES:
        return True

    return statement["publication_state"] != "publishable"
