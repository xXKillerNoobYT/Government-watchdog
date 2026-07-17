"""WL-1..WL-6 synthetic-workload driver (PILOT-2026 §1, GOV-781 leg 2).

Drives the **real** merged stack, never mocks (protocol §1): MCP capability
grants + the guarded resource/tool boundary, ``mcp_service.routing`` on a local
adapter, ``job_queue`` lanes, the budget fail-closed path, and the
consent-gated email outbox on the null adapter. Every write lands in the same
0021/0022/0023/0025 tables the economics ledger already reads, so the snapshot
(§2) needs no extra instrumentation.

Two modes:

* **plan** (dry-run, the default): pure, clock-free, DB-free enumeration of the
  bounded job manifest from ``(seed, bounds)``. Same inputs => byte-identical
  manifest (§5.3 test 1). Writes nothing.
* **run(..., apply=True)**: bootstraps a synthetic Alpine substrate (invented
  data, never registry rows) and executes the manifest against the live stack,
  returning an outcome report plus the §1.2 zero-credit assertion result.

All data here is synthetic. No network beyond a local Ollama (opt-in); the paid
provider registry stays at cap 0 (BUD-5).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

# Sibling top-level modules under scripts/ (on sys.path via the CLI / conftest).
import job_queue
from accounts import consent as consent_mod
from accounts import service as accounts_service
from email_gateway import outbox as email_outbox
from economics import areas as econ_areas
from mcp_service import budget as mcp_budget
from mcp_service import capability, contracts, lenses, service
from mcp_service.errors import MCPDenied
from mcp_service.providers import base as provider_base

from . import (
    ALPINE_AREA_ID,
    DEFAULT_BOUNDS,
    LINCOLN_COUNTY_ID,
    MAX_TOTAL_CALLS,
    WL5_NOCONSENT_PROBES,
    WYOMING_STATE_ID,
)

# Stable ids for the synthetic substrate (invented; never registry ids).
_READ_JOB = "pilot-job-alpine"
_TRIPWIRE_JOB = "pilot-job-tripwire"
_SHARED_JOB = "pilot-job-shared"
_CLEAN_STMT = "pilot-stmt-clean"
_TRIPWIRE_STMT = "pilot-stmt-tripwire"
_SEGMENT = "pilot-seg"
_TRANSCRIPT_ID = 990001
_BREACH_PROVIDER = "fakecap"
_BREACH_BUDGET_ID = "budget-pilot-breach-probe"
_BREACH_JOB_KIND = "lens_breach"
_LENS_JOB_KIND = "lens_analysis"
_QUEUE_LANE = "2_extraction"

#: A raw filesystem path embedded in an allowlisted value — the frozen
#: ``read_api`` RawPathLeak scanner denies it (mirrors test_mcp_boundary).
_TRIPWIRE_TEXT = "see /Users/IA/Obsidian Vault/TownOfAlpine/secret.pdf"

#: Resource scopes a wide-open read grant carries.
_READ_SCOPES = sorted(contracts.RESOURCE_SCOPES.values())


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def current_period(now: datetime | None = None) -> str:
    """The ``YYYY-MM`` period a run lands in (audit/queue rows stamp wall-clock)."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def default_bounds() -> dict[str, int]:
    """A fresh copy of the ASSUMED default per-type call bounds."""
    return dict(DEFAULT_BOUNDS)


# ---------------------------------------------------------------------------
# plan (dry-run): deterministic, clock-free, DB-free
# ---------------------------------------------------------------------------

def _lens_pack_ids() -> list[str]:
    """The shipped lens pack ids (module constant — no DB, no clock)."""
    return list(lenses.LENSES)


