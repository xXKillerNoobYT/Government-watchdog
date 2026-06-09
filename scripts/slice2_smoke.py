"""Second-slice end-to-end integration smoke (GOV-84, Stage 1 Slice 2 Issue E).

Integration of Contract 1.07 over the Slice-1 registry. Source: GOV-71 §3 +
GOV-79 Part D. This is the single end-to-end smoke that proves the *whole*
transcript -> evidence pipeline holds together when every Slice-2 module runs in
sequence — not just in isolation. It performs a real:

    apply migrations  ->  reuse the Slice-1-seeded Alpine source registry  ->
    load one real (sanitized) Alpine fixture (the 2026-05-08 WWTP-financing
    meeting) as a meeting + transcript  ->  deterministically segment it  ->
    create deterministic statements + evidence_links with exact-source pointers
    ->  run the speaker-attribution safety gate

and then asserts the contract invariants this slice exists to guarantee:

  1. NO ORPHAN CLAIMS (1.07 §2.3) — every statement in the DB resolves to a
     `statement_from_segment` edge OR a complete evidence_link pointer, AND an
     attempted orphan insert is rejected by `statements.insert_statement`.
  2. DEFAULT NOT-PUBLISHABLE (1.05 / 1.07 §5) — every statement defaults
     `publication_state='not_publishable'` with a computed `ui_status` that is
     NOT on the fail-closed publication allowlist.
  3. EVERY EVIDENCE_LINK POINTER IS VALID (1.07 §2.2/§2.3) — re-validating every
     persisted evidence_link pointer (required fields, locator matching
     locator_kind, resolving `to_source_id`) passes; an invalid pointer is
     rejected at insert time.
  4. SPEAKER ATTRIBUTION IS SAFE (1.07 §3) — a low-confidence `attributed`
     request fails closed to a name-free label binding no `person_id` and
     creating no `made_statement` edge ("no name is better than wrong
     attribution"); naming an `on-record-public` speaker is a hard stop; and a
     fully-justified `on-record-official` CAN be named (so the gate is not
     trivially passing by never naming anyone — proving no wrong-name without
     also proving "no name ever").

Design (why this is a deterministic tool, not just a test):
  * `run_smoke()` does all the work in a **caller-provided sandbox dir** — a
    throwaway DB under `<sandbox>/Database/`. It NEVER touches the real
    `Database/gov_watchdog.db`, so the smoke is read-only w.r.t. real data by
    construction (no `--apply` gate to trip).
  * It returns a structured result so `tests/test_slice2_integration_smoke.py`
    can assert each invariant granularly, while the CLI prints loud PASS/FAIL
    evidence lines and exits non-zero on any failure (so CI fails LOUDLY if a
    contract invariant regresses — the Issue E success criterion).

Data boundary (WORKFLOW_GOVERNANCE.md / 1.07 §7): only the sanitized fixture
under `tests/fixtures/alpine/` is read; `transcript_path` is a synthetic
vault-only provenance string, never written to disk; nothing is published and no
real raw/DB is committed.

Usage:
    python scripts/slice2_smoke.py [--fixture PATH] [--keep] [--workdir DIR]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db  # noqa: E402
import publication as pub  # noqa: E402
import segment_transcript as seg  # noqa: E402
import source_inventory as si  # noqa: E402
import speakers as spk  # noqa: E402
import statements as stmt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "alpine" / "alpine-sample-transcript.json"
)

# The fixture is the 2026-05-08 regular meeting whose item 7 is the WWTP
# financing update (the meeting named in the issue). Its YouTube watch URL
# reconciles to the Slice-1-seeded `alpine_youtube_channel` video source, so the
# whole chain hangs off a registry source we did NOT hand-wire.
_MEETING_DATE = "2026-05-08"
_MEETING_BODY = "Alpine Town Council"
_MEETING_TITLE = "Regular Meeting"
_VIDEO_SOURCE_ID = "alpine_youtube_channel"  # Slice-1 seed (source_inventory.py)
_AGENDA_SOURCE_ID = "alpinewy_gov"  # Slice-1 seed: the agenda packet site
_AGENDA_ITEM_ID = "alpine:2026-05-08:item-7"
_TRANSCRIPT_VAULT_PATH = "Transcripts/2026/alpine-2026-05-08-regular.json"  # synthetic, never written

# The segment carrying the substantive WWTP financing-gap claim (01:33).
_FINANCING_SEGMENT_INDEX = 3
# The segment carrying the procedural "continued to next meeting" outcome (05:09).
_CONTINUED_SEGMENT_INDEX = 6


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SmokeFailure(AssertionError):
    """Raised by run_smoke(strict=True) when any contract invariant regresses."""


# --- pipeline steps --------------------------------------------------------


def _load_meeting_and_transcript(conn, fixture: dict) -> tuple[int, int]:
    """Insert the meeting + transcript from the sanitized fixture.

    Mirrors what scripts/fetch_transcripts.py preserves at fetch time
    (timestamped_text + provenance). Inserts the transcript with source_id NULL
    so the registry reconciliation (si.load) must back-fill it via URL host —
    exercising the Slice-1 provenance path, not hand-wiring the link.
    Returns (meeting_id, transcript_id).
    """
    meta, tr = fixture["meta"], fixture["transcript"]
    cur = conn.execute(
        "INSERT INTO transcripts (video_id, video_url, channel_id, channel_title, "
        "upload_date, meeting_date, duration_seconds, language, segment_count, "
        "full_text, timestamped_text, local_path, sha256, fetch_time_utc, source_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            meta["video_id"], meta["video_url"], meta.get("channel_id"),
            meta.get("channel_title"), meta.get("upload_date"), _MEETING_DATE,
            meta.get("duration_seconds"), tr["language"], tr["segment_count"],
            tr["full_text"], tr["timestamped_text"],
            _TRANSCRIPT_VAULT_PATH, "0" * 64, _now(), None,  # source_id NULL on purpose
        ),
    )
    transcript_id = int(cur.lastrowid)
    cur = conn.execute(
        "INSERT INTO meetings (meeting_date, body, title, transcript_id, fetch_time_utc) "
        "VALUES (?, ?, ?, ?, ?)",
        (_MEETING_DATE, _MEETING_BODY, _MEETING_TITLE, transcript_id, _now()),
    )
    meeting_id = int(cur.lastrowid)
    conn.commit()
    return meeting_id, transcript_id


def _create_agenda_item(conn, meeting_id: int) -> str:
    """The §1 spine: meeting -> agenda_item (item 7, WWTP financing)."""
    conn.execute(
        "INSERT OR IGNORE INTO agenda_items "
        "(agenda_item_id, meeting_id, item_order, title, agenda_doc_source_id, created_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            _AGENDA_ITEM_ID, meeting_id, 7,
            "Wastewater treatment plant financing update",
            _AGENDA_SOURCE_ID, _now(),
        ),
    )
    conn.commit()
    return _AGENDA_ITEM_ID


def _segment_for(records: list[dict], index: int) -> dict:
    for rec in records:
        if rec["segment_index"] == index:
            return rec
    raise SmokeFailure(f"fixture produced no segment at index {index}")


def _timestamp_pointer(segment: dict, video_url: str) -> dict:
    """A complete §2 exact-source pointer into the meeting video at a timestamp."""
    return {
        "to_source_id": _VIDEO_SOURCE_ID,
        "relation": "substantiates",
        "locator_kind": "timestamp",
        "timestamp_seconds": segment["timestamp_seconds"],
        "timestamp_human": segment["timestamp_human"],
        "original_url": video_url,
        "archive_status": "not_checked",
        "scan_date": _MEETING_DATE,
        "captured_at_utc": _now(),
        "verification_status": "machine_extracted_unreviewed",
        "confidence": "medium",
        "transcript_path": _TRANSCRIPT_VAULT_PATH,  # vault-only provenance
    }


def _create_statements(conn, segments: list[dict], agenda_item_id: str,
                       video_url: str) -> list[str]:
    """Create deterministic statements + evidence_links with exact pointers.

    Two statements, both segment-anchored (the `statement_from_segment` edge) AND
    each carrying a complete timestamp pointer evidence_link — so each is
    non-orphan via BOTH disjuncts of the 1.07 §2.3 rule. No paraphrase, no AI:
    statement_text is the verbatim segment slice.
    """
    created: list[str] = []
    plan = [
        ("alpine:2026-05-08:stmt-financing-gap", _FINANCING_SEGMENT_INDEX, "references"),
        ("alpine:2026-05-08:stmt-continued", _CONTINUED_SEGMENT_INDEX, "substantiates"),
    ]
    for statement_id, seg_index, relation in plan:
        segment = _segment_for(segments, seg_index)
        pointer = _timestamp_pointer(segment, video_url)
        pointer["relation"] = relation
        statement = {
            "statement_id": statement_id,
            "segment_id": segment["segment_id"],
            "agenda_item_id": agenda_item_id,
            "statement_text": segment["segment_text"],  # verbatim, never paraphrased
            "is_verbatim": 1,
            "produced_by": "automation",
        }
        stmt.insert_statement(conn, statement, [pointer])
        created.append(statement_id)
    return created


# --- invariant checks ------------------------------------------------------


def _check_no_orphan_claims(conn) -> dict:
    """Invariant 1: every statement is non-orphan AND an orphan insert is rejected."""
    detail: dict = {"name": "no_orphan_claims", "passed": False}
    rows = conn.execute(
        "SELECT statement_id, segment_id FROM statements"
    ).fetchall()
    if not rows:
        detail["error"] = "no statements created — nothing to assert"
        return detail
    orphans: list[str] = []
    for row in rows:
        links = conn.execute(
            "SELECT to_source_id, relation, original_url, archive_status, scan_date, "
            "captured_at_utc, locator_kind, timestamp_seconds, timestamp_human, page, "
            "section, paragraph, verification_status, confidence, layer "
            "FROM evidence_links WHERE from_node_id = ? AND from_node_type = 'statement'",
            (row["statement_id"],),
        ).fetchall()
        statement = {"segment_id": row["segment_id"]}
        if stmt.is_orphan(statement, [dict(l) for l in links], conn=conn):
            orphans.append(row["statement_id"])
    detail["statements_checked"] = len(rows)

    # Negative path: an orphan (no segment edge, no pointer) must be rejected.
    orphan_rejected = False
    try:
        stmt.insert_statement(
            conn,
            {"statement_id": "alpine:smoke:orphan-probe", "statement_text": "orphan probe"},
            [],
            commit=False,
        )
    except stmt.OrphanClaimError:
        orphan_rejected = True
    finally:
        conn.rollback()
    detail["orphan_insert_rejected"] = orphan_rejected

    if orphans:
        detail["error"] = f"orphan statement(s) present: {orphans}"
    elif not orphan_rejected:
        detail["error"] = "an orphan statement insert was NOT rejected"
    else:
        detail["passed"] = True
    return detail


def _check_default_not_publishable(conn) -> dict:
    """Invariant 2: every statement defaults not-publishable + gated ui_status (1.05)."""
    rows = conn.execute(
        "SELECT statement_id, verification_status, correction_status, source_changed, "
        "publication_state, ui_status FROM statements"
    ).fetchall()
    detail: dict = {"name": "default_not_publishable", "passed": False, "rows": len(rows)}
    if not rows:
        detail["error"] = "no statements created — nothing to assert"
        return detail
    offenders: list[dict] = []
    for row in rows:
        record = {
            "verificationStatus": row["verification_status"],
            "correctionStatus": row["correction_status"],
            "sourceChanged": bool(row["source_changed"]),
            "sourcePresent": True,
            "archivePresent": False,
            "rawPreserved": False,
        }
        eligible = pub.is_publication_eligible(record)
        if (
            row["publication_state"] != pub.DEFAULT_PUBLICATION_STATE
            or row["ui_status"] is None
            or eligible
        ):
            offenders.append({
                "statement_id": row["statement_id"],
                "publication_state": row["publication_state"],
                "ui_status": row["ui_status"],
                "publication_eligible": eligible,
            })
    if offenders:
        detail["error"] = f"{len(offenders)} statement(s) not default-not-publishable"
        detail["offenders"] = offenders
    else:
        detail["passed"] = True
    return detail


def _check_pointers_valid(conn) -> dict:
    """Invariant 3: every persisted evidence_link pointer re-validates (1.07 §2.2)."""
    rows = conn.execute(
        "SELECT evidence_link_id, to_source_id, relation, original_url, archive_status, "
        "scan_date, captured_at_utc, locator_kind, timestamp_seconds, timestamp_human, "
        "page, section, paragraph, verification_status, confidence, layer "
        "FROM evidence_links"
    ).fetchall()
    detail: dict = {"name": "evidence_pointers_valid", "passed": False, "rows": len(rows)}
    if not rows:
        detail["error"] = "no evidence_links created — nothing to assert"
        return detail
    invalid: list[dict] = []
    for row in rows:
        pointer = dict(row)
        link_id = pointer.pop("evidence_link_id")
        try:
            stmt.validate_pointer(pointer, conn=conn)
        except stmt.PointerError as exc:
            invalid.append({"evidence_link_id": link_id, "error": str(exc)})

    # Negative path: a pointer whose to_source_id does not resolve is rejected.
    bad_rejected = False
    try:
        stmt.validate_pointer(
            {
                "to_source_id": "does_not_exist", "relation": "references",
                "original_url": "https://example.invalid", "archive_status": "not_checked",
                "scan_date": _MEETING_DATE, "captured_at_utc": _now(),
                "locator_kind": "timestamp", "timestamp_seconds": 0,
                "timestamp_human": "00:00:00",
                "verification_status": "machine_extracted_unreviewed", "confidence": "low",
            },
            conn=conn,
        )
    except stmt.PointerError:
        bad_rejected = True
    detail["unresolved_pointer_rejected"] = bad_rejected

    if invalid:
        detail["error"] = f"{len(invalid)} evidence_link pointer(s) failed re-validation"
        detail["invalid"] = invalid
    elif not bad_rejected:
        detail["error"] = "a pointer with an unresolved to_source_id was NOT rejected"
    else:
        detail["passed"] = True
    return detail


def _check_speaker_attribution_safe(conn, statement_ids: list[str]) -> dict:
    """Invariant 4: the §3 speaker-attribution safety gate holds end-to-end.

    Three sub-assertions over a real candidate name "Pat Maxwell":
      a. low-confidence `attributed` official -> downgraded (not 'attributed'),
         name-free label, no bound person_id, no made_statement edge;
      b. naming an `on-record-public` speaker raises a hard stop (never auto-named);
      c. a fully-justified high-confidence confirmed `on-record-official` IS
         named (proving the gate is not passing by never naming anyone).
    """
    detail: dict = {"name": "speaker_attribution_safe", "passed": False}
    candidate_name = "Pat Maxwell"
    person_id = "alpine:person:pat-maxwell"
    conn.execute(
        "INSERT OR IGNORE INTO persons (person_id, display_name, person_type, created_utc) "
        "VALUES (?, ?, 'official', ?)",
        (person_id, candidate_name, _now()),
    )
    conn.commit()

    findings: dict = {}

    # (a) weak attribution must fail closed to a name-free label.
    weak_stmt = statement_ids[0]
    weak = spk.attribute_speaker(
        conn,
        {
            "speaker_attribution_id": "alpine:attr:weak",
            "statement_id": weak_stmt,
            "attribution_state": "attributed",
            "speaker_class": "on-record-official",
            "confidence": "low",              # <-- gate fails closed
            "person_id": person_id,
            "person_confirmed": True,
            "role_title": "Mayor",
        },
    )
    made_for_weak = conn.execute(
        "SELECT COUNT(*) FROM made_statement WHERE statement_id = ?", (weak_stmt,)
    ).fetchone()[0]
    weak_safe = (
        weak["attribution_state"] != "attributed"
        and weak["person_id"] is None
        and candidate_name not in (weak["speaker_label"] or "")
        and made_for_weak == 0
    )
    findings["weak_downgraded_name_free"] = {
        "state": weak["attribution_state"], "label": weak["speaker_label"],
        "person_id": weak["person_id"], "made_statement_rows": made_for_weak,
        "ok": weak_safe,
    }

    # (b) naming an on-record-public speaker is a hard stop.
    hard_stop = False
    try:
        spk.attribute_speaker(
            conn,
            {
                "speaker_attribution_id": "alpine:attr:public",
                "statement_id": statement_ids[1],
                "attribution_state": "attributed",
                "speaker_class": "on-record-public",
                "confidence": "high",
                "person_id": person_id,
                "person_confirmed": True,
            },
        )
    except spk.SpeakerAttributionHardStop:
        hard_stop = True
    findings["public_naming_hard_stop"] = {"raised": hard_stop, "ok": hard_stop}

    # (c) a fully-justified official CAN be named (gate is not trivially failing).
    strong = spk.attribute_speaker(
        conn,
        {
            "speaker_attribution_id": "alpine:attr:strong",
            "statement_id": statement_ids[1],
            "attribution_state": "attributed",
            "speaker_class": "on-record-official",
            "confidence": "high",
            "person_id": person_id,
            "person_confirmed": True,
            "role_title": "Mayor",
        },
    )
    made_for_strong = conn.execute(
        "SELECT COUNT(*) FROM made_statement WHERE statement_id = ? AND person_id = ?",
        (statement_ids[1], person_id),
    ).fetchone()[0]
    strong_named = (
        strong["attribution_state"] == "attributed"
        and strong["person_id"] == person_id
        and candidate_name in (strong["speaker_label"] or "")
        and made_for_strong == 1
    )
    findings["justified_official_named"] = {
        "state": strong["attribution_state"], "label": strong["speaker_label"],
        "made_statement_rows": made_for_strong, "ok": strong_named,
    }

    detail["findings"] = findings
    failed = [k for k, v in findings.items() if not v["ok"]]
    if failed:
        detail["error"] = f"speaker-safety sub-check(s) failed: {failed}"
    else:
        detail["passed"] = True
    return detail


def run_smoke(
    fixture: Path = DEFAULT_FIXTURE,
    sandbox: Path | None = None,
    *,
    strict: bool = False,
) -> dict:
    """Run the full second-slice integration smoke in a sandbox.

    `sandbox` is the throwaway root for the DB (`<sandbox>/Database/...`). If
    None, a TemporaryDirectory is created and removed before returning. Returns a
    structured result:

        {ok, meeting_id, transcript_id, segment_count, statement_ids, db_path,
         checks: [<invariant detail>, ...]}

    With `strict=True`, raises `SmokeFailure` if any invariant fails (used by the
    CLI for a loud non-zero exit).
    """
    fixture = Path(fixture)
    if not fixture.exists():
        raise FileNotFoundError(f"fixture not found: {fixture}")
    fixture_data = json.loads(fixture.read_text(encoding="utf-8"))

    tmp_holder: tempfile.TemporaryDirectory | None = None
    if sandbox is None:
        tmp_holder = tempfile.TemporaryDirectory(prefix="gov84-slice2-smoke-")
        sandbox = Path(tmp_holder.name)
    sandbox = Path(sandbox)
    db_path = sandbox / "Database" / "slice2_smoke.db"

    try:
        # 1. migrate + seed the Slice-1 Alpine source registry.
        si.load(db_path)
        with db.open_db(db_path) as conn:
            # 2. load the real (sanitized) Alpine fixture as meeting + transcript.
            meeting_id, transcript_id = _load_meeting_and_transcript(conn, fixture_data)
        # 3. reconcile so the transcript back-fills its source_id from the registry.
        si.load(db_path)
        with db.open_db(db_path) as conn:
            video_url = conn.execute(
                "SELECT video_url, source_id FROM transcripts WHERE id = ?",
                (transcript_id,),
            ).fetchone()
            agenda_item_id = _create_agenda_item(conn, meeting_id)
            # 4. deterministically segment the preserved transcript.
            segments = seg.segment_transcript(conn, transcript_id)
            # 5. create deterministic statements + evidence_links (exact pointers).
            statement_ids = _create_statements(
                conn, segments, agenda_item_id, video_url["video_url"]
            )
            # 6. assert the contract invariants.
            checks = [
                _check_no_orphan_claims(conn),
                _check_default_not_publishable(conn),
                _check_pointers_valid(conn),
                _check_speaker_attribution_safe(conn, statement_ids),
            ]
        result = {
            "ok": all(c["passed"] for c in checks),
            "meeting_id": meeting_id,
            "transcript_id": transcript_id,
            "transcript_source_id": video_url["source_id"],
            "segment_count": len(segments),
            "statement_ids": statement_ids,
            "db_path": str(db_path),
            "checks": checks,
        }
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()

    if strict and not result["ok"]:
        failed = [c["name"] for c in result["checks"] if not c["passed"]]
        raise SmokeFailure(f"slice 2 integration smoke FAILED: {failed}")
    return result


def _print_report(result: dict) -> None:
    print("=== GOV-84 second-slice integration smoke (Contract 1.07 end-to-end) ===")
    print(
        f"sandbox db: {result['db_path']}  meeting={result['meeting_id']} "
        f"transcript={result['transcript_id']} -> source={result['transcript_source_id']}"
    )
    print(
        f"segments={result['segment_count']} statements={result['statement_ids']}"
    )
    for check in result["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        print(f"[{status}] {check['name']}")
        if check["name"] == "speaker_attribution_safe" and check.get("findings"):
            for sub, val in check["findings"].items():
                print(f"         {sub}: {val}")
        if not check["passed"]:
            print(f"         -> {check.get('error', 'invariant failed')}")
            for key in ("offenders", "invalid"):
                for item in check.get(key, []):
                    print(f"         {key[:-1]}: {item}")
    print("=== RESULT:", "OK" if result["ok"] else "FAILED", "===")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE,
                        help="sanitized Alpine transcript fixture to ingest")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="sandbox dir (default: a removed TemporaryDirectory)")
    parser.add_argument("--keep", action="store_true",
                        help="with --workdir, keep the sandbox after the run")
    args = parser.parse_args(argv)

    sandbox = args.workdir
    if sandbox is not None:
        sandbox.mkdir(parents=True, exist_ok=True)
    try:
        result = run_smoke(args.fixture, sandbox)
    except FileNotFoundError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    _print_report(result)
    if sandbox is not None and not args.keep:
        shutil.rmtree(sandbox, ignore_errors=True)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
