# GOV-601 — Agenda-item / thread backend contract for website boards

*Backend/data contract supporting GOV-599 (agenda Kanban UX parent) and GOV-600
(website implementation). Reviewer-internal · Alpine-only. Public launch stays
gated on Isaac via GOV-420.*

**Owner:** BackendCrawlerEngineer · **Repo:** `xXKillerNoobYT/Government-watchdog`
(backend/core) · **Consumer:** `xXKillerNoobYT/Government-watchdog-website`.

This is a **data/presentation contract and gap analysis**, not an implementation
and not a public feed. It answers three questions for GOV-599's website agenda
board:

1. What minimum backend fields does the board need?
2. Which already exist in the current schema / read-API / surface, and which are gaps?
3. Which concrete backend changes should become follow-up issues — and when?

---

## §0 — Repo boundary & non-negotiables

- **Backend/core** owns crawlers, source registry, raw/archived preservation, the
  transcript/evidence/statement model, the reviewed card-feed / read-API, and the
  agenda-thread / source truth. It is the **source of truth**.
- **Website** consumes the reviewed card-feed / read-API and *presents* it. The
  website **must not invent civic claims** or become a source of truth.
- **No public deploy. No raw/unreviewed data publication.** Everything here is
  `access: reviewer_internal` / `scope: alpine`.
- **Additive only (I4).** Any board enrichment layers on top of existing modules;
  it never mutates or re-derives the seven prod modules
  `stage5_frontend_surface.py` consumes by reference. Cards remain presentation
  nodes over the graph, never a new claim (Isaac concept-map directive, GOV-36).

---

## §1 — Backend model already in place (evidence)

The concept-map backbone GOV-599 wants a board over **already exists**. The full
join path from a served claim up to meeting/agenda/thread is present:

```
statements.agenda_item_id            (0007_statements_evidence.sql:54)
  -> agenda_items.meeting_id         (0006_agenda_transcript_segments.sql:36)
       -> meetings                   (0001_init.sql:44-53)
  -> agenda_item_in_thread edge      (0012:70; concept_map.py:117)
       -> agenda_threads             (0012:28-37)
statements.segment_id                (0007:53) -> transcript_segments (timestamp_seconds, 0006:58)
statements.updates_statement_id      (0007:71)  correction lineage
evidence_links                       (source_id + originalUrl/archiveUrl/locator/timestamp)
```

Relevant tables / columns that exist today:

| Table | Columns relevant to the board | Migration |
|---|---|---|
| `meetings` | `id`, `meeting_date`, `body`, `title`, `source_url`, `transcript_id`, `notes` | 0001:44–53 |
| `agenda_items` | `agenda_item_id`, `meeting_id`, `item_order`, `title`, `agenda_doc_source_id` | 0006:34–41 |
| `agenda_threads` | `agenda_thread_id`, `title`, `jurisdiction_id`, `status ∈ {open,decided,dormant}`, `first_seen_date`, `last_seen_date` | 0012:28–37 |
| `agenda_threads` label layer | `canonical_human_label` + web-safe `sourceAliases` | 0013; read_api.py:735–753 |
| `transcripts` | `video_url` (YouTube), `title` | 0001:28; 0002 |
| `transcript_segments` / `evidence_links` | `timestamp_seconds`, `timestamp_human`, `page`, `section`, `char_start/end` | 0006:58; 0016:62; 0017 |
| typed agenda lifecycle edges | `agenda_item_supersedes`, `agenda_item_amends`, `agenda_item_revisits`, `agenda_item_in_thread` | 0012:70–73; concept_map.py:102–103 |

Read-API endpoints that already project this web-safely:

- `read_api.agenda_thread(conn, thread_id)` → `{thread, members[], lifecycle_edges[]}`
  with members ordered by `meeting_date, item_order` (known-then chronology) and
  **typed** lifecycle edges (never an untyped "related") — read_api.py:761–803.