def plan(seed: str, bounds: dict[str, int] | None = None) -> list[dict]:
    """Enumerate the bounded planned-job manifest deterministically.

    Each entry is ``{wl, index, kind, area_id, detail}``. WL-2 fans out one
    block per shipped lens pack; WL-5 appends the fixed no-consent probes. The
    manifest is a pure function of ``(seed, bounds)`` — no clock, no DB, no
    randomness — so two plans with the same inputs are byte-identical.
    """
    b = default_bounds()
    if bounds:
        b.update({k: int(v) for k, v in bounds.items() if k in b})

    jobs: list[dict] = []

    def _nonce(*parts: object) -> str:
        raw = "|".join([seed, *(str(p) for p in parts)])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    for i in range(b["WL-1"]):
        jobs.append({"wl": "WL-1", "index": i, "kind": "resource_read",
                     "area_id": ALPINE_AREA_ID, "detail": _CLEAN_STMT})

    for pack in _lens_pack_ids():
        for j in range(b["WL-2"]):
            jobs.append({"wl": "WL-2", "index": j, "kind": "lens_job",
                         "area_id": ALPINE_AREA_ID,
                         "detail": pack, "nonce": _nonce("WL-2", pack, j)})

    for k in range(b["WL-3"]):
        jobs.append({"wl": "WL-3", "index": k, "kind": "queue_job",
                     "area_id": ALPINE_AREA_ID, "detail": _QUEUE_LANE})

    for probe in ("scope", "redaction", "budget", "revocation"):
        for n in range(b["WL-4"]):
            jobs.append({"wl": "WL-4", "index": n, "kind": "safety_probe",
                         "area_id": ALPINE_AREA_ID, "detail": probe})

    for s in range(b["WL-5"]):
        jobs.append({"wl": "WL-5", "index": s, "kind": "notification_send",
                     "area_id": ALPINE_AREA_ID, "detail": "consent_recorded"})
    for s in range(WL5_NOCONSENT_PROBES):
        jobs.append({"wl": "WL-5", "index": b["WL-5"] + s, "kind": "notification_send",
                     "area_id": ALPINE_AREA_ID, "detail": "no_consent_probe"})

    for m in range(b["WL-6"]):
        jobs.append({"wl": "WL-6", "index": m, "kind": "shared_pool_job",
                     "area_id": None, "detail": _QUEUE_LANE})

    if len(jobs) > MAX_TOTAL_CALLS:
        raise ValueError(
            f"planned {len(jobs)} calls exceeds MAX_TOTAL_CALLS={MAX_TOTAL_CALLS}; "
            "lower the bounds")
    return jobs


def manifest(seed: str, bounds: dict[str, int] | None = None) -> dict:
    """The dry-run artifact: planned jobs + a content hash for reproducibility."""
    jobs = plan(seed, bounds)
    canonical = json.dumps(jobs, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "schema": "PILOT-2026/manifest/v1",
        "seed": seed,
        "bounds": bounds or default_bounds(),
        "planned_job_count": len(jobs),
        "planned_jobs": jobs,
        "manifest_sha256": digest,
    }


# ---------------------------------------------------------------------------
# run (--apply): execute against the live stack
# ---------------------------------------------------------------------------

def _adapter_for(provider_id: str, provider_kind: str):
    """Return a provider adapter instance for the chosen local provider."""
    if provider_kind == "ollama":
        from mcp_service.providers.ollama import OllamaAdapter

        return OllamaAdapter(provider_id=provider_id)
    from mcp_service.providers.fake import FakeAdapter

    return FakeAdapter(provider_id=provider_id, model=f"{provider_id}-1")


def _seed_envelope(conn: sqlite3.Connection, envelope_id: int,
                   area_id: str | None) -> None:
    now = _utcnow()
    conn.execute(
        "INSERT OR IGNORE INTO webhook_sources (source_key, secret_ref, active, created_at)"
        " VALUES ('pilot-src', 'ref:pilot', 1, ?)", (now,))
    conn.execute(
        "INSERT INTO event_envelopes (envelope_id, received_at, source_key,"
        " canonical_payload, payload_sha256, source_hash, area_id, event_kind,"
        " policy_version, dedupe_key) VALUES (?, ?, 'pilot-src', '{}', ?, ?, ?,"
        " 'ingest', 'p1', ?)",
        (envelope_id, now, f"h{envelope_id}", f"sh{envelope_id}", area_id,
         f"pilot-dk-{envelope_id}"))


