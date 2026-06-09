"""Speaker-attribution safety + person/role + temporal layering (GOV-83, Slice 2 D).

Contract 1.07 §3 (speaker attribution; persons/roles) + §4 (known-then vs later
layers; outcome / outcome_updates). Builds on the GOV-82 (0007) statements spine.

This module turns the migration-0008 tables into a *guarded* write path. Two
contract invariants that a single-row SQL CHECK cannot express live here:

* **"No name is better than wrong attribution" (1.07 §3; COMPANY.md).** An
  attribution requested as ``attributed`` is mechanically DOWNGRADED to a safe
  state whenever its evidence is weak — low confidence, an unconfirmed person, or
  a ``speaker_class`` that does not permit naming. A downgraded record renders the
  safe generic/role-only label, binds NO ``person_id``, and gets NO
  ``made_statement`` edge. Naming an ``on-record-public`` speaker is a hard stop
  (raises) — it routes to CEO, never auto-names (§3.4).
* **Append-only temporal layering (1.07 §4.2).** :func:`link_outcome_updates`
  inserts a forward-only edge from a new ``outcome`` to a prior node and NEVER
  issues an UPDATE/DELETE against the target — the ``known_then`` row is left
  byte-for-byte intact.

ENUM REUSE (1.07 §5 / gap analysis D-5): the 6-value record ``verificationStatus``
enum and ``compute_ui_status`` are owned by :mod:`publication`; the §4 ``layer``
enum and ``confidence`` domain are owned by :mod:`statements`. Both are IMPORTED
here, never re-declared. The only new vocabularies (the §3 ``attribution_state``,
``speaker_class``, ``reviewer_state``) are defined once below.

SCOPE LOCK (this slice): Alpine-only, local/vault-only, NO AI, no network.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

# Reuse the SSOT enums — do NOT re-type them (1.07 §5; gap analysis D-5).
import publication as pub
import statements as st

# --- §3 vocabularies introduced by 1.07 (defined once, here) ----------------

# §3.2 — the explicit uncertain/withheld states. uncertain + unattributed both
# fail closed to a generic label.
ALLOWED_ATTRIBUTION_STATES = frozenset({"attributed", "uncertain", "unattributed"})

# §3.3 — attribution permission. Only on-record-official may be auto-named;
# on-record-public is a CEO hard stop; unidentified/private-context never name.
ALLOWED_SPEAKER_CLASSES = frozenset(
    {"on-record-official", "on-record-public", "unidentified", "private-context"}
)

# Classes that *may* carry a name when the attribution is `attributed`.
# on-record-public is intentionally absent here — it requires recorded CEO
# approval and is a hard stop in automation (§3.4).
AUTO_NAMEABLE_CLASSES = frozenset({"on-record-official"})

ALLOWED_REVIEWER_STATES = frozenset({"unreviewed", "approved", "rejected"})

# Reuse — never copy — the layer + confidence domains.
ALLOWED_LAYERS = st.ALLOWED_LAYERS
ALLOWED_CONFIDENCE = st.ALLOWED_CONFIDENCE

# Confidence levels strong enough to keep an `attributed` state. `low` fails
# closed — "no name is better than wrong attribution".
NAMING_CONFIDENCE = frozenset({"high", "medium"})

# The safe generic labels (§3.2). Never a name, never a candidate.
SAFE_GENERIC_LABEL = "Meeting Attendee"
SAFE_COMMUNITY_LABEL = "Community Member"

# The "speaker not confirmed" note attached to an `uncertain` render (§3.2).
UNCERTAIN_NOTE = "speaker not confirmed"


class SpeakerAttributionHardStop(RuntimeError):
    """Naming a speaker that requires a human/CEO gate (on-record-public, §3.4)."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def derive_attribution_state(
    requested_state: str,
    speaker_class: str,
    confidence: str,
    *,
    person_confirmed: bool,
) -> str:
    """Fail-closed resolution of the final attribution_state (1.07 §3).

    Returns ``attributed`` ONLY when every safety gate passes: the caller asked
    for ``attributed``, an identity is confirmed from official records, the
    confidence is not ``low``, and the ``speaker_class`` permits auto-naming
    (``on-record-official``). Any failure downgrades to a safe state — ``uncertain``
    when a candidate identity existed (so reviewers keep the context), otherwise
    ``unattributed``. ``private-context`` always resolves to ``unattributed``
    (never attribute a non-public-meeting speaker).
    """
    if requested_state not in ALLOWED_ATTRIBUTION_STATES:
        raise ValueError(
            f"attribution_state {requested_state!r} not in {sorted(ALLOWED_ATTRIBUTION_STATES)}"
        )
    if speaker_class not in ALLOWED_SPEAKER_CLASSES:
        raise ValueError(
            f"speaker_class {speaker_class!r} not in {sorted(ALLOWED_SPEAKER_CLASSES)}"
        )
    if confidence not in ALLOWED_CONFIDENCE:
        raise ValueError(f"confidence {confidence!r} not in {sorted(ALLOWED_CONFIDENCE)}")

    # private-context: never attribute, and do not even retain a candidate hint.
    if speaker_class == "private-context":
        return "unattributed"

    if requested_state == "unattributed":
        return "unattributed"

    # A request to name (attributed) must clear every gate or it fails closed.
    naming_ok = (
        requested_state == "attributed"
        and person_confirmed
        and confidence in NAMING_CONFIDENCE
        and speaker_class in AUTO_NAMEABLE_CLASSES
    )
    if naming_ok:
        return "attributed"

    # A candidate existed (requested attributed/uncertain) but a gate failed:
    # keep it as `uncertain` so the candidate survives as reviewer-only context.
    return "uncertain"