- `read_api.reviewer_internal_records(conn)` → fail-closed reviewer-cleared served
  statements (`_serialize_statement`, read_api.py:426–460), each web-safe with
  `ui_status`, `confidence_label`, `speaker_label`, `provenance_status`, and an
  `evidence` drawer.
- `stage5_frontend_surface.build_surface(...)` → `watchdogBoard` (six frozen lanes)
  + `correctionsSurface` + `hotTopicsSurface`.

---

## §2 — Minimum website agenda-board contract (target shape)

The website board is an **agenda Kanban**: columns are lifecycle lanes; cards are
agenda items grouped under their meeting and threaded across meetings. The
minimum web-safe contract per card the website should consume:

```jsonc
{
  // --- meeting identity (meetings) ---
  "meetingId": 42,
  "meetingDate": "2026-03-11",
  "meetingBody": "Town Council",
  "meetingTitle": "Regular Meeting",
  "meetingSourceUrl": "https://...",           // web-safe original/source url only

  // --- agenda item (agenda_items) ---
  "agendaItemId": "ai-...",
  "itemOrder": 3,
  "agendaItemTitle": "CUP-2026-04 conditional use permit",

  // --- thread (agenda_threads) ---
  "agendaThreadId": "thr-...",
  "threadLabel": "canonical_human_label",       // plain-English primary label
  "threadStatus": "open",                        // open|decided|dormant (thread lifecycle)

  // --- board lane (watchdog signals) ---
  "lane": "pending-decision",                    // upcoming|active|pending-decision|decided|follow-up|correction
  "laneLabel": "Pending decision",
  "statusBadge": "…", "confidenceBadge": "…",    // fail-closed labels; never "Verified" unless verified

  // --- source & media deep-links ---
  "sourceRefs": [{ "sourceId": "...", "originalUrl": "...", "archiveUrl": "...",
                   "locator": { "timestampHuman": "...", "page": 3 } }],
  "videoRef": { "url": "https://youtu.be/...", "timestampSeconds": 512 },  // when available

  // --- decisions / actions (LATENT — see §3) ---
  "decisions": [],                               // empty + disclosed until vote/decision rows land

  // --- categories (LATENT — see §3) ---
  "categoryAnchor": { "kind": "agenda_thread", "disclosure": "…no explicit topic edge…" },

  // --- lineage / related-card refs (typed, never untyped) ---
  "lineage": [{ "relation": "agenda_item_supersedes", "ref": "ai-..." }],

  // --- gaps (never hidden) ---
  "gapBadges": ["…"]
}
```

**Rule:** every field is a web-safe projection of an existing column or a typed
edge. A field with no backing data is emitted **empty + disclosed**, never
omitted silently and never fabricated (the same honest-anchor discipline the
`hotTopicsSurface` already applies to `topic_id`, stage5_frontend_surface.py:202–290).

---

## §3 — Gap analysis: exists / latent / projection-gap

| GOV-599 field | Backend status | Evidence | Action |
|---|---|---|---|
| meeting id / date / body / title / source_url | **EXISTS** | meetings 0001:44–53 | project onto card |
| agenda item id / order / title | **EXISTS** | agenda_items 0006:34–41 | project onto card |
| thread id / label / status | **EXISTS** | agenda_threads 0012:28–37; read_api.py:761–803 | project onto card |
| lifecycle / status lane | **EXISTS (two sources)** | thread `status` (0012) + six watchdog lanes (stage5_frontend_surface.py §3, :320–338) | expose both; keep distinct |
| source links | **EXISTS** | evidence_links + `_safe_alias` sourceRef, read_api.py:693–732 | project onto card |
| YouTube / timestamp links | **EXISTS but not projected onto board card** | `transcripts.video_url` 0001:28; `timestamp_seconds` 0006:58/0016:62 | **PROJECTION GAP** — compose `videoRef` |
| decisions / actions | **LATENT** — node/edge types allowed (`vote`, `decision`, `voted_on`, `vote_decided`, `decision_affects`, concept_map.py:60–89) but **no landed table/rows** | concept_map.py:60–89 | emit `decisions: []` + disclose; issue only if Isaac wants decision extraction |
| categories | **LATENT** — `topic_id` structurally absent today; agenda_thread is the honest anchor (VSR GOV-521) | stage5_frontend_surface.py:202–290 | emit `categoryAnchor.kind = agenda_thread` disclosure |
| card lineage / related-card refs | **EXISTS (typed)** but not on board card | agenda lifecycle edges 0012:70–73; `updates_statement_id` 0007:71; read_api.py:786–802 | **PROJECTION GAP** — compose typed `lineage` |

