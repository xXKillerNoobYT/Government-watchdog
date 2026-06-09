"""Data-layer SSOT + publication control for Government Watchdog records.

Stage 1 Slice 1, Issue D (GOV-76). Contract 1.05 (single-source-of-truth +
publication allowlist), aligned to 1.06 / 1.11 / 1.12. Source: GOV-71 §2.D,
GOV-72 gap analysis §3.4 / §5.1.

This module is the **single source of truth** for the record-authoritative
6-value ``verificationStatus`` enum, the ``uiStatus`` vocabulary, the fail-closed
publication allowlist, and the ``compute_ui_status`` mapping. The enum/uiStatus
core is **ported verbatim** from the reviewed gov-17 export validator
(``scripts/validate_concept_map_export.py`` on
``gov-17-newsletter-briefing-contract``) so the two consumers cannot drift
(1.05-g structural drift guard, GOV-36/37/38/39 fail-closed correction). Do not
re-type these constants elsewhere — import them.

Issue D adds two binding pieces on top of the ported core:

* **D-1** — the explicit 11-value 1.02 *registry* vocabulary -> 6-value
  *record* enum mapping (:data:`VERIFICATION_STATUS_MAP`), with a parity guard
  that every registry value resolves to a defined record value and that the
  "source changed / needs review" signal is preserved as ``sourceChanged=True``
  (never silently dropped). The 11->6 delta is a *mapping* concern; the 6-value
  enum stays the enum-of-record.
* **D-2** — the web-safe backend->frontend field allowlist
  (:data:`WEB_SAFE_FIELD_ALLOWLIST`) and the fail-closed :func:`to_web_safe`
  serializer. A field is publishable only if explicitly allowlisted; raw paths,
  hashes, owner/agent provenance, and all reviewer-state fields are excluded.

Default posture everywhere: **not publishable / unreviewed** unless an explicit
reviewed transition says otherwise.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Ported core (single source of truth) — keep byte-for-byte aligned with
# gov-17 validate_concept_map_export.py. Changing a value here is an
# enum-of-record decision in the VerificationSafetyReviewer / CTO lane.
# ---------------------------------------------------------------------------

# Record-authoritative 6-value verificationStatus enum (1.05-c). ``None`` (a
# nullable column) means pre-review.
ALLOWED_VERIFICATION_STATUSES = {
    "source_recorded",
    "machine_extracted_unreviewed",
    "reviewed_source_linked",
    "human_verified",
    "disputed",
    "do_not_publish",
}

# uiStatus-map.v1 (GOV-36/37/38/39): backend-canonical 10-state trust
# vocabulary. kebab-case is the wire form.
ALLOWED_UI_STATUSES = {
    "do-not-publish",
    "disputed",
    "source-missing",
    "source-changed",
    "corrected",
    "needs-clarification",
    "unverified",
    "pending-review",
    "archived-source-backed",
    "source-backed",
}

# Fail-closed publication allowlist (GOV-37 Blocker 2 / GOV-39): a record may be
# publicationState=publishable ONLY when its computed uiStatus is one of these.
# Every other (and any future) uiStatus is publication-gated by default.
PUBLICATION_ELIGIBLE_UI_STATUSES = {
    "source-backed",
    "archived-source-backed",
    "corrected",
}

# Each verificationStatus value the uiStatus mapping consumes, keyed off the
# authoritative enum so divergence fails at import time rather than shipping a
# fail-open mapping (GOV-36 CTO Blocker 6). "reviewed_set" marks the two
# completed-review states (rules 5, 10, 11).
_VERIFICATION_STATUS_ROLES = {
    "do_not_publish": "rule1",                 # -> do-not-publish
    "disputed": "rule2",                       # -> disputed
    "machine_extracted_unreviewed": "rule7",   # -> unverified
    "source_recorded": "rule8",                # -> pending-review
    "reviewed_source_linked": "reviewed_set",  # -> rules 5/10/11
    "human_verified": "reviewed_set",          # -> rules 5/10/11
}

# Structural drift guard (1.05-g): the verificationStatus values the mapping
# reasons about MUST equal the authoritative enum exactly — no missing value
# (would fail open through rule 12's default) and no invented value (a dead
# rule). Fails at import time.
assert set(_VERIFICATION_STATUS_ROLES) == ALLOWED_VERIFICATION_STATUSES, (
    "uiStatus-map.v1 verificationStatus inputs drifted from "
    "ALLOWED_VERIFICATION_STATUSES: "
    f"mapping_only={set(_VERIFICATION_STATUS_ROLES) - ALLOWED_VERIFICATION_STATUSES}, "
    f"enum_only={ALLOWED_VERIFICATION_STATUSES - set(_VERIFICATION_STATUS_ROLES)}"
)

# Completed-review set, derived from the same authoritative data so it cannot
# drift independently.
REVIEWED_VERIFICATION_STATUSES = frozenset(
    status
    for status, role in _VERIFICATION_STATUS_ROLES.items()
    if role == "reviewed_set"
)


def compute_ui_status(record: dict[str, Any]) -> str:
    """Compute a record's uiStatus per uiStatus-map.v1 (rules #1-#12).

    Evaluated top-down, first match wins (publication-gating states outrank
    reassuring ones). Total and fail-closed: any otherwise-unhandled or unknown
    input combination resolves to the gated ``pending-review`` (rule 12), never
    to a reassuring ``source-backed``. Absent boolean signals are treated as
    ``False`` — the conservative, fail-closed direction.

    Accepts the camelCase signal keys used by the ported card mapping
    (``verificationStatus``, ``correctionStatus``, ``sourceChanged``,
    ``sourcePresent``, ``archivePresent``, ``rawPreserved``).
    """
    status = record.get("verificationStatus")
    correction = record.get("correctionStatus")
    source_changed = bool(record.get("sourceChanged"))
    source_present = bool(record.get("sourcePresent"))
    archive_present = bool(record.get("archivePresent"))
    raw_preserved = bool(record.get("rawPreserved"))
    reviewed = status in REVIEWED_VERIFICATION_STATUSES

    if status == "do_not_publish":  # 1
        return "do-not-publish"
    if status == "disputed":  # 2
        return "disputed"
    if not source_present and not archive_present and not raw_preserved:  # 3
        return "source-missing"
    if source_changed:  # 4
        return "source-changed"
    if reviewed and correction == "corrected":  # 5 (reviewed guard)
        return "corrected"
    if correction == "needs_clarification":  # 6
        return "needs-clarification"
    if status == "machine_extracted_unreviewed":  # 7
        return "unverified"
    if status == "source_recorded":  # 8
        return "pending-review"
    if status is None:  # 9
        return "pending-review"
    if reviewed and not source_present and (archive_present or raw_preserved):  # 10
        return "archived-source-backed"
    if reviewed and source_present:  # 11
        return "source-backed"
    return "pending-review"  # 12 fail closed


def is_publication_eligible(record: dict[str, Any]) -> bool:
    """True only if the record's computed uiStatus is on the publish allowlist.

    Fail-closed: anything not explicitly allowlisted is gated. This is the
    *eligibility* test (uiStatus side). The DB ``publication_state`` column is a
    separate, default-not-publishable gate that must also be flipped by an
    explicit reviewed transition — both must agree before anything publishes.
    """
    return compute_ui_status(record) in PUBLICATION_ELIGIBLE_UI_STATUSES


# ---------------------------------------------------------------------------
# D-1: registry (1.02, 11-value) -> record (1.05, 6-value) mapping.
# ---------------------------------------------------------------------------

# The 1.02 source-registry verification vocabulary (11 values). Source of
# record: GOV-GOAL-stage-1-alpine-source-inventory-02 "Verification Status
# Values". This is the *input* domain crawler/registry tooling writes to
# ``sources.verification_status``; it is NOT the record enum.
REGISTRY_VERIFICATION_STATUSES = {
    "verified_live_source",
    "verified_local_and_live_source",
    "verified_local_reference",
    "reviewed_source_linked",
    "machine_extracted_unreviewed",
    "source_backed_interpretation_open",
    "unverified",
    "disputed",
    "changed_needs_review",
    "unavailable_needs_review",
    "do_not_publish",
}

# Explicit 11 -> (6-value verificationStatus, sourceChanged) mapping. GOV-36/37
# moved "source changed" out of the verificationStatus enum into the separate
# ``sourceChanged`` boolean signal; this mapping is where that split is honored.
# Every registry value resolves to a defined 6-value record status — no silent
# fallthrough. None of these *default* to publishable: only the reviewed/verified
# registry states resolve to a reviewed record status, and even then the DB
# publication_state gate stays not-publishable until an explicit transition.
VERIFICATION_STATUS_MAP: dict[str, tuple[str, bool]] = {
    # Reviewed/verified registry states -> reviewed record enum.
    "verified_live_source":             ("reviewed_source_linked", False),
    "verified_local_and_live_source":   ("human_verified", False),
    "verified_local_reference":         ("reviewed_source_linked", False),
    "reviewed_source_linked":           ("reviewed_source_linked", False),
    # Machine / unreviewed registry states -> unreviewed record enum.
    "machine_extracted_unreviewed":     ("machine_extracted_unreviewed", False),
    "unverified":                       ("machine_extracted_unreviewed", False),
    # Source-backed but interpretation not yet reviewed -> pending review.
    "source_backed_interpretation_open": ("source_recorded", False),
    # Dispute / block carry through unchanged.
    "disputed":                         ("disputed", False),
    "do_not_publish":                   ("do_not_publish", False),
    # "Needs review" registry states revert to the recorded/unreviewed base.
    # changed_needs_review is the load-bearing parity case: the change signal is
    # preserved as sourceChanged=True so uiStatus rule #4 (-> "source-changed")
    # fires and the record can never be silently treated as still-current.
    "changed_needs_review":             ("source_recorded", True),
    "unavailable_needs_review":         ("source_recorded", False),
}

# Parity drift guard (D-1): the mapping domain MUST equal the registry
# vocabulary exactly, and every output MUST be a real 6-value record status.
# Fails at import time so a dropped/added registry value can never ship a
# silent fallthrough.
assert set(VERIFICATION_STATUS_MAP) == REGISTRY_VERIFICATION_STATUSES, (
    "VERIFICATION_STATUS_MAP domain drifted from REGISTRY_VERIFICATION_STATUSES: "
    f"mapping_only={set(VERIFICATION_STATUS_MAP) - REGISTRY_VERIFICATION_STATUSES}, "
    f"registry_only={REGISTRY_VERIFICATION_STATUSES - set(VERIFICATION_STATUS_MAP)}"
)
assert all(
    record_status in ALLOWED_VERIFICATION_STATUSES
    for record_status, _ in VERIFICATION_STATUS_MAP.values()
), "VERIFICATION_STATUS_MAP produced a value outside the 6-value record enum"


class UnknownRegistryStatus(ValueError):
    """Raised when an unrecognized registry verification_status is mapped."""


def map_registry_verification(value: str | None) -> tuple[str | None, bool]:
    """Map a 1.02 registry ``verification_status`` to ``(verificationStatus, sourceChanged)``.

    Fail-closed:

    * ``None`` (registry not yet set) -> ``(None, False)`` = pre-review.
    * a known registry value -> its mapped ``(record_status, source_changed)``.
    * an unknown value -> :class:`UnknownRegistryStatus` (never a silent
      fallthrough to a reassuring state).
    """
    if value is None:
        return (None, False)
    try:
        return VERIFICATION_STATUS_MAP[value]
    except KeyError as exc:
        raise UnknownRegistryStatus(
            f"unknown registry verification_status {value!r}; "
            f"expected one of {sorted(REGISTRY_VERIFICATION_STATUSES)}"
        ) from exc


# ---------------------------------------------------------------------------
# D-2: web-safe backend -> frontend field allowlist (1.05-l), fail-closed.
# ---------------------------------------------------------------------------

# Explicit allowlist of fields that may cross the backend->frontend boundary.
# Fail-closed: a field is publishable ONLY if it appears here. Anything not
# listed — including any future column — is dropped by to_web_safe(). This is
# the data-layer enforcement of 1.05-l, not a UI concern.
WEB_SAFE_FIELD_ALLOWLIST = frozenset({
    # identity / classification (presentation-safe)
    "source_id",
    "name",
    "scope",
    "jurisdiction",
    "source_type",
    "source_class",
    "source_authority_level",
    # public locators (the published, citable URLs)
    "url",
    "original_url",
    "archive_url",
    "archive_status",
    "expected_artifacts",
    "topic_tags",
    # public timing
    "scan_date",
    "last_validated_utc",
    # publication-control surface (the computed, safe-to-show labels)
    "verification_status",   # the mapped 6-value record status
    "correction_status",
    "produced_by",
    "ui_status",
    "source_changed",
    "publication_state",
    # --- Stage 1 Slice 4 Prereq-0 (GOV-98) concept-map read-API fields -------
    # The 1.07 graph + agenda-thread/topic-tree shapes the reviewer-internal
    # read-API serves. Same fail-closed rule: a field crosses only if named here.
    # Deliberately EXCLUDES every raw/private locator (transcript_path,
    # deep_link, segment_id, timestamp_seconds-only raw cues), reviewer identity
    # (created_by), and free-text notes (note) — see WEB_UNSAFE_FIELDS.
    # statement (web-safe subset of the record)
    "statement_id",
    "statement_text",
    "layer",
    "is_verbatim",
    "confidence",
    "updates_statement_id",
    # agenda_item / meeting grouping (slugs + ordinal, no paths)
    "agenda_item_id",
    "item_order",
    "meeting_id",
    "title",
    # evidence drawer (citation locators + public URLs only)
    "relation",
    "locator_kind",
    "timestamp_human",
    "timestamp_seconds",
    "page",
    "section",
    "paragraph",
    "final_url",
    "to_source_id",
    # agenda_thread node
    "agenda_thread_id",
    "jurisdiction_id",
    "status",
    "first_seen_date",
    "last_seen_date",
    # topic node
    "topic_id",
    # concept_edge (typed graph edge)
    "edge_id",
    "edge_type",
    "from_node_id",
    "from_node_type",
    "to_node_id",
    "to_node_type",
})

# Fields that must NEVER cross to the frontend. Enforced implicitly by the
# allowlist (they are simply absent), but named explicitly so the D-2 exclusion
# set is testable and self-documenting. raw_local_path / raw_sha256 = raw store
# internals; owner_agent = provenance; notes = reviewer notes; review_state +
# raw_preservation_status + robots_policy + local_note_path + registered_utc =
# reviewer/operational state.
WEB_UNSAFE_FIELDS = frozenset({
    "raw_local_path",
    "raw_sha256",
    "owner_agent",
    "notes",
    "review_state",
    "raw_preservation_status",
    "robots_policy",
    "local_note_path",
    "registered_utc",
    # Slice-4 (GOV-98) graph internals that must never cross the boundary:
    # transcript_path/deep_link = raw/private locators (1.07 §7); segment_id =
    # vault-segment provenance; created_by = reviewer/agent identity; note =
    # free-text reviewer/movement note on a concept_edge.
    "transcript_path",
    "deep_link",
    "segment_id",
    "created_by",
    "note",
    # GOV-98 label-layer addendum: the alias sourceRef's vault/local pointer is
    # reviewer-internal provenance only. read_api builds the web-safe sourceRef
    # from the public source id + original/archive URL + locator and omits this.
    "source_ref_local_ref",
})


def to_web_safe(record: dict[str, Any]) -> dict[str, Any]:
    """Project a backend record onto the web-safe field allowlist (fail-closed).

    Only keys in :data:`WEB_SAFE_FIELD_ALLOWLIST` survive; every other key —
    including unknown/future columns and all reviewer-state fields — is dropped.
    This is the single data-layer serializer the backend->frontend handoff must
    go through (1.05-l).
    """
    return {k: v for k, v in record.items() if k in WEB_SAFE_FIELD_ALLOWLIST}


# Sanity guard: the named-unsafe set must never overlap the allowlist, or a
# reviewer-state/raw field could leak. Fails at import time.
assert not (WEB_SAFE_FIELD_ALLOWLIST & WEB_UNSAFE_FIELDS), (
    "web-safe allowlist overlaps the explicitly-unsafe set: "
    f"{WEB_SAFE_FIELD_ALLOWLIST & WEB_UNSAFE_FIELDS}"
)


# ---------------------------------------------------------------------------
# Default publication posture (no record defaults to publishable).
# ---------------------------------------------------------------------------

DEFAULT_PRODUCED_BY = "automation"
DEFAULT_REVIEW_STATE = "unreviewed"
DEFAULT_PUBLICATION_STATE = "not_publishable"

ALLOWED_PRODUCED_BY = {"automation", "ai", "human"}
ALLOWED_PUBLICATION_STATES = {"not_publishable", "publishable"}
