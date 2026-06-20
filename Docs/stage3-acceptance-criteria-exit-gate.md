# Stage 3 — Acceptance-Criteria Set + Exit Gate

> **Issue:** GOV-337 (Stage 3.02 · Plan · CTO→BackendCrawlerEngineer). **Parent:** GOV-335.
> **Stage:** 3.02 — planning only. **NON-implementation. NON-unlock.**
> **Scope:** Town of Alpine only · reviewer-internal · no public launch.
> **Blocked-by (now cleared):** GOV-336 (Stage 3.01 spec package), merged `origin/main` HEAD `faa4220` (PR #60).
> **Goal:** Stage 3.02 `442f5521-a9c6-459c-aba1-fe3cdda1648c`.
> **Inputs of record:**
> - `Docs/stage3-alpine-timeline-card-mvp-spec.md` (GOV-336) — issue map (§1) + card↔read_api data contract (§2) + premium gate (§5) + non-unlock (§7).
> - Master plan Stage 3 "Passing criteria" (`…/Government-Watchdog v1 Plans/Docs/2026-06-06-Government-Watchdog-Staged-Master-Plan.md`, lines ~470–476).
> - Reviewer-internal read-surface field reference `Docs/stage2-reviewer-internal-read-surface-reference.md` (GOV-326).

This document is the **testable Stage 3 acceptance-criteria set and the Stage 3 exit gate**. It turns the
five master-plan Stage 3 passing criteria into concrete, reproducible acceptance criteria — each with an
artifact/command/UI behavior, at least one negative/adversarial case, and the source-linked evidence that
proves it — and names the evidence required to flip the Stage 3 parent goal. It authorizes **no**
implementation: see §7. The premium success-criteria gate (§6) is a hard precondition before any Stage 3
implementation child is created or activated.

---

## 0. Owner reconciliation note (non-blocking)

The GOV-336 issue map (§1, row 3.02) tentatively listed VerificationSafetyReviewer as the 3.02 owner. The
**authoritative routing is the CTO-assigned issue GOV-337**, which assigns this planning deliverable to
**BackendCrawlerEngineer** (backend `Docs/`, Docs-only diff). The VSR/SecPriv roles remain the **review
legs** of this planning doc (§5 review lane), not its author. No conflict requiring escalation: the issue
assignment governs; this note records the reconciliation as required by the carry-forward rule.

---

## 1. How to read this doc

The master plan defines five Stage 3 passing criteria (verbatim, master plan Stage 3 "Passing criteria"):

> 1. Private website route shows Alpine timeline with at least 5 sourced cards.
> 2. At least one card links to a meeting timestamp.
> 3. At least one card shows source drawer data.
> 4. AI/unverified/disputed/corrected block pattern exists even if test data is limited.
> 5. Workflow runs pass on website/backend local Mac runners.

Each becomes one **acceptance criterion (AC-1 … AC-5)** below. Every AC has four mandatory parts:

| Part | Meaning |
|---|---|
| **Requirement** | The pass condition, stated as a concrete artifact / command / observable UI behavior. |
| **Positive verification** | The exact reproducible step (command run + expected output, or named UI behavior at named viewport) that demonstrates PASS. |
| **Negative / adversarial case(s)** | At least one failure-shaped input that MUST be handled correctly (fail-closed, gated, or omitted) — not merely "not crash". |
| **Evidence** | The source-linked, reproducible artifact recorded at closeout (file path, command + captured output, screenshot at named viewport, test id + result, API output). |

**Grounding rule (inherited from GOV-336 §2.1):** every field an AC inspects must be a field the Stage 2
reviewer-internal `read_api` already emits (the 5 overlays + `evidence` + the gap lane). No AC may demand a
field the read surface does not expose. The one genuine gap — correction/dispute *relationship edges* —
is handled by AC-4's scope note, not by requiring an un-built field. The Stage 3 card feed (subgoal 3.05)
and frontend timeline (3.06) are the *future artifacts under test*; these ACs are the contract they must
satisfy, and the impl children are blocked behind the §6 premium gate until then.

**Viewport floor (COMPANY.md "UI viewport coverage floor"):** any AC whose verification is a UI behavior
requires **desktop (≈1440×900) + tablet (≈768×1024) + mobile (≈390×844)** evidence, or a named, issue-level
exception stating which class was not rendered, why, and who owns the gap. A UI `Pass` with only one
viewport is not a pass.

---

## 2. Acceptance criteria (testable)

### AC-1 — Alpine timeline shows ≥5 sourced cards (reviewer-internal route)

- **Requirement.** The reviewer-internal route (`access === 'reviewer_internal'`, behind the gated-beta
  lane) renders an Alpine timeline whose jurisdiction filter is pre-set **Wyoming → Lincoln County → Town
  of Alpine** (Alpine default; broader scope shown as *planned*, never live), containing **≥ 5 typed cards
  that each carry a non-empty source trail**. "Sourced" = the card's `evidence` drawer (GOV-336 §2.2)
  resolves to ≥ 1 public `http(s)://` source/archive URL. No orphan card is counted toward the 5
  (GOV-336 §2.2: orphan records are never served).
- **Positive verification.**
  - *Backend:* the Stage 3 card feed (3.05, built on `read_api`) returns ≥ 5 Alpine cards each with a
    non-empty `evidence` array — captured as committed/printed JSON, e.g.
    `python3 -c "import json,sys; cards=json.load(sys.stdin)['cards']; assert sum(1 for c in cards if c['evidence']) >= 5"`
    over the feed output, exit 0.
  - *Frontend:* the timeline route renders ≥ 5 cards at all three viewport classes; Alpine filter is the
    default selection; broader filter values are visibly labeled *planned*.
- **Negative / adversarial cases.**
  - **Orphan / unsupported claim:** a record with no resolvable public source MUST NOT appear as a normal
    card and MUST NOT count toward the 5 (it surfaces only as a `source_missing` gap card per AC-4, or not
    at all). A card with an empty drawer is a defect, not a pass.
  - **< 5 sourced cards available:** the route shows the real count plus visible gap/status indicators; it
    MUST NOT pad the timeline with unsourced or fabricated cards to reach 5. (Under-count ⇒ Stage 3 exit
    not met; surface the gap honestly per GOV-336 §0 and BACKEND_CRAWLER_WORKFLOWS Isaac directive.)
- **Evidence.** Feed JSON file path + the assertion command and its exit-0 output; timeline screenshots at
  desktop/tablet/mobile showing the ≥5 cards and the Alpine-default filter.

### AC-2 — ≥1 card links to a meeting timestamp

- **Requirement.** At least one card (a meeting or statement card) exposes a **meeting timestamp link** —
  a public source/archive URL that points at a specific moment in a meeting recording (or a
  timestamped agenda/minutes anchor). The timestamp link rides in the same `evidence` drawer (GOV-336
  §2.2 source-links row); statement cards additionally carry `speaker_label` (GOV-290) and
  `confidence_label` (GOV-283/290) so the timed claim is attributable.
- **Positive verification.**
  - *Backend:* ≥ 1 feed card whose `evidence` contains a timestamped public URL (e.g. a `…?t=` /
    `#t=` fragment or an explicitly timestamped archive anchor) — assert present and `http(s)://`-scheme.
  - *Frontend:* clicking the card's timestamp opens the source at the cited moment; the statement card
    shows `confidence_label` at or above the floor `auto_caption_untimed`.
- **Negative / adversarial cases.**
  - **Broken / unreachable archive:** if the timestamp's archive URL is unavailable, the card MUST show a
    degraded but honest state (status reflects `source_changed`/unavailable via `ui_status`; the drawer
    still cites the original URL + scan date) and MUST NOT silently drop the source trail or present a dead
    link as verified.
  - **Untimed caption only:** a claim backed only by an untimed auto-caption surfaces with
    `confidence_label = auto_caption_untimed` (the floor) and is **not** presented as a precise-timestamp
    claim. No claim is upgraded above the evidence it has.
- **Evidence.** The card id + its timestamped `evidence` URL (captured JSON); screenshot of the opened
  source at the cited moment (or, if archive is down, the degraded-state screenshot + the recorded
  `ui_status`).

### AC-3 — ≥1 card shows source-drawer data

- **Requirement.** At least one card opens a **source drawer** that displays the source trail: original
  source/archive URL(s), and the source-trail fields the read surface allows (scan date, source type,
  jurisdiction). The drawer is the `evidence` envelope key (GOV-336 §2.2) — public `http(s)://` only;
  `file://`, local refs, `.sha256` files, internal/raw ids, and vault paths are dropped *by construction*
  because they never reach `read_api` output.
- **Positive verification.**
  - *Backend:* the feed card's `evidence` array deserializes to objects exposing only web-safe
    source-trail fields (no internal id / raw path / `file://`). A grep/scan over the feed output for
    `file://`, absolute FS paths, `.sha256`, and any internal-id key returns **zero hits**.
  - *Frontend:* the drawer opens at all three viewports and lists the source link(s) + allowed trail
    fields; a card with no resolvable source has no normal drawer (it is a gap card, AC-4).
- **Negative / adversarial cases.**
  - **Private-data leak attempt:** a poisoned backing record carrying PII (email/phone/address),
    voter-registry data, a `file://` path, or an internal id MUST NOT surface any of it in the drawer. The
    drawer renders only the web-safe `evidence` fields; any leak is a **reportable defect**, not an
    expected state. (Re-uses the Stage 2 dual web-safe boundary; the card layer never calls `to_web_safe`/
    `publication.py` and never re-derives a dropped field — GOV-336 §2.1.)
  - **Empty drawer:** a card that would render an empty drawer (no edge) is invalid — orphan records are
    never served (GOV-336 §2.2). An empty drawer ⇒ defect.
- **Evidence.** Drawer screenshot(s) at the three viewports; the leak-scan command over the feed output and
  its zero-hit result; the card's `evidence` JSON.

### AC-4 — AI / unverified / disputed / corrected gated-block pattern exists (even with limited test data)

- **Requirement.** The timeline visibly **gates** AI-presented / unverified / disputed / corrected content
  behind a distinct gated-block pattern, distinguishable from plain verified content, **even when test data
  is limited** (a single synthetic/fixture card per gated kind is sufficient to demonstrate the pattern).
  The gate is driven only by existing read keys (GOV-336 §2.2/§2.4):
  - **AI-presented / unverified:** `provenance_status` (GOV-311, reviewer-internal lane-gated; frozen
    `{grounded, unverified}`, floor `unverified`) powers the AI/unverified gated block + trust badge.
  - **render/review state** (incl. corrected/source-changed states expressible today): `ui_status`
    (re-derived via `publication.compute_ui_status`; never trusted from storage).
  - **Scope note on disputed/corrected *relationship edges* (GOV-336 §2.4 gap):** the read surface does
    **not yet** emit correction/dispute relationship edges, nor a dedicated `ai_presented` gate beyond
    `provenance_status`. Therefore Stage 3 demonstrates correction/dispute state **only insofar as it is
    expressible via `ui_status` / `provenance_status`** and MUST NOT fabricate correction linkage. The
    additive, lane-gated, fail-closed relationship-edge contract is **Stage 3 subgoal 3.07** and is
    explicitly out of scope for this exit gate.
- **Positive verification.**
  - *Backend:* a fixture card with `provenance_status = unverified` and one with `grounded` both serialize
    correctly; the unverified one carries the gated-block marker; `provenance_status` appears **only** under
    the reviewer-internal lane (`include_provenance_status=True`), and the public lane is byte-identical to
    its pre-2.12 shape (GOV-336 §2.3).
  - *Frontend:* the gated block is visually distinct (label + icon + hover explanation) from verified
    cards, at all three viewports; default (fail-closed) presentation for missing/unknown provenance is the
    gated `unverified` state, never silently "verified".
- **Negative / adversarial cases.**
  - **Fail-open attempt:** a card whose provenance cannot be established renders the **gated `unverified`**
    block, not a clean verified card (fail-closed default = `unverified`). A verified-looking render on
    absent provenance is a defect.
  - **Lane bleed:** `provenance_status` (and statement free-text / reviewed summary) MUST NOT render in a
    public lane. Since the entire MVP runs at `access: reviewer_internal`, a card presenter that is
    lane-blind (renders provenance regardless of access) is a defect even if not user-visible yet.
  - **Fabricated correction edge:** any card asserting a correction/dispute *relationship* not derivable
    from `ui_status`/`provenance_status` is out of contract (it presupposes the un-built 3.07 edge) and
    fails review.
- **Evidence.** Fixture card JSON for each gated kind; screenshots of the distinct gated block vs a
  verified card at the three viewports; the lane-gating assertion (public lane unchanged) output.

### AC-5 — Website + backend local Mac runner workflows pass

- **Requirement.** The Stage 3 work passes the **backend** local Mac runner workflow (`IA-Mac-GOV-Backend`)
  and the **website** local Mac runner workflow (`IA-Mac-GOV-Website`): backend test suite green
  (including the Stage 3 card-feed tests and the carried-forward Stage 2 trust auditors GOV-306/318/322),
  and the website typecheck + test + build green (including timeline/card/drawer component tests).
- **Positive verification.**
  - *Backend:* `pytest` (or the runner-invoked equivalent) exits 0; the full pre-existing Stage 2 suite
    still passes (no regression) and the new Stage 3 feed tests pass. Capture the pass count + exit code.
  - *Website:* typecheck + test + build all exit 0 (the GOV-314 precedent for website closeout); capture
    each command + result.
  - *Traceability/back-gap guards:* the card feed passes a Stage 3 traceability check (3.12, reusing
    GOV-306/318) and a back-gap regression check (3.13, reusing GOV-322) proving the feed never silently
    drops a record/gap the read surface emits.
- **Negative / adversarial cases.**
  - **Silent record drop:** if the card feed omits a record/gap the read surface emits, the back-gap guard
    (3.13/GOV-322 pattern) MUST go RED — a green suite that tolerates silent drops is not a pass.
  - **Runner unavailable:** if a runner cannot execute (e.g. tunnel dead), fall back to local
    `127.0.0.1:3100` and run the suite locally on this Mac, and the closeout MUST name which runner was
    substituted and by what (no "skipped ⇒ pass").
- **Evidence.** Captured command lines + exit codes + pass counts for backend pytest and website
  typecheck/test/build; the traceability + back-gap guard results (CLEAN / exit codes); the runner id or
  the named local-substitution note.

---

## 3. Cross-cutting adversarial matrix (the five named failure classes)

The issue names five adversarial classes; this matrix maps each to the AC that owns it and the required
fail-closed behavior, so no class is left only to "happy path".

| Adversarial class | Owning AC | Required behavior (fail-closed) | Read-surface basis |
|---|---|---|---|
| **Missing pages / missing source** | AC-1, AC-4 | Record surfaces as a `source_missing` gap card (or is not served); never a normal card with an empty drawer; never padded to hit ≥5. | `completeness_gap_cards` (GOV-298) + orphan-never-served rule. |
| **Ambiguous / unverifiable names** | AC-2 | `speaker_label` floors to `Community Member` / `Meeting Attendee`; a poisoned name is never read. "No name is better than wrong speaker attribution." | `speaker_label` (GOV-290) floor. |
| **Broken / unavailable archives** | AC-2, AC-3 | Degraded-but-honest state (status via `ui_status`; original URL + scan date retained); no dead link presented as verified; no silent drop of the source trail. | `evidence` + `ui_status`. |
| **Private data (PII / voter-registry / raw paths / internal ids)** | AC-3, AC-4 | Never surfaced in any card or drawer; dropped by construction (card layer never calls `to_web_safe`/`publication.py`, never re-derives a dropped field); leak ⇒ reportable defect. | GOV-336 §2.1 boundary; dual web-safe layers. |
| **Unsupported claims / fabricated correction edges** | AC-1, AC-4 | No orphan claim served; provenance fail-closes to gated `unverified`; correction/dispute *edges* not fabricated (await 3.07). | `provenance_status` floor + §2.4 gap. |

---

## 4. Stage 3 exit gate — evidence required to flip the Stage 3 parent goal

The **Stage 3 parent goal `88190dca`** (Alpine timeline + card-model MVP) may be flipped to **achieved**
only when **all** of the following are recorded as source-linked, reproducible evidence. Comments, plans,
and `Remaining` bullets are **not** sufficient; each line below requires the concrete artifact named.

- [ ] **EX-1 (AC-1):** Backend feed JSON path + assertion output proving **≥ 5 sourced Alpine cards**, AND
      timeline screenshots at **desktop + tablet + mobile** showing the ≥5 cards with the Alpine-default
      Wyoming→Lincoln→Alpine filter (broader scope marked *planned*).
- [ ] **EX-2 (AC-2):** A card id + its **timestamped public `evidence` URL**, AND the opened-source
      screenshot at the cited moment (or the recorded degraded `ui_status` if the archive is down).
- [ ] **EX-3 (AC-3):** **Source-drawer** screenshots at the three viewports, AND the leak-scan over the feed
      output returning **zero** hits for `file://` / absolute FS paths / `.sha256` / internal-id keys.
- [ ] **EX-4 (AC-4):** Fixture card JSON for each gated kind (`unverified` + `grounded`), AND a screenshot
      showing the **distinct gated block vs a verified card** at the three viewports, AND the lane-gating
      assertion output (public lane byte-identical to pre-2.12). Correction/dispute *edge* demos are NOT
      required (deferred to 3.07).
- [ ] **EX-5 (AC-5):** Captured backend `pytest` exit-0 + pass count (no Stage 2 regression), website
      typecheck/test/build exit-0, AND the traceability (3.12) + back-gap (3.13) guard results, AND the
      runner id(s) or named local-substitution note.
- [ ] **EX-6 (gate hygiene):** Every Stage 3 `plan→impl` impl child that contributed to the above was
      created **after** the §6 premium gate was applied to its parent goal, with the applying agent's
      evidence recorded (no out-of-order impl child).
- [ ] **EX-7 (boundary):** No public launch, no public newsletter send, no non-Alpine source, no weakening
      of `to_web_safe`/`publication.py` occurred (confirmed by SecPriv leg of the relevant impl children).

**Flip authority.** Per the goal-flip-at-merge lane, the **CTO** (non-author) flips `88190dca` → achieved
once EX-1…EX-7 evidence is linked. Any unmet line ⇒ Stage 3 stays open; surface the gap honestly rather
than flipping early. Stage 4 stays locked until Stage 3 passes (master plan "Unlocks").

---

## 5. Review lane for THIS planning doc

`Impl(Plan)` → **VSR** (VerificationSafetyReviewer leg) → **SecPriv** (SecurityPrivacyAgent leg) →
**CTO non-author merge + goal-flip-at-merge** (CTO PATCHes Stage 3.02 goal `442f5521` → achieved AT merge).
The two reviewer legs are created as `todo` child issues of GOV-337.

**Verification evidence (this doc).** See the closing PR: file path, `wc -l`, `git diff --stat` proving a
**Docs-only** addition (0 production-code diff), and section greps proving (a) all five master-plan criteria
are mapped to ACs (§2), (b) each AC carries ≥ 1 negative/adversarial case (§2 + §3), (c) the exit gate
names the evidence to flip the parent goal (§4), and (d) the premium gate is named as a hard precondition
(§6).

---

## 6. Premium success-criteria gate (hard precondition — staging rule #8)

The premium success-criteria framework at
`/Users/IA/Documents/Obsidian Vault/01_projects/Government-Watchdog v1 Plans/Docs/2026-06-06-Premium-Success-Criteria-Framework.md`
**MUST be applied** (its paste-in success-criteria block added to the parent goal) to **any Stage 3
implementation child's parent goal before that child is created or activated**.

- **This planning issue (GOV-337 / 3.02) and the VSR/SecPriv review legs** may proceed **without** the
  premium block — they author/verify the criteria; they do **not** authorize implementation.
- **Implementation children** (the impl child of any `plan→impl` subgoal: 3.03/3.04/3.05/3.06/3.07/3.12/
  3.13 per GOV-336 §1) are **blocked** until the premium success-criteria block is applied to their parent
  goal and the applying agent records the evidence.

This acceptance set **does not itself authorize implementation.** It defines *what done looks like*; an
implementation child created without the applied premium block is out of order and must be blocked back to
this gate (and recorded as a failed EX-6 line).

---

## 7. Explicit non-unlock statement

This document is **planning only**. It does **NOT**:

- authorize any Stage 3 implementation, code, migration, crawler run, or card-feed build;
- create or activate any Stage 3 implementation child (it defines acceptance + exit evidence only);
- unlock public launch, public newsletter send, or any public-facing surface;
- unlock non-Alpine expansion (Star Valley / Lincoln County / Wyoming / US);
- approve budget, donations, paid services, or official-contact automation;
- cross or weaken the public-projection boundary (`to_web_safe` / `publication.py`);
- override any Stage 0 safety/governance gate or any Stage 2 accepted artifact.

**Pass-up trigger:** any discovered need for public launch, legal/privacy/publication judgment,
budget/donation, official-contact, or scope expansion beyond Alpine → STOP, comment, escalate to
CEO/Isaac. Alpine-first, reviewer-internal only, until an owner decision says otherwise.
