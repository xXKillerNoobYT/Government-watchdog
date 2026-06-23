# Stage 4.12 — Traceability & Audit-Trail Closeout for the Stage 4 Newsletter Evidence Chain

> **Issue:** GOV-474 (Stage 4.12 · CTO). **Type:** closeout / audit-readiness evidence package — *no new production code*; it runs the already-merged Stage 4 chain end-to-end over a representative reviewer-internal Alpine corpus and certifies the traceability contract.
> **Stage:** 4.12 — runs *after* the buildable Stage 4 chain (4.03–4.07) and the editorial/automation contracts (4.08 / 4.09) it depends on are merged.
> **Scope:** Town of Alpine only · reviewer-internal · **no public launch** · no email/sender · no signup/auth · no person-naming · no new crawl · no editorial prose / no AI-generated statements.
> **Grounded on:** canonical `origin/main` HEAD `8c581ef` (GOV-467, Stage 4.07 binding validator merged, PR #80).
> **Dependencies satisfied at closeout:** GOV-470 (Stage 4.08 reviewer-internal weekly briefing editorial contract) = **done**; GOV-471 (Stage 4.09 automation-vs-AI boundary lock) = **done**.
> **Sibling closeout legs (parallel, independent owners):** GOV-472 (4.10 VSR QA workflow plan), GOV-473 (4.11 SecPriv security/privacy/publication gate). This document is the CTO (4.12) leg. All three gate the CEO Stage 4 exit decision (GOV-475).

---

## 0. What "traceability closeout" means here

The Stage 4 reviewer-internal newsletter backbone is a **defense-in-depth stack**, not a single check. Each layer
re-derives the statement→exact-source binding independently, so a regression in any one layer is caught by the layer
above it:

| Layer | Artifact (@ `8c581ef`) | Traceability role |
|---|---|---|
| Write-time | `scripts/statements.py` — `is_orphan`, `validate_pointer`, `LOCATOR_REQUIRED_FIELDS` | No statement is written without a complete, valid exact-source pointer (no orphan claims). |
| Serve-time | `scripts/read_api.py` — `reviewer_internal_records`, `_evidence_links_for`, `_segment_resolves`, `assert_no_raw_paths` | Only reviewer-internal records are served; raw vault paths / `file://` / `.sha256` are stripped at the boundary. |
| Feed | `scripts/stage4_newsletter_feed.py` (GOV-449) | Deterministic item feed over the served records; chronology + readiness + orphan routing; zero new labels. |
| Digest | `scripts/stage4_newsletter_digest_assembler.py` (GOV-457) | Groups served items into GOV-15 sections as structured data; labels + source trail carried verbatim. |
| Binding regression net | `scripts/stage4_statement_evidence_binding.py` (GOV-467) | **Re-proves**, one layer up over the assembled digest, that every statement-bearing item still resolves to its exact source; orphans routed to VSR; labels never silently upgraded; paraphrase ≠ verbatim. |

This closeout is the **auditor/consumer** of that stack. It does not add a layer; it runs the stack over a
representative corpus and records the genuine output as Stage 4 exit evidence.

**Reproduction (any reviewer can re-run):**

```bash
# 1. Build the representative reviewer-internal Alpine corpus into a temp DB
#    (reuses the GOV-467 test seed verbatim — no new fixtures, no real-corpus dependency).
python - /tmp/gov474_audit.db <<'PY'
import sys, importlib.util
from pathlib import Path
ROOT = Path.cwd(); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT))
import db
spec = importlib.util.spec_from_file_location("t", ROOT/"tests"/"test_stage4_statement_evidence_binding.py")
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
dbp = Path(sys.argv[1]); db.apply_migrations(dbp); c = db.open_db(dbp); mod._seed(c); c.commit(); c.close()
PY

# 2. Per-assertion traceability log (digest item -> statement -> exact-source pointer), with guards
python scripts/stage4_statement_evidence_binding.py --db /tmp/gov474_audit.db --artifact log --check

# 3. Audit overlay (exit-readiness summary)
python scripts/stage4_statement_evidence_binding.py --db /tmp/gov474_audit.db --artifact overlay --check
```

The representative corpus deliberately spans every readiness class a real briefing produces: page-anchored paraphrase
claims, a verbatim char-span quote, an AI-produced statement, and a corrected statement.

---

## 1. AC1 — Digest → source document/transcript → pointer chain (representative assertions)

Running the binding validator over the representative corpus resolves **all 7** statement-bearing digest items to an
exact-source pointer. Captured output (`--artifact log --check`, exit `0`):

| Digest item | Statement | Pointer kind | Resolves | Claim label | Route |
|---|---|---|---|---|---|
| `alpine-newsletter-item-001` | `stmt-1` | `page` | ✅ | `unverified` | — |
| `alpine-newsletter-item-002` | `stmt-2` | `page` | ✅ | `unverified` | — |
| `alpine-newsletter-item-003` | `stmt-3` | `page` | ✅ | `unverified` | — |
| `alpine-newsletter-item-004` | `stmt-4` | `page` | ✅ | `unverified` | — |
| `alpine-newsletter-item-005` | `stmt-verbatim` | `char_span` | ✅ | `unverified` | — |
| `alpine-newsletter-item-006` | `stmt-ai` | `page` | ✅ | `ai_presented` | — |
| `alpine-newsletter-item-007` | `stmt-corrected` | `page` | ✅ | `corrected` | — |

Each row is a genuine resolution: `cardIds[0]` → forward `card_handle → statement_id` index → real `statements` row →
a resolving `segment_id` segment edge **or** an `evidence_link` whose pointer passes `statements.validate_pointer`
(complete per `LOCATOR_REQUIRED_FIELDS`). The verbatim item (`item-005`) binds to a `char_span` anchor carrying
`quoted_text` — paraphrase is never presented as a quote. Labels are carried verbatim from the served read surface and
match an **independent** recompute (`stage3_card_feed._compose_record_status`); none is upgraded to `verified`.

**The chain that each assertion traces:**

```
digest item.cardIds[0]
  → card_handle(card_type, statement_id)        # forward index, never a reverse of the one-way hash
  → statements row (statement_id)               # statement_text, speaker, status, correction
  → evidence_link (from_node_id = statement_id) # original_url/final_url, archive_url+status, scan_date, locator
  → exact pointer (page | char_span | section | paragraph | timestamp) validated complete
```

---

## 2. AC2 — Required metadata, and where each field lives

Every reviewer-internal assertion served by `read_api.reviewer_internal_records` carries the following audit metadata.
Field provenance is grounded in the live schema (`scripts/db` migrations) and the served record surface, not asserted.

| Required metadata (GOV-474) | Field(s) on the served surface | Where it originates |
|---|---|---|
| Source URL | `evidence[].original_url`, `evidence[].final_url` | `evidence_links`; `sources.original_url` |
| Local archive / source record | `evidence[].archive_url`, `evidence[].archive_status`, `evidence[].to_source_id` | `evidence_links` → `sources` (`source_id`, `source_class`, `source_authority_level`) |
| Date | `evidence[].scan_date`, `evidence[].captured_at_utc` (served as `scan_date`; capture stamp held reviewer-internal) | `evidence_links` |
| Jurisdiction | `scope: "alpine"` (stamped on every feed/digest/log/overlay artifact) | `sources.scope`; `stage4_newsletter_feed.SCOPE` |
| People / entities (when verified) | `speaker_label` (statement), `to_source_id`/`relation` (evidence) | `statements.speaker_attribution_id`; `evidence_links` |
| Confidence / status | `confidence`, `confidence_label`, `verification_status`, `provenance_status`, `ui_status`, `publication_state` | `statements` + `evidence_links` |
| Correction state | `correction_status`, `updates_statement_id`, `source_changed` | `statements` (and `evidence_links.correction_status`) |
| Exact pointer (the binding itself) | `evidence[].locator_kind` + `page`/`section`/`paragraph`/`timestamp_*`/`char_start`/`char_end`/`quoted_text` | `evidence_links`, validated by `statements.LOCATOR_REQUIRED_FIELDS` |

**Pointer-completeness contract** (`statements.LOCATOR_REQUIRED_FIELDS`, what makes a pointer "exact"):

```
timestamp → (timestamp_seconds, timestamp_human)
page      → (page,)
section   → (section,)
paragraph → (paragraph,)
char_span → (char_start, char_end, quoted_text)   # offsets half-open, length == len(quoted_text)
```

**No-leak boundary (audit safety):** the raw private locators the corpus plants on every evidence link
(`transcript_path = /Users/IA/Obsidian Vault/Source-Data/raw.txt`, `deep_link = /Users/IA/Raw-PDFs/...`) are **absent**
from the served surface — verified by scanning the full served blob for `transcript_path`, `deep_link`, `Source-Data`,
`Raw-PDFs`, `/Users/IA` (all `False`). Every emitted artifact is swept by `read_api.assert_no_raw_paths`; a raw vault
path / `file://` / `.sha256` leak fails **loudly** at the boundary rather than reaching an auditor's screen.

---

## 3. AC3 — Assertions lacking exact-source evidence are flagged not-publishable and routed

The flag-and-route mechanism is **load-bearing**, proven by a poison probe rather than asserted. Strip the only exact
pointer from one assertion (`stmt-3`: `page → NULL`, `segment_id → NULL`) and re-run the chain:

- **Held + routed, never silently dropped** — the validation log emits a routing entry:
  ```json
  {"itemId": "alpine-newsletter-item-003", "reason": "no_exact_source_pointer",
   "routedTo": "VerificationSafetyReviewer", "statementId": "stmt-3", "status": "held"}
  ```
  and the row carries `resolves: false`, `pointerKind: null`, `route: "VerificationSafetyReviewer"`.
- **Publishability guard fails closed** — `--check` exits **non-zero** with
  `OrphanStatementError: 1 statement-bearing digest item(s) carry no exact-source pointer (orphan claims): ['alpine-newsletter-item-003']`.
- **Audit overlay flips the exit gate** — `all_bound: false`, `orphan_count: 1`, `bound_count: 6` (of 7).

**Semantics worth recording for reviewers:** the log's `passed` field means *no **unrouted** orphan* — a held+routed
orphan is the **safe** outcome of the routing net, so `passed` stays `true` while the orphan is correctly quarantined.
The stricter publishability decision ("is this assertion clear to publish?") is the separate `assert_every_statement_bound`
guard, which is what goes RED and blocks exit. The two are intentionally distinct: routing must never fail just because
an unbound claim exists; *publication* must.

**Follow-up routing owner:** any flagged orphan routes to **VerificationSafetyReviewer** (`stage4_newsletter_feed.VSR`)
with `status: held`. No code change is needed to "route follow-up" — the held entry *is* the follow-up handoff; VSR owns
re-binding or rejecting the claim before it can leave reviewer-internal.

---

## 4. AC4 — Stage 4 exit evidence: traceability / audit readiness

**Audit overlay over the clean representative corpus** (`--artifact overlay --check`, exit `0`):

```json
{
  "access": "reviewer_internal",
  "scope": "alpine",
  "statement_item_count": 7,
  "bound_count": 7,
  "orphan_count": 0,
  "all_bound": true,
  "no_unrouted_orphans": true,
  "labels_conservative": true,
  "verbatim_anchored": true,
  "binding_digest": "4baa0de328a529b791373fd30a399d47cc624517d0b95e8949f03d6c6391c68f",
  "violations": {"orphans": [], "routing": [], "labels": [], "verbatim": []}
}
```

**Regression suite** — the chain's own tests are green at HEAD `8c581ef`:

```
tests/test_stage4_statement_evidence_binding.py
tests/test_stage4_newsletter_digest_assembler.py
tests/test_stage4_newsletter_preservation_audit.py
tests/test_gov449_newsletter_feed.py
tests/test_statements_evidence.py  tests/test_read_api.py  tests/test_publication.py
→ 126 passed
```

**Reproducibility:** the binding validation log is byte-identical across rebuilds (`assert_reproducible`); same DB →
same `binding_digest`. This makes the audit trail itself reproducible — a reviewer can re-derive the exact fingerprint.

### Exit-readiness verdict (CTO leg)

| Acceptance criterion | Status | Evidence |
|---|---|---|
| AC1 — digest → source → pointer chain for representative assertions | ✅ Met | §1 — 7/7 assertions resolve (page + char_span); labels independently re-checked |
| AC2 — required metadata documented with provenance | ✅ Met | §2 — field→column table grounded in live served surface; no-leak verified |
| AC3 — unsourced assertions flagged not-publishable + routed | ✅ Met | §3 — poison probe: held + routed to VSR, `--check` fails closed, overlay flips |
| AC4 — Stage 4 exit evidence for traceability/audit readiness | ✅ Met | §4 — clean overlay, 126 tests green, reproducible `binding_digest` |

**CTO recommendation:** the Stage 4 reviewer-internal newsletter evidence chain is **traceability/audit-ready** for the
reviewer-internal Alpine beta. Every representative briefing assertion is traceable to an exact source; required audit
metadata is present and raw paths are stripped at the boundary; assertions that lose their exact source are held and
routed to VSR, never silently dropped, and the publishability guard fails closed.

**Scope guard — this leg certifies the backbone only.** It does **not** approve public launch, editorial prose, AI
statements, or any non-Alpine expansion. Public publication stays GOV-420 / Isaac-gated. The Stage 4 exit + Stage 5
unlock decision (GOV-475) remains with the CEO/Isaac, pending the parallel VSR (GOV-472) and SecPriv (GOV-473) closeout
legs alongside this one.