def _route_provider(conn: sqlite3.Connection, *, provider_id: str, kind: str,
                    job_kind: str, cap_units: int, budget_cap: int,
                    budget_id: str, model: str) -> None:
    """Make a local provider routable + attach a routing policy (mirrors the CLI
    composition root / conftest.seed_local_routing, all additive)."""
    provider_base.register_provider(conn, provider_id=provider_id, kind=kind,
                                    budget_cap_units=cap_units)
    conn.execute("UPDATE mcp_provider_registry SET enabled = 1, budget_cap_units = ?"
                 " WHERE provider_id = ?", (cap_units, provider_id))
    mcp_budget.create_budget(conn, budget_id=budget_id, provider_id=provider_id,
                             cap_units=budget_cap, window_kind="total",
                             area_id=ALPINE_AREA_ID)
    conn.execute(
        "INSERT OR IGNORE INTO mcp_routing_policies (policy_id, version, job_kind,"
        " context_class, provider_preference, model, max_output_units, created_utc)"
        " VALUES (?, '1.0.0', ?, 'local_only', ?, ?, 50, ?)",
        (f"policy-{provider_id}", job_kind, json.dumps([provider_id]), model,
         _utcnow()))
    conn.commit()


def _bootstrap(conn: sqlite3.Connection, *, provider_id: str,
               provider_kind: str) -> dict:
    """Seed the synthetic Alpine substrate and mint the grants the run needs.

    Idempotent-ish: safe on a fresh migrated DB. Everything here is invented
    test data (INV-7 keeps registry rows out of the loop).
    """
    now = _utcnow()

    # Rollup spine: alpine (town) under lincoln (county) under wy (state).
    econ_areas.create_area(conn, area_id=WYOMING_STATE_ID, kind="state", name="Wyoming")
    econ_areas.create_area(conn, area_id=LINCOLN_COUNTY_ID, kind="county",
                           name="Lincoln County", parent_area_id=WYOMING_STATE_ID)
    econ_areas.create_area(conn, area_id=ALPINE_AREA_ID, kind="town", name="Alpine",
                           parent_area_id=LINCOLN_COUNTY_ID)

    # One clean statement + one redaction-tripwire statement, both over one
    # segment, each in its own job selector.
    conn.execute(
        "INSERT OR IGNORE INTO transcripts (id, video_id, video_url, full_text,"
        " local_path, sha256, fetch_time_utc) VALUES (?, 'pilot-vid',"
        " 'https://youtube.com/watch?v=pilot', 'synthetic transcript',"
        " '/Users/IA/vault/pilot.txt', 'sha-pilot', ?)", (_TRANSCRIPT_ID, now))
    conn.execute(
        "INSERT OR IGNORE INTO transcript_segments (segment_id, transcript_id,"
        " segment_index, timestamp_seconds, timestamp_human, segment_text)"
        " VALUES (?, ?, 0, 12, '0:12', 'The council approved the budget.')",
        (_SEGMENT, _TRANSCRIPT_ID))
    conn.execute(
        "INSERT OR IGNORE INTO statements (statement_id, segment_id, statement_text,"
        " verification_status, publication_state) VALUES (?, ?,"
        " 'The council approved the quarterly budget line.',"
        " 'reviewed_source_linked', 'not_publishable')", (_CLEAN_STMT, _SEGMENT))
    conn.execute(
        "INSERT OR IGNORE INTO statements (statement_id, segment_id, statement_text,"
        " verification_status, publication_state) VALUES (?, ?, ?,"
        " 'reviewed_source_linked', 'not_publishable')",
        (_TRIPWIRE_STMT, _SEGMENT, _TRIPWIRE_TEXT))

    # Three MCP jobs: clean read (alpine), tripwire read (alpine), shared (NULL).
    conn.execute(
        "INSERT OR IGNORE INTO mcp_jobs (job_id, area_id, job_kind, input_selector,"
        " policy_pack_id, policy_pack_version) VALUES (?, ?, 'summarize', ?, NULL, NULL)",
        (_READ_JOB, ALPINE_AREA_ID,
         json.dumps({"statement_ids": [_CLEAN_STMT], "segment_ids": [_SEGMENT]})))
    conn.execute(
        "INSERT OR IGNORE INTO mcp_jobs (job_id, area_id, job_kind, input_selector,"
        " policy_pack_id, policy_pack_version) VALUES (?, ?, 'summarize', ?, NULL, NULL)",
        (_TRIPWIRE_JOB, ALPINE_AREA_ID,
         json.dumps({"statement_ids": [_TRIPWIRE_STMT]})))
    conn.execute(
        "INSERT OR IGNORE INTO mcp_jobs (job_id, area_id, job_kind, input_selector,"
        " policy_pack_id, policy_pack_version) VALUES (?, NULL, 'summarize', '{}', NULL, NULL)",
        (_SHARED_JOB,))

    # Lens packs (WL-2) + the local routing provider (fake or ollama).
    lenses.seed_lens_packs(conn)
    lenses.register_output_schema()
    _route_provider(conn, provider_id=provider_id, kind=provider_kind,
                    job_kind=_LENS_JOB_KIND, cap_units=100000, budget_cap=100000,
                    budget_id=f"budget-{provider_id}", model=f"{provider_id}-1")
    # WL-4(c) breach provider: registry-callable, but a tiny enforced budget cap
    # so a single routed call trips the fail-closed pause (D3 / AM-4).
    _route_provider(conn, provider_id=_BREACH_PROVIDER, kind="fake",
                    job_kind=_BREACH_JOB_KIND, cap_units=100, budget_cap=1,
                    budget_id=_BREACH_BUDGET_ID, model="fakecap-1")

    # Envelopes for the queue lanes (one alpine, one shared-pool).
    env_alpine, env_shared = 990101, 990102
    _seed_envelope(conn, env_alpine, ALPINE_AREA_ID)
    _seed_envelope(conn, env_shared, None)
    conn.commit()

    # Grants (per-job HMAC tokens). Needs MCP_HMAC_SECRET set in the run env.
    _, full_token = capability.mint_grant(conn, job_id=_READ_JOB, scopes=_READ_SCOPES)
    _, tripwire_token = capability.mint_grant(conn, job_id=_TRIPWIRE_JOB, scopes=_READ_SCOPES)
    _, noscope_token = capability.mint_grant(conn, job_id=_READ_JOB, scopes=[])
    revoke_grant_id, revoke_token = capability.mint_grant(
        conn, job_id=_READ_JOB, scopes=_READ_SCOPES)

    return {
        "provider_id": provider_id,
        "env_alpine": env_alpine,
        "env_shared": env_shared,
        "full_token": full_token,
        "tripwire_token": tripwire_token,
        "noscope_token": noscope_token,
        "revoke_grant_id": revoke_grant_id,
        "revoke_token": revoke_token,
        "breach_budget_ids": [_BREACH_BUDGET_ID],
        "lens_packs": _lens_pack_ids(),
    }


