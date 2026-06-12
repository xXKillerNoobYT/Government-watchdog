"""GOV-138 / GOV-126 Phase 3+4 — volume run, Lanes 2->5 over real Alpine prose.

This is the run HARNESS that chains the four AI-gateway lanes over the preserved,
untimed Alpine transcript corpus and emits the Phase-4 evidence ledger + the
aggregate "nothing publishable by default" proof. It owns NO new policy: every
gate it exercises already lives in the merged lane modules. It only sequences
them over the operational corpus and reports what they did.

    Lane 2  ai_extraction.run_extraction   (production_proposer, source-grounded)
      -> Lane 3  ai_verification.run_verification  (deterministic compare)
        -> Lane 4  ai_risk_gate.run_risk           (deterministic risk screen)
          -> Lane 5  ai_risk_gate runtime reviewer-gate (allowlist; default-deny)

Binding bounds (owner-accepted GOV-126 plan rev e9435b55, confirmation bb425bf7):
- Alpine-only, reviewer-internal / vault-only. This harness PUBLISHES nothing.
- AI output is produced_by='ai', verification_status='machine_extracted_unreviewed'.
- Fail-closed: nothing is publishable by default. ``promote_statement`` is the
  ONLY sanctioned promotion path and never flips ``publication_state`` — this
  harness NEVER calls it (no human decision is being made here), so the run can
  only ever leave the corpus in the "nothing publishable" state it asserts.
- Conservative attribution: the proposer drops every speaker name (no name >
  wrong name). The mayor-investigation corpus stays excluded upstream.

Model seam (three injectable :class:`production_proposer.ModelClient` impls):
- ``--live`` injects :class:`production_proposer.AnthropicModelClient`, the real
  billable Claude call (lazy Anthropic SDK; reads ``ANTHROPIC_API_KEY`` from the
  env, never logged). Option A — not available in this run env (no SDK/key).
- ``--agent-inline --claims <path>`` injects :class:`_AgentInlineModelClient`,
  which returns claims the executing agent (claude-opus-4-8) authored by READING
  the preserved prose, loaded from a vault-only claims file. This is the
  CTO-authorized Option B mechanism for this bounded pass (GOV-141 decision).
  The client performs NO grounding/offset math and drops NO speaker — it only
  recovers each authored quote's exact stored whitespace against the live source
  so the DOWNSTREAM ``str.find`` grounding seam in ``build_claude_proposer`` can
  anchor it (B1: guard seam intact). The ledger records the true ``model_name``
  plus an ``agent_inline`` mechanism marker (B2: audit trade-off visible).
- The default (no flag) injects :class:`_NullModelClient`, which returns zero
  claims. That exercises the FULL chain + ledger + the publishable sweep over
  the real DB while fabricating nothing — a zero-spend wiring proof. It is even
  safe with ``--apply`` (it only records empty Lane-2/3/4 ledger rows).

Default is ``--dry-run`` (no commit). ``--apply`` commits the ledger rows. Per
BACKEND_CRAWLER_WORKFLOWS, the first live ``--apply`` is a CTO-escalation gate
(cleared for the Option-B pass by the GOV-141 decision).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import ai_extraction as ai  # noqa: E402
import ai_risk_gate as rg  # noqa: E402
import ai_verification as av  # noqa: E402
import read_api  # noqa: E402
from db import DEFAULT_DB_PATH, open_db  # noqa: E402
from production_proposer import (  # noqa: E402
    DEFAULT_MODEL_NAME,
    build_claude_proposer,
)

DEFAULT_SOURCE_ID = "alpine_local_corpus"
DEFAULT_REVIEWER_ID = "reviewer:isaac"


class _NullModelClient:
    """Offline model seam: extracts NOTHING. Proves the harness without spend.

    Returning ``[]`` is the model client's correct answer when no live model is
    attached — Lane 2 still opens a ledger row (output_count=0), and Lanes 3->5 +
    the publishable sweep all run over an empty statement set. Nothing is
    fabricated, so an offline run cannot create a publishable claim.
    """

    def extract(self, source_text: str, *, source_id: str) -> list[dict[str, Any]]:
        return []


def _resolve_exact_span(source_text: str, phrase: str) -> str | None:
    """Recover the EXACT stored substring for an agent-authored quote ``phrase``.

    The preserved prose is line-wrapped / irregularly spaced (ASR + PDF-extracted
    text), so an agent-authored quote is normalized on whitespace. This locates the
    first contiguous occurrence of the phrase's word tokens separated by ANY run of
    whitespace, and returns the verbatim matched span from ``source_text`` (or
    ``None`` if it does not occur). This is pure whitespace recovery — it does NOT
    invent text, change words, or compute char offsets; the returned string is a
    literal substring of ``source_text``, which the downstream grounding seam
    (:func:`production_proposer._ground_claim`) then independently anchors with
    ``str.find`` and from which IT derives ``char_start``/``char_end``. A phrase
    that does not occur verbatim resolves to ``None`` and the claim is dropped —
    fail-closed, the same outcome the grounding seam would reach.
    """
    tokens = phrase.split()
    if not tokens:
        return None
    pattern = re.compile(r"\s+".join(re.escape(t) for t in tokens))
    m = pattern.search(source_text)
    return m.group(0) if m else None


class _AgentInlineModelClient:
    """Option-B model seam (GOV-141): the executing agent IS the model client.

    Returns RAW claims the agent (claude-opus-4-8) authored by reading the
    preserved Alpine prose, loaded from a vault-only JSON file. Each claim carries
    a ``statement_text`` (neutral paraphrase) and a ``quote_phrase`` (a real,
    contiguous quote from the source, normalized on whitespace). ``extract`` resolves
    each quote to its exact stored span against the source it is handed and emits the
    proposer's raw-claim shape — ``speaker_name`` is always empty (conservative
    attribution; the proposer drops it regardless). The client does NO grounding,
    NO char-offset math, and NO PII screening — every guard stays downstream in
    ``build_claude_proposer`` + the Lane-2 write boundary (B1). An authored quote
    that does not occur verbatim in the source is dropped here, fail-closed.

    The claims file (real civic text) is vault-only / git-ignored — never committed
    (B3 / C1). Only this client class (which contains no real data) is committed.
    """

    def __init__(self, claims_path: Path) -> None:
        data = json.loads(Path(claims_path).read_text(encoding="utf-8"))
        claims = data.get("claims", data) if isinstance(data, dict) else data
        self._claims: list[dict[str, Any]] = list(claims) if isinstance(claims, list) else []

    def extract(self, source_text: str, *, source_id: str) -> list[dict[str, Any]]:
        raw: list[dict[str, Any]] = []
        for c in self._claims:
            statement_text = (c.get("statement_text") or "").strip()
            phrase = c.get("quote_phrase") or c.get("quoted_text") or ""
            if not statement_text or not phrase.strip():
                continue
            span = _resolve_exact_span(source_text, phrase)
            if span is None:
                continue  # unresolvable quote -> agent drops it (fail-closed)
            raw.append(
                {
                    "statement_text": statement_text,
                    "quoted_text": span,  # exact substring; proposer re-grounds via str.find
                    "confidence": c.get("confidence", "low"),
                    "speaker_name": "",  # conservative attribution: never name a speaker
                }
            )
        return raw


def _stamp(conn: sqlite3.Connection) -> str:
    """A run-id stamp from the shared UTC clock (compact, collision-resistant)."""
    return ai._now_utc_iso().replace(":", "").replace("-", "").replace(".", "")[:15]


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return -1  # table not present in this DB revision


def _publication_state_breakdown(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for state, n in conn.execute(
            "SELECT publication_state, COUNT(*) FROM statements GROUP BY publication_state"
        ):
            out[state or "<null>"] = int(n)
    except sqlite3.OperationalError:
        pass
    return out


def gather_evidence(
    conn: sqlite3.Connection, *, written_statement_ids: list[str]
) -> dict[str, Any]:
    """Phase-4 evidence: ledger row counts + the aggregate publishable sweep."""
    served = read_api.published_records(conn)
    pub_breakdown = _publication_state_breakdown(conn)
    ai_statements = _count(conn, "SELECT COUNT(*) FROM statements WHERE produced_by = 'ai'")
    return {
        "ledger": {
            "ai_extraction_runs_total": _count(conn, "SELECT COUNT(*) FROM ai_extraction_runs"),
            "ai_extraction_runs_by_lane": {
                lane: _count(
                    conn, "SELECT COUNT(*) FROM ai_extraction_runs WHERE lane = ?", (lane,)
                )
                for lane in ("2_extraction", "3_verification", "4_risk")
            },
            "ai_verification_results": _count(conn, "SELECT COUNT(*) FROM ai_verification_results"),
            "ai_risk_flags": _count(conn, "SELECT COUNT(*) FROM ai_risk_flags"),
            "reviewer_decisions": _count(conn, "SELECT COUNT(*) FROM reviewer_decisions"),
        },
        "corpus": {
            "statements_total": _count(conn, "SELECT COUNT(*) FROM statements"),
            "ai_statements": ai_statements,
            "statements_written_this_run": len(written_statement_ids),
            "publication_state_breakdown": pub_breakdown,
        },
        "nothing_publishable_sweep": {
            # The load-bearing aggregate assertion: the web-safe read API serves
            # NOTHING by default (both gates must agree; AI rows are
            # not_publishable + machine_extracted_unreviewed).
            "published_records_served": len(served),
            "any_publishable_state": pub_breakdown.get("publishable", 0),
            "holds": len(served) == 0 and pub_breakdown.get("publishable", 0) == 0,
        },
    }


def run_volume(
    conn: sqlite3.Connection,
    *,
    model_client: Any,
    source_id: str = DEFAULT_SOURCE_ID,
    model_name: str = DEFAULT_MODEL_NAME,
    model_version: str = "untimed-prose-v1",
    tool_version: str = "gov138-volume-run@local",
    extraction_mechanism: str = "offline",
    max_claims_per_source: int | None = None,
    reviewer_id: str = DEFAULT_REVIEWER_ID,
    commit: bool = False,
) -> dict[str, Any]:
    """Chain Lanes 2->5 over ``source_id`` and return a structured run report.

    Never promotes: there is no ``promote_statement`` call here. The reviewer
    allowlist is only INSPECTED (Lane-5 readiness) — the gate's enforcement is
    proven by the aggregate sweep showing nothing was served despite real AI rows.

    ``extraction_mechanism`` (B2): when not ``"offline"`` it is recorded INTO the
    ``ai_extraction_runs`` ledger ``tool_version`` (e.g. ``...+agent_inline(not-
    externally-re-derivable)``) so the audit trade-off of a non-SDK mechanism is
    visibly persisted, not hidden. The true runtime ``model_name`` is recorded
    unchanged.
    """
    dry_run = not commit
    stamp = _stamp(conn)
    # B2 — make the extraction mechanism visible in the ledger's tool_version.
    if extraction_mechanism and extraction_mechanism != "offline":
        tool_version = f"{tool_version}+{extraction_mechanism}(not-externally-re-derivable)"
    proposer = build_claude_proposer(model_client, max_claims_per_source=max_claims_per_source)

    # Lane 2 — source-grounded AI extraction (the only lane that can WRITE claims).
    lane2 = ai.run_extraction(
        conn,
        run_id=f"gov138-lane2-{stamp}",
        input_source_ids=[source_id],
        input_segment_ids=[],  # untimed source-anchored path
        proposer=proposer,
        tool_version=tool_version,
        model_name=model_name,
        model_version=model_version,
        dry_run=dry_run,
        commit=commit,
    )
    written: list[str] = list(lane2.get("written_statements", []))

    # Lane 3 — deterministic verification over what Lane 2 wrote.
    lane3 = av.run_verification(
        conn,
        run_id=f"gov138-lane3-{stamp}",
        input_statement_ids=written,
        input_source_ids=[source_id],
        tool_version=tool_version,
        dry_run=dry_run,
        commit=commit,
    )

    # Lane 4 — deterministic risk screen over the same set.
    lane4 = rg.run_risk(
        conn,
        run_id=f"gov138-lane4-{stamp}",
        input_statement_ids=written,
        input_source_ids=[source_id],
        tool_version=tool_version,
        dry_run=dry_run,
        commit=commit,
    )

    # Lane 5 — runtime reviewer-gate readiness (allowlist inspection only; the
    # gate's default-deny is what keeps everything not_publishable). We do NOT
    # promote: no human decision is being recorded in a volume run.
    reviewer_registered = rg.is_registered_reviewer(conn, reviewer_id)

    evidence = gather_evidence(conn, written_statement_ids=written)

    return {
        "source_id": source_id,
        "committed": commit,
        "model_name": model_name,
        "model_version": model_version,
        "tool_version": tool_version,
        "extraction_mechanism": extraction_mechanism,
        "reproducibility_note": (
            "agent_inline: extraction authored by the executing agent (claude-opus-4-8) "
            "reading preserved prose; not externally re-derivable from an API call. "
            "Integrity is carried by the mechanism-blind Lane-3 verification + char_span "
            "grounding, not by byte-reproducibility (AI_GATEWAY §; GOV-141 B2)."
            if extraction_mechanism == "agent_inline"
            else f"mechanism={extraction_mechanism}"
        ),
        "lanes": {
            "lane2_extraction": {
                "run_id": lane2.get("run_id"),
                "ok": lane2.get("ok"),
                "output_count": lane2.get("output_count"),
                "orphans_rejected": len(lane2.get("rejected", [])),
            },
            "lane3_verification": {"run_id": lane3.get("run_id"), "ok": lane3.get("ok")},
            "lane4_risk": {
                "run_id": lane4.get("run_id"),
                "ok": lane4.get("ok"),
                "flag_count": lane4.get("flag_count"),
            },
            "lane5_reviewer_gate": {
                "reviewer_id": reviewer_id,
                "reviewer_registered": reviewer_registered,
                "promotions_attempted": 0,
            },
        },
        "evidence": evidence,
    }


def _build_model_client(
    *, live: bool, agent_inline_claims: Path | None
) -> tuple[Any, str]:
    """Resolve the (model_client, extraction_mechanism) for the run.

    Exactly one non-offline mechanism may be selected. Returns the offline null
    client when neither ``--live`` nor ``--agent-inline`` is requested.
    """
    if live and agent_inline_claims is not None:
        raise SystemExit("choose at most one of --live (Option A) / --agent-inline (Option B)")
    if agent_inline_claims is not None:
        return _AgentInlineModelClient(agent_inline_claims), "agent_inline"
    if live:
        from production_proposer import AnthropicModelClient

        return AnthropicModelClient(), "anthropic_sdk"  # reads ANTHROPIC_API_KEY from env
    return _NullModelClient(), "offline"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GOV-138 Lane 2->5 volume run over Alpine prose")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH), help="operational DB path")
    ap.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    ap.add_argument("--reviewer-id", default=DEFAULT_REVIEWER_ID)
    ap.add_argument("--max-claims-per-source", type=int, default=None)
    ap.add_argument(
        "--live",
        action="store_true",
        help="Option A: use the real Claude proposer (needs anthropic SDK + "
        "ANTHROPIC_API_KEY); FIRST live --apply is a CTO-escalation gate",
    )
    ap.add_argument(
        "--agent-inline",
        metavar="CLAIMS_JSON",
        default=None,
        help="Option B (GOV-141): use agent-authored claims from this vault-only "
        "JSON file as the model seam (claude-opus-4-8). Mutually exclusive with --live.",
    )
    ap.add_argument("--apply", action="store_true", help="commit ledger rows (default: dry-run)")
    ap.add_argument("--log-dir", default=str(_SCRIPTS.parent / "Logs"))
    ap.add_argument("--report", default=None, help="optional path to write the JSON report")
    args = ap.parse_args(argv)

    # NOTE: open_db() returns a raw sqlite3.Connection, which auto-COMMITS on a
    # clean ``with`` exit. We must not let a dry-run persist, so we manage the
    # connection explicitly and rollback when not applying. The lanes only commit
    # internally when commit=True; a dry-run leaves writes uncommitted and we drop
    # them here.
    agent_inline_claims = Path(args.agent_inline) if args.agent_inline else None
    model_client, mechanism = _build_model_client(
        live=args.live, agent_inline_claims=agent_inline_claims
    )

    conn = open_db(Path(args.db))
    try:
        report = run_volume(
            conn,
            model_client=model_client,
            source_id=args.source_id,
            extraction_mechanism=mechanism,
            max_claims_per_source=args.max_claims_per_source,
            reviewer_id=args.reviewer_id,
            commit=args.apply,
        )
        if not args.apply:
            conn.rollback()  # dry-run: discard every uncommitted ledger write
    finally:
        conn.close()

    report["mode"] = {
        "anthropic_sdk": "live-anthropic-sdk",
        "agent_inline": "live-agent-inline",
        "offline": "offline-null-model",
    }[mechanism]
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{report['lanes']['lane2_extraction']['run_id']}.log"
    log_path.write_text(text + "\n", encoding="utf-8")
    print(f"\nrun log: {log_path}", file=sys.stderr)
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")

    holds = report["evidence"]["nothing_publishable_sweep"]["holds"]
    return 0 if holds else 1


if __name__ == "__main__":
    raise SystemExit(main())