**Summary:**
- **No new tables are required** for the frontend first pass. The meeting → agenda
  item → thread hierarchy, source links, video/timestamp anchors, and typed
  lineage all exist.
- The only *true code gaps* are **projection/plumbing**: an additive board
  projection that keys cards on agenda item + meeting + thread (not just
  `statementId`) and composes `videoRef` and typed `lineage`.
- **decisions/actions** and **topic categories** are **latent by data reality**,
  not by a missing contract. They must be surfaced as empty + disclosed, never
  faked. Landing real vote/decision rows or a topic layer is a *separate,
  Isaac-scoped* expansion — out of scope for GOV-599's first pass.

---

## §4 — Follow-up implementation issues (deferred, gated on GOV-600)

Per the GOV-601 task, follow-up **implementation** issues are created **only for
concrete backend changes needed after the frontend first pass**. The frontend
first pass is GOV-600 (website implementation), which has not run yet. Creating
impl issues now would build ahead of the consumer and risks specifying a shape
the frontend does not actually request.

**Trigger to create these issues:** GOV-600 reaches a first working pass and the
website reports which board fields it actually binds. **Owner of that trigger:**
FrontendTimelineEngineer / website lane reports back; CTO routes the backend
follow-ups to BackendCrawlerEngineer.

Candidate follow-up issues (specified now, created later):

1. **`agendaBoard` additive surface projection** — new additive function in
   `stage5_frontend_surface.py` (or a sibling module) that projects each served
   agenda item into the §2 card shape by joining meeting/agenda/thread columns
   that already exist. Additive, I4-safe, reviewer-internal, Alpine-only. Tests:
   determinism, no-leak (web-safe subset), lane completeness, empty-lane visible.
2. **`videoRef` + typed `lineage` composition** — compose `transcripts.video_url`
   + `timestamp_seconds` into a per-card deep-link, and project the typed agenda
   lifecycle edges as `lineage` (never untyped "related"). Depends on #1.
3. **(Isaac-scoped, NOT auto-created) decisions/actions data layer** — landing
   real `vote`/`decision` rows + `voted_on`/`vote_decided`/`decision_affects`
   edges. This is a civic-data expansion, escalate to Isaac/CTO before scoping.
   Until then the board discloses `decisions: []`.

Each follow-up must carry: stage, owner, repo/project, Alpine scope lock,
acceptance criteria, and verification evidence, and must preserve the no-public /
reviewer-internal / additive invariants above.

---

## §5 — Verification evidence for this contract

- Schema claims grounded in migrations `0001/0006/0007/0012/0013/0016/0017`
  (paths + line numbers cited in §1/§3).
- Read-API projection claims grounded in `scripts/read_api.py`
  (`agenda_thread` :761–803; `reviewer_internal_records` :509–561;
  `_serialize_statement` :426–460; `_safe_alias` :693–732).
- Surface claims grounded in `scripts/stage5_frontend_surface.py`
  (`watchdogBoard` card :305–338; honest-anchor disclosure :202–290).
- No code was changed; no data was published; scope stays Alpine / reviewer-internal.
