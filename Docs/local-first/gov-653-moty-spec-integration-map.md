# GOV-653 — MOTY Government Watchdog Spec: Integration Map (2026-07-07)

**Status:** Non-gated planning artifact. Nothing in this document authorizes implementation,
publication, AI lane-2 usage, or expansion beyond Alpine. All standing owner decisions on
record (GOV-545 Option A hold, GOV-612 ingest ladder, GOV-639 parser Option A, GOV-646
reviewer-promotion Option A) remain governing.

**Source:** Isaac's "MOTY GOVERNMENT WATCHDOG APP: DESIGN DETAILS, REQUIREMENTS, AND AI-RUN
BUSINESS PLAN" spec, filed as issue GOV-653 (`9cfcc858-7dfd-4a27-bc0d-a36d3063cf5c`) against
HEAD goal `5e8b8006`, targeting the Backend project (`0a1832c4`) and Website project
(`78066972`).

**What this document does:** maps every MOTY spec section onto the existing GOV goal ledger
and stage gates so future staged issues cite the right home, and records conflicts/decisions
that must NOT be silently resolved by implementation.

---

## 1. Stage-number translation (spec MVP stages ≠ GOV ledger stages)

The MOTY spec uses its own 6-stage MVP ladder. The GOV company ledger uses Stages 0–12.
Do not renumber either; translate:

| MOTY spec stage | Content | GOV ledger home | Current gate state |
|---|---|---|---|
| Stage 1 — Public prototype (Reader Mode, town boards, basic Kanban/timeline, newsletter template, semi-automated JSON, source links) | Frontend presentation | **Stage 6** (`c10c406c`, Alpine private beta) + Website track (`fe3fc35a`) | `planned`, Isaac-gated; NOT a public launch — Stage 6 is limited-access beta only |
| Stage 2 — Windows backend automation (crawler, archive, hashing, extraction, change detection, AI summaries, export builder, publisher) | Backend pipeline | Backend track (`ce908143`) + Stages 7/10 backend contracts | Deterministic lanes 1–5 already built and run (34,696-statement registry); AI summaries = lane-2, **not authorized** |
| Stage 3 — Trust layer (source vault, generation logs, correction button, confidence scores, diff viewer, prompt version display, manifests) | Cross-cutting trust | Transcript/evidence track (`b1d69179`) + Human-verification track (`2d8c4151`); partially exists (signed manifests GOV-133, VSR gates, provenance columns) | Backend proof layer partly real; public-facing surface Isaac-gated |
| Stage 4 — Watchlists and alerts | Product feature | Stage 7 (`fbd4665c`, current-date operations) and later | `planned` |
| Stage 5 — Business automation (support bot, SEO, onboarding, revenue) | Business ops | Stage 9+ (`75434c93` public launch gate and beyond) | `planned`, owner-gated |
| Stage 6 — Proper server migration | Infrastructure | Future stage decision; interacts with the Windows-box question (§6) | Owner decision, not scheduled |

## 2. Website project mapping (`78066972` / `fe3fc35a` Website track)

- **Three modes:** Reader Mode (newspaper-style, ≥18px body, high contrast, print/email
  friendly, single-column mobile), Dashboard Mode (timeline, Kanban, search/filters, source
  vault, diff viewer, watchlists), Admin Mode (private ops monitoring). Admin Mode remains
  private-substrate only; Reader/Dashboard are Stage 6 beta surfaces first.
- **Kanban columns (11):** Captured → Agenda Posted → Packet Available → Public Comment Open
  → Meeting Scheduled → Discussed → Voted → Adopted/Failed → Implementation → Follow-Up
  Needed → Archived. This extends the existing `agenda_board` projection; the current
  frozen 8-clause reviewer-internal read gate stays untouched until an owner card says
  otherwise.
- **Issue-card field contract:** plain-English title, official title, jurisdiction,
  board/department, meeting date, stage, topic tags, impact score, confidence score,
  first-seen/last-changed, comment deadline, summary, why-it-matters, who-is-affected,
  next action, source links, document hashes, version history, diff, lens links, newsletter
  link, verification manifest, correction button.
