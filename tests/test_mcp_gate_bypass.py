"""§3.9 gate-bypass regression (AM-3, D2, D5) — the RED-condition proofs.

Four structural guarantees:

* **AM-3 canonical isolation** — canonical tables are byte-identical before and
  after a full three-lens run (a lens cannot mutate a fact).
* **D2 single call site** — ``adapter.generate()`` is called from exactly one
  module (``routing.py``); a static AST scan asserts nothing else does.
* **Write-surface guard** — the new domain modules never emit an INSERT/UPDATE/
  DELETE against any canonical table; the only persistence is the staging
  ``submit_output`` write (+ its own review_state bookkeeping).
* **Risk-scanner rejection path** — a generated body carrying a privacy/legal/
  moderation signal is staged ``rejected`` (never usable), not silently accepted.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from conftest import fake_adapter

from mcp_service import analysis
from mcp_service.providers.base import GenerationRequest, GenerationResult

PKG = Path(__file__).resolve().parent.parent / "scripts" / "mcp_service"

CANONICAL_TABLES = [
    "statements", "evidence_links", "sources", "transcript_segments",
    "transcripts", "agenda_items", "reviewer_decisions", "speaker_attributions",
]


def _snapshot(conn) -> str:
    h = hashlib.sha256()
    for table in CANONICAL_TABLES:
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1"):
            h.update(repr(tuple(row)).encode("utf-8"))
        h.update(f"|{table}|".encode())
    return h.hexdigest()


# --- AM-3: a full lens run mutates ZERO canonical rows ------------------------

def test_full_three_lens_run_leaves_canonical_byte_identical(routed):
    before = _snapshot(routed)
    token = analysis.mint_submit_token(routed, "job1")
    summary = analysis.run_multi_lens(
        routed, job_id="job1", adapters={"fake": fake_adapter()}, token=token)
    after = _snapshot(routed)
    assert before == after, "a lens run mutated a canonical record (RED)"
    # The only new rows are in the staging sink.
    assert summary["lens_count"] == 3
    assert routed.execute("SELECT COUNT(*) FROM mcp_job_outputs").fetchone()[0] == 3


# --- D2: exactly one call site of adapter.generate() --------------------------

def _modules_calling_generate() -> set[str]:
    callers = set()
    for path in PKG.rglob("*.py"):
        if "providers" in path.parts:
            continue  # adapters DEFINE generate; they do not call an adapter
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "generate":
                    callers.add(path.name)
    return callers


def test_single_generation_call_site_is_routing_only():
    assert _modules_calling_generate() == {"routing.py"}


# --- write-surface guard: no canonical writes in the new domain modules -------

# Match only real SQL writes (keyword + table + trailing SQL syntax), so prose in
# a docstring ("...a review_state bookkeeping UPDATE on that row...") is ignored.
_WRITE_RE = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([a-zA-Z_]\w*)\b"
    r"|UPDATE\s+([a-zA-Z_]\w*)\s+SET\b"
    r"|DELETE\s+FROM\s+([a-zA-Z_]\w*)\b",
    re.IGNORECASE)


def _written_tables(src: str) -> set[str]:
    return {t for match in _WRITE_RE.findall(src) for t in match if t}
NEW_MODULES = ["analysis.py", "routing.py", "budget.py", "health.py", "lenses.py"]


def test_new_modules_never_write_a_canonical_table():
    canonical = set(CANONICAL_TABLES) | {"mcp_capability_grants", "mcp_provider_registry"}
    for name in NEW_MODULES:
        for table in _written_tables((PKG / name).read_text(encoding="utf-8")):
            assert table not in canonical, (
                f"{name} writes canonical/registry table {table!r} (write-surface breach)")


def test_analysis_only_writes_staging_sink():
    tables = _written_tables((PKG / "analysis.py").read_text(encoding="utf-8"))
    # analysis.py's only direct write is the review_state bookkeeping UPDATE.
    assert tables <= {"mcp_job_outputs"}, f"analysis.py writes unexpected tables {tables}"


# --- risk-scanner rejection path ---------------------------------------------

class _RiskyAdapter:
    """A local adapter whose output trips ai_risk_gate.scan_text (moderation+legal)."""

    provider_id, kind = "fake", "fake"

    def capabilities(self):
        return {"local": True, "network": False}

    def generate(self, request: GenerationRequest) -> GenerationResult:
        text = "Allegedly the official embezzled funds and should resign."
        return GenerationResult(text=text, input_units=5, output_units=8, latency_ms=0)


def test_risky_output_is_staged_rejected_never_usable(routed):
    token = analysis.mint_submit_token(routed, "job1")
    summary = analysis.run_multi_lens(
        routed, job_id="job1", adapters={"fake": _RiskyAdapter()}, token=token,
        lens_ids=["lens.libertarian"])
    run = summary["runs"][0]
    assert run["validation"] == "rejected"
    assert run["finding_categories"]  # the frozen scanner fired
    row = routed.execute(
        "SELECT review_state FROM mcp_job_outputs WHERE output_id = ?",
        (run["output_id"],)).fetchone()
    assert row["review_state"] == "rejected"  # never promotable


def test_clean_output_is_unreviewed_not_auto_approved(routed):
    token = analysis.mint_submit_token(routed, "job1")
    summary = analysis.run_multi_lens(
        routed, job_id="job1", adapters={"fake": fake_adapter()}, token=token,
        lens_ids=["lens.libertarian"])
    run = summary["runs"][0]
    assert run["validation"] == "accepted"
    row = routed.execute(
        "SELECT review_state FROM mcp_job_outputs WHERE output_id = ?",
        (run["output_id"],)).fetchone()
    # Clean != approved: it stays 'unreviewed' pending a human — never auto-promoted.
    assert row["review_state"] == "unreviewed"