def _require_secret() -> None:
    if not os.environ.get("MCP_HMAC_SECRET") and not os.environ.get("MCP_HMAC_SECRET_FILE"):
        raise RuntimeError(
            "MCP_HMAC_SECRET (or MCP_HMAC_SECRET_FILE) must be set to run the "
            "capability-gated workload (INV-7: never a repo default)")


def run(conn: sqlite3.Connection, *, seed: str, bounds: dict[str, int] | None = None,
        apply: bool = False, provider_id: str = "fake",
        provider_kind: str = "fake") -> dict:
    """Execute (or, dry-run, only plan) the Wave-0 workload.

    Dry-run (``apply=False``, the default per GOV-631) returns the planned
    manifest and writes nothing. ``apply=True`` bootstraps the substrate and
    drives the live stack, returning per-WL outcome counts, the run period, and
    the §1.2 zero-credit assertion.
    """
    man = manifest(seed, bounds)
    if not apply:
        return {"schema": "PILOT-2026/run/v1", "applied": False,
                "manifest": man, "period": None}

    _require_secret()
    b = default_bounds()
    if bounds:
        b.update({k: int(v) for k, v in bounds.items() if k in b})

    h = _bootstrap(conn, provider_id=provider_id, provider_kind=provider_kind)
    adapter = _adapter_for(provider_id, provider_kind)
    adapters = {provider_id: adapter, _BREACH_PROVIDER: _adapter_for(_BREACH_PROVIDER, "fake")}

    from mcp_service import routing

    counts = {"WL-1": 0, "WL-2": 0, "WL-3": 0, "WL-4": {}, "WL-5": {}, "WL-6": 0}
    period = current_period()

    # WL-1: typed resource reads (read-path audit envelope + latency; §1.1).
    for _ in range(b["WL-1"]):
        service.read_resource(conn, "evidence.statement", _CLEAN_STMT,
                              h["full_token"], job_id=_READ_JOB)
        counts["WL-1"] += 1

    # WL-2: lens analysis jobs, one block per shipped lens pack, on the local
    # adapter (provider half of LED-1; lens_version stamped on every audit row).
    for pack in h["lens_packs"]:
        for j in range(b["WL-2"]):
            routing.route_and_generate(
                conn, job_kind=_LENS_JOB_KIND, adapters=adapters,
                context_parts=[f"pilot|{seed}|{pack}|{j}"], area_id=ALPINE_AREA_ID,
                job_id=_READ_JOB, lens_version=pack)
            counts["WL-2"] += 1

    # WL-3: queue-lane jobs (enqueue -> lease -> success with LED-1 metrics).
    for k in range(b["WL-3"]):
        jid = job_queue.enqueue_job(conn, envelope_id=h["env_alpine"],
                                    lane=_QUEUE_LANE, area_id=ALPINE_AREA_ID)
        job_queue.lease_job(conn, jid, "pilot-worker")
        job_queue.record_success(conn, jid, metrics={
            "cpu_s": 0.5 + (k % 5) * 0.1, "queue_wait_s": 0.2 + (k % 3) * 0.1})
        conn.commit()
        counts["WL-3"] += 1

    # WL-4: deliberate safety failures — each must fail closed with an audit row.
    counts["WL-4"] = _run_safety_probes(conn, h, adapters, bounds=b)

    # WL-5: consent-gated notification sends on the null adapter + no-consent probes.
    counts["WL-5"] = _run_notifications(conn, seed, bounds=b)

    # WL-6: shared-pool attribution probes (area_id NULL — AREA-2 disclosed pool).
    for _ in range(b["WL-6"]):
        jid = job_queue.enqueue_job(conn, envelope_id=h["env_shared"],
                                    lane=_QUEUE_LANE, area_id=None)
        job_queue.lease_job(conn, jid, "pilot-worker")
        job_queue.record_success(conn, jid, metrics={"cpu_s": 1.0, "queue_wait_s": 0.3})
        conn.commit()
        counts["WL-6"] += 1

    zero_credit = assert_zero_credit(conn, period, breach_budget_ids=h["breach_budget_ids"])

    return {
        "schema": "PILOT-2026/run/v1",
        "applied": True,
        "seed": seed,
        "period": period,
        "area_id": ALPINE_AREA_ID,
        "provider_id": provider_id,
        "manifest_sha256": man["manifest_sha256"],
        "counts": counts,
        "zero_credit": zero_credit,
    }