- **Premium UX:** Civic Weather strip, "What changed?" per issue, "Why am I seeing this?"
  ranking explanation, receipts drawer, Plain-English/Official/Side-by-side toggle,
  "Verify this issue" proof page, correction button on every public item.
- **Consumption model:** website loads static public JSON exports pushed outward from the
  private backend; the public site never reaches inward. This matches the existing
  data-publication boundary (WORKFLOW_GOVERNANCE.md).
- Viewport floor still applies to all future UI verification: desktop 1440×900, tablet
  768×1024, mobile 390×844.

## 3. Backend project mapping (`0a1832c4` / `ce908143` Backend track)

- **Crawler cadence:** initial full baseline crawl; incremental crawls 3×/week; randomized
  full recrawls over a 6-month cycle; structure-change tracking; backdated/silently-replaced
  document detection. Crawling live Alpine sources is still gated — current authorized corpus
  is the local TOA archive only (GOV-612 ladder).
- **15-step data flow:** crawl → save raw → hash → extract text → extract metadata → diff
  prior version → classify → summarize → lenses → citation check → risk check → export build
  → manifest build → publish → archive. Steps 7–9 (classify/summarize/lenses) are AI lane-2,
  **not authorized**; steps 1–6 and 10–15 map to existing deterministic lanes + VSR gate.
- **Public export file set:** latest.json, issues.json, cards.json, timeline.json,
  kanban.json, sources.json, meetings.json, officials.json, newsletters.json,
  transparency-alerts.json, honesty-tracker.json, search-index.json, generation-log.json,
  manifests.json under a `public-data/` tree. Nothing is exported publicly today — all rows
  are `not_publishable`, reviewer-internal.
- **Storage split:** hot 1 TB cache (recent crawls, extraction, AI context, search indexes,
  embeddings, drafts) vs cold archive (old PDFs, packets, minutes, video/transcripts, old
  newsletters, historical manifests).
- **Immutable proof layer (not a blockchain database):** SHA-256 local archive → public
  verification manifests → signed newsletter manifests → offsite backups → content
  addressing (IPFS-style) → permanent archive (Filecoin/Arweave) → WORM backups, phased.
  Never store immutably: emails, subscriber prefs, private notes, unverified accusations,
  personal data, payment records, temp drafts. The signed GOV-133 corpus manifest is the
  existing seed of this layer.

## 4. Trust, verification, and safety mapping (`b1d69179`, `2d8c4151`, `527b9486`)

- **Source vault per-item contract:** original URL, archived copy path, first/last-seen,
  last-changed, PDF hash, extraction version, prior versions, diff summary, official title,
  meeting date, AI prompt version, AI model version, confidence score, published artifact
  ID, correction history, verification manifest.
- **Hard publishing rules (adopt verbatim into the publication gate spec when Stage 6+
  opens):** no source no claim; no exact citation no quote; no official-action claim without
  official record; no "broken promise" label without promise source AND action source; no
  criminal/corruption claims unless directly sourced from official/legal records; unclear
  items say "unclear"; low-confidence labeled; correction button on every public page;
  generation log on every issue; AI perspectives labeled as perspectives.
- **Risk ladder:** L0 auto-publish (routine, source-backed) / L1 auto-publish with caution
  label / L2 hold for human review (promise contradiction, misconduct implication, legal,
  personal-data risk) / L3 never auto-publish (unsupported criminal accusation, doxxing,
  speculation). This slots directly into the existing VSR gate + owner-escalation lanes.
- **Publication Honesty Tracker:** verdicts Kept / Broken / Partial / No vote recorded /
  Unclear; always dual-sourced; maps to Human-verification track and is L2-by-default.
- **"Verify This Issue" page contract:** issue ID, publication timestamp, source/document/
  card counts, newsletter hash, manifest hash, prompt+model versions, generation log, file
  hashes, archive status, correction history.
- **Human-only controls (owner escalation triggers, consistent with existing rules):** legal
  threats, defamation disputes, correction appeals, serious accusations, takedown demands,
  source-trust rule changes, lens definition changes, payment disputes, archive
  deletion/alteration, high-risk publication.

