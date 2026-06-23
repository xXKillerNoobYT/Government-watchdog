# Stage 4.11 — Security / Privacy / Publication Gate for the Reviewer-Internal Newsletter Backbone

> **Issue:** GOV-473 (Stage 4.11 · SecurityPrivacyAgent). **Depends on (both `done`):** GOV-470 (Stage 4.08 editorial contract), GOV-471 (Stage 4.09 automation-vs-AI boundary).
> **Stage:** 4.11 — **gate / audit**, not a code-build slice. The enforcing controls already live in merged code; this document *verifies* they hold as a system and *defines* what must stay deferred to Isaac.
> **Scope audited:** Town of Alpine only · reviewer-internal · no public launch · no email/sender · no signup/auth · no person-naming · no new crawl.
> **Grounded on:** `origin/main` HEAD `8c581ef` (GOV-467 / PR #80 merged).
> **Parent gate framework:** `docs/stage1-security-privacy-publication-gates-contract.md` (Stage 0.21 / GOV-21). This is its Stage-4 application.
> **Ruling:** ✅ **PASS — the reviewer-internal newsletter backbone clears the security/privacy boundary.** No public-launch path is opened. The minimum publication gate (§4 below) remains deferred to Isaac via GOV-420, which stays `blocked`.

The Stage 4 reviewer-internal newsletter backbone is a **deterministic, read-only projection** over the
already-web-safe Stage-3 reviewer-internal read surface. It assembles a weekly item feed → digest → preservation
audit → statement→source binding entirely at `access: reviewer_internal`, `scope: alpine`. It performs **no
mutation, no AI generation, no network I/O, and no public publication.** This audit confirms that posture across
the four acceptance criteria and pins the publication gate that must not be crossed without owner sign-off.

**Audited artifacts (all @ `8c581ef`, consumed read-only / 0-diff by this audit):**

- `scripts/stage4_newsletter_feed.py` (GOV-449, 4.03) — `SCOPE`, `ACCESS`, `STAGE3_CLAIM_VOCAB`, `_assert_local_safe`.
- `scripts/stage4_newsletter_digest_assembler.py` (GOV-457, 4.05) — `assert_labels_preserved`, `assert_source_trail_preserved`.
- `scripts/stage4_newsletter_preservation_audit.py` (GOV-453, 4.04) — raw-preservation / reproducibility net.
- `scripts/stage4_statement_evidence_binding.py` (GOV-467, 4.07) — statement→exact-source binding validator.
- `scripts/read_api.py` — `reviewer_internal_records` (`:509`), person-label neutering (`:290`–`:319`), `assert_no_raw_paths`, `RawPathLeak`.
- `scripts/publication.py` — the public-contract surface. **Not imported by any Stage 4 script.**

---

## AC1 — No public-launch path is opened; GOV-420 remains blocked ✅

**Verified.**

- **No code path to the public lane.** No Stage 4 script imports `publication.py` or calls `to_web_safe` /
  `compute_ui_status` / `publish_*`. Confirmed by `grep -E "import publication|to_web_safe|publish_|compute_ui_status" scripts/stage4_*.py`
  — the only matches are docstrings stating the layer **never** calls them
  (`stage4_newsletter_feed.py:28`, `stage4_newsletter_digest_assembler.py:35`).
- **Access constant is reviewer-internal.** `stage4_newsletter_feed.py:67` → `ACCESS = "reviewer_internal"`; every
  artifact is stamped `{"scope": SCOPE, "access": ACCESS, ...}` and the feed reads only
  `read_api.reviewer_internal_records(conn)` (`:453`, `:508`, `:580`) — the reviewer lane, **minus** the owner
  `publishable` flip that the public lane requires (`read_api.py:515`).
- **Publication axis sits on `draft`.** `stage4_newsletter_feed.py:77` → `PUBLICATION_STATUS_DRAFT = "draft"`;
  every item carries `"publicationStatus": "draft"` (`:317`). This is the publication-control axis, distinct from
  the claim axis.
- **Owner gate intact.** GOV-420 (`[Launch-readiness][Owner decision] Public deploy of preview-launch — GATED on
  Isaac`) status = **`blocked`** at audit time. This audit opens no path that would unblock it; the public deploy
  decision remains Isaac's alone.

## AC2 — Labels distinguish verified / source-backed / unverified / disputed / corrected / AI-presented ✅

**Verified.** The backbone carries every Stage-3 claim/status label **verbatim** and mints **zero new labels**
(`stage4_newsletter_feed.py:32`, EG-7). Two orthogonal axes cover the required distinctions:

| Required distinction | Where it lives | Evidence |
|---|---|---|
| **verified** | claim axis (`labels.claimStatus`) | `STAGE3_CLAIM_VOCAB` ∋ `verified` |
| **unverified** | claim axis | `STAGE3_CLAIM_VOCAB` ∋ `unverified`; digest `UNVERIFIED_STATUSES = vocab − {verified}` (`assembler:77`) so anything not affirmatively verified is surfaced as unverified — conservative |
| **disputed** | claim axis | `STAGE3_CLAIM_VOCAB` ∋ `disputed`; `CONFLICT_STATUSES = {disputed}` (`assembler:72`) |
| **corrected** | correction axis (`labels.correctionStatus`) | `STAGE3_CLAIM_VOCAB` ∋ `corrected` |
| **AI-presented** | claim axis + structural `aiPresented` bool | `STAGE3_CLAIM_VOCAB` ∋ `ai_presented`; `TYPE_AI_PRESENTED → "ai_presented_context"` (`feed:107`) — an AI/unreviewed item keeps its card-layer label and is **never styled as verified fact** (§2.2) |
| **source-backed** | provenance/trust axis (GOV-311) | `read_api.py:524` re-derives `ui_status` as source-backed (publication-eligible) independently of storage |

Full claim vocabulary observed at runtime: `['ai_presented', 'corrected', 'disputed', 'needs_human_review',
'source_changed', 'source_missing', 'speaker_unidentified', 'unverified', 'verified']`. Label integrity is
**enforced, not merely declared**: `assert_labels_preserved` (`assembler:274`) goes RED if any digest item's
`labels` differs byte-for-byte from the feed item of the same id; `assert_source_trail_preserved` does the same
for `sourceTrail`.

## AC3 — Privacy / defamation / unsupported-allegation / accidental-publication / person-label risks ✅

**Checked — all five risk classes are mitigated by load-bearing controls; no residual high-risk finding.**

- **Civic-servant / person-label risk → fail-closed neutering.** `read_api.py:290`–`:319`: for any row that is not
  `attributed` **and** in an auto-nameable class, the stored free-text `display_label` is **never consulted** —
  the label is derived from `speaker_class` alone (`SAFE_COMMUNITY_LABEL` for `on-record-public`, else
  `SAFE_GENERIC_LABEL`). A name poisoned past the write guard cannot reach the envelope. Missing/NULL attribution →
  conservative `_SAFE_SPEAKER_LABEL`; **never `None`, never a candidate name.** The newsletter inherits this
  because it reads only the already-neutered reviewer surface.
- **Privacy / raw-data leak → transport sweep.** `_assert_local_safe` (`feed:144`) hard-stops any artifact carrying
  a raw marker (vault path, local fs path, transcript path) with `RawPathLeak`. **Independently re-proved this
  audit:** planting `localSourcePath: /Users/IA/vault/raw/secret.pdf` into a feed → `RawPathLeak` raised; a
  legitimate `https://council.alpine.gov/min.html` URL passes (no false-positive). Only genuine public `http(s)://`
  locators are exempt, matching `read_api`'s exemption.
- **Defamation / unsupported allegation → conservative label + exact-source binding.** Anything not affirmatively
  `verified` is surfaced as `unverified` (`assembler:77`), so an unreviewed/disputed/AI item is never presented as
  established fact. Stage 4.07 (`stage4_statement_evidence_binding.py`) re-proves every surfaced statement binds to
  an exact-source pointer and is **never silently upgraded to verified** (`:24`) — a paraphrase is never presented
  as verbatim.
- **Accidental publication → no public sink exists.** Per AC1, the backbone has no import of and no call into the
  public publication path; every artifact is stamped `reviewer_internal` / `draft`. There is no surface from which
  a reviewer-internal digest can leak to a public reader.

## AC4 — Minimum publication gate (must stay deferred to Isaac / public-launch approval)

The following requirements are the **minimum bar** that must ALL be satisfied **and explicitly approved by Isaac
via GOV-420** before any newsletter content crosses from reviewer-internal to a public reader. None may be
self-approved by an agent; this gate stays **deferred** and GOV-420 stays `blocked` until owner sign-off.

1. **Owner go/no-go (GOV-420).** Explicit Isaac approval of public deploy. No agent may flip `publicationStatus`
   from `draft` or route through `publication.to_web_safe` for newsletter output without it.
2. **Per-item verification floor.** Only `verified` + `source-backed` items are eligible for public surfacing;
   `unverified` / `disputed` / `ai_presented` / `needs_human_review` items stay reviewer-internal. The
   dual-gate (`uiStatus` publication-eligible **AND** DB `publication_state == "publishable"`, `read_api.py:466`)
   must both agree.
3. **Exact-source binding present.** Every publicly surfaced statement must pass the 4.07 binding validator
   (statement → complete, valid exact-source pointer; no orphan claims; no paraphrase-as-verbatim).
4. **No raw / PII / identity leak.** The `_assert_local_safe` + `assert_no_raw_paths` transport sweep must pass on
   the public artifact; no raw crawler output, registry, vault path, or private PII may cross. Person labels stay
   neutered except `on-record-public` officials with correct attribution.
5. **AI/automation boundary honored (GOV-471).** Any AI-presented context stays labelled `ai_presented` and is
   never styled as fact; the automation-vs-AI boundary from Stage 4.09 governs what may be generated vs. surfaced.
6. **Editorial contract honored (GOV-470).** Public briefing content conforms to the Stage 4.08 reviewer-internal
   editorial contract; no editorial prose is introduced below that contract.
7. **Scope lock.** Town of Alpine only. No Wyoming/US expansion, no new crawl scope, no email/sender, no
   signup/auth introduced as a side effect of publication.

Crossing this gate is an **owner + public-launch decision**, not an engineering task. Until GOV-420 is approved,
the backbone remains a reviewer-internal tool.

---

## Verification evidence

- **Tests:** `pytest tests/test_gov449_newsletter_feed.py tests/test_stage4_newsletter_digest_assembler.py
  tests/test_stage4_newsletter_preservation_audit.py tests/test_stage4_statement_evidence_binding.py` →
  **49 passed** @ `8c581ef`.
- **Independent leak probe (this audit):** raw vault path → `RawPathLeak` raised (gate load-bearing); public
  `https://` URL → exempt (no false-positive); `ACCESS == "reviewer_internal"`, `PUBLICATION_STATUS_DRAFT == "draft"`.
- **No-public-path grep:** `grep -E "import publication|to_web_safe|publish_|compute_ui_status" scripts/stage4_*.py`
  → only docstring mentions; no live call.
- **GOV-420 status:** `blocked` (owner-gated on Isaac) at audit time.
- **Prior per-layer SecPriv audits (all PASS, folded in here):** GOV-451 (4.03 feed), GOV-459 (4.05 digest),
  GOV-455 (4.04 preservation), GOV-469 (4.07 binding).

**Disposition:** ✅ PASS for reviewer-internal Alpine use. Public launch stays deferred to Isaac via GOV-420.
