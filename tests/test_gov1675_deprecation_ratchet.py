"""GOV-1675 (AUTO GO C6): the DeprecationWarning ratchet must keep covering us.

The ratchet itself is proven by mutation (break something, watch CI go red) —
that cannot be asserted from inside the suite it would fail. What CAN rot
silently, and is therefore worth pinning, is the *derivation* of the module
list in ``conftest._first_party_module_pattern``.

It reads ``scripts/`` at run time precisely so a module added later is covered
without anyone remembering to update a list. If that glob is ever narrowed —
dropped package branch, a stricter pattern, a refactor that moves code — the
ratchet would keep passing while quietly protecting less. That is the
"property holds by absence" shape: nothing fails, coverage just shrinks.
"""
from __future__ import annotations

import re

import pytest
from conftest import ROOT, _first_party_module_pattern


def _matches(pattern: str, module_name: str) -> bool:
    return re.match(pattern + "$", module_name) is not None


def test_every_top_level_module_under_scripts_is_covered():
    pattern = _first_party_module_pattern()
    scripts = ROOT / "scripts"
    expected = {p.stem for p in scripts.glob("*.py") if not p.stem.startswith("_")}
    expected |= {d.name for d in scripts.iterdir()
                 if d.is_dir() and (d / "__init__.py").exists()}
    assert expected, "no first-party modules discovered — the glob is broken"
    uncovered = sorted(n for n in expected if not _matches(pattern, n))
    assert not uncovered, f"first-party modules outside the ratchet: {uncovered}"


def test_submodules_are_covered_not_just_top_level_names():
    """`accounts.service` must match, not merely `accounts`."""
    pattern = _first_party_module_pattern()
    assert _matches(pattern, "accounts")
    assert _matches(pattern, "accounts.service")
    assert _matches(pattern, "beta.provision")
    assert _matches(pattern, "db"), "bare top-level modules must be covered too"


@pytest.mark.parametrize("third_party", [
    "urllib3.connectionpool", "bs4.builder", "yt_dlp.extractor",
    "cryptography.hazmat", "requests.adapters", "argon2.low_level",
])
def test_third_party_modules_are_NOT_ratcheted(third_party):
    """The constraint pytest.ini set deliberately, kept enforceable.

    `pytest.ini` refused a blanket `-W error` because it "would fail on
    third-party deprecations outside this repo's control". If this ever starts
    matching, CI becomes hostage to someone else's release schedule.
    """
    assert not _matches(_first_party_module_pattern(), third_party)


def test_the_pattern_is_actually_installed_as_a_filter(pytestconfig):
    """A derived pattern nobody installs protects nothing.

    Deriving the right regex and forgetting to register it would leave every
    test above green while the suite ratcheted nothing at all.
    """
    prefix = "error::DeprecationWarning:"
    ratchets = [f for f in pytestconfig.getini("filterwarnings")
                if f.startswith(prefix)]
    assert len(ratchets) == 1, f"expected exactly one ratchet, got {ratchets}"
    installed = ratchets[0][len(prefix):]
    assert _matches(installed, "accounts.service"), \
        "the INSTALLED filter does not cover a first-party module"
    assert not _matches(installed, "urllib3.connectionpool"), \
        "the INSTALLED filter would error on third-party deprecations"