## 5. Newsletter/lens mapping (`55f432ec` Newsletter track; deferred 5.08/5.09/5.11)

- 15-minute-read target; levels Local / Regional / Statewide — **Alpine (Local) only** is in
  scope; Regional/Statewide remain planning-only per COMPANY.md scope gates.
- Sections per level: Agenda / News / Areas of Interest.
- Layout: Conservative + Progressive lenses side-by-side (top), Libertarian/Constitutional
  full-width (bottom), optional History Looks Back (middle), Honesty Tracker
  (sidebar/footer), Transparency Alert box when triggered.
- Lens rules: must cite official sources, must label alignment, must be labeled as
  perspective not objective truth; Libertarian lens direct but not defamatory/unsupported.
- **Gate reality:** all lens/newsletter generation is AI lane-2 + editorial behavior =
  deferred goals 5.08 (`d96ceaed`), 5.09 (`057024bf`), 5.11 (`5bcc2b9b`) under the GOV-545
  Option A hold. This spec enriches those deferred goals' requirements; it does NOT reopen
  them. Only a fresh Isaac card reopens them.

## 6. Conflicts / open owner decisions recorded (do not resolve silently)

1. **Windows backend box.** Spec architecture: Mac = cockpit, Windows PC = private
   backend/foundry, deployed site = public frontend. Current reality: the deterministic
   backend + registry run on the Mac (`/Users/IA/Code/Government-watchdog`). Moving to a
   Windows foundry is an infrastructure/owner decision (cost, security, ops) — park until
   the spec's own "Stage 6: server migration" horizon; no migration work authorized.
2. **AI-run business (19 agents, 99% AI-run).** The agent taxonomy (Crawler, Archive,
   Change, Extractor, Classifier, Impact, Timeline, Newsletter, Lens, History,
   Transparency, Honesty, Citation, Risk, Publisher, Support, Correction, Ops, Revenue)
   is a target operating model. Several are live today in deterministic form (crawler/
   archive/extract lanes, VSR ≈ Citation+Risk). AI-generation agents (Classifier through
   Lens) require the still-unauthorized AI lane-2; Revenue/Support are Stage 9+.
3. **Business model (free/paid tiers, sponsors).** Owner/budget territory, Stage 9+;
   sponsor-firewall rule (sponsors must not influence sources, summaries, rankings, lenses,
   alerts, honesty tracker, scores, corrections) should be adopted verbatim when that stage
   opens.
4. **Perspective lenses ARE political-content generation.** Lens definitions are an
   Isaac-only control (already in Human-only controls). No lens prompt work without an
   owner card.
5. **Spec vs standing decisions:** nothing in GOV-653 overrides the GOV-545 Stage 5 hold,
   the not-publishable posture, the AI lane-2 exclusion, or Alpine-first scope. Isaac filed
   this as "things to consider and work into the plan," i.e., planning input, not a gate
   unlock.

## 7. What changed in Paperclip records (evidence)

- Goal descriptions appended with a `## MOTY spec integration (GOV-653, 2026-07-07)` block:
  Website track `fe3fc35a`, Newsletter track `55f432ec`, Backend track `ce908143`,
  Human-verification track `2d8c4151`, Transcript track `b1d69179`, Stage 6 `c10c406c`.
- This document committed to the verification-substrate repo `Docs/`.
- No statuses flipped; no deferred goal reopened; no new implementation issues created —
  the live reviewer-promotion chain (GOV-647→652) continues as the only active execution.

## 8. Where the next spec-driven work fires from

- **Now:** the running GOV-649 merge gate → GOV-650 pilot → GOV-652 CEO closeout.
- **After GOV-652:** the closeout report is the natural place to present Isaac the next
  options; Stage 6 activation (private beta presentation = MOTY "Stage 1 prototype") is the
  first stage where this spec's frontend surface becomes buildable, behind an Isaac card and
  the premium success-criteria template (GOV-38 framework) before any implementation chain.
