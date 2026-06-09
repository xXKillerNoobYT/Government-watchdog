"""Reviewer-internal read-API over the gated record store (GOV-98, Prereq-0).

Stage 1 Slice 4 Prereq-0. Contract: GOV-97 plan Part A + Part C; reuses the 1.05
publication SSOT (:mod:`publication`) and the GOV-98 concept-map registry
(:mod:`concept_map`). Source: Docs/stage1-slice4-prereq0-read-api-concept-map.md.

This is **not** an HTTP server. It is a local, read-only, stateless module (+ a
CLI) that projects the already-gated record store onto a web-safe response shape
the frontend A→E chain reads. No network listener, no public surface, Alpine-only.

Two gating principles, both reused (never re-typed):

* **Eligibility (fail-closed).** A statement is served only when BOTH gates agree
  — :func:`publication.compute_ui_status` resolves to a value in
  ``PUBLICATION_ELIGIBLE_UI_STATUSES`` AND the DB ``publication_state`` is
  ``publishable``. Default posture: not returned. ``do_not_publish`` / disputed /
  unreviewed / pending records never reach the render lane. The ui_status is
  RE-derived here (not trusted from the stored column) so a stale write cannot
  fail open. No orphan claim is served (a served statement resolves to ≥1
  evidence pointer or a segment edge). Labels travel with every record.
* **Web-safe boundary (two independent layers).** Every record crosses through
  :func:`publication.to_web_safe` (field allowlist, fail-closed), AND the whole
  assembled response is swept by :func:`assert_no_raw_paths` (a transport-level
  guard that rejects filesystem/absolute paths and raw markers while allowing
  public URLs). The second layer catches a leak even if a field were
  mis-allowlisted (GOV-34 transport-leak finding).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import concept_map as cm  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402

# ---------------------------------------------------------------------------
# Transport-level raw/absolute-path guard (GOV-34). Independent of to_web_safe.
# ---------------------------------------------------------------------------

# Substrings that mark a raw/vault/private locator. A response body containing any
# of these has leaked something the field allowlist should have stripped.
RAW_PATH_MARKERS = (
    "/Users/",
    "/home/",
    "/var/",
    "/tmp/",
    "/private/",
    "/Volumes/",
    "\\Users\\",
    "Obsidian Vault",
    "Source-Data",
    "TownOfAlpine",
    "Raw-PDFs",
    ".sha256",
    "raw_local",
    "transcript_path",
    "local_note",
)

_URL_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_WIN_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")


class RawPathLeak(ValueError):
    """The response body contains a raw/absolute filesystem path (transport leak)."""


def _looks_like_url(value: str) -> bool:
    return bool(_URL_RE.match(value.strip()))


def _is_filesystem_path(value: str) -> bool:
    s = value.strip()
    if not s or _looks_like_url(s):
        return False
    # POSIX absolute path, or a Windows drive-absolute path.
    return s.startswith("/") or bool(_WIN_ABS_RE.match(s))


def _iter_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_strings(item)


def assert_no_raw_paths(body: Any) -> Any:
    """Raise :class:`RawPathLeak` if any string in ``body`` is a raw/absolute path.

    The transport-level assertion required by the acceptance criteria. Walks every
    string (keys + values, nested) and rejects a filesystem/absolute path or a
    known raw marker. Public URLs (``http(s)://…``) are allowed — only non-URL
    absolute/vault paths fail. Returns ``body`` unchanged on success so it can wrap
    a response inline.
    """
    for text in _iter_strings(body):
        if _is_filesystem_path(text):
            raise RawPathLeak(f"absolute/filesystem path in response body: {text!r}")
        if not _looks_like_url(text):
            for marker in RAW_PATH_MARKERS:
                if marker in text:
                    raise RawPathLeak(
                        f"raw marker {marker!r} in response body: {text!r}"
                    )
    return body


# ---------------------------------------------------------------------------
# Eligibility (reused, fail-closed) + statement serving.
# ---------------------------------------------------------------------------


def _evidence_links_for(conn: sqlite3.Connection, statement_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM evidence_links WHERE from_node_id = ? AND from_node_type = 'statement' "
        "ORDER BY evidence_link_id",
        (statement_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _segment_resolves(conn: sqlite3.Connection, segment_id: str | None) -> bool:
    if not segment_id:
        return False
    return (
        conn.execute(
            "SELECT 1 FROM transcript_segments WHERE segment_id = ?", (segment_id,)
        ).fetchone()
        is not None
    )


def _eligible_ui_status(record: dict[str, Any], links: list[dict[str, Any]]) -> str:
    """Re-derive the record's ui_status from current signals (never trust storage)."""
    source_present = bool(record.get("segment_id")) or any(
        link.get("to_source_id") for link in links
    )
    archive_present = any(link.get("archive_status") == "available" for link in links)
    return pub.compute_ui_status(
        {
            "verificationStatus": record.get("verification_status"),
            "correctionStatus": record.get("correction_status"),
            "sourceChanged": bool(record.get("source_changed")),
            "sourcePresent": source_present,
            "archivePresent": archive_present,
            "rawPreserved": False,  # statements track no raw-preserved flag; conservative
        }
    )


def _serialize_statement(
    conn: sqlite3.Connection, record: dict[str, Any], ui_status: str
) -> dict[str, Any]:
    """Project a served statement + its evidence drawer onto the web-safe shape.

    The flat record fields go through ``to_web_safe``; the ``evidence`` list is an
    API-envelope key holding already-web-safe drawer entries. ``ui_status`` is the
    re-derived eligible value (the label the frontend consumes verbatim).
    """
    flat = dict(record)
    flat["ui_status"] = ui_status
    safe = pub.to_web_safe(flat)
    safe["evidence"] = [pub.to_web_safe(link) for link in _evidence_links_for(conn, record["statement_id"])]
    return safe


def published_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Web-safe served statements: eligibility-gated, orphan-dropped, labels attached.

    Both gates must agree (uiStatus eligible AND publication_state publishable),
    and the statement must not be an orphan. Everything else is silently not
    served (the fail-closed default), never served unlabeled.
    """
    served: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM statements ORDER BY statement_id"):
        record = dict(row)
        links = _evidence_links_for(conn, record["statement_id"])
        ui_status = _eligible_ui_status(record, links)
        if ui_status not in pub.PUBLICATION_ELIGIBLE_UI_STATUSES:
            continue
        if record.get("publication_state") != "publishable":
            continue
        # No orphan claim served (1.07 §2.3): segment edge OR ≥1 evidence pointer.
        if not (_segment_resolves(conn, record.get("segment_id")) or links):
            continue
        served.append(_serialize_statement(conn, record, ui_status))
    return served


# ---------------------------------------------------------------------------
# Agenda thread (GOV-98 A.4): node + chronological members + typed lifecycle.
# ---------------------------------------------------------------------------


def agenda_thread(conn: sqlite3.Connection, thread_id: str) -> dict[str, Any] | None:
    """Web-safe agenda_thread: node + its members (chronological) + lifecycle edges.

    Members are the ``agenda_item``s linked to the thread via
    ``agenda_item_in_thread``, ordered by meeting date then item order (known-then
    chronology). Lifecycle edges are the typed ``Supersedes``/``Amends``/
    ``Revisits`` relations among members — never an untyped "related" (BEH-AGENDA-2).
    Returns ``None`` if the thread does not exist.
    """
    thread_row = conn.execute(
        "SELECT * FROM agenda_threads WHERE agenda_thread_id = ?", (thread_id,)
    ).fetchone()
    if thread_row is None:
        return None

    member_rows = conn.execute(
        "SELECT ai.* FROM agenda_items ai "
        "JOIN concept_edges ce ON ce.from_node_id = ai.agenda_item_id "
        "LEFT JOIN meetings m ON m.id = ai.meeting_id "
        "WHERE ce.edge_type = 'agenda_item_in_thread' AND ce.to_node_id = ? "
        "ORDER BY m.meeting_date, ai.item_order, ai.agenda_item_id",
        (thread_id,),
    ).fetchall()
    member_ids = {row["agenda_item_id"] for row in member_rows}

    lifecycle: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT * FROM concept_edges WHERE edge_type IN "
        "('agenda_item_supersedes', 'agenda_item_amends', 'agenda_item_revisits') "
        "ORDER BY edge_id"
    ):
        edge = dict(row)
        # Only edges whose endpoints are both members of this thread.
        if edge["from_node_id"] in member_ids and edge["to_node_id"] in member_ids:
            lifecycle.append(pub.to_web_safe(edge))

    return {
        "thread": pub.to_web_safe(dict(thread_row)),
        "members": [pub.to_web_safe(dict(row)) for row in member_rows],
        "lifecycle_edges": lifecycle,
    }


# ---------------------------------------------------------------------------
# Topic tree (GOV-98 A.3): acyclicity-validated rollup subtree + breadcrumb.
# ---------------------------------------------------------------------------


def _topic_children_map(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """parent_topic_id -> [child_topic_id, ...] over topic_rollup (child→parent)."""
    children: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT from_node_id, to_node_id FROM concept_edges WHERE edge_type = 'topic_rollup'"
    ):
        children.setdefault(row[1], []).append(row[0])
    return children


def _topic_row(conn: sqlite3.Connection, topic_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM topics WHERE topic_id = ?", (topic_id,)).fetchone()
    return dict(row) if row is not None else None


def topic_descendants(conn: sqlite3.Connection, topic_id: str) -> set[str]:
    """All descendant topic ids (inclusive of ``topic_id``) via topic_rollup.

    Powers BEH-TOPICTREE-2 rollup filtering: filtering to a parent returns items
    in that topic AND all descendants. Acyclicity is validated first so this can't
    loop forever.
    """
    cm.assert_acyclic(conn)
    children = _topic_children_map(conn)
    out: set[str] = set()
    frontier = [topic_id]
    while frontier:
        node = frontier.pop()
        if node in out:
            continue
        out.add(node)
        frontier.extend(children.get(node, ()))
    return out


def _breadcrumb(conn: sqlite3.Connection, topic_id: str) -> list[dict[str, Any]]:
    """Path from the top ancestor down to ``topic_id`` (where it sits, BEH-TOPICTREE-3)."""
    parents = cm._topic_rollup_parent_map(conn)
    chain = [topic_id]
    seen = {topic_id}
    node = topic_id
    while parents.get(node):
        parent = parents[node][0]  # tree: a single parent; first is canonical
        if parent in seen:
            break  # defensive: assert_acyclic should already have rejected this
        chain.append(parent)
        seen.add(parent)
        node = parent
    chain.reverse()  # top ancestor first
    return [pub.to_web_safe(_topic_row(conn, tid) or {"topic_id": tid}) for tid in chain]


def _subtree(conn: sqlite3.Connection, topic_id: str, children: dict[str, list[str]]) -> dict[str, Any]:
    row = _topic_row(conn, topic_id) or {"topic_id": topic_id}
    return {
        "topic": pub.to_web_safe(row),
        "children": [
            _subtree(conn, child, children) for child in sorted(children.get(topic_id, ()))
        ],
    }


def topic_tree(conn: sqlite3.Connection, root_topic_id: str) -> dict[str, Any]:
    """Web-safe topic_rollup subtree rooted at ``root_topic_id`` + breadcrumb.

    Validates acyclicity BEFORE building (BEH-TOPICTREE-4): raises
    :class:`concept_map.TopicTreeCycleError` rather than serving a broken tree.
    """
    cm.assert_acyclic(conn)
    children = _topic_children_map(conn)
    return {
        "root": pub.to_web_safe(_topic_row(conn, root_topic_id) or {"topic_id": root_topic_id}),
        "breadcrumb": _breadcrumb(conn, root_topic_id),
        "tree": _subtree(conn, root_topic_id, children),
    }


# ---------------------------------------------------------------------------
# Response assembly (projects + transport-asserts the whole body).
# ---------------------------------------------------------------------------


def build_response(
    conn: sqlite3.Connection,
    *,
    thread_id: str | None = None,
    topic_root: str | None = None,
    include_records: bool = True,
) -> dict[str, Any]:
    """Assemble the reviewer-internal read response and transport-assert it.

    Every leaf record is already web-safe (projected via ``to_web_safe``); the
    whole assembled body is then swept by :func:`assert_no_raw_paths` before
    return so a leak fails LOUDLY at the boundary, not silently downstream.
    """
    response: dict[str, Any] = {"scope": "alpine", "access": "reviewer_internal"}
    if include_records:
        response["records"] = published_records(conn)
    if thread_id is not None:
        response["agenda_thread"] = agenda_thread(conn, thread_id)
    if topic_root is not None:
        response["topic_tree"] = topic_tree(conn, topic_root)
    return assert_no_raw_paths(response)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reviewer-internal read-API (GOV-98).")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--thread", dest="thread_id", default=None)
    parser.add_argument("--topic-root", dest="topic_root", default=None)
    parser.add_argument("--no-records", dest="include_records", action="store_false")
    args = parser.parse_args(argv)

    with db.open_db(args.db) as conn:
        response = build_response(
            conn,
            thread_id=args.thread_id,
            topic_root=args.topic_root,
            include_records=args.include_records,
        )
    print(json.dumps(response, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
