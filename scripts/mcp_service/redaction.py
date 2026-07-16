"""The single response choke-point (CONTRACT-2026-MCP §3, D2).

Every serialized payload leaving the boundary passes through :func:`assert_clean`
BEFORE it is returned or audited. This module owns NO redaction logic of its own
— it *imports* the two frozen, reviewed scanners so there is exactly one copy of
each in the codebase and neither can drift:

* ``read_api.assert_no_raw_paths`` — rejects absolute/filesystem paths and the
  raw-marker set (vault paths, ``.sha256``, ``transcript_path``, …). Public
  ``http(s)://`` URLs are exempt.
* ``ai_risk_gate.scan_text`` — deterministic privacy/legal/moderation screen; a
  non-empty finding list means the text carries something a reviewer must clear.

A hit on either scanner is a fail-closed ``denied:redaction`` — the payload is
never returned. This is defense-in-depth behind the deny-by-default field
allowlist (:mod:`.allowlists`, D3): the allowlist strips raw *columns*
structurally; this scan catches a raw marker embedded in an allowlisted *value*.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# The frozen serving surface lives in the parent scripts/ dir. Import (never
# copy) the reviewed scanners; byte-0 diff on those modules is an acceptance gate.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import ai_risk_gate as _gate  # noqa: E402  (frozen — imported, not modified)
import read_api as _read_api  # noqa: E402  (frozen — imported, not modified)

from .errors import DENY_REDACTION, MCPDenied  # noqa: E402

RawPathLeak = _read_api.RawPathLeak


def _iter_strings(obj: Any):
    """Yield every string (keys + values, nested) in a JSON-ish structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_strings(item)


def scan_findings(body: Any) -> list[dict[str, Any]]:
    """Return every privacy/legal/moderation finding across ``body`` (may be empty)."""
    findings: list[dict[str, Any]] = []
    for text in _iter_strings(body):
        findings.extend(_gate.scan_text(text))
    return findings


def assert_clean(body: Any) -> Any:
    """Run both frozen scanners over ``body``; return it unchanged if clean.

    Raises :class:`MCPDenied` (``denied:redaction``) on the first leak — a raw
    path/marker or any PII/legal/moderation finding. Fail-closed: an unexpected
    scanner error is also treated as a denial, never a pass.
    """
    try:
        _read_api.assert_no_raw_paths(body)
        findings = scan_findings(body)
    except RawPathLeak as exc:
        raise MCPDenied(DENY_REDACTION, f"raw path/marker in payload: {exc}")
    except MCPDenied:
        raise
    except Exception as exc:  # noqa: BLE001 — any scanner failure fails closed.
        raise MCPDenied(DENY_REDACTION, f"redaction scan error: {exc}")
    if findings:
        cats = sorted({f.get("category", "?") for f in findings})
        raise MCPDenied(DENY_REDACTION, f"content finding(s) {cats} in payload")
    return body
