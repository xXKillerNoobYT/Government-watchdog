"""GOV-1707 (C1b): the reviewer gate is the sole promotion path — now enforced.

The Stage 1.09 boundary matrix (`docs/stage1-automation-ai-boundary-matrix-contract.md`,
step 11) assigns `verificationStatus` assignment to **HUM**, and puts this in the
MUST NOT column:

> Let AI or a script set a **reviewed** status; auto-promote on confidence score.

`ai_risk_gate.promote_statement` implements that gate properly — allowlist,
default-deny, fail-closed, `ReviewerGateError` before any write — and its docstring
calls itself *"the ONLY sanctioned promotion path"*. Measured 2026-08-01: **that
claim is true.** `UPDATE statements SET verification_status` occurs exactly once in
all 160 modules, at `ai_risk_gate.py:765`, inside that function. Every other
`UPDATE statements` in the tree touches non-review columns (`agenda_item_id`,
`speaker_attribution_id`).

**What was missing is any guard on the EXCLUSIVITY.** The gate's own contents are
protected — `ai_risk_gate.py` is byte-frozen against `origin/main`. But a freeze
protects a file from being *edited*; it does nothing about a **second** writer
appearing somewhere else. That is the reachable threat, and it is the same shape
this repo has now hit three times:

- `WEB_SAFE_DIFF_FIELDS` — a denylist whose exclusivity nothing checked (GOV-1705);
- `file_read_api` — "the sole Backend→Website crossing", asserted only in prose (W-7);
- here — "the ONLY sanctioned promotion path", asserted only in a docstring.

A sole-path claim that lives in a docstring is a comment. This makes it a test.

**Why the pattern is spelled the way it is.** It matches the SQL, not the helper
name, because a bypass would not call the helper — that is what makes it a bypass.
A first attempt matched `SET\\s+verification_status` and found **zero** rows in a
tree that demonstrably contains one, which would have shipped a guard that could
never fire; the working pattern was derived from the real statement text.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

#: The one module permitted to write review fields on `statements`.
SANCTIONED_WRITER = "ai_risk_gate.py"

#: Columns that carry a reviewer's decision. Writing any of them IS a promotion,
#: whatever the code around it is called.
REVIEW_COLUMNS = ("verification_status", "review_state", "ui_status")

#: An `UPDATE statements SET ...` naming a review column. Deliberately tolerant of
#: whitespace and of the statement being split across adjacent string literals,
#: which is how the sanctioned one is actually written.
_UPDATE_STATEMENTS = re.compile(
    r"UPDATE\s+statements\s+SET\s+(?P<cols>(?:[^\"';]|\"\s*\n\s*\")*)", re.I)


def _review_field_writers() -> dict[str, list[int]]:
    """{module filename: [line numbers]} for every review-field UPDATE."""
    found: dict[str, list[int]] = {}
    for path in sorted(SCRIPTS.rglob("*.py")):
        source = path.read_text(encoding="utf-8", errors="ignore")
        for m in _UPDATE_STATEMENTS.finditer(source):
            cols = m.group("cols")
            if any(c in cols for c in REVIEW_COLUMNS):
                line = source[:m.start()].count("\n") + 1
                found.setdefault(path.name, []).append(line)
    return found


def test_the_pattern_actually_finds_the_sanctioned_writer():
    """Non-vacuity. A pattern matching nothing would make the guard below pass forever.

    This is not hypothetical: the first version of this pattern found **zero**
    matches across a tree that contains one, and would have shipped green.
    """
    writers = _review_field_writers()
    assert SANCTIONED_WRITER in writers, (
        f"the pattern did not find the known review-field UPDATE in "
        f"{SANCTIONED_WRITER} (found: {sorted(writers)}). Either the sanctioned "
        "statement was rewritten in a shape the regex no longer matches — in which "
        "case this guard is inert and must be re-derived from the real SQL — or the "
        "gate itself moved.")


def test_only_the_reviewer_gate_writes_review_fields_on_statements():
    """Step 11's MUST NOT, enforced instead of described.

    A second writer means a claim can reach a reviewed status without passing
    `promote_statement`'s allowlist — no reviewer identity, no audit row, no
    failed-run block. The boundary matrix calls that outcome out by name.
    """
    writers = _review_field_writers()
    unsanctioned = {k: v for k, v in writers.items() if k != SANCTIONED_WRITER}
    assert not unsanctioned, (
        f"these modules write reviewer-decision fields on `statements` without "
        f"going through ai_risk_gate.promote_statement: {unsanctioned}. That "
        "bypasses the registered-reviewer allowlist, the DECISION_TABLE audit row, "
        "and the failed-run downstream block — so a claim can reach a reviewed "
        "status with no human behind it, which Stage 1.09 step 11 lists as a MUST "
        "NOT. Route the write through promote_statement, or if this genuinely is a "
        "second sanctioned path, say so in the boundary matrix first and then here.")


def test_the_sanctioned_writer_still_requires_a_registered_reviewer():
    """Guards the gate's *character*, not just its uniqueness.

    Exclusivity is worthless if the sole path stops checking. `ai_risk_gate.py` is
    byte-frozen, so this cannot regress by edit today — but the freeze is a policy,
    not a language feature, and it is exactly the kind of thing that gets lifted
    (see #229) without anyone re-reading what it was holding still.
    """
    source = (SCRIPTS / SANCTIONED_WRITER).read_text(encoding="utf-8")
    assert "def promote_statement(" in source, "the sole promotion path is gone"
    assert "is_registered_reviewer" in source, (
        "promote_statement no longer consults the reviewer allowlist — the sole "
        "promotion path stopped being a gate")
    assert "ReviewerGateError" in source, (
        "promote_statement no longer raises on rejection; a gate that returns "
        "instead of raising is one ignored return value away from being no gate")
