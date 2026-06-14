"""Owner-gated concept-graph seed — reviewer-internal TOPIC layer (GOV-149).

Writes a small, owner-curated TOPIC tree over the REAL Alpine corpus so the
GOV-129 ``/topics`` civic topic surface can render real data instead of its
honest empty-state. It is the GOV-149 sibling of :mod:`gov146_promotion_seed`:
the DB is git-ignored/ephemeral, so this SCRIPT is the durable, re-runnable
deliverable (the GOV-135 / GOV-146 seed precedent).

OWNER GATE (two-phase, mirrors GOV-144 -> GOV-146)
--------------------------------------------------
* Gate 1 (approach) — Isaac accepted ``request_confirmation 14d375a1``
  (2026-06-13): a small owner-curated topic layer over the 6 GOV-146 reviewed
  statements (Town water system / Town budget & taxes / Town Council
  governance; cap <=6 nodes, <=4 rollup edges), agenda threads stay honest-EMPTY
  on real data (no title-similarity threads).
* Gate 2 (concrete manifest, BEFORE any write) — Isaac must accept the exact
  node/edge/grounding manifest this module encodes. ``--apply`` MUST NOT be run
  against the vault DB until that acceptance is recorded on GOV-149. Until then
  the default dry-run prints the plan and writes nothing.

BINDING SCOPE (do not widen here)
---------------------------------
* Exactly 4 topic nodes: 1 jurisdiction root (``Town of Alpine``, no civic claim,
  no alias) + 3 civic topics, each grounded in a promoted statement's cited
  source via a label alias (mandatory sourceRef). Cap <=6 nodes / <=4 rollup
  edges is asserted in code.
* 3 ``topic_rollup`` edges (each civic topic -> the jurisdiction root). Acyclic
  by construction; re-validated at serve.
* ZERO agenda_thread rows: the 6 AI rows carry no agenda-item membership and no
  ``updates`` chain, so the real record supports 0 threads. We do NOT fabricate
  threads to fill the UI; that surface keeps its labelled ``?demo=graph``
  fixture until real agenda structure exists.
* Reviewer-internal / vault-only. NOTHING here flips ``publication_state`` — the
  public lane (:func:`read_api.published_records`) stays 0; the reviewer-internal
  set (the 6 promoted rows) is read for grounding and is never mutated.

Fail-closed guarantees
----------------------
* Refuses unless every grounding statement is currently served by
  :func:`read_api.reviewer_internal_records` (i.e. promoted under the GOV-146
  seed). A topic may not claim grounding in a row that is not reviewer-cleared.
* Each civic topic's grounding alias is derived from a REAL evidence link on its
  primary member statement (source id + locator). A ``file://`` vault provenance
  URI is written as the never-projected ``local_ref`` (never as ``original_url``)
  so it cannot ride across the web-safe boundary.
* All labels / aliases pass :func:`concept_map.assert_no_pii` (GOV-105).
* Writes inside ONE transaction; on any error nothing commits.
* After apply, asserts the post-state: ``topic_tree(root)`` has the 3 civic
  children each carrying >=1 source alias, public serve == 0, the reviewer-
  internal set is unchanged, 0 agenda_thread rows exist, and the whole assembled
  reviewer-internal response body passes the (file://-aware) transport sweep.

Usage::

    # dry-run (default): print the plan + current state, write nothing
    python3 scripts/gov149_concept_graph_seed.py --db Database/gov_watchdog.db
    # apply (ONLY after Isaac accepts the Gate-2 concrete manifest on GOV-149)
    python3 scripts/gov149_concept_graph_seed.py --db Database/gov_watchdog.db --apply
"""

from __future__ import annotations

import argparse
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

# The jurisdiction root: a plain factual jurisdiction label, not a civic claim,
# so it carries NO grounding alias. It exists solely to give the three civic
# topics a single tree root the frontend can request as ``topic_root``.
ROOT_TOPIC_ID = "topic:alpine:jurisdiction"
ROOT_TOPIC_NAME = "Town of Alpine"
ROOT_TOPIC_LABEL = "Town of Alpine"