def _run_safety_probes(conn, h, adapters, *, bounds) -> dict:
    """WL-4 (a) out-of-scope, (b) redaction, (c) budget breach, (d) revocation."""
    from mcp_service import routing

    out = {"scope_deny": 0, "redaction_deny": 0, "budget_deny": 0,
           "revocation_deny": 0}
    n = bounds["WL-4"]

    for _ in range(n):
        # (a) out-of-scope tool/resource call -> deny.
        try:
            service.read_resource(conn, "evidence.statement", _CLEAN_STMT,
                                  h["noscope_token"], job_id=_READ_JOB)
        except MCPDenied:
            out["scope_deny"] += 1

        # (b) redaction tripwire (raw path in an allowlisted value) -> denied:redaction.
        try:
            service.read_resource(conn, "evidence.statement", _TRIPWIRE_STMT,
                                  h["tripwire_token"], job_id=_TRIPWIRE_JOB)
        except MCPDenied as exc:
            if exc.code == "denied:redaction":
                out["redaction_deny"] += 1

        # (c) synthetic budget breach -> pause + budget event + outbox (AM-4).
        try:
            routing.route_and_generate(
                conn, job_kind=_BREACH_JOB_KIND, adapters=adapters,
                context_parts=["pilot|budget-breach-probe"], area_id=ALPINE_AREA_ID,
                job_id=_READ_JOB, lens_version="breach-probe")
        except MCPDenied as exc:
            if exc.code == "denied:budget":
                out["budget_deny"] += 1

    # (d) grant revocation mid-run -> revoked token denies (once; grant is spent).
    capability.revoke(conn, h["revoke_grant_id"])
    for _ in range(n):
        try:
            service.read_resource(conn, "evidence.statement", _CLEAN_STMT,
                                  h["revoke_token"], job_id=_READ_JOB)
        except MCPDenied:
            out["revocation_deny"] += 1
    return out


