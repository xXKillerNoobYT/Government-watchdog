"""Tests for the concept-map registry SSOT + GOV-98 additions (Slice 4 Prereq-0).

Covers: the registry vocabulary (1.07 set + GOV-98 node/edges), the edge
endpoint contract, generic-table parity with the migration CHECK, and the
topic_rollup acyclicity invariant (BEH-TOPICTREE-4) at both insert time and
serve time.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import concept_map as cm  # noqa: E402
import db  # noqa: E402


@pytest.fixture()
def conn(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.apply_migrations(db_path)
    connection = db.open_db(db_path)
    yield connection
    connection.close()


# --- registry vocabulary ----------------------------------------------------


def test_gov98_node_added() -> None:
    assert "agenda_thread" in cm.ALLOWED_NODE_TYPES


def test_gov98_edges_added() -> None:
    for edge in (
        "agenda_item_in_thread",
        "agenda_item_supersedes",
        "agenda_item_amends",
        "agenda_item_revisits",
        "topic_rollup",
    ):
        assert edge in cm.ALLOWED_EDGE_TYPES


def test_existing_1_07_vocabulary_preserved() -> None:
    # Additive: the GOV-98 change never drops a 1.07 type.
    for node in ("meeting", "agenda_item", "statement", "topic", "evidence_link"):
        assert node in cm.ALLOWED_NODE_TYPES
    for edge in ("contains_agenda_item", "statement_from_segment", "topic_groups"):
        assert edge in cm.ALLOWED_EDGE_TYPES


def test_every_edge_endpoint_is_a_known_node_type() -> None:
    for etype, (froms, tos) in cm.EDGE_ENDPOINTS.items():
        assert etype in cm.ALLOWED_EDGE_TYPES
        assert (froms | tos) <= cm.ALLOWED_NODE_TYPES


def test_generic_edge_types_match_migration_check(conn: sqlite3.Connection) -> None:
    # The Python generic-edge set must equal the migration-0012 CHECK literal, or
    # an edge the registry accepts would be rejected by the DB (or vice versa).
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'concept_edges'"
    ).fetchone()[0]
    for etype in cm.GENERIC_EDGE_TYPES:
        assert f"'{etype}'" in sql, f"{etype} missing from concept_edges CHECK"


# --- edge endpoint contract -------------------------------------------------


def test_insert_edge_rejects_wrong_endpoint_type(conn: sqlite3.Connection) -> None:
    with pytest.raises(cm.EdgeError):
        # topic_rollup must be topic->topic, not agenda_item->topic.
        cm.insert_edge(conn, "topic_rollup", "alpine:item-1", "topic:fire",
                       from_node_type="agenda_item")


def test_insert_edge_rejects_non_generic_edge(conn: sqlite3.Connection) -> None:
    with pytest.raises(cm.EdgeError):
        cm.insert_edge(conn, "statement_from_segment", "s1", "seg1")


def test_insert_edge_is_idempotent(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:fireworks", "Fireworks", "fireworks")
    cm.insert_topic(conn, "topic:fire", "Fire prevention", "fire prevention")
    cm.insert_edge(conn, "topic_rollup", "topic:fireworks", "topic:fire")
    cm.insert_edge(conn, "topic_rollup", "topic:fireworks", "topic:fire")  # no dup
    count = conn.execute(
        "SELECT COUNT(*) FROM concept_edges WHERE edge_type = 'topic_rollup'"
    ).fetchone()[0]
    assert count == 1


# --- acyclicity (BEH-TOPICTREE-4) ------------------------------------------


def _seed_topic_chain(conn: sqlite3.Connection) -> None:
    for tid, name, label in (
        ("topic:fireworks", "Fireworks", "fireworks"),
        ("topic:fire", "Fire prevention", "fire prevention"),
        ("topic:safety", "General safety", "general safety"),
    ):
        cm.insert_topic(conn, tid, name, label)
    cm.insert_edge(conn, "topic_rollup", "topic:fireworks", "topic:fire")
    cm.insert_edge(conn, "topic_rollup", "topic:fire", "topic:safety")


def test_topic_rollup_chain_serves_acyclic(conn: sqlite3.Connection) -> None:
    _seed_topic_chain(conn)
    cm.assert_acyclic(conn)  # must not raise


def test_topic_rollup_self_loop_rejected(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:x", "X", "x")
    with pytest.raises(cm.TopicTreeCycleError):
        cm.insert_edge(conn, "topic_rollup", "topic:x", "topic:x")


# --- plain-language label layer (owner addendum / §A.7) --------------------


_GOOD_SOURCE_REF = {
    "source_id": "alpine_packet",
    "original_url": "https://alpinewy.gov/packet.pdf",
    "page": 3,
}


def test_canonical_human_label_required_on_topic(conn: sqlite3.Connection) -> None:
    with pytest.raises(cm.LabelAliasError):
        cm.insert_topic(conn, "topic:y", "Y", "")


def test_canonical_human_label_required_on_thread(conn: sqlite3.Connection) -> None:
    with pytest.raises(cm.LabelAliasError):
        cm.insert_agenda_thread(conn, "alpine:thread:z", "Z", "alpine", "")


def test_alias_with_full_source_ref_accepted(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    cm.insert_label_alias(conn, "topic:safety", "topic", "public safety",
                          "government_term", _GOOD_SOURCE_REF)
    aliases = cm.aliases_for_node(conn, "topic:safety", "topic")
    assert len(aliases) == 1
    assert aliases[0]["term"] == "public safety"
    assert aliases[0]["source_ref_source_id"] == "alpine_packet"


def test_alias_missing_source_ref_rejected(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    with pytest.raises(cm.LabelAliasError):
        cm.insert_label_alias(conn, "topic:safety", "topic", "public safety",
                              "government_term", None)


def test_alias_source_ref_missing_locator_rejected(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    with pytest.raises(cm.LabelAliasError):
        # has a source_id + url but NO locator (timestamp/page/section/paragraph)
        cm.insert_label_alias(conn, "topic:safety", "topic", "public safety",
                              "government_term",
                              {"source_id": "alpine_packet",
                               "original_url": "https://alpinewy.gov/packet.pdf"})


def test_alias_source_ref_missing_source_id_rejected(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    with pytest.raises(cm.LabelAliasError):
        cm.insert_label_alias(conn, "topic:safety", "topic", "public safety",
                              "government_term", {"original_url": "https://x", "page": 1})


def test_alias_bad_alias_type_rejected(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    with pytest.raises(cm.LabelAliasError):
        cm.insert_label_alias(conn, "topic:safety", "topic", "public safety",
                              "made_up_kind", _GOOD_SOURCE_REF)


def test_alias_on_non_labelled_node_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(cm.LabelAliasError):
        cm.insert_label_alias(conn, "stmt-1", "statement", "x",
                              "government_term", _GOOD_SOURCE_REF)


def test_topic_rollup_cycle_rejected_at_insert(conn: sqlite3.Connection) -> None:
    _seed_topic_chain(conn)
    # safety -> fireworks would close fireworks -> fire -> safety -> fireworks.
    with pytest.raises(cm.TopicTreeCycleError):
        cm.insert_edge(conn, "topic_rollup", "topic:safety", "topic:fireworks")


def test_assert_acyclic_catches_a_cycle_written_around_the_guard(conn: sqlite3.Connection) -> None:
    # Serve-time guard is independent: write a cycle straight to the table
    # (bypassing insert_edge) and prove assert_acyclic still rejects it.
    _seed_topic_chain(conn)
    conn.execute(
        "INSERT INTO concept_edges (edge_id, edge_type, from_node_id, from_node_type, "
        "to_node_id, to_node_type) VALUES "
        "('raw-cycle', 'topic_rollup', 'topic:safety', 'topic', 'topic:fireworks', 'topic')"
    )
    conn.commit()
    with pytest.raises(cm.TopicTreeCycleError):
        cm.assert_acyclic(conn)


# --- GOV-105: PII guard at the alias/label write boundary -------------------


def test_pii_guard_error_subclasses_label_alias_error() -> None:
    # Subclassing keeps every existing `except LabelAliasError` caller closed.
    assert issubclass(cm.PiiGuardError, cm.LabelAliasError)


def test_alias_term_with_email_rejected(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    with pytest.raises(cm.PiiGuardError):
        cm.insert_label_alias(conn, "topic:safety", "topic",
                              "contact jane.doe@gmail.com", "government_term",
                              _GOOD_SOURCE_REF)


def test_alias_term_with_phone_rejected(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    with pytest.raises(cm.PiiGuardError):
        cm.insert_label_alias(conn, "topic:safety", "topic",
                              "call 307-555-0123", "government_term",
                              _GOOD_SOURCE_REF)


def test_alias_locator_with_street_address_rejected(conn: sqlite3.Connection) -> None:
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    with pytest.raises(cm.PiiGuardError):
        cm.insert_label_alias(conn, "topic:safety", "topic", "public safety",
                              "government_term",
                              {"source_id": "p", "original_url": "https://x",
                               "section": "resident at 123 Main Street"})


def test_topic_label_with_ssn_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(cm.PiiGuardError):
        cm.insert_topic(conn, "topic:x", "X", "applicant 123-45-6789")


def test_agenda_thread_label_with_email_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(cm.PiiGuardError):
        cm.insert_agenda_thread(conn, "alpine:thread:z", "Z", "alpine",
                                "petition from j.smith@example.org")


def test_pii_guard_message_does_not_echo_the_value(conn: sqlite3.Connection) -> None:
    # Non-disclosure: the rejection names the field + kind, never the PII value.
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    with pytest.raises(cm.PiiGuardError) as exc:
        cm.insert_label_alias(conn, "topic:safety", "topic",
                              "reach me at 307-555-0123", "government_term",
                              _GOOD_SOURCE_REF)
    assert "307-555-0123" not in str(exc.value)


@pytest.mark.parametrize("civic_term", [
    "public safety",
    "Lincoln Street Bridge",
    "Drive-through permit application",
    "Highway 89 repaving",
    "Resolution 2024-15",
    "general safety",
])
def test_legitimate_civic_terms_accepted(conn: sqlite3.Connection,
                                         civic_term: str) -> None:
    # No false positives: real civic vocabulary writes through the guard cleanly.
    tid = f"topic:{abs(hash(civic_term))}"
    cm.insert_topic(conn, tid, civic_term, civic_term)
    cm.insert_label_alias(conn, tid, "topic", civic_term, "government_term",
                          _GOOD_SOURCE_REF)


def test_local_ref_written_but_not_web_projected(conn: sqlite3.Connection) -> None:
    # GOV-105 acceptance: vault local_ref stays write-allowed but is NEVER in the
    # web-safe projection (regression on the WRITE path).
    import read_api as ra  # noqa: E402

    vault = "/Users/IA/Documents/TOA/TownOfAlpine/packet.pdf"
    cm.insert_topic(conn, "topic:safety", "General safety", "general safety")
    cm.insert_label_alias(conn, "topic:safety", "topic", "public safety",
                          "government_term",
                          {"source_id": "alpine_packet", "local_ref": vault,
                           "page": 3})
    rows = cm.aliases_for_node(conn, "topic:safety", "topic")
    assert rows[0]["source_ref_local_ref"] == vault  # stored, reviewer-internal
    flat = repr(ra._safe_alias(rows[0]))
    assert "local_ref" not in flat
    assert vault not in flat
    assert "/Users/" not in flat


# --- GOV-1702 (C7b, read-api): the registry guards must survive `python -O` ----
#
# C7b hunted runtime safety across read-api's 14 bound modules. Four classes came
# back clean (no division, no unguarded `json.loads`, three indexing candidates all
# verified unreachable, and `assert_no_pii` RAISES despite its name). The one real
# finding was here.
#
# `concept_map` carried **eight** module-level `assert` statements under a comment
# reading "Import-time drift guards (fail at import, not at runtime)". `python -O`
# deletes assert statements outright, so they held in a normal run and were absent
# in an optimised one. This registry is the vocabulary every edge is validated
# against, and `read_api` — a byte-frozen serving surface — imports it.
#
# Same class as the four in `publication.py` (#229), which stay open only because
# that module is byte-frozen and converting them needs an unfreeze. `concept_map`
# is not frozen, so this half needed no decision — which narrows #229 to the part
# that genuinely does.


class TestRegistryGuardsSurviveOptimisation:

    def test_the_module_carries_no_bare_assert_at_all(self):
        """The property, stated structurally rather than by grepping prose.

        `-O` cannot delete what is not an assert. Checking the AST is what makes
        this hold for guards added later, not just the eight converted here.
        """
        import ast

        tree = ast.parse(Path(cm.__file__).read_text(encoding="utf-8"))
        asserts = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]
        assert not asserts, (
            f"concept_map.py has bare `assert` at line(s) {asserts}. `python -O` "
            "deletes them; use `_require(...)` so the guard survives optimisation.")

    def test_a_broken_registry_raises_under_O_as_well_as_normally(self):
        """The claim measured, not asserted — compile the module both ways.

        A source-level check alone would not prove the guard still FIRES under
        `-O`; this compiles a deliberately-inconsistent registry at optimize=0 and
        optimize=2 and requires the same refusal from both.
        """
        import textwrap

        broken = textwrap.dedent('''
            class RegistryConsistencyError(RuntimeError):
                pass
            def _require(condition, message):
                if not condition:
                    raise RegistryConsistencyError(message)
            ALLOWED = {"a"}
            _require("b" in ALLOWED, "unknown edge 'b'")
        ''')
        for optimize in (0, 1, 2):
            code = compile(broken, "<registry>", "exec", optimize=optimize)
            try:
                exec(code, {})
            except RuntimeError as exc:
                assert "unknown edge" in str(exc)
            else:
                raise AssertionError(
                    f"the registry guard did NOT fire at optimize={optimize} — "
                    "this is exactly the `assert` failure mode it replaced")

    def test_the_real_registry_is_consistent(self):
        """Non-vacuity: the guards above protect a registry that actually passes."""
        for etype, (froms, tos) in cm.EDGE_ENDPOINTS.items():
            assert etype in cm.ALLOWED_EDGE_TYPES
            assert not (froms | tos) - cm.ALLOWED_NODE_TYPES
