"""GOV-1712 (C4): `speakers.safe_speaker_label` had no tests at all.

It is the **write-side** naming guard — the function that decides what string
lands in `speaker_attributions.display_label`. Its docstring makes the strongest
claim in this area:

    "The renderable speaker label — provably name-free unless safely attributed."

Measured 2026-08-02: `grep -c safe_speaker_label tests/` returned **nothing**.
The compensating *read*-side control is well covered — `test_gov290_speaker_label.py`
has `test_fail_closed_name_poison_on_non_attributed_row_is_not_leaked`, and
`read_api` re-derives the label for any non-attributed row rather than trusting
storage, explicitly because it "could hold a name poisoned in past the write
guard". So the system already documents that this guard is bypassable, tests the
compensation, and never tested the guard.

Stage 1.09 step 9 is the rule being protected: `attribution_state == attributed`
only via official records + review, otherwise a generic label, and naming an
`on-record-public` speaker is a **CEO hard stop**. "No name > wrong name."
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import speakers as sp  # noqa: E402

_NAME = "Pat Maxwell"


class TestSafeSpeakerLabelNamesOnlyWhenPermitted:
    """The paths that are correct today, pinned so they stay that way."""

    def test_attributed_official_renders_name_and_role(self):
        assert sp.safe_speaker_label({
            "attribution_state": "attributed", "speaker_class": "on-record-official",
            "display_name": _NAME, "role_title": "Mayor"}) == f"{_NAME}, Mayor"

    def test_attributed_official_without_role_renders_name_alone(self):
        assert sp.safe_speaker_label({
            "attribution_state": "attributed", "speaker_class": "on-record-official",
            "display_name": _NAME}) == _NAME

    def test_attributed_PUBLIC_speaker_is_never_named(self):
        """The CEO hard stop (1.07 §3): `on-record-public` may not be named at all.

        Note this holds even at `attributed` with a resolved `display_name` —
        `AUTO_NAMEABLE_CLASSES` is exactly `{'on-record-official'}`, so the naming
        branch is not entered. This is the single most consequential line in the
        function and it was untested.
        """
        assert sp.safe_speaker_label({
            "attribution_state": "attributed", "speaker_class": "on-record-public",
            "display_name": _NAME, "role_title": "Resident"}) == sp.SAFE_COMMUNITY_LABEL

    @pytest.mark.parametrize("state", ["uncertain", "unattributed", None, "attributed"])
    def test_a_candidate_name_is_never_consulted(self, state):
        """`candidate_person_id` is the reviewer-only field; it must not render."""
        label = sp.safe_speaker_label({
            "attribution_state": state, "speaker_class": "unidentified",
            "candidate_person_id": "person:pat-maxwell", "display_name": _NAME})
        assert _NAME not in label, (
            f"state={state!r}: a candidate/display name reached the label on a "
            f"non-nameable class: {label!r}")

    def test_unattributed_public_gets_the_community_label(self):
        assert sp.safe_speaker_label({
            "attribution_state": "unattributed",
            "speaker_class": "on-record-public"}) == sp.SAFE_COMMUNITY_LABEL

    def test_everything_else_falls_through_to_the_generic_label(self):
        assert sp.safe_speaker_label({}) == sp.SAFE_GENERIC_LABEL
        assert sp.safe_speaker_label({
            "attribution_state": "uncertain",
            "speaker_class": "unidentified"}) == sp.SAFE_GENERIC_LABEL

    def test_a_name_free_role_label_is_passed_through(self):
        """Non-vacuity for the xfail below: the role-only path DOES render text.

        Without this, the failing test could be read as "role_only_label is
        ignored", which would be a different (and safe) implementation.
        """
        assert sp.safe_speaker_label({
            "attribution_state": "uncertain", "speaker_class": "unidentified",
            "role_only_label": "Council Member, Town of Alpine",
        }) == "Council Member, Town of Alpine"


@pytest.mark.xfail(strict=True, reason=(
    "role_only_label is returned verbatim with no name-free validation, so a "
    "caller-supplied name renders on a non-attributed row. ai_extraction passes "
    "the AI proposer's value straight through. Contained at the serving surface "
    "by read_api's re-derivation (GOV-290), NOT by this function. Remove this "
    "marker when the write-side guard validates the label."))
def test_role_only_label_cannot_smuggle_a_name_onto_an_unattributed_row():
    """The docstring's claim, asserted: "provably name-free unless safely attributed".

    Measured today: `role_only_label="Pat Maxwell, Mayor"` on an `uncertain` row
    returns `"Pat Maxwell, Mayor"`. The field is named *role_only* and the code
    comment beside it says "Prefer an explicit **name-free** role-only label" —
    but nothing checks that it is name-free, and `ai_extraction.py:318` forwards
    the AI proposer's value unvalidated.

    Deliberately left failing rather than fixed here: deciding what counts as a
    name is a real design question with a real false-positive cost ("Mayor" vs
    "Pat Maxwell"), and it is a behaviour change on a naming-safety boundary.
    """
    label = sp.safe_speaker_label({
        "attribution_state": "uncertain", "speaker_class": "unidentified",
        "role_only_label": f"{_NAME}, Mayor"})
    assert _NAME not in label, (
        f"a caller-supplied name rendered on an unattributed row: {label!r}")