class CivicTopic:
    """One curated civic topic + its grounding member statements."""

    def __init__(self, topic_id: str, name: str, label: str, alias_term: str,
                 grounding_statement_ids: tuple[str, ...]):
        self.topic_id = topic_id
        self.name = name
        self.label = label  # plain-English canonicalHumanLabel
        self.alias_term = alias_term  # government/agenda term (carries sourceRef)
        self.grounding_statement_ids = grounding_statement_ids


# The exact Gate-2 manifest. Each civic topic rolls up into the jurisdiction root
# and is grounded in member statements that are all in the GOV-146 reviewer-
# internal 6. The grounding alias is derived from the PRIMARY member (index 0).
CIVIC_TOPICS: tuple[CivicTopic, ...] = (
    CivicTopic(
        "topic:alpine:water-system",
        "Town water system",
        "Town water system",
        "Town Water System",
        (
            "alpine_local_corpus:ai:01661553:0010",  # water shutdown May 21 2026 (main break)
            "alpine_local_corpus:ai:01819080:0017",  # bacteriological testing confirmed safe
        ),
    ),
    CivicTopic(
        "topic:alpine:budget-taxes",
        "Town budget & taxes",
        "Town budget and taxes",
        "Town Budget and Taxes",
        (
            "alpine_local_corpus:ai:01617859:0008",  # mill levy 5 mills
            "alpine_local_corpus:ai:01664750:0013",  # Budget Work Session Thu Jun 11 2026 2pm
        ),
    ),
    CivicTopic(
        "topic:alpine:council-governance",
        "Town Council governance",
        "Town Council governance",
        "Town Council Governance",
        (
            "alpine_local_corpus:ai:00000064:0021",  # Special Town Council mtg Oct 9 2024
            "alpine_local_corpus:ai:01821771:0027",  # council took no action in executive session
        ),
    ),
)

MAX_TOPIC_NODES = 6
MAX_ROLLUP_EDGES = 4
SEED_CREATED_BY = "reviewer:isaac"  # reviewer-internal provenance, never web-safe


