"""GOV-1716 (C9 hunt): the email PII pattern was quadratic on a long token.

`concept_map.assert_no_pii` runs at the Lane-2 AI write boundary on every
proposed claim's `evidence_link.quoted_text` — a span copied **verbatim from a
source document**. So its input is whatever the source contains: a base64 blob, a
long URL, an OCR run of digits, a table with no spaces.

Measured 2026-08-02, before the fix, on `"a" * n`:

    n=  6250      67 ms
    n= 12500     195 ms   (x2.9)
    n= 25000     713 ms   (x3.7)
    n= 50000    2800 ms   (x3.9)   <- ~x4 per doubling: QUADRATIC

Isolated to one pattern. At n=25000: email **698 ms**, every other pattern
<= 1.3 ms. Its local part `[A-Za-z0-9._%+-]+` matches greedily from every start
position and then fails to find `@`.

The fix is a cheap literal prerequisite, and it is **provably** semantics-
preserving rather than a heuristic: the pattern contains a literal `@`, so a value
without `@` cannot match it. 3000 ms -> 3.8 ms.

**Residual, named not hidden:** `"a" * 25000 + "@"` still costs ~800 ms. Bounding
the local part to `{1,64}` (RFC 5321's cap) would fix it, but that narrows what
the guard detects — a security call for the owner, filed separately.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import concept_map as cm  # noqa: E402

#: Generous vs the 3.0s measured before the fix and the ~4ms after; the point is
#: to catch a return to quadratic, not to police milliseconds on a busy machine.
_BUDGET_MS = 250


def _elapsed_ms(value: str) -> float:
    start = time.perf_counter()
    try:
        cm.assert_no_pii(value, "quoted_text")
    except cm.PiiGuardError:
        pass
    return (time.perf_counter() - start) * 1000


class TestTheShortCircuitMapIsCorrect:
    """The dangerous half of the fix: a wrong entry silently disables a PII check."""

    def test_every_shortcircuit_literal_really_is_required_by_its_pattern(self):
        """If a listed literal is not in the pattern source, the skip is unsound."""
        by_kind = dict(cm._PII_PATTERNS)
        for kind, literal in cm._PII_REQUIRED_LITERAL.items():
            assert kind in by_kind, (
                f"_PII_REQUIRED_LITERAL names {kind!r}, which is not a real "
                "pattern kind — the skip would never fire, or worse, a rename "
                "left it pointing at nothing")
            assert literal in by_kind[kind].pattern, (
                f"{kind!r} is skipped when {literal!r} is absent, but that "
                f"literal does not appear in its pattern "
                f"{by_kind[kind].pattern!r}. The pattern could match without it, "
                "so the skip silently disables this PII check.")

    def test_a_value_lacking_the_literal_genuinely_cannot_match(self):
        """Behavioural check of the same claim, not just a source-text check."""
        by_kind = dict(cm._PII_PATTERNS)
        for kind, literal in cm._PII_REQUIRED_LITERAL.items():
            for probe in ("a" * 200, "no marker here at all", "1234567890" * 20):
                assert literal not in probe, "probe accidentally contains the literal"
                assert not by_kind[kind].search(probe), (
                    f"{kind!r} matched {probe[:30]!r} which lacks {literal!r} — "
                    "the skip is unsound and would drop a real detection")


class TestPiiDetectionIsUnchanged:
    """Non-vacuity: the fix must not have bought speed by detecting less."""

    @pytest.mark.parametrize("text", [
        "Write to jane.doe@example.com.",
        "Contact: a@b.co",
        "Call 307-555-0199.",
        "His SSN is 123-45-6789.",
        "She lives at 145 Cedar Street.",
        "Voter ID 4471902 was challenged.",
    ])
    def test_real_pii_is_still_rejected(self, text):
        with pytest.raises(cm.PiiGuardError):
            cm.assert_no_pii(text, "quoted_text")

    @pytest.mark.parametrize("text", [
        "The council discussed the treatment plant financing gap.",
        "Item 4 was continued to the next regular meeting.",
    ])
    def test_clean_civic_text_still_passes(self, text):
        cm.assert_no_pii(text, "quoted_text")


class TestNoQuadraticBlowupOnALongToken:
    """The cliff itself. A source document with one long unbroken token."""

    @pytest.mark.parametrize("filler,label", [("a", "letters"), ("1", "digits")])
    def test_a_50k_token_is_screened_quickly(self, filler, label):
        ms = _elapsed_ms(filler * 50_000)
        assert ms < _BUDGET_MS, (
            f"screening 50k {label} took {ms:.0f} ms (budget {_BUDGET_MS} ms). "
            "Before GOV-1716 this was ~3000 ms and scaled quadratically — check "
            "whether a pattern lost its _PII_REQUIRED_LITERAL entry.")

    def test_cost_does_not_quadruple_when_the_input_doubles(self):
        """Pins the SHAPE, which is what actually regressed — not a constant."""
        small = max(_elapsed_ms("a" * 25_000), 0.05)
        large = _elapsed_ms("a" * 50_000)
        assert large < small * 3, (
            f"doubling the input multiplied cost by {large / small:.1f}x "
            f"({small:.1f} -> {large:.1f} ms). Linear screening should roughly "
            "double; ~4x is the quadratic backtracking signature.")