def safe_speaker_label(attribution: dict[str, Any]) -> str:
    """The renderable speaker label — provably name-free unless safely attributed.

    Emits a name ONLY when ``attribution_state == 'attributed'`` and the
    ``speaker_class`` permits naming; otherwise returns a role-only or generic
    label and NEVER the candidate name (§3.2). The label rendered for an
    ``attributed`` official is ``"Name, Role"`` (role omitted if unknown).
    """
    state = attribution.get("attribution_state")
    speaker_class = attribution.get("speaker_class")

    if state == "attributed" and speaker_class in AUTO_NAMEABLE_CLASSES:
        name = attribution.get("display_name")
        role = attribution.get("role_title")
        if not _is_missing(name) and not _is_missing(role):
            return f"{name}, {role}"
        if not _is_missing(name):
            return str(name)
        # attributed but no name available -> fall through to safe label.

    # Fail closed. Prefer an explicit name-free role-only label if the caller
    # supplied one (e.g. "Council Member, Town of Alpine" with no person), else a
    # generic label. The candidate name is never consulted here.
    role_only = attribution.get("role_only_label")
    if not _is_missing(role_only):
        return str(role_only)
    if speaker_class == "on-record-public":
        return SAFE_COMMUNITY_LABEL
    return SAFE_GENERIC_LABEL


