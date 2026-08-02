"""GOV-1715 (C8 hunt): the Lane-2 -> Lane-4 PII hand-off is not symmetric.

`ai_extraction._assert_claim_pii_free` guards **only** `evidence_link.quoted_text`,
and its docstring says so deliberately::

    SCOPING (deliberate, flagged to VSR/SecurityPrivacy): this guards the NEW
    verbatim field only. It does NOT guard the paraphrased ``statement_text`` — a
    paraphrase that surfaces private PII remains the domain of the Lane-4 RISK
    layer (``ai_risk_gate`` privacy flag + Lane-5 block), the established contract.

That is a **compensating control**: Lane 2 declines to hard-drop, on the stated
grounds that Lane 4 will flag it instead. This module checks whether the receiving
guard actually covers what the hand-off assigns it.

Measured 2026-08-02 — both sides name five PII kinds and the names line up, but
the `voter/registration` patterns differ in reach:

    Lane-2  \\b(?:voter|registration|reg|sos)\\b[ .]*(?:id|no\\.?|num(?:ber)?|#)\\b...
    Lane-4  \\bvoter\\s+(?:registration|roll|id|file)\\b

Lane-4 requires the literal word **voter**. So `Registration ID 88213345`,
`Reg No. 55231` and `SOS No. AB-99120` are hard-dropped from verbatim text and
carry **no privacy flag at all** in a paraphrase — no flag means no Lane-5 block,
so nothing in the chain objects.

**`scripts/ai_risk_gate.py` is byte-frozen**, so the pattern cannot be widened
here. The parity assertion is `xfail(strict=True)`: it XPASSes the moment someone
widens Lane-4 (or unfreezes and fixes it), which fails the suite and says to drop
the marker. Filed for the owner rather than worked around.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ai_risk_gate as rg  # noqa: E402
import concept_map as cm  # noqa: E402

#: Phrasings Lane-2 hard-drops from `quoted_text`. Each is a realistic way a
#: meeting record refers to a registration identifier.
_LANE2_DROPS = (
    ("email", "Write to jane.doe@example.com."),
    ("ssn", "His SSN is 123-45-6789."),
    ("phone", "Call 307-555-0199."),
    ("street_address", "She lives at 145 Cedar Street."),
    ("voter id", "Voter ID 4471902 was challenged."),
    ("registration id", "Registration ID 88213345 appears on the roll."),
    ("reg no", "Reg No. 55231 is on file."),
    ("sos no", "SOS No. AB-99120 was cited."),
)


def _lane2_drops(text: str) -> bool:
    try:
        cm.assert_no_pii(text, "quoted_text")
    except cm.PiiGuardError:
        return True
    return False


def _lane4_flags(text: str) -> bool:
    return any(f["category"] == "privacy" for f in rg.scan_text(text))


@pytest.mark.parametrize("kind,text", _LANE2_DROPS, ids=[k for k, _ in _LANE2_DROPS])
def test_lane2_still_hard_drops_every_pii_phrasing(kind, text):
    """Non-vacuity for the parity test below.

    If Lane-2 stopped dropping these, the parity assertion would pass for the
    wrong reason — nothing to be asymmetric *about*.
    """
    assert _lane2_drops(text), (
        f"Lane-2 no longer rejects {kind!r} in quoted_text: {text!r}. That is a "
        "bigger regression than the asymmetry this file tracks.")


@pytest.mark.parametrize(
    "kind,text",
    [c for c in _LANE2_DROPS if c[0] in {"email", "ssn", "phone", "street_address", "voter id"}],
    ids=["email", "ssn", "phone", "street_address", "voter-id"])
def test_the_handoff_holds_for_the_kinds_it_currently_covers(kind, text):
    """Five of eight phrasings ARE symmetric. Pinned so they stay that way."""
    assert _lane4_flags(text), (
        f"Lane-4 stopped flagging {kind!r} as privacy: {text!r}. Lane-2 does not "
        "scan statement_text, so this phrasing now passes BOTH guards.")


@pytest.mark.xfail(strict=True, reason=(
    "ai_risk_gate._PRIVACY_PATTERNS['voter_data'] requires the literal word "
    "'voter', while concept_map's equivalent also matches registration/reg/sos. "
    "Those phrasings are hard-dropped from quoted_text and unflagged in "
    "statement_text. ai_risk_gate.py is byte-frozen so it cannot be widened here. "
    "Remove this marker when Lane-4's pattern covers them."))
@pytest.mark.parametrize(
    "kind,text",
    [c for c in _LANE2_DROPS if c[0] in {"registration id", "reg no", "sos no"}],
    ids=["registration-id", "reg-no", "sos-no"])
def test_every_kind_lane2_drops_is_at_least_FLAGGED_by_lane4(kind, text):
    """The hand-off's own premise: what Lane 2 declines to drop, Lane 4 flags.

    Lane 2 chose not to hard-drop `statement_text` specifically so the risk lane
    could flag it for a reviewer instead. For these phrasings the risk lane does
    not flag, so the claim reaches a reviewer with nothing marking it — the
    hand-off's premise does not hold.
    """
    assert _lane4_flags(text), (
        f"{kind!r} is dropped by Lane-2 in verbatim text but carries NO privacy "
        f"flag in a paraphrase: {text!r}. No flag means no Lane-5 block.")
