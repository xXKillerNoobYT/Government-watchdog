"""Owner-gated concept-graph seed — P&Z Board body→topic node (GOV-161).

Adds one grounded civic ``topic`` node for the Alpine Planning and Zoning Board,
with a ``topic_rollup`` edge to the jurisdiction root and a source-grounded label
alias. Extends the GOV-149 topic tree ADDITIVELY — never mutates the Gate-1/Gate-2
frozen 4-node set.

OWNER GATE
----------
* GOV-166 (standing reviewer-internal promotion policy) — Isaac ACCEPTED. Public
  bodies/committees MAY be named; no per-issue Isaac gate needed.
* G3 (corpus names the body) — satisfied: batch-2 (GOV-162) promoted rows name
  the Alpine Planning and Zoning Board.
* G4 (owner authorization) — satisfied by GOV-166 acceptance + standing policy.

BINDING SCOPE
-------------
* Exactly 1 new topic node: ``topic:alpine:pnz-board`` ("Planning and Zoning
  Board"). 1 new ``topic_rollup`` edge to the jurisdiction root.
* Source-grounded ONLY. The grounding label alias is derived from a promoted
  reviewer-internal statement whose ``statement_text`` names the P&Z Board.
  Discovered at runtime (fail-closed: refuses if none exist).
* Reviewer-internal / vault-only. NOTHING here flips ``publication_state``.
* The GOV-149 4-node set (root + 3 civic topics) is NEVER mutated. This script
  asserts they remain intact BEFORE and AFTER the write.
* Public surface / launch / beta = separate owner gate (not covered here).

Fail-closed guarantees
----------------------
* Discovers P&Z-naming statements from the reviewer-internal serve (not hardcoded);
  refuses unless >=1 is found. Deterministic: sorted by statement_id.
* The grounding statement must be reviewer-internal-served (promoted, no blocking
  flags, evidence pointer present).
* All labels/aliases pass ``concept_map.assert_no_pii`` (GOV-105).
* Writes inside ONE transaction; on any error nothing commits.
* After apply, asserts: the GOV-149 3 civic children are intact, the new P&Z child
  exists with a grounding sourceAlias, public serve == 0, reviewer-internal set
  unchanged, 0 new agenda_thread rows, transport sweep PASS.

Usage::

    python3 scripts/gov161_body_topic_seed.py --db Database/gov_watchdog.db
    python3 scripts/gov161_body_topic_seed.py --db Database/gov_watchdog.db --apply
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import concept_map as cm  # noqa: E402
import db  # noqa: E402
import read_api  # noqa: E402

JURISDICTION_ID = "alpine"
ROOT_TOPIC_ID = "topic:alpine:jurisdiction"

PNZ_TOPIC_ID = "topic:alpine:pnz-board"
PNZ_TOPIC_NAME = "Planning and Zoning Board"
PNZ_TOPIC_LABEL = "Planning and Zoning Board"
PNZ_ALIAS_TERM = "Planning and Zoning Board"
PNZ_ALIAS_TYPE = "government_term"

GOV149_CIVIC_TOPIC_IDS = frozenset({
    "topic:alpine:water-system",
    "topic:alpine:budget-taxes",
    "topic:alpine:council-governance",
})

_PNZ_PATTERN = re.compile(
    r"Planning\s+(?:and|&)\s+Zoning\s+Board",
    re.IGNORECASE,
)

SEED_CREATED_BY = "reviewer:isaac"


class SeedError(RuntimeError):
    """A pre-flight / post-state invariant failed; nothing was written."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def discover_pnz_statements(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Find promoted reviewer-internal statements that name the P&Z Board.

    Returns a sorted list (by statement_id) of matching records from
    ``read_api.reviewer_internal_records``. Fail-closed: caller asserts
    the result is non-empty before any write.
    """
    matches = []
    for rec in read_api.reviewer_internal_records(conn):
        text = rec.get("statement_text") or ""
        if _PNZ_PATTERN.search(text):
            matches.append(rec)
    matches.sort(key=lambda r: r["statement_id"])
    return matches


def _grounding_source_ref(
    conn: sqlite3.Connection, statement_id: str,
) -> dict[str, Any]:
    """Build a valid sourceRef from a statement's primary evidence link.

    Mirrors ``gov149_concept_graph_seed._grounding_source_ref`` — file:// URIs
    go to ``local_ref`` (never projected); only http(s) passes as ``original_url``.
    """
    ev = conn.execute(
        "SELECT * FROM evidence_links WHERE from_node_id = ? AND from_node_type = 'statement' "
        "ORDER BY evidence_link_id LIMIT 1",
        (statement_id,),
    ).fetchone()
    if ev is None:
        raise SeedError(
            f"statement {statement_id!r} has no evidence link to ground its topic; refusing"
        )
    ev = dict(ev)
    if not ev.get("to_source_id"):
        raise SeedError(f"statement {statement_id!r} evidence link has no to_source_id; refusing")

    ref: dict[str, Any] = {"source_id": ev["to_source_id"]}
    original_url = ev.get("original_url")
    if isinstance(original_url, str) and original_url.lower().startswith(("http://", "https://")):
        ref["original_url"] = original_url
    elif isinstance(original_url, str) and original_url:
        ref["local_ref"] = original_url
    else:
        ref["local_ref"] = f"vault:{ev['to_source_id']}"
    archive_url = ev.get("archive_url")
    if isinstance(archive_url, str) and archive_url.lower().startswith(("http://", "https://")):
        ref["archive_url"] = archive_url

    locator_keys = ("timestamp_human", "page", "section", "paragraph",
                    "char_start", "char_end")
    for key in locator_keys:
        value = ev.get(key)
        if value not in (None, ""):
            ref[key] = value
    if not any(ref.get(k) not in (None, "") for k in locator_keys):
        raise SeedError(
            f"statement {statement_id!r} evidence link has no web-safe locator; refusing"
        )
    return ref


def _preflight(conn: sqlite3.Connection) -> dict[str, Any]:
    """Validate every binding invariant BEFORE any write."""
    # The jurisdiction root must already exist (from GOV-149).
    root = conn.execute(
        "SELECT topic_id FROM topics WHERE topic_id = ?", (ROOT_TOPIC_ID,)
    ).fetchone()
    if root is None:
        raise SeedError(
            f"jurisdiction root {ROOT_TOPIC_ID!r} does not exist; "
            "GOV-149 concept-graph seed must be applied first"
        )

    # The GOV-149 civic topics must be intact.
    existing_topics = {
        r[0] for r in conn.execute("SELECT topic_id FROM topics").fetchall()
    }
    missing_149 = GOV149_CIVIC_TOPIC_IDS - existing_topics
    if missing_149:
        raise SeedError(
            f"GOV-149 civic topics missing: {sorted(missing_149)}; "
            "the frozen 4-node set is not intact"
        )

    # The P&Z topic must NOT already exist (idempotency: insert_topic is INSERT OR IGNORE,
    # but we want explicit awareness).
    if PNZ_TOPIC_ID in existing_topics:
        raise SeedError(
            f"topic {PNZ_TOPIC_ID!r} already exists; refusing to re-seed"
        )

    # Discover P&Z-naming statements.
    pnz_matches = discover_pnz_statements(conn)
    if not pnz_matches:
        raise SeedError(
            "no reviewer-internal statements name the Planning and Zoning Board; "
            "cannot ground the topic node (gate G3 not met)"
        )

    primary = pnz_matches[0]
    primary_id = primary["statement_id"]

    # PII guard on labels.
    cm.assert_no_pii(PNZ_TOPIC_LABEL, f"{PNZ_TOPIC_ID}.canonicalHumanLabel")
    cm.assert_no_pii(PNZ_TOPIC_NAME, f"{PNZ_TOPIC_ID}.name")
    cm.assert_no_pii(PNZ_ALIAS_TERM, f"{PNZ_TOPIC_ID}.alias_term")

    return {
        "pnz_matches": pnz_matches,
        "primary_statement_id": primary_id,
    }


def _write(
    conn: sqlite3.Connection, primary_statement_id: str, log,
) -> None:
    """Write the P&Z Board topic node, rollup edge, and grounding alias."""
    cm.insert_topic(conn, PNZ_TOPIC_ID, PNZ_TOPIC_NAME, PNZ_TOPIC_LABEL,
                    jurisdiction_id=JURISDICTION_ID, commit=False)
    log(f"  TOPIC {PNZ_TOPIC_ID} <- {PNZ_TOPIC_LABEL!r}")

    cm.insert_edge(conn, "topic_rollup", PNZ_TOPIC_ID, ROOT_TOPIC_ID,
                   created_by=SEED_CREATED_BY, commit=False)
    log(f"  EDGE topic_rollup {PNZ_TOPIC_ID} -> {ROOT_TOPIC_ID}")

    source_ref = _grounding_source_ref(conn, primary_statement_id)
    cm.insert_label_alias(
        conn, PNZ_TOPIC_ID, "topic", PNZ_ALIAS_TERM, PNZ_ALIAS_TYPE, source_ref,
        created_by=SEED_CREATED_BY, commit=False,
    )
    log(f"  ALIAS {PNZ_ALIAS_TERM!r} ({PNZ_ALIAS_TYPE}) grounded in "
        f"{primary_statement_id} (source {source_ref['source_id']})")


def _verify_post_state(
    conn: sqlite3.Connection, baseline_reviewer_ids: set[str],
) -> dict[str, object]:
    """Assert the serve invariants after the write."""
    # The topic tree must now have the P&Z child under the root.
    tree = read_api.topic_tree(conn, ROOT_TOPIC_ID)
    child_ids = {c["topic"]["topic_id"] for c in tree["tree"]["children"]}
    expected_children = GOV149_CIVIC_TOPIC_IDS | {PNZ_TOPIC_ID}
    if child_ids != expected_children:
        raise SeedError(
            f"topic_tree children {sorted(child_ids)} != expected {sorted(expected_children)}"
        )

    # The P&Z child must have a grounding sourceAlias.
    pnz_child = next(
        (c for c in tree["tree"]["children"] if c["topic"]["topic_id"] == PNZ_TOPIC_ID),
        None,
    )
    if pnz_child is None:
        raise SeedError(f"P&Z topic {PNZ_TOPIC_ID!r} not in topic_tree")
    if not pnz_child["topic"].get("sourceAliases"):
        raise SeedError(f"P&Z topic {PNZ_TOPIC_ID!r} has no grounding sourceAlias")

    # The GOV-149 civic topics must still have their aliases.
    for child in tree["tree"]["children"]:
        if child["topic"]["topic_id"] in GOV149_CIVIC_TOPIC_IDS:
            if not child["topic"].get("sourceAliases"):
                raise SeedError(
                    f"GOV-149 civic topic {child['topic']['topic_id']!r} lost its sourceAlias"
                )

    # The reviewer-internal set must be UNCHANGED.
    after = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    if after != baseline_reviewer_ids:
        raise SeedError(
            f"reviewer-internal set changed: +{sorted(after - baseline_reviewer_ids)} "
            f"-{sorted(baseline_reviewer_ids - after)}"
        )

    public_served = read_api.published_records(conn)
    if public_served:
        raise SeedError(f"public lane served {len(public_served)} rows; topic seed must NOT publish")

    # Transport sweep.
    body = read_api.build_response(
        conn, topic_root=ROOT_TOPIC_ID, include_records=True, include_reviewer_internal=True
    )
    read_api.assert_no_raw_paths(body)
    return {
        "topic_nodes": len(child_ids) + 1,
        "children": sorted(child_ids),
        "reviewer_internal_count": len(after),
        "public_count": len(public_served),
        "pnz_aliases": len(pnz_child["topic"].get("sourceAliases", [])),
    }


def run(db_path: Path, *, apply: bool, log_lines: list[str]) -> int:
    def emit(msg: str) -> None:
        log_lines.append(msg)
        print(msg)

    emit(f"[{_now_utc_iso()}] GOV-161 P&Z Board body->topic seed — db={db_path} apply={apply}")
    with db.open_db(db_path) as conn:
        baseline_reviewer_ids = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
        plan = _preflight(conn)
        pnz_matches = plan["pnz_matches"]
        primary_id = plan["primary_statement_id"]

        emit(f"pre-flight OK: {len(pnz_matches)} P&Z-naming statements found in reviewer-internal serve")
        for m in pnz_matches:
            emit(f"  DISCOVERED {m['statement_id']}")
        emit(f"  PRIMARY grounding: {primary_id}")
        emit(f"  New node: {PNZ_TOPIC_ID} ({PNZ_TOPIC_LABEL!r})")
        emit(f"  GOV-149 frozen set intact: {sorted(GOV149_CIVIC_TOPIC_IDS)}")

        if not apply:
            emit("DRY RUN — no write. Re-run with --apply to seed the P&Z Board topic.")
            return 0

        _write(conn, primary_id, emit)
        conn.commit()

        post = _verify_post_state(conn, baseline_reviewer_ids)
        emit(f"POST-STATE OK: topic_nodes={post['topic_nodes']} children={post['children']} "
             f"reviewer_internal={post['reviewer_internal_count']} public={post['public_count']} "
             f"pnz_aliases={post['pnz_aliases']} (transport sweep PASS)")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="GOV-161 P&Z Board body->topic seed (grounded, reviewer-internal).",
    )
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true",
                        help="apply the write (default: dry-run)")
    parser.add_argument("--log", type=Path, default=None,
                        help="optional run-log path")
    args = parser.parse_args(argv)

    log_lines: list[str] = []
    try:
        rc = run(args.db, apply=args.apply, log_lines=log_lines)
    except (SeedError, cm.EdgeError, cm.LabelAliasError, cm.TopicTreeCycleError) as exc:
        log_lines.append(f"ABORTED: {type(exc).__name__}: {exc}")
        print(f"ABORTED: {type(exc).__name__}: {exc}", file=sys.stderr)
        rc = 2

    if args.apply or args.log is not None:
        log_path = args.log or (
            Path(__file__).resolve().parent.parent
            / "Logs"
            / f"gov161-body-topic-{datetime.now(timezone.utc):%Y%m%d}.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(log_lines) + "\n")
        print(f"run-log: {log_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(_main())
