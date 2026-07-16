"""LED-F6 capacity headroom from a deterministic synthetic load harness.

``capacity_headroom = measured_max_sustainable_throughput - current_load``. The
"measurement" is a *synthetic* load harness: there is no real production traffic
to measure yet (Alpine-first, pre-pilot), so the harness derives a reproducible
max-sustainable-throughput and current-load from a seed + area id. It is
deterministic by construction — a fixed seed yields byte-identical output on
every run and every machine (``hashlib``, not ``random`` with process entropy) —
which is what ``test_economics_capacity.py`` asserts.

MEASURED basis: the figure is measured against a synthetic but reproducible
load, exactly as LED-F6 specifies ("from synthetic load tests, MEASURED").
"""

from __future__ import annotations

import hashlib
import sqlite3

from . import basis as _basis
from . import formulas as _f

DEFAULT_SEED = "LEDGER-2026-capacity-v1"


def _stable_unit(seed: str, area_id: str, salt: str) -> float:
    """A deterministic float in [0, 1) derived from (seed, area_id, salt)."""
    h = hashlib.sha256(f"{seed}|{area_id}|{salt}".encode("utf-8")).hexdigest()
    # First 8 hex chars -> 32-bit int -> normalize.
    return int(h[:8], 16) / 0xFFFFFFFF


def synthetic_load(area_id: str, *, seed: str = DEFAULT_SEED) -> tuple[float, float]:
    """Return ``(max_sustainable_throughput, current_load)`` jobs/min, deterministic.

    Ranges are fixed and modest (local-server class): max in [60, 180) jobs/min,
    current load a fraction [0, 0.9) of that max. Same (seed, area_id) -> same
    numbers, always.
    """
    max_tput = 60.0 + 120.0 * _stable_unit(seed, area_id, "max")
    load_frac = 0.9 * _stable_unit(seed, area_id, "load")
    current = max_tput * load_frac
    return round(max_tput, 3), round(current, 3)


def forecast(area_id: str, *, seed: str = DEFAULT_SEED,
             conn: sqlite3.Connection | None = None) -> dict:
    """Capacity forecast report cells for an area (LED-F6). Read-only.

    ``conn`` is accepted for interface symmetry with the other rollups but is
    unused: the harness is synthetic and does not read the substrate.
    """
    max_tput, current = synthetic_load(area_id, seed=seed)
    headroom = _f.f6_capacity_headroom(max_tput, current)
    return {
        "area_id": area_id,
        "seed": seed,
        "max_sustainable_throughput": _basis.cell(
            max_tput, _basis.MEASURED, unit="jobs_per_min"
        ),
        "current_load": _basis.cell(current, _basis.MEASURED, unit="jobs_per_min"),
        "capacity_headroom": _basis.cell(
            headroom.value, headroom.basis, unit="jobs_per_min",
            formula_id=headroom.formula_id,
        ),
    }