def attribute_speaker(
    conn: sqlite3.Connection,
    attribution: dict[str, Any],
    *,
    ceo_approved_public: bool = False,
    commit: bool = True,
) -> dict[str, Any]:
    """Insert one speaker_attribution under the §3 safety rules.

    The caller passes its *requested* attribution. This function resolves the
    SAFE final state via :func:`derive_attribution_state`, binds ``person_id``
    ONLY when the final state is ``attributed`` (the candidate otherwise lives in
    the private ``candidate_person_id``), computes the safe label, and — only for
    a safely-named official — writes the ``made_statement`` person edge.

    ``on-record-public`` naming is a hard stop: unless ``ceo_approved_public`` is
    explicitly passed (a recorded human gate), any attempt to *name* such a
    speaker raises :class:`SpeakerAttributionHardStop`. Automation never sets the
    flag, so it never auto-names a community member (§3.4).

    Returns the stored attribution dict (final ``attribution_state``, resolved
    ``speaker_label``, and any ``note``).
    """
    attribution_id = attribution.get("speaker_attribution_id")
    if _is_missing(attribution_id):
        raise ValueError("attribution requires a non-empty speaker_attribution_id")
    statement_id = attribution.get("statement_id")
    if _is_missing(statement_id):
        raise ValueError("attribution requires a statement_id")

    requested_state = attribution.get("attribution_state", "unattributed")
    speaker_class = attribution.get("speaker_class", "unidentified")
    confidence = attribution.get("confidence", "low")
    candidate_person_id = attribution.get("person_id") or attribution.get("candidate_person_id")
    person_confirmed = bool(attribution.get("person_confirmed"))
    reviewer_state = attribution.get("reviewer_state", "unreviewed")
    if reviewer_state not in ALLOWED_REVIEWER_STATES:
        raise ValueError(f"reviewer_state {reviewer_state!r} invalid")

    # The statement must exist — an attribution joins an existing statement (§3.4).
    if conn.execute(
        "SELECT 1 FROM statements WHERE statement_id = ?", (statement_id,)
    ).fetchone() is None:
        raise ValueError(f"statement_id {statement_id!r} does not resolve to a statement")

    final_state = derive_attribution_state(
        requested_state, speaker_class, confidence, person_confirmed=person_confirmed
    )

    # Hard stop: naming an on-record-public speaker needs a recorded CEO gate.
    # We only trip it when the caller actually tried to name (attributed) such a
    # speaker without the flag — failing closed to `uncertain` would silently
    # swallow a decision that must reach a human, so we raise instead.
    if (
        speaker_class == "on-record-public"
        and requested_state == "attributed"
        and not ceo_approved_public
    ):
        raise SpeakerAttributionHardStop(
            f"naming on-record-public speaker for statement {statement_id!r} requires "
            "recorded CEO approval — routing to CEO (1.07 §3.4 hard stop)"
        )

    # CEO-approved public naming is the one extra path to a bound identity.
    if (
        final_state != "attributed"
        and speaker_class == "on-record-public"
        and requested_state == "attributed"
        and ceo_approved_public
        and person_confirmed
        and confidence in NAMING_CONFIDENCE
    ):
        final_state = "attributed"

    named = final_state == "attributed" and (
        speaker_class in AUTO_NAMEABLE_CLASSES
        or (speaker_class == "on-record-public" and ceo_approved_public)
    )

    # Bind a resolved identity ONLY when safely named; otherwise the candidate
    # survives only as the private reviewer hint.
    bound_person_id = candidate_person_id if named else None
    stored_candidate = None if named else (candidate_person_id if final_state == "uncertain" else None)

    # The name is authoritative on the `persons` record (public-record identity),
    # NOT on the caller's payload — resolve it from the DB only when safely named.
    display_name = None
    if named and not _is_missing(bound_person_id):
        prow = conn.execute(
            "SELECT display_name FROM persons WHERE person_id = ?", (bound_person_id,)
        ).fetchone()
        if prow is None:
            raise ValueError(f"person_id {bound_person_id!r} does not resolve to a person")
        display_name = prow["display_name"]

    label = safe_speaker_label(
        {
            "attribution_state": final_state,
            "speaker_class": speaker_class,
            "display_name": display_name,
            "role_title": attribution.get("role_title"),
            "role_only_label": attribution.get("role_only_label"),
        }
    )
    note = UNCERTAIN_NOTE if final_state == "uncertain" else None

    now = _now_utc_iso()
    conn.execute(
        "INSERT INTO speaker_attributions ("
        "speaker_attribution_id, statement_id, attribution_state, speaker_class, "
        "person_id, role_id, candidate_person_id, display_label, basis, "
        "minutes_source_id, reviewer_state, confidence, created_utc"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attribution_id,
            statement_id,
            final_state,
            speaker_class,
            bound_person_id,
            attribution.get("role_id"),
            stored_candidate,
            label,
            attribution.get("basis"),
            attribution.get("minutes_source_id"),
            reviewer_state,
            confidence,
            now,
        ),
    )

    # Keep the statement's denormalized pointer in sync (forward pointer added in
    # 0007). Never rewrites any other field.
    conn.execute(
        "UPDATE statements SET speaker_attribution_id = ? WHERE statement_id = ?",
        (attribution_id, statement_id),
    )

    made_statement_id = None
    if named and not _is_missing(bound_person_id):
        made_statement_id = f"{statement_id}:by:{bound_person_id}"
        conn.execute(
            "INSERT OR IGNORE INTO made_statement ("
            "made_statement_id, person_id, statement_id, role_id, created_utc"
            ") VALUES (?, ?, ?, ?, ?)",
            (made_statement_id, bound_person_id, statement_id, attribution.get("role_id"), now),
        )

    if commit:
        conn.commit()

    return {
        "speaker_attribution_id": attribution_id,
        "statement_id": statement_id,
        "attribution_state": final_state,
        "speaker_class": speaker_class,
        "person_id": bound_person_id,
        "candidate_person_id": stored_candidate,
        "speaker_label": label,
        "made_statement_id": made_statement_id,
        "note": note,
    }