class SeedError(RuntimeError):
    """A pre-flight / post-state invariant failed; nothing was written."""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _grounding_source_ref(conn: sqlite3.Connection, statement_id: str) -> dict[str, Any]:
    """Build a valid, web-safe-clean sourceRef from a statement's primary evidence link.

    A ``file://`` (or otherwise non-``http(s)``) provenance URI is placed in the
    never-projected ``local_ref`` so it cannot cross the web-safe boundary; only a
    genuine public ``http(s)`` URL is passed as ``original_url``. Fail-closed: the
    statement must resolve to >=1 evidence link carrying a usable locator.
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
        # file:// vault URI or bare path -> reviewer-internal local_ref (never projected).
        ref["local_ref"] = original_url
    else:
        ref["local_ref"] = f"vault:{ev['to_source_id']}"
    archive_url = ev.get("archive_url")
    if isinstance(archive_url, str) and archive_url.lower().startswith(("http://", "https://")):
        ref["archive_url"] = archive_url

    # A usable web-safe locator must exist. char_start/char_end are the GOV-137
    # char-span anchor (offsets into the preserved source text) — the honest
    # locator for the real untimed Alpine prose; timed/page/section/paragraph
    # remain supported for any future timed source.
    locator_keys = ("timestamp_human", "page", "section", "paragraph",
                    "char_start", "char_end")
    for key in locator_keys:
        value = ev.get(key)
        if value not in (None, ""):
            ref[key] = value
    if not any(ref.get(k) not in (None, "") for k in locator_keys):
        raise SeedError(
            f"statement {statement_id!r} evidence link has no web-safe locator "
            "(timestamp_human/page/section/paragraph/char_span); refusing to ground a topic on it"
        )
    return ref


def _preflight(conn: sqlite3.Connection) -> list[dict[str, str]]:
    """Validate every binding invariant BEFORE any write. Returns the per-topic plan."""
    total_nodes = 1 + len(CIVIC_TOPICS)
    if total_nodes > MAX_TOPIC_NODES:
        raise SeedError(f"manifest has {total_nodes} topic nodes; owner gate caps at {MAX_TOPIC_NODES}")
    if len(CIVIC_TOPICS) > MAX_ROLLUP_EDGES:
        raise SeedError(
            f"manifest has {len(CIVIC_TOPICS)} rollup edges; owner gate caps at {MAX_ROLLUP_EDGES}"
        )
    ids = [ROOT_TOPIC_ID, *(t.topic_id for t in CIVIC_TOPICS)]
    if len(set(ids)) != len(ids):
        raise SeedError("manifest contains a duplicate topic id")

    # Every grounding statement must be currently reviewer-internal-served (promoted).
    served_ids = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    plan: list[dict[str, str]] = []
    for topic in CIVIC_TOPICS:
        for sid in topic.grounding_statement_ids:
            if sid not in served_ids:
                raise SeedError(
                    f"topic {topic.topic_id!r} grounds in {sid!r}, which is NOT in the "
                    "reviewer-internal serve (not promoted under the GOV-146 seed); refusing"
                )
        # PII guard on every label / alias term before any write.
        cm.assert_no_pii(topic.label, f"{topic.topic_id}.canonicalHumanLabel")
        cm.assert_no_pii(topic.name, f"{topic.topic_id}.name")
        cm.assert_no_pii(topic.alias_term, f"{topic.topic_id}.alias_term")
        plan.append({
            "topic_id": topic.topic_id,
            "label": topic.label,
            "grounded_in": topic.grounding_statement_ids[0],
        })
    cm.assert_no_pii(ROOT_TOPIC_LABEL, f"{ROOT_TOPIC_ID}.canonicalHumanLabel")
    return plan


def _write(conn: sqlite3.Connection, log) -> None:
    """Write the topic nodes, rollup edges, and grounding aliases (one transaction)."""
    cm.insert_topic(conn, ROOT_TOPIC_ID, ROOT_TOPIC_NAME, ROOT_TOPIC_LABEL,
                    jurisdiction_id=JURISDICTION_ID, commit=False)
    log(f"  TOPIC {ROOT_TOPIC_ID} (root, jurisdiction) <- {ROOT_TOPIC_LABEL!r}")
    for topic in CIVIC_TOPICS:
        cm.insert_topic(conn, topic.topic_id, topic.name, topic.label,
                        jurisdiction_id=JURISDICTION_ID, commit=False)
        cm.insert_edge(conn, "topic_rollup", topic.topic_id, ROOT_TOPIC_ID,
                       created_by=SEED_CREATED_BY, commit=False)
        primary = topic.grounding_statement_ids[0]
        source_ref = _grounding_source_ref(conn, primary)
        cm.insert_label_alias(
            conn, topic.topic_id, "topic", topic.alias_term, "government_term", source_ref,
            created_by=SEED_CREATED_BY, commit=False,
        )
        log(f"  TOPIC {topic.topic_id} <- {topic.label!r}; rollup -> {ROOT_TOPIC_ID}; "
            f"grounded in {primary} (source {source_ref['source_id']})")


def _verify_post_state(conn: sqlite3.Connection, baseline_reviewer_ids: set[str]) -> dict[str, object]:
    """Assert the serve invariants after the write. Raises on any violation."""
    tree = read_api.topic_tree(conn, ROOT_TOPIC_ID)
    child_ids = {c["topic"]["topic_id"] for c in tree["tree"]["children"]}
    expected_children = {t.topic_id for t in CIVIC_TOPICS}
    if child_ids != expected_children:
        raise SeedError(f"topic_tree children {sorted(child_ids)} != expected {sorted(expected_children)}")
    for child in tree["tree"]["children"]:
        if not child["topic"].get("sourceAliases"):
            raise SeedError(f"civic topic {child['topic']['topic_id']!r} has no grounding sourceAlias")
        if not child["topic"].get("canonicalHumanLabel"):
            raise SeedError(f"civic topic {child['topic']['topic_id']!r} has no canonicalHumanLabel")

    # The reviewer-internal set must be UNCHANGED (this seed never touches statements).
    after = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
    if after != baseline_reviewer_ids:
        raise SeedError(
            f"reviewer-internal set changed: +{sorted(after - baseline_reviewer_ids)} "
            f"-{sorted(baseline_reviewer_ids - after)}"
        )

    public_served = read_api.published_records(conn)
    if public_served:
        raise SeedError(f"public lane served {len(public_served)} rows; topic seed must NOT publish")

    thread_count = conn.execute("SELECT COUNT(*) AS n FROM agenda_threads").fetchone()["n"]
    if thread_count:
        raise SeedError(
            f"{thread_count} agenda_thread rows exist; the seed asserts honest-EMPTY threads "
            "(no title-similarity threads on real data)"
        )

    # The whole assembled reviewer-internal body (records + topic tree) is swept.
    body = read_api.build_response(
        conn, topic_root=ROOT_TOPIC_ID, include_records=True, include_reviewer_internal=True
    )
    read_api.assert_no_raw_paths(body)
    return {
        "topic_nodes": 1 + len(CIVIC_TOPICS),
        "civic_children": sorted(child_ids),
        "reviewer_internal_count": len(after),
        "public_count": len(public_served),
        "agenda_thread_count": thread_count,
    }


def run(db_path: Path, *, apply: bool, log_lines: list[str]) -> int:
    def emit(msg: str) -> None:
        log_lines.append(msg)
        print(msg)

    emit(f"[{_now_utc_iso()}] GOV-149 concept-graph seed — db={db_path} apply={apply}")
    with db.open_db(db_path) as conn:
        baseline_reviewer_ids = {r["statement_id"] for r in read_api.reviewer_internal_records(conn)}
        plan = _preflight(conn)
        emit(f"pre-flight OK: 1 root + {len(plan)} civic topics, {len(CIVIC_TOPICS)} rollup edges, "
             f"0 agenda threads (reviewer-internal baseline={len(baseline_reviewer_ids)})")
        for item in plan:
            emit(f"  PLAN topic {item['topic_id']} ({item['label']!r}) grounded in {item['grounded_in']}")

        if not apply:
            emit("DRY RUN — no write. Re-run with --apply ONLY after Isaac accepts the Gate-2 manifest.")
            return 0

        _write(conn, emit)
        conn.commit()

        post = _verify_post_state(conn, baseline_reviewer_ids)
        emit(f"POST-STATE OK: topic_nodes={post['topic_nodes']} civic_children={post['civic_children']} "
             f"reviewer_internal={post['reviewer_internal_count']} public={post['public_count']} "
             f"agenda_threads={post['agenda_thread_count']} (transport sweep PASS)")
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GOV-149 owner-gated reviewer-internal topic-layer seed.")
    parser.add_argument("--db", type=Path, default=db.DEFAULT_DB_PATH)
    parser.add_argument("--apply", action="store_true",
                        help="apply the write (default: dry-run; gated on Isaac Gate-2 acceptance)")
    parser.add_argument("--log", type=Path, default=None,
                        help="optional run-log path (defaults to Logs/gov149-concept-graph-<UTCdate>.log)")
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
            / f"gov149-concept-graph-{datetime.now(timezone.utc):%Y%m%d}.log"
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(log_lines) + "\n")
        print(f"run-log: {log_path}")
    return rc


if __name__ == "__main__":
    raise SystemExit(_main())
