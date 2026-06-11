"""Vault-only Stage-1 seed of the reviewer-identity registry (GOV-135).

Executes the GOV-132 owner decision (accepted by Isaac 2026-06-11): seed
EXACTLY ONE Stage-1 reviewer — Isaac (owner) — into the fail-closed
reviewer-identity registry built in GOV-131 (migration 0014). It uses the
vault-only ``ai_risk_gate.register_reviewer()`` admin helper; it adds no schema,
widens no web-safe allowlist, and writes no other table.

OPERATIONAL / VAULT-ONLY (ADR §5; 1.11 §2.1; AI_GATEWAY §7.1)
------------------------------------------------------------
This is a local seeding helper, NOT a web/API surface. It writes only to the
local vault DB (``db.DEFAULT_DB_PATH`` unless ``--db`` overrides). The seeded
row is reviewer/vault-only evidence: ``reviewer_identities`` is on no
``publication.WEB_SAFE_FIELD_ALLOWLIST`` entry and is never returned by
``read_api`` / ``to_web_safe`` (guarded by GOV-131's boundary tests), so seeding
it exposes nothing on any public surface.

GUARANTEE — exactly one
-----------------------
After seeding, the helper asserts the registry holds exactly ONE active
identity (``reviewer:isaac``) and refuses — non-zero exit, nothing committed —
if any OTHER active identity is present. The fail-closed default is preserved:
every other id resolves to ``is_registered_reviewer() == False``. Re-running is
idempotent (``register_reviewer`` upserts), so a second seed converges to the
same single row rather than duplicating.

Authorization (this seed only)
------------------------------
Isaac's acceptance of GOV-132 authorizes seeding Isaac himself; no further
sign-off is needed for THIS seed. Add/revoke of any OTHER identity is out of
scope here and needs its own authorization:

* add another reviewer: CEO request -> Isaac written by-name sign-off ->
  ``register_reviewer()``;
* revoke: immediate on any owner/CEO/CTO flag -> ``revoke_reviewer()``
  (single sign-off, recorded reason, ``status='revoked'`` — fail toward less
  access).

Usage::

    python3 scripts/seed_reviewer_registry.py            # dry-run (no write)
    python3 scripts/seed_reviewer_registry.py --apply    # commit the seed
    python3 scripts/seed_reviewer_registry.py --apply --db /path/to/vault.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_risk_gate as rg  # noqa: E402
import db  # noqa: E402

# ---------------------------------------------------------------------------
# The single authorized Stage-1 reviewer (GOV-132 owner decision).
# ---------------------------------------------------------------------------
# A STABLE INTERNAL id — opaque, vault-only, not PII. `display_name` is the only
# human-facing label and is a vault-only column (never web-projected). We keep it
# deliberately generic ("Isaac (owner)") so even the vault row carries no contact
# detail — just the role binding the owner decision authorized.
REVIEWER_ID = "reviewer:isaac"
DISPLAY_NAME = "Isaac (owner)"
REGISTERED_BY = "GOV-132 owner decision (Isaac, accepted 2026-06-11)"
NOTE = "Stage-1 sole reviewer; seeded per GOV-135."


class SeedError(RuntimeError):
    """Refuse to seed — the resulting registry would violate exactly-one."""


# Sampled non-Isaac ids used to prove the fail-closed default is preserved.
_SAMPLE_OTHERS = ("reviewer:unknown", "ai", "automation", "", None)


def seed_isaac(conn: sqlite3.Connection, *, apply: bool) -> dict[str, object]:
    """Seed (or re-activate) the single Stage-1 reviewer, fail-closed.

    Runs inside one transaction: registers ``reviewer:isaac`` (``commit=False``),
    then asserts the registry holds exactly one active identity and it is Isaac.
    Any other active identity -> :class:`SeedError` and a rollback (nothing is
    written). On success: commits iff ``apply`` is true, else rolls back (a
    dry-run leaves the DB untouched). Returns a redacted summary.
    """
    # Upsert Isaac without committing — we validate the invariant first.
    rg.register_reviewer(
        conn,
        REVIEWER_ID,
        display_name=DISPLAY_NAME,
        registered_by=REGISTERED_BY,
        note=NOTE,
        commit=False,
    )

    active = [
        row[0]
        for row in conn.execute(
            f"SELECT reviewer_id FROM {rg.REVIEWER_REGISTRY_TABLE} "
            "WHERE status = 'active' ORDER BY reviewer_id"
        )
    ]
    extra = [rid for rid in active if rid != REVIEWER_ID]
    if extra:
        conn.rollback()
        raise SeedError(
            "registry would hold more than the single authorized Stage-1 reviewer; "
            f"unexpected active identities present: {extra}. Refusing to seed "
            "(GOV-135 authorizes exactly one: reviewer:isaac)."
        )

    # Capture the validated state INSIDE the transaction (before commit/rollback),
    # so a dry-run still reports the intended result; `applied` says if persisted.
    summary = {
        "reviewer_id": REVIEWER_ID,
        "applied": apply,
        "active_reviewer_count": len(active),
        "isaac_registered": rg.is_registered_reviewer(conn, REVIEWER_ID),
        "others_all_rejected": all(
            rg.is_registered_reviewer(conn, rid) is False for rid in _SAMPLE_OTHERS
        ),
    }

    if apply:
        conn.commit()
    else:
        conn.rollback()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=db.DEFAULT_DB_PATH,
        help="vault DB path (default: %(default)s)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="commit the seed (default: dry-run, no write)",
    )
    args = parser.parse_args(argv)

    db.apply_migrations(args.db)  # ensure migration 0014 is present, idempotent.
    with db.open_db(args.db) as conn:
        summary = seed_isaac(conn, apply=args.apply)

    mode = "APPLIED" if args.apply else "DRY-RUN (validated, not written)"
    # Vault-only output: reviewer_id is an opaque internal id (safe to echo);
    # display_name is NOT printed. Counts reflect the validated seed state.
    print(f"[seed_reviewer_registry] {mode}")
    print(f"  reviewer_id            : {summary['reviewer_id']}  (display_name vault-only, not shown)")
    print(f"  active_reviewer_count  : {summary['active_reviewer_count']}")
    print(f"  isaac_registered       : {summary['isaac_registered']}")
    print(f"  all_other_ids_rejected : {summary['others_all_rejected']}")
    print(f"  db                     : {args.db}")

    ok = (
        summary["active_reviewer_count"] == 1
        and summary["isaac_registered"] is True
        and summary["others_all_rejected"] is True
    )
    if not ok:
        print("  RESULT                 : FAIL (invariant not satisfied)", file=sys.stderr)
        return 1
    print("  RESULT                 : OK (exactly one active reviewer, fail-closed preserved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
