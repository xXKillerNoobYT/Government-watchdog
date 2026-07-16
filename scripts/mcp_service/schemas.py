"""JSON-Schema registry + a tiny fail-closed validator (CONTRACT-2026-MCP §3.2).

Every tool request/response and resource payload is validated against a schema
registered under a stable ``{schema_id, semver}`` key. Two hard rules, both
required by the plan:

* **No third-party dependency (D1).** We do not import ``jsonschema``. The
  validator below implements only the keyword subset the contract schemas use
  (``type``/``properties``/``required``/``enum``/``items``/``minimum``/
  ``minItems``) — small, auditable, and offline.
* **Fail-closed unknown fields.** Objects are validated as if
  ``additionalProperties: false`` unless a schema explicitly opts in. A field
  the schema does not list is a rejection (``denied:schema``), never a silent
  pass-through — this is what stops an un-allowlisted column from riding across
  the boundary at the *shape* layer, complementing the redaction scan.

Version pinning is caller-visible: a schema is looked up by exact
``(schema_id, semver)``; there is no "latest" resolution.
"""

from __future__ import annotations

from typing import Any

from .errors import DENY_SCHEMA, MCPDenied

# schema_id -> {semver -> schema dict}
_REGISTRY: dict[str, dict[str, dict[str, Any]]] = {}


class SchemaError(ValueError):
    """A schema definition (not the data) is malformed — a programming error."""


def register(schema_id: str, semver: str, schema: dict[str, Any]) -> None:
    """Register ``schema`` under ``(schema_id, semver)`` (write-once per pair)."""
    if not isinstance(schema, dict):
        raise SchemaError("schema must be a dict")
    versions = _REGISTRY.setdefault(schema_id, {})
    if semver in versions and versions[semver] != schema:
        raise SchemaError(f"schema {schema_id}@{semver} already registered differently")
    versions[semver] = schema


def get(schema_id: str, semver: str) -> dict[str, Any]:
    try:
        return _REGISTRY[schema_id][semver]
    except KeyError:
        # An unknown schema id/version is a fail-closed denial, not a 500.
        raise MCPDenied(DENY_SCHEMA, f"no registered schema {schema_id}@{semver}")


def registered_ids() -> dict[str, list[str]]:
    """``{schema_id: [semver, ...]}`` — introspection for tests/audit."""
    return {sid: sorted(v) for sid, v in _REGISTRY.items()}


# ---------------------------------------------------------------------------
# Minimal validator. Raises MCPDenied(denied:schema) on ANY mismatch.
# ---------------------------------------------------------------------------

_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def _fail(path: str, msg: str) -> None:
    raise MCPDenied(DENY_SCHEMA, f"{path or '<root>'}: {msg}")


def _check(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = schema.get("type")
    if expected is not None:
        types = _TYPE_MAP.get(expected)
        if types is None:
            raise SchemaError(f"unsupported type {expected!r} in schema")
        # bool is a subclass of int in Python; keep them distinct so a boolean is
        # not silently accepted where an integer is required (and vice versa).
        if expected in ("integer", "number") and isinstance(value, bool):
            _fail(path, f"expected {expected}, got boolean")
        if not isinstance(value, types):
            _fail(path, f"expected {expected}, got {type(value).__name__}")

    if "enum" in schema and value not in schema["enum"]:
        _fail(path, f"value {value!r} not in enum")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            _fail(path, f"{value} < minimum {schema['minimum']}")

    if isinstance(value, dict):
        _check_object(value, schema, path)
    elif isinstance(value, list):
        _check_array(value, schema, path)


def _check_object(value: dict[str, Any], schema: dict[str, Any], path: str) -> None:
    props = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in value:
            _fail(path, f"missing required field {req!r}")
    # Fail-closed on unknown fields unless explicitly opted in.
    if not schema.get("additionalProperties", False):
        for key in value:
            if key not in props:
                _fail(path, f"unknown field {key!r} (additionalProperties denied)")
    for key, sub in props.items():
        if key in value:
            _check(value[key], sub, f"{path}.{key}" if path else key)


def _check_array(value: list[Any], schema: dict[str, Any], path: str) -> None:
    if "minItems" in schema and len(value) < schema["minItems"]:
        _fail(path, f"len {len(value)} < minItems {schema['minItems']}")
    items = schema.get("items")
    if isinstance(items, dict):
        for i, item in enumerate(value):
            _check(item, items, f"{path}[{i}]")


def validate(value: Any, schema_id: str, semver: str) -> Any:
    """Validate ``value`` against the registered schema; return it unchanged.

    Raises :class:`MCPDenied` (``denied:schema``) on any violation, so it can be
    used inline as a fail-closed gate on both requests and responses.
    """
    _check(value, get(schema_id, semver), "")
    return value
