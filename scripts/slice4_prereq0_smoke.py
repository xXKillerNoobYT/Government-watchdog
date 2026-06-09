"""Stage 1 Slice 4 Prereq-0 integration smoke (GOV-98).

End-to-end against a throwaway DB: migrate -> seed a registry source + meetings +
agenda items -> write one ELIGIBLE reviewed/publishable statement (with a valid
evidence pointer carrying a vault path + raw deep-link) and two non-eligible
statements -> register the agenda_thread + members + a typed supersede edge + a
topic_rollup chain -> drive the reviewer-internal read-API.

Asserts the Prereq-0 acceptance invariants and exits non-zero (fails LOUDLY) on
any regression:

1. Only the reviewed/eligible record is served (default not returned).
2. The serialized response body contains ZERO raw/absolute paths (transport-level).
3. The new node/edge types are exposed: an agenda_thread with its
   agenda_item_in_thread members (chronological) and a typed lifecycle edge, and
   a topic_rollup chain with a breadcrumb.
4. Acyclicity is enforced before serving the tree (a written cycle is rejected).
5. Labels travel with the served record; no orphan claim is served.

Throwaway sandbox: no real DB / network / AI. Mirrors the slice1/2/3 smokes.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import concept_map as cm  # noqa: E402
import db  # noqa: E402
import publication as pub  # noqa: E402
import read_api  # noqa: E402
import statements as st  # noqa: E402


def _fail(msg: str) -> None:
    print(f"SLICE4-PREREQ0 SMOKE FAIL: {msg}")
    raise SystemExit(1)


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sources (source_id, name, scope, source_type, source_class, "
        "original_url) VALUES ('alpine_packet', 'Agenda Packet', 'alpine', "
        "'document', 'official', 'https://alpinewy.gov/packet.pdf')"
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (1, '2026-04-10', 'Town Council', '2026-04-10T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO meetings (id, meeting_date, body, fetch_time_utc) "
        "VALUES (2, '2026-05-08', 'Town Council', '2026-05-08T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:2026-04-10:item-3', 1, 3, 'Fireworks ban — first reading')"
    )
    conn.execute(
        "INSERT INTO agenda_items (agenda_item_id, meeting_id, item_order, title) "
        "VALUES ('alpine:2026-05-08:item-7', 2, 7, 'Fireworks ban — adoption')"
    )
    conn.commit()

    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-eligible",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "The council adopted the fireworks ban.",
            "verification_status": "human_verified",
            "produced_by": "human",
            "publication_state": "publishable",
        },
        [
            {
                "to_source_id": "alpine_packet",
                "relation": "substantiates",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "archive_url": "https://web.archive.org/web/2026/https://alpinewy.gov/packet.pdf",
                "archive_status": "available",
                "scan_date": "2026-05-09",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "page",
                "page": 3,
                "verification_status": "human_verified",
                "confidence": "high",
                "transcript_path": "/Users/IA/Obsidian Vault/Source-Data/raw.txt",
                "deep_link": "/Users/IA/Raw-PDFs/packet.pdf#page=3",
            }
        ],
    )
    st.insert_statement(
        conn,
        {
            "statement_id": "stmt-unreviewed",
            "agenda_item_id": "alpine:2026-05-08:item-7",
            "statement_text": "Draft claim pending review.",
        },
        [
            {
                "to_source_id": "alpine_packet",
                "relation": "references",
                "original_url": "https://alpinewy.gov/packet.pdf",
                "archive_status": "not_checked",
                "scan_date": "2026-05-09",
                "captured_at_utc": "2026-05-09T12:00:00Z",
                "locator_kind": "page",
                "page": 4,
                "verification_status": "machine_extracted_unreviewed",
                "confidence": "low",
            }
        ],
    )

    cm.insert_agenda_thread(conn, "alpine:thread:fireworks-ban", "Fireworks ban",
                            "alpine", "fireworks ban")
    cm.insert_edge(conn, "agenda_item_in_thread", "alpine:2026-04-10:item-3", "alpine:thread:fireworks-ban")
    cm.insert_edge(conn, "agenda_item_in_thread", "alpine:2026-05-08:item-7", "alpine:thread:fireworks-ban")
    cm.insert_edge(conn, "agenda_item_supersedes", "alpine:2026-05-08:item-7", "alpine:2026-04-10:item-3")
    for tid, name, label in (
        ("topic:fireworks", "Fireworks", "fireworks"),
        ("topic:fire", "Fire prevention", "fire prevention"),
        ("topic:safety", "General safety", "general safety"),
    ):
        cm.insert_topic(conn, tid, name, label, jurisdiction_id="alpine")
    cm.insert_edge(conn, "topic_rollup", "topic:fireworks", "topic:fire")
    cm.insert_edge(conn, "topic_rollup", "topic:fire", "topic:safety")
    # plain-language label layer: a government alias with mandatory provenance,
    # including a vault local_ref that MUST be stripped at the read-API boundary.
    cm.insert_label_alias(
        conn, "topic:safety", "topic", "public safety", "government_term",
        {
            "source_id": "alpine_packet",
            "original_url": "https://alpinewy.gov/packet.pdf",
            "local_ref": "/Users/IA/Obsidian Vault/Source-Data/alpine/safety.md",
            "page": 3,
        },
        first_seen_meeting_id=2, first_seen_date="2026-05-08",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "slice4_smoke.db"
        db.apply_migrations(db_path)
        conn = db.open_db(db_path)
        try:
            _seed(conn)

            response = read_api.build_response(
                conn,
                thread_id="alpine:thread:fireworks-ban",
                topic_root="topic:safety",
            )
            blob = json.dumps(response, indent=2, sort_keys=True)

            # (1) only the eligible record is served
            served = response["records"]
            ids = {r["statement_id"] for r in served}
            if ids != {"stmt-eligible"}:
                _fail(f"served set is {ids}, expected exactly {{'stmt-eligible'}}")

            # (5) labels travel; eligible record is source-backed; no orphan
            rec = served[0]
            for label in ("verification_status", "produced_by", "correction_status", "ui_status"):
                if label not in rec:
                    _fail(f"served record missing label {label}")
            if rec["ui_status"] not in pub.PUBLICATION_ELIGIBLE_UI_STATUSES:
                _fail(f"served ui_status {rec['ui_status']!r} not publication-eligible")
            if not rec.get("evidence"):
                _fail("served record has no evidence pointer (orphan leaked)")

            # (2) transport-level: zero raw/absolute paths in the body
            for marker in ("/Users/", "Obsidian Vault", "Raw-PDFs", "transcript_path", "deep_link"):
                if marker in blob:
                    _fail(f"raw marker {marker!r} leaked into the response body")
            if "https://alpinewy.gov/packet.pdf" not in blob:
                _fail("public source URL was stripped (over-redaction)")

            # (3) new node/edge types exposed
            thread = response["agenda_thread"]
            member_ids = [m["agenda_item_id"] for m in thread["members"]]
            if member_ids != ["alpine:2026-04-10:item-3", "alpine:2026-05-08:item-7"]:
                _fail(f"thread members not chronological: {member_ids}")
            if not thread["lifecycle_edges"] or thread["lifecycle_edges"][0]["edge_type"] != "agenda_item_supersedes":
                _fail("typed supersede lifecycle edge missing from thread")
            crumb = [t["topic_id"] for t in response["topic_tree"]["breadcrumb"]]
            if crumb != ["topic:safety"]:
                _fail(f"root breadcrumb unexpected: {crumb}")
            fire = response["topic_tree"]["tree"]["children"][0]
            if fire["topic"]["topic_id"] != "topic:fire" or fire["children"][0]["topic"]["topic_id"] != "topic:fireworks":
                _fail("topic_rollup chain (safety -> fire -> fireworks) not exposed")

            # (3b) label layer: ≥1 topic node has canonicalHumanLabel + a government
            # sourceAlias carrying provenance; the government string is NOT primary;
            # the vault local_ref is stripped (already covered by the body sweep).
            root = response["topic_tree"]["root"]
            if root.get("canonicalHumanLabel") != "general safety":
                _fail(f"topic canonicalHumanLabel missing/wrong: {root.get('canonicalHumanLabel')}")
            aliases = root.get("sourceAliases") or []
            gov = [a for a in aliases if a.get("aliasType") == "government_term"]
            if not gov:
                _fail("no government sourceAlias on the topic node")
            ref = gov[0].get("sourceRef") or {}
            if not (ref.get("sourceId") and (ref.get("originalUrl") or ref.get("archiveUrl")) and ref.get("locator")):
                _fail(f"government alias sourceRef lacks mandatory provenance: {ref}")
            if root.get("canonicalHumanLabel") == gov[0].get("term"):
                _fail("government string leaked as the primary canonicalHumanLabel")
            if "local_ref" in blob or "localRef" in blob:
                _fail("alias local_ref leaked into the response body")

            # (3c) validator rejects an alias missing sourceRef (acceptance add).
            try:
                cm.insert_label_alias(conn, "topic:safety", "topic", "no-prov",
                                      "government_term", None)
            except cm.LabelAliasError:
                pass
            else:
                _fail("validator accepted an alias with NO sourceRef")

            # (4) acyclicity enforced before serving the tree
            conn.execute(
                "INSERT INTO concept_edges (edge_id, edge_type, from_node_id, "
                "from_node_type, to_node_id, to_node_type) VALUES "
                "('cycle', 'topic_rollup', 'topic:safety', 'topic', 'topic:fireworks', 'topic')"
            )
            conn.commit()
            try:
                read_api.topic_tree(conn, "topic:safety")
            except cm.TopicTreeCycleError:
                pass
            else:
                _fail("acyclicity guard did not reject a written cycle")
        finally:
            conn.close()

    print("SLICE4-PREREQ0 SMOKE PASS: eligible-only serve; zero raw paths in body; "
          "agenda_thread + chronological members + typed supersede; topic_rollup "
          "chain + breadcrumb; canonicalHumanLabel + government sourceAlias w/ "
          "provenance (local_ref stripped) + validator rejects missing sourceRef; "
          "acyclicity enforced; labels travel; no orphan served.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
