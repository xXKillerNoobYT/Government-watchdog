# Stage 1 · Slice 4 · Prereq-0 — Reviewer-internal read-API + concept-map registry additions

- **Issue:** GOV-98 (child of GOV-97; activates the GOV-96 A→E blueprint).
- **Stage / scope:** Stage 1 → Slice 4, Prereq-0. **Alpine-only, reviewer-internal/local, branch/PR-first.** No public exposure.
- **Owner:** BackendCrawlerEngineer. Review: CTO primary + VerificationSafetyReviewer; SecurityPrivacyAgent consult (transport/privacy).
- **Repo/runner:** `xXKillerNoobYT/Government-watchdog` / `IA-Mac-GOV-Backend`.
- **Builds on:** 1.05 publication SSOT (`scripts/publication.py`), 1.07 transcript/evidence/statement model (`scripts/statements.py`, migrations 0006/0007), GOV-94 owner conditions, GOV-97 plan Part A.1/A.2 + Part C.

This Prereq-0 is **design-independent** (no frontend dependency), so it is the first unblocked child. It delivers the backend the frontend A→E chain reads from, and it never invents a public surface: it is a local, reviewer-internal read layer over the already-gated record store.

---

## 1. What this slice adds (and what it deliberately does not)

| Added | Not added (out of scope / later) |
|---|---|
| In-repo concept-map registry SSOT (`scripts/concept_map.py`): the complete 1.07 `ALLOWED_NODE_TYPES`/`ALLOWED_EDGE_TYPES` vocabulary + the GOV-98 additions, with import-time drift guards. | Migrating the existing relational-FK spine edges (`contains_agenda_item`, `statement_from_segment`, …) into a generic edge table. They stay relational FKs; the registry only *names* them. |
| New node `agenda_thread` + table (migration 0012). | A full bill-style thread state machine (GOV-97 tradeoff (a): kept lightweight — slug + instances + typed edges). |
| New forward-linking edges `agenda_item_in_thread`, `agenda_item_supersedes`, `agenda_item_amends`, `agenda_item_revisits`, `topic_rollup`, stored in a generic append-only `concept_edges` table; `topic_groups` reused for thread-under-topic. | The frontend rendering of threads/trees (A→E, frontend repo). The category-**move** audit log (BEH-TOPICTREE-1) — a frontend-D product behavior; the edge table carries forward-compatible provenance columns but the move UX/audit ledger is not built here. |
| Minimal `topics` table (topic nodes are storage-required to serve a `topic_rollup` chain). | A populated Alpine topic taxonomy (data, not schema). |
| Reviewer-internal read-API (`scripts/read_api.py`): serves only reviewed/eligible records; server-side web-safe allowlist projection at the boundary; transport-level raw/absolute-path assertion; acyclicity validation before serving a tree. | Any HTTP server / network listener / public deployment / account/waitlist gate. The "API" is a local, read-only, stateless Python module + CLI that emits web-safe JSON. |

## 2. Eligibility gate (fail-closed, reused — never re-typed)

A statement is served to the render lane **only when both** independent gates agree (per `publication.py` docstring "both must agree before anything publishes"):

1. `publication.compute_ui_status(record)` ∈ `PUBLICATION_ELIGIBLE_UI_STATUSES` (`source-backed` / `archived-source-backed` / `corrected`), **and**
2. the DB `publication_state == 'publishable'`.

Default posture: **not returned.** `do_not_publish`, `disputed`, unreviewed (`machine_extracted_unreviewed` → `unverified`), `pending-review`, `source-missing`, `source-changed`, and anything `compute_ui_status` fails closed to are never served. The frontend **never recomputes** status — it consumes the backend `ui_status` label verbatim.

**No orphan claims (1.07 §2.3):** a statement is served only if it resolves to ≥1 evidence pointer (or a `statement_from_segment` segment edge). An orphan is dropped from the response, not served unlabeled.

**Labels travel (BEH-AGENDA-4):** every served record carries `verification_status` / `produced_by` / `correction_status` / `ui_status`. A reviewed-later instance never upgrades an earlier unreviewed one — each instance keeps its own labels.

## 3. Web-safe boundary + transport assertion (GOV-34)

Two independent layers:

1. **Field allowlist (fail-closed):** every record crosses the boundary through `publication.to_web_safe()`. Only allowlisted field names survive; raw store internals (`raw_local_path`, `raw_sha256`, `transcript_path`, `deep_link`, `local_note_path`), provenance (`owner_agent`, `created_by`), reviewer state (`review_state`, `notes`), and any unknown/future column are dropped by construction. The Slice-4 graph field names are added to the single SSOT allowlist in `publication.py` (imported, not re-typed).
2. **Transport-level assertion (`read_api.assert_no_raw_paths`):** an independent body-level guard that walks the response and rejects any string that is a filesystem/absolute path or carries a known raw marker (`/Users/`, `Obsidian Vault`, `Source-Data`, `.sha256`, `transcript_path`, …). Public **URLs** (`http(s)://…`) are allowed; vault/absolute paths are not. This catches a leak even if a future field were mis-allowlisted. The Slice-4 integration smoke and `tests/test_read_api.py` assert **zero** raw/absolute paths in the response body.

## 4. Concept-map registry additions (GOV-97 Part A.1/A.2)

### 4.1 New node
- `agenda_thread` — a durable civic subject recurring across meetings. `agenda_thread_id` (slug, e.g. `alpine:thread:fireworks-ban`), `title`, `jurisdiction_id` (Alpine-locked), `status` ∈ {`open`,`decided`,`dormant`}, `first_seen_date`, `last_seen_date`. `topic` is reused as-is (no new node).

### 4.2 New edges (forward-linking only)
| Edge type | Reads as | Endpoints |
|---|---|---|
| `agenda_item_in_thread` | per-meeting item is an instance of a recurring thread | `agenda_item` → `agenda_thread` |
| `agenda_item_supersedes` | later item supersedes an earlier item | `agenda_item` → `agenda_item` |
| `agenda_item_amends` | later item amends an earlier item | `agenda_item` → `agenda_item` |
| `agenda_item_revisits` | later item revisits an earlier item | `agenda_item` → `agenda_item` |
| `topic_rollup` | child topic rolls up to a parent topic (the tree) | `topic` → `topic` (child → parent) |

Thread-under-topic reuses `topic_groups` (`topic` → `agenda_thread`). `topic_groups` stays a flat grouping edge; the **tree** is carried solely by `topic_rollup` (GOV-36 separate-concepts rule). All additions are **additive** — they never rewrite known-then context or touch the fail-closed publication path.

### 4.3 Acyclicity (BEH-TOPICTREE-4)
A topic cannot roll up into its own descendant. Acyclicity is enforced **twice**: at insert time (`concept_map.insert_edge` rejects an edge that would close a cycle) and again at serve time (`read_api.topic_tree` validates before returning, raising `TopicTreeCycleError` rather than serving a broken tree). Self-loops are rejected outright.

## 5. Read-API surface (local, read-only)

`scripts/read_api.py` (all functions take an open `sqlite3.Connection`, return JSON-serializable web-safe dicts, and never write):

- `published_records(conn)` → list of served statements (eligibility-gated, orphan-dropped, labels attached, web-safe evidence drawer).
- `agenda_thread(conn, thread_id)` → thread node + its `agenda_item_in_thread` members in chronological (known-then) order + the typed lifecycle edges (`Supersedes`/`Amends`/`Revisits`) among members.
- `topic_tree(conn, root_topic_id)` → acyclicity-validated `topic_rollup` subtree + breadcrumb path.
- `build_response(conn, …)` → assembles the above into one response object, projects every record through `to_web_safe`, and runs `assert_no_raw_paths` on the serialized body before returning.
- CLI: `python3 scripts/read_api.py --db <path> [--thread <id>] [--topic-root <id>]` emits the sample web-safe JSON used as issue evidence.

## 6. Acceptance criteria → evidence map

| Acceptance criterion | Evidence |
|---|---|
| Read-API returns reviewed/eligible Alpine records only | `tests/test_read_api.py::test_only_eligible_served` + smoke |
| Response body free of raw/absolute paths (transport-level, in CI) | `read_api.assert_no_raw_paths`; `tests/test_read_api.py::test_transport_has_zero_raw_paths`; Slice-4 smoke step |
| New node/edge types validated + exposed; sample shows a thread with members + a `topic_rollup` chain | `scripts/slice4_prereq0_smoke.py` sample JSON; `tests/test_concept_map.py` |
| Acyclicity rejection test passes | `tests/test_concept_map.py::test_topic_rollup_cycle_rejected` (insert + serve) |
| Labels travel; no orphan served | `tests/test_read_api.py::test_labels_travel`, `::test_orphan_not_served` |

## 7. Pass-up trigger
Any need to relax the fail-closed publication path, the web-safe allowlist, or Alpine scope → stop, escalate to CTO/CEO/Isaac. Nothing in this slice relaxes any of them.
