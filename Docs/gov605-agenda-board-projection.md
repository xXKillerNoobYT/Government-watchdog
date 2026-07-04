# GOV-605 — Agenda-board projection over reviewed Alpine data

*Implements the GOV-601 §4 follow-ups #1 + #2 (`agendaBoard` additive surface +
`videoRef`/typed-`lineage` composition). Closes the projection/plumbing gap the
GOV-601 contract named so GOV-599's shipped agenda-Kanban renders REAL reviewed
Alpine data. Reviewer-internal · Alpine-only · additive-only (I4). Public launch
stays Isaac-gated (GOV-420 — untouched).*

**Module:** `scripts/stage5_agenda_board.py` (new, leaf consumer).
**Entry point:** `stage5_agenda_board.agenda_board(conn) -> dict` (six-lane Kanban
of agenda-item cards). **Tests:** `tests/test_gov605_stage5_agenda_board.py`.

## Why a sibling module, not `read_api.agenda_board`

The GOV-601 contract §4 blessed "a sibling module". Two hard constraints forced it:

1. **No import cycle.** `read_api` is a leaf (imports none of the stage5 modules);
   `stage5_watchdog_signals` and `stage5_frontend_surface` import `read_api`. Putting
   the board in `read_api` would create `read_api → watchdog → read_api`. The board
   consumes lanes (watchdog) + display vocab (frontend surface), so it must sit
   *above* them.
2. **`read_api` is frozen.** The "extend not fork the SSOT" zero-diff guards
   (`test_*_zero_diff_vs_main`) require `read_api.py` / `publication.py` to be
   byte-identical to `main`. The board therefore consumes `read_api`'s public API
   unchanged and re-implements no gate.

## The single fail-closed gate (AC4)

Every statement a card is built from is a row `read_api.reviewer_internal_records`
returned — the one eight-clause clearance gate. The board never re-implements it. The
only raw touch is a `segment_id` column fetch for an *already-cleared* id (needed for
`videoRef` because `segment_id` is web-UNSAFE and absent from the web-safe record);
that is a lookup, not a gate. `test_board_shares_reviewer_internal_gate` proves the
board's statement set is a subset of the reviewer serve.

## §2 field-by-field mapping (contract target → this projection)

Card is keyed on **agenda_item** (one card per agenda item, aggregating its reviewed
statements — not one card per `statementId`).

| GOV-601 §2 field | Source | Notes |
|---|---|---|
| `meetingId` / `meetingDate` / `meetingBody` / `meetingTitle` | `agenda_items.meeting_id` → `meetings` | `_meeting_fields` |
| `meetingSourceUrl` | `meetings.source_url` | emitted only when a public `http(s)` URL (`_is_web_url`); else dropped |
| `agendaItemId` / `itemOrder` / `agendaItemTitle` | `agenda_items` | `_agenda_item_row` |
| `agendaThreadId` / `threadLabel` / `threadStatus` | `agenda_item_in_thread` edge → `agenda_threads` (`canonical_human_label`, `status`) | `_thread_for_item`; absent → `agenda_thread_unlinked` gap badge |
| `lane` / `laneLabel` | `stage5_watchdog_signals.build_watchdog_view` per statement, aggregated **most-specific-wins** | frozen LANE_ORDER; `surface.lane_label` |
| `statusBadge` | composed record status, aggregated **most-conservative-wins** | never `Verified` unless *every* statement is `verified` (AC4) |
| `confidenceBadge` | `confidence_label`; shared label, else conservative floor when mixed | `_card_confidence` |
| `sourceRefs[]` | web-safe evidence drawer → `{sourceId, originalUrl, archiveUrl, locator}` | `_source_refs`; deduped + sorted |
| **`videoRef`** | `statements.segment_id` → `transcript_segments.timestamp_seconds` + `transcripts.video_url` | **PROJECTION GAP #1**; earliest segment wins; public URL + int only; fail-closed to omitted + `video_ref_unavailable` gap |
| **`lineage[]`** | `agenda_item_supersedes`/`_amends`/`_revisits` edges + `updates_statement_id` | **PROJECTION GAP #2**; typed `{relation, ref}`, never untyped "related" |
| `decisions` | — | **LATENT**: always `[]` + disclosed (never fabricated; Isaac-scoped, AC3) |
| `categoryAnchor` | — | **LATENT**: `{kind: agenda_thread, disclosure}` (no topic edge in data, AC3) |
| `gapBadges[]` | watchdog gaps + board gaps | rendered visible; unknown codes pass through verbatim (never hidden) |

Additive traceability fields (not in §2, honest extras): `cardId`, `statementIds[]`,
`recordCount`; board-level `unanchoredStatementCount` (cleared statements with no
agenda item — disclosed, not dropped) and `disclosures{}`.

## Empty-state (AC5)

No reviewed Alpine agenda records → a well-formed board: all six lanes present and
empty, `cardCount: 0`, `disclosures.emptyState: true`. Never an error.

## Web-safe boundary (AC4)

Every leaf value is a web-safe projection or a derived public locator; the whole board
is swept by `read_api.assert_no_raw_paths` before return. `segment_id` never crosses
even though `videoRef` is derived from it (`test_no_raw_path_leak`).

## Out of scope (GOV-601 §3, Isaac-scoped)

No vote/decision rows and no topic layer are landed here — those are civic-data
expansions gated on Isaac. The board discloses their absence rather than faking them.