def _run_notifications(conn, seed, *, bounds) -> dict:
    """WL-5: consented sends via the null adapter + no-consent hard-fail probes."""
    salt = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8]
    out = {"sent": 0, "no_consent_refused": 0}

    for s in range(bounds["WL-5"]):
        user_id = accounts_service.create_user(
            conn, email=f"pilot-{salt}-{s}@example.invalid", area_interest=ALPINE_AREA_ID)
        consent_mod.grant_email_consent(conn, user_id)
        email_outbox.queue_email(conn, user_id=user_id, template_id="consent_recorded")
    results = email_outbox.send_pending(conn, limit=MAX_TOTAL_CALLS)
    out["sent"] = sum(1 for r in results if r["status"] == "sent")

    # No-consent probes: NOTIF-1 hard-fail (AM-5) — no row is queued.
    for s in range(WL5_NOCONSENT_PROBES):
        user_id = accounts_service.create_user(
            conn, email=f"pilot-{salt}-noc-{s}@example.invalid")
        try:
            email_outbox.queue_email(conn, user_id=user_id, template_id="consent_recorded")
        except email_outbox.ConsentMissing:
            out["no_consent_refused"] += 1
    return out


# ---------------------------------------------------------------------------
# §1.2 zero-credit assertion (exit check of every Wave-0 run)
# ---------------------------------------------------------------------------

def assert_zero_credit(conn: sqlite3.Connection, period: str, *,
                       breach_budget_ids: list[str]) -> dict:
    """§1.2: no non-local provider was called; only the deliberate WL-4(c) budget
    breach exists (and it is paused). Raises ``ZeroCreditViolation`` on any breach.
    """
    violations: list[str] = []

    # (a) every audit provider is local (fake/ollama) or NULL (read path).
    bad = conn.execute(
        "SELECT DISTINCT provider FROM mcp_audit_events"
        " WHERE substr(created_at, 1, 7) = ? AND provider IS NOT NULL"
        "   AND provider NOT IN (SELECT provider_id FROM mcp_provider_registry"
        "                        WHERE kind IN ('fake', 'ollama', 'local'))",
        (period,)).fetchall()
    for r in bad:
        violations.append(f"non-local provider named in audit: {r[0]!r}")

    # (b) the only budget breach is the declared WL-4(c) probe, and it is paused.
    breaches = conn.execute(
        "SELECT e.budget_id, b.paused_at FROM mcp_budget_events e"
        " JOIN mcp_budgets b ON b.budget_id = e.budget_id"
        " WHERE e.event_kind = 'breach' AND substr(e.created_utc, 1, 7) = ?",
        (period,)).fetchall()
    allowed = set(breach_budget_ids)
    for budget_id, paused_at in breaches:
        if budget_id not in allowed:
            violations.append(f"unexpected budget breach on {budget_id!r}")
        elif not paused_at:
            violations.append(f"breach probe {budget_id!r} did not pause (AM-4)")

    result = {
        "period": period,
        "non_local_providers": [r[0] for r in bad],
        "breach_budget_ids": [r[0] for r in breaches],
        "ok": not violations,
        "violations": violations,
    }
    if violations:
        raise ZeroCreditViolation("; ".join(violations))
    return result


class ZeroCreditViolation(RuntimeError):
    """A Wave-0 run named a non-local provider or an unexpected budget breach."""
