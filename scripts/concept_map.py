"""Concept-map type registry (SSOT) — node/edge vocabulary + GOV-98 additions.

Stage 1 Slice 4 Prereq-0 (GOV-98). Contract: GOV-97 plan Part A.1/A.2; the 1.07
transcript/evidence/statement contract §1.1/§1.2 (the node/edge tables that
"map 1:1 to ``ALLOWED_NODE_TYPES`` / ``ALLOWED_EDGE_TYPES``"). Source:
Docs/stage1-slice4-prereq0-read-api-concept-map.md.

The 1.07 contract referenced ``ALLOWED_NODE_TYPES`` / ``ALLOWED_EDGE_TYPES`` as
living in an export validator that was never committed to this repo (only its
*status* enum core was ported into :mod:`publication`). This module makes the
concept-map **type vocabulary** a real in-repo single source of truth: the
complete 1.07 set PLUS the GOV-98 additions, guarded at import time.

GOV-98 additions (forward-linking only, additive — they never rewrite known-then
context or touch the fail-closed publication path):

* node ``agenda_thread`` (a durable civic subject recurring across meetings).
* edges ``agenda_item_in_thread``, ``agenda_item_supersedes``,
  ``agenda_item_amends``, ``agenda_item_revisits``, ``topic_rollup``.

``topic_groups`` (already in the 1.07 set) is reused for thread-under-topic; the
topic TREE is carried solely by ``topic_rollup`` so grouping and tree stay
separate concerns (GOV-36 separate-concepts rule).

Storage (migration 0012): the new edges live in the generic append-only
``concept_edges`` table; the existing relational-FK spine edges are NOT migrated
here — this module only *names* them in the registry. Acyclicity for
``topic_rollup`` is a cross-row invariant enforced by :func:`insert_edge` (insert
time) and re-validated by :func:`assert_acyclic` / ``read_api.topic_tree``
(serve time).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Node type registry (1.07 §1.1 + GOV-98 A.1).
# ---------------------------------------------------------------------------

# The 1.07 typed-concept vocabulary (the node table that "maps 1:1 to
# ALLOWED_NODE_TYPES"). place_asset / entity are referenced as decision_affects
# endpoints in §1.2 but not yet given their own record table — they are named
# here so every edge endpoint resolves to a known node type (no dangling type).
_NODE_TYPES_1_07 = frozenset({
    "jurisdiction",
    "government_body",
    "meeting",
    "agenda_item",
    "transcript_segment",
    "statement",
    "person",
    "role",
    "source_record",
    "document",
    "evidence_link",
    "vote",
    "decision",
    "outcome",
    "topic",
    "place_asset",
    "entity",
    "card",
})

# GOV-98 A.1 addition.
_NODE_TYPES_GOV98 = frozenset({"agenda_thread"})

ALLOWED_NODE_TYPES = _NODE_TYPES_1_07 | _NODE_TYPES_GOV98

# ---------------------------------------------------------------------------
# Edge type registry (1.07 §1.2 + GOV-98 A.2).
# ---------------------------------------------------------------------------

_EDGE_TYPES_1_07 = frozenset({
    "contains_body",
    "held_meeting",
    "contains_agenda_item",
    "references_source",
    "served_in_role",
    "role_in_body",
    "statement_from_segment",
    "made_statement",
    "voted_on",
    "vote_decided",
    "decision_affects",
    "source_supports",
    "document_supersedes",
    "document_amends",
    "document_replaces",
    "outcome_updates",
    "topic_groups",
    "card_presents",
    "card_links_card",
})

# GOV-98 A.2 additions — forward-linking only.
_EDGE_TYPES_GOV98 = frozenset({
    "agenda_item_in_thread",
    "agenda_item_supersedes",
    "agenda_item_amends",
    "agenda_item_revisits",
    "topic_rollup",
})

ALLOWED_EDGE_TYPES = _EDGE_TYPES_1_07 | _EDGE_TYPES_GOV98

# Endpoint type contract for the GOV-98 edges (+ topic_groups for thread-under-
# topic). Each maps edge_type -> ({allowed from_node_types}, {allowed to_node_types}).
# The generic concept_edges table stores exactly these edge types; insert_edge
# validates endpoints against this contract. Existing relational-FK spine edges
# are not stored generically, so they are not listed here.
EDGE_ENDPOINTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "agenda_item_in_thread":   (frozenset({"agenda_item"}), frozenset({"agenda_thread"})),
    "agenda_item_supersedes":  (frozenset({"agenda_item"}), frozenset({"agenda_item"})),
    "agenda_item_amends":      (frozenset({"agenda_item"}), frozenset({"agenda_item"})),
    "agenda_item_revisits":    (frozenset({"agenda_item"}), frozenset({"agenda_item"})),
    "topic_rollup":            (frozenset({"topic"}),       frozenset({"topic"})),
    "topic_groups":            (frozenset({"topic"}),       frozenset({"agenda_thread", "topic", "statement", "agenda_item"})),
}

# The edge types physically stored in the generic concept_edges table (mirrors
# the migration-0012 CHECK literal). A parity test asserts the two cannot drift.
GENERIC_EDGE_TYPES = frozenset(EDGE_ENDPOINTS)

# Forward-linking edges never rewrite/delete the earlier (to_*) node — consistent
# with outcome_updates and document_* lifecycle. The later node points back.
FORWARD_LINKING_EDGE_TYPES = frozenset({
    "agenda_item_supersedes",
    "agenda_item_amends",
    "agenda_item_revisits",
    "agenda_item_in_thread",
    "topic_rollup",
})

# The lifecycle edges that render with a typed label (BEH-AGENDA-2). UI never
# renders an untyped "related".
AGENDA_LIFECYCLE_EDGE_TYPES = frozenset({
    "agenda_item_supersedes",
    "agenda_item_amends",
    "agenda_item_revisits",
})

# ---------------------------------------------------------------------------
# Plain-language label layer (GOV-98 owner addendum / GOV-97 §A.7).
# ---------------------------------------------------------------------------

# Node types that carry the label layer (canonicalHumanLabel + sourceAliases).
LABELLED_NODE_TYPES = frozenset({"topic", "agenda_thread"})

# A source/government alias is one of these typed kinds. A government string is
# NEVER the primary label — it lives here, with mandatory provenance.
ALLOWED_ALIAS_TYPES = frozenset({
    "government_term",
    "legal_term",
    "historical_term",
    "agenda_label",
})

# ---------------------------------------------------------------------------
# Import-time drift guards (fail at import, not at runtime).
# ---------------------------------------------------------------------------


class RegistryConsistencyError(RuntimeError):
    """The node/edge vocabulary is internally inconsistent. Raised at IMPORT.

    Deliberately NOT ``assert``: `python -O` deletes assert statements outright, so
    these guards would hold in a normal run and be **absent** in an optimised one —
    exactly the condition under which a future edit goes unchecked. That matters here
    because this registry is the vocabulary every edge is validated against, and
    ``read_api`` (a byte-frozen serving surface) imports it (GOV-1702, read-api C7b).
    """


def _require(condition: object, message: str) -> None:
    if not condition:
        raise RegistryConsistencyError(message)


# Every edge endpoint type in the contract must be a known node type — no edge
# may point at a type the registry does not define.
for _etype, (_froms, _tos) in EDGE_ENDPOINTS.items():
    _require(_etype in ALLOWED_EDGE_TYPES,
             f"EDGE_ENDPOINTS names unknown edge {_etype!r}")
    _unknown = (_froms | _tos) - ALLOWED_NODE_TYPES
    _require(not _unknown,
             f"edge {_etype!r} references unknown node type(s) {_unknown}")

# The GOV-98 additions must actually be additive (present, and not already in the
# 1.07 set) so a future rename can't silently collide.
_require(_NODE_TYPES_GOV98 <= ALLOWED_NODE_TYPES,
         "GOV-98 node types are not all in ALLOWED_NODE_TYPES")
_require(_EDGE_TYPES_GOV98 <= ALLOWED_EDGE_TYPES,
         "GOV-98 edge types are not all in ALLOWED_EDGE_TYPES")
_require(not (_NODE_TYPES_1_07 & _NODE_TYPES_GOV98), "GOV-98 node collides with 1.07")
_require(not (_EDGE_TYPES_1_07 & _EDGE_TYPES_GOV98), "GOV-98 edge collides with 1.07")
_require(FORWARD_LINKING_EDGE_TYPES <= ALLOWED_EDGE_TYPES,
         "FORWARD_LINKING_EDGE_TYPES names an edge outside ALLOWED_EDGE_TYPES")
_require(AGENDA_LIFECYCLE_EDGE_TYPES <= ALLOWED_EDGE_TYPES,
         "AGENDA_LIFECYCLE_EDGE_TYPES names an edge outside ALLOWED_EDGE_TYPES")


class EdgeError(ValueError):
    """An edge violates the registry endpoint contract or a node is missing."""


class LabelAliasError(ValueError):
    """A label alias is malformed or missing its mandatory sourceRef provenance."""


class TopicTreeCycleError(ValueError):
    """A topic_rollup edge would close a cycle (BEH-TOPICTREE-4)."""


# ---------------------------------------------------------------------------
# PII guard at the alias/label write boundary (GOV-105).
# ---------------------------------------------------------------------------
#
# The read-API web-safe layer projects `term`, `canonicalHumanLabel`, and the
# locator fields (timestampHuman/page/section/paragraph) VERBATIM, and the
# GOV-34 transport sweep only rejects filesystem/raw PATHS -- not arbitrary
# non-path PII strings. That was safe while alias/label rows were written only
# by deterministic civic-term structuring. GOV-126 introduces a volume AI pass
# over REAL Alpine civic content, whose public-comment transcripts carry
# private-individual names, home addresses, and phone numbers. This guard is a
# POSITIVE, fail-closed check AT THE WRITE BOUNDARY so private-identity strings
# are rejected at submission, never relying on the downstream read-time sweep.
#
# Honest scope limit: this rejects ENUMERABLE structured PII (email, phone,
# SSN-like, residential street address, labelled voter/registration id).
# Detecting arbitrary *personal names* of non-public individuals by regex is not
# safe here -- civic terms legitimately contain proper nouns ("Lincoln Street
# Bridge", "Smith Park") -- so the name residual stays covered by the typed
# alias-kind/node-type allowlists, schema-scoping, and the reviewer gate.


class PiiGuardError(LabelAliasError):
    """A write-boundary field carries private-individual PII (GOV-105).

    Subclasses :class:`LabelAliasError` so every existing ``except
    LabelAliasError`` call site keeps failing closed.
    """


# Each pattern targets a private-identity shape that has no legitimate place in
# a civic concept/agenda label or a source locator. Tuned for high precision so
# the guard does not reject real civic vocabulary.
_PII_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("email address",
     re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("SSN-like identifier",
     re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Separator-bearing phone (avoids matching bare numeric ids / resolution #s).
    ("phone number",
     re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")),
    # House-number + street-type. The street-type must be a whole word NOT
    # followed by '-' or another word char, so "Drive-through permit" and
    # "Highway 89 repaving" (no street suffix) are NOT mistaken for addresses.
    ("residential street address",
     re.compile(
         r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?(?:[A-Za-z0-9.'-]+\s+){0,3}"
         r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
         r"Court|Ct|Way|Place|Pl|Terrace|Ter|Circle|Cir)(?![\w-])\.?",
         re.IGNORECASE)),
    ("voter/registration identifier",
     re.compile(
         r"\b(?:voter|registration|reg|sos)\b[ .]*"
         r"(?:id|no\.?|num(?:ber)?|#)\b[ .:#]*[A-Za-z0-9-]+", re.IGNORECASE)),
)


def assert_no_pii(value: Any, field: str) -> None:
    """Reject a write-boundary value that carries private-individual PII.

    Positive, fail-closed guard (GOV-105) for the free-text/locator fields the
    read-API projects verbatim. Non-string or empty values pass (numeric
    locators, ``None``). Raises :class:`PiiGuardError` naming the field and the
    pattern KIND only -- the matched value is never echoed into the message
    (it would leak the PII into logs/comments).
    """
    if not isinstance(value, str) or not value.strip():
        return
    for kind, pattern in _PII_PATTERNS:
        if pattern.search(value):
            raise PiiGuardError(
                f"{field} rejected: matches a {kind}; private-individual PII may "
                f"not be written to a civic label/locator field (GOV-105)"
            )


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _topic_rollup_parent_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """child_topic_id -> [parent_topic_id, ...] over the current topic_rollup edges."""
    parents: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT from_node_id, to_node_id FROM concept_edges WHERE edge_type = 'topic_rollup'"
    ):
        parents.setdefault(row[0], []).append(row[1])
    return parents


def _would_create_cycle(
    parents: dict[str, list[str]], child: str, new_parent: str
) -> bool:
    """True if adding child -> new_parent closes a cycle (or is a self-loop).

    ``child`` rolls up INTO ``new_parent``; a cycle exists if ``new_parent`` can
    already reach ``child`` by following parent pointers. Walks the existing
    parent map (which does not yet contain the candidate edge).
    """
    if child == new_parent:
        return True
    seen: set[str] = set()
    frontier = [new_parent]
    while frontier:
        node = frontier.pop()
        if node == child:
            return True
        if node in seen:
            continue
        seen.add(node)
        frontier.extend(parents.get(node, ()))
    return False


def assert_acyclic(conn: sqlite3.Connection) -> None:
    """Validate the whole topic_rollup graph is acyclic (BEH-TOPICTREE-4).

    Serve-time guard, independent of insert-time enforcement: raises
    :class:`TopicTreeCycleError` if any topic can reach itself by following
    ``topic_rollup`` parent pointers. ``read_api.topic_tree`` calls this before
    returning a tree rather than rendering a broken one.
    """
    parents = _topic_rollup_parent_map(conn)
    for start in parents:
        seen: set[str] = set()
        frontier = list(parents.get(start, ()))
        while frontier:
            node = frontier.pop()
            if node == start:
                raise TopicTreeCycleError(
                    f"topic_rollup cycle detected through topic {start!r}"
                )
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(parents.get(node, ()))


def insert_edge(
    conn: sqlite3.Connection,
    edge_type: str,
    from_node_id: str,
    to_node_id: str,
    *,
    from_node_type: str | None = None,
    to_node_type: str | None = None,
    created_by: str | None = None,
    note: str | None = None,
    edge_id: str | None = None,
    commit: bool = True,
) -> str:
    """Insert one typed concept edge under the registry endpoint contract.

    Validates (raising before any write):

    * ``edge_type`` is a generic-table edge type (:data:`GENERIC_EDGE_TYPES`).
    * the endpoint node types match :data:`EDGE_ENDPOINTS` (defaulting to the
      edge's canonical from/to types when not given).
    * for ``topic_rollup``, the edge does not close a cycle / self-loop
      (:class:`TopicTreeCycleError`).

    ``created_by`` / ``note`` are reviewer-internal provenance and are never
    web-safe. Returns the ``edge_id``. Idempotent on
    ``(edge_type, from_node_id, to_node_id)`` via the table's UNIQUE constraint
    (an ``INSERT OR IGNORE``).
    """
    if edge_type not in GENERIC_EDGE_TYPES:
        raise EdgeError(
            f"edge_type {edge_type!r} is not stored in concept_edges; "
            f"expected one of {sorted(GENERIC_EDGE_TYPES)}"
        )
    allowed_from, allowed_to = EDGE_ENDPOINTS[edge_type]
    # Default to the edge's canonical endpoint type when a single type is allowed.
    from_node_type = from_node_type or (next(iter(allowed_from)) if len(allowed_from) == 1 else None)
    to_node_type = to_node_type or (next(iter(allowed_to)) if len(allowed_to) == 1 else None)
    if from_node_type not in allowed_from:
        raise EdgeError(
            f"edge {edge_type!r} from_node_type {from_node_type!r} not in {sorted(allowed_from)}"
        )
    if to_node_type not in allowed_to:
        raise EdgeError(
            f"edge {edge_type!r} to_node_type {to_node_type!r} not in {sorted(allowed_to)}"
        )
    if not from_node_id or not to_node_id:
        raise EdgeError("edge requires non-empty from_node_id and to_node_id")

    if edge_type == "topic_rollup":
        parents = _topic_rollup_parent_map(conn)
        if _would_create_cycle(parents, from_node_id, to_node_id):
            raise TopicTreeCycleError(
                f"topic_rollup {from_node_id!r} -> {to_node_id!r} would create a cycle"
            )

    edge_id = edge_id or f"{edge_type}:{from_node_id}->{to_node_id}"
    conn.execute(
        "INSERT OR IGNORE INTO concept_edges ("
        "edge_id, edge_type, from_node_id, from_node_type, to_node_id, to_node_type, "
        "created_by, created_utc, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge_id,
            edge_type,
            from_node_id,
            from_node_type,
            to_node_id,
            to_node_type,
            created_by,
            _now_utc_iso(),
            note,
        ),
    )
    if commit:
        conn.commit()
    return edge_id


def insert_agenda_thread(
    conn: sqlite3.Connection,
    agenda_thread_id: str,
    title: str,
    jurisdiction_id: str,
    canonical_human_label: str,
    *,
    status: str = "open",
    first_seen_date: str | None = None,
    last_seen_date: str | None = None,
    commit: bool = True,
) -> str:
    """Insert an ``agenda_thread`` node (GOV-98 A.1). Alpine-locked by caller.

    ``canonical_human_label`` is the REQUIRED plain-English primary label (owner
    addendum / §A.7). Government/source terms are added separately as
    :func:`insert_label_alias` rows — never as the primary label.
    """
    if status not in {"open", "decided", "dormant"}:
        raise EdgeError(f"agenda_thread status {status!r} invalid")
    if not (agenda_thread_id and title and jurisdiction_id):
        raise EdgeError("agenda_thread requires id, title, jurisdiction_id")
    if not canonical_human_label or not canonical_human_label.strip():
        raise LabelAliasError("agenda_thread requires a non-empty canonical_human_label")
    assert_no_pii(canonical_human_label, "agenda_thread canonicalHumanLabel")
    assert_no_pii(title, "agenda_thread title")
    conn.execute(
        "INSERT OR IGNORE INTO agenda_threads ("
        "agenda_thread_id, title, jurisdiction_id, status, first_seen_date, "
        "last_seen_date, canonical_human_label, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (agenda_thread_id, title, jurisdiction_id, status, first_seen_date,
         last_seen_date, canonical_human_label, _now_utc_iso()),
    )
    if commit:
        conn.commit()
    return agenda_thread_id


def insert_topic(
    conn: sqlite3.Connection,
    topic_id: str,
    name: str,
    canonical_human_label: str,
    *,
    jurisdiction_id: str | None = None,
    commit: bool = True,
) -> str:
    """Insert a flat ``topic`` node. The tree is carried by topic_rollup edges.

    ``canonical_human_label`` is the REQUIRED plain-English primary label (owner
    addendum / §A.7): the everyday core-concept term (e.g. 'general safety'), not
    a government string. Government terms are :func:`insert_label_alias` rows.
    """
    if not (topic_id and name):
        raise EdgeError("topic requires id and name")
    if not canonical_human_label or not canonical_human_label.strip():
        raise LabelAliasError("topic requires a non-empty canonical_human_label")
    assert_no_pii(canonical_human_label, "topic canonicalHumanLabel")
    assert_no_pii(name, "topic name")
    conn.execute(
        "INSERT OR IGNORE INTO topics (topic_id, name, jurisdiction_id, "
        "canonical_human_label, created_utc) VALUES (?, ?, ?, ?, ?)",
        (topic_id, name, jurisdiction_id, canonical_human_label, _now_utc_iso()),
    )
    if commit:
        conn.commit()
    return topic_id


# Pointer keys consumed from a sourceRef dict (the MANDATORY provenance object).
# char_start/char_end are the GOV-137/0016 char-span anchor (offsets into the
# preserved source text) — the honest locator for untimed prose (GOV-149).
_SOURCE_REF_REF_FIELDS = ("original_url", "archive_url", "local_ref")
_SOURCE_REF_LOCATOR_FIELDS = (
    "timestamp_human", "page", "section", "paragraph", "char_start", "char_end",
)


def validate_source_ref(source_ref: dict[str, Any] | None) -> None:
    """Validate an alias's MANDATORY sourceRef provenance. Raises on failure.

    Owner rule "an alias may not exist without a source trail". A valid sourceRef
    carries, at minimum: a ``source_id`` (source/doc id), at least one ref
    (``original_url`` / ``archive_url`` / ``local_ref``), and at least one locator
    (``timestamp_human`` / ``page`` / ``section`` / ``paragraph`` / ``char_start`` /
    ``char_end``). Fail-closed: a missing or incomplete sourceRef is rejected,
    never silently accepted.
    """
    if not isinstance(source_ref, dict):
        raise LabelAliasError("alias sourceRef is required (missing provenance object)")
    if not source_ref.get("source_id"):
        raise LabelAliasError("alias sourceRef requires a non-empty source_id (source/doc id)")
    if not any(source_ref.get(f) for f in _SOURCE_REF_REF_FIELDS):
        raise LabelAliasError(
            "alias sourceRef requires at least one of "
            f"{_SOURCE_REF_REF_FIELDS} (original/archive/local ref)"
        )
    if not any(source_ref.get(f) not in (None, "") for f in _SOURCE_REF_LOCATOR_FIELDS):
        raise LabelAliasError(
            "alias sourceRef requires at least one locator "
            f"{_SOURCE_REF_LOCATOR_FIELDS} (timestamp/page/section/paragraph/char_span)"
        )


def insert_label_alias(
    conn: sqlite3.Connection,
    node_id: str,
    node_type: str,
    term: str,
    alias_type: str,
    source_ref: dict[str, Any],
    *,
    first_seen_meeting_id: int | None = None,
    first_seen_date: str | None = None,
    created_by: str | None = None,
    alias_id: str | None = None,
    commit: bool = True,
) -> str:
    """Append a source/government alias to a topic/agenda_thread node (append-only).

    Validates (raising before any write): the node type carries the label layer;
    ``alias_type`` is a known kind; ``term`` is non-empty; and the ``source_ref``
    passes :func:`validate_source_ref` (the alias may not exist without a source
    trail). There is intentionally NO delete path — aliases are append/curate, so
    a reviewer can never strip an alias's sourceRef. Idempotent on
    ``(node_id, node_type, term, alias_type)``.
    """
    if node_type not in LABELLED_NODE_TYPES:
        raise LabelAliasError(
            f"node_type {node_type!r} does not carry a label layer; "
            f"expected one of {sorted(LABELLED_NODE_TYPES)}"
        )
    if alias_type not in ALLOWED_ALIAS_TYPES:
        raise LabelAliasError(
            f"alias_type {alias_type!r} not in {sorted(ALLOWED_ALIAS_TYPES)}"
        )
    if not node_id or not term or not term.strip():
        raise LabelAliasError("alias requires a non-empty node_id and term")
    validate_source_ref(source_ref)

    # GOV-105 PII guard: term + the verbatim-projected locators. The vault
    # local_ref is intentionally NOT guarded here -- it is a controlled path,
    # never projected, and is covered by the GOV-34 transport sweep.
    assert_no_pii(term, "alias term")
    for _locator in _SOURCE_REF_LOCATOR_FIELDS:
        assert_no_pii(source_ref.get(_locator), f"alias locator {_locator}")

    alias_id = alias_id or f"{node_type}:{node_id}:{alias_type}:{term}"
    conn.execute(
        "INSERT OR IGNORE INTO node_label_aliases ("
        "alias_id, node_id, node_type, term, alias_type, source_ref_source_id, "
        "source_ref_original_url, source_ref_archive_url, source_ref_local_ref, "
        "source_ref_locator_kind, source_ref_timestamp_human, source_ref_page, "
        "source_ref_section, source_ref_paragraph, source_ref_char_start, "
        "source_ref_char_end, first_seen_meeting_id, "
        "first_seen_date, created_by, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            alias_id, node_id, node_type, term, alias_type,
            source_ref.get("source_id"),
            source_ref.get("original_url"),
            source_ref.get("archive_url"),
            source_ref.get("local_ref"),
            source_ref.get("locator_kind"),
            source_ref.get("timestamp_human"),
            source_ref.get("page"),
            source_ref.get("section"),
            source_ref.get("paragraph"),
            source_ref.get("char_start"),
            source_ref.get("char_end"),
            first_seen_meeting_id,
            first_seen_date,
            created_by,
            _now_utc_iso(),
        ),
    )
    if commit:
        conn.commit()
    return alias_id


def aliases_for_node(
    conn: sqlite3.Connection, node_id: str, node_type: str
) -> list[dict[str, Any]]:
    """Raw alias rows for a node (reviewer-internal; web-safe projection in read_api)."""
    rows = conn.execute(
        "SELECT * FROM node_label_aliases WHERE node_id = ? AND node_type = ? "
        "ORDER BY alias_type, term",
        (node_id, node_type),
    ).fetchall()
    return [dict(row) for row in rows]