def insert_outcome(
    conn: sqlite3.Connection,
    outcome: dict[str, Any],
    *,
    commit: bool = True,
) -> dict[str, Any]:
    """Insert one ``outcome`` row (1.07 §4). Validates the layer/verification enums.

    An outcome is a later real-world result; it defaults to the ``actual_later``
    layer and the fail-closed ``machine_extracted_unreviewed`` record status.
    Inserting an outcome does NOT touch any prior node — linking is a separate
    forward-only step (:func:`link_outcome_updates`).
    """
    outcome_id = outcome.get("outcome_id")
    if _is_missing(outcome_id):
        raise ValueError("outcome requires a non-empty outcome_id")
    if _is_missing(outcome.get("outcome_text")):
        raise ValueError("outcome requires non-empty outcome_text")

    layer = outcome.get("layer", "actual_later")
    if layer not in ALLOWED_LAYERS:
        raise ValueError(f"layer {layer!r} not in {sorted(ALLOWED_LAYERS)}")

    verification_status = outcome.get("verification_status", "machine_extracted_unreviewed")
    if verification_status not in pub.ALLOWED_VERIFICATION_STATUSES:
        raise ValueError(
            f"verification_status {verification_status!r} not a 6-value record status"
        )
    confidence = outcome.get("confidence", "medium")
    if confidence not in ALLOWED_CONFIDENCE:
        raise ValueError(f"confidence {confidence!r} invalid")

    now = _now_utc_iso()
    conn.execute(
        "INSERT INTO outcomes ("
        "outcome_id, outcome_date, outcome_text, layer, source_id, "
        "verification_status, correction_status, confidence, created_utc"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            outcome_id,
            outcome.get("outcome_date"),
            outcome["outcome_text"],
            layer,
            outcome.get("source_id"),
            verification_status,
            outcome.get("correction_status", "none"),
            confidence,
            now,
        ),
    )
    if commit:
        conn.commit()
    return {"outcome_id": outcome_id, "layer": layer}


def link_outcome_updates(
    conn: sqlite3.Connection,
    outcome_id: str,
    to_node_id: str,
    *,
    to_node_type: str = "statement",
    relation: str = "updates",
    commit: bool = True,
) -> str:
    """Forward-only link from an outcome to a prior node — NEVER mutates the target.

    This is the structural guarantee of 1.07 §4.2: a later outcome links FORWARD
    to the ``known_then`` node it updates; the prior row is left intact so the
    historical record ("what was known then") stays auditable. The function only
    ever INSERTs into ``outcome_updates`` — it issues no UPDATE/DELETE against the
    target table — and a unit test asserts the target row is unchanged afterward.
    """
    if relation not in ("updates", "corrects"):
        raise ValueError(f"relation {relation!r} must be 'updates' or 'corrects'")
    if conn.execute(
        "SELECT 1 FROM outcomes WHERE outcome_id = ?", (outcome_id,)
    ).fetchone() is None:
        raise ValueError(f"outcome_id {outcome_id!r} does not resolve to an outcome")

    edge_id = f"{outcome_id}:updates:{to_node_type}:{to_node_id}"
    conn.execute(
        "INSERT OR IGNORE INTO outcome_updates ("
        "outcome_update_id, outcome_id, to_node_id, to_node_type, relation, created_utc"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (edge_id, outcome_id, to_node_id, to_node_type, relation, _now_utc_iso()),
    )
    if commit:
        conn.commit()
    return edge_id
