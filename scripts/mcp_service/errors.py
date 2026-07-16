"""Deny vocabulary for the MCP service boundary (CONTRACT-2026-MCP §3.3/§3.4).

Every rejection at the boundary is a :class:`MCPDenied` carrying one of the
enumerated ``denied:*`` error codes. The code is what lands in the
``mcp_audit_events.error_code`` column, so the audit trail is a closed set of
outcomes — never a free-text stack trace. Deny-by-default: any unexpected
condition maps to a deny, never a silent pass.
"""

from __future__ import annotations

# Closed set of denial reasons. Each maps 1:1 to an audit ``error_code``.
DENY_CAPABILITY = "denied:capability"   # expired / revoked / wrong-job / out-of-scope
DENY_SCHEMA = "denied:schema"           # request or response failed JSON-Schema validation
DENY_REDACTION = "denied:redaction"     # a leak scanner (D2) fired on the serialized payload
DENY_NOT_FOUND = "denied:not_found"     # id not authorized by / present in the job selector
DENY_BUDGET = "denied:budget"           # grant call/unit budget exhausted
DENY_UNSUPPORTED = "denied:unsupported"  # unknown tool/resource type

DENY_CODES = frozenset(
    {
        DENY_CAPABILITY,
        DENY_SCHEMA,
        DENY_REDACTION,
        DENY_NOT_FOUND,
        DENY_BUDGET,
        DENY_UNSUPPORTED,
    }
)


class MCPDenied(Exception):
    """A request was denied at the boundary. ``code`` is a ``denied:*`` constant."""

    def __init__(self, code: str, message: str = "") -> None:
        if code not in DENY_CODES:
            # Fail-closed even on a mis-constructed error: an unknown code is
            # itself a capability-class denial rather than an accidental allow.
            message = f"[unknown deny code {code!r}] {message}"
            code = DENY_CAPABILITY
        self.code = code
        super().__init__(message or code)
