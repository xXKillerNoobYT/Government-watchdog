"""Minimal stdio JSON-RPC 2.0 binding (CONTRACT-2026-MCP §2 D1).

A thin, transport-agnostic adapter that exposes the domain core MCP-style over
line-delimited stdin/stdout. **No network listener, no new third-party
dependency, no async.** Adopting the official MCP SDK later is a swap of this one
file. Every dispatch runs through :mod:`.service`, so the capability / schema /
redaction / audit guarantees hold regardless of transport.

Supported methods:
  * ``tools/list``      → public tool metadata (no auth; names + scopes only)
  * ``tools/call``      → params ``{name, arguments, job_id, token}``
  * ``resources/read``  → params ``{resource_type, resource_id, job_id, token}``
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import Any, TextIO

from . import contracts, service
from .errors import MCPDenied

# JSON-RPC error code for a boundary denial (implementation-defined range).
_DENY_CODE = -32001
_BAD_REQUEST = -32600
_METHOD_NOT_FOUND = -32601


def _ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": error}


def _tools_list() -> dict[str, Any]:
    return {
        "tools": [
            {"name": s.name, "scope": s.scope, "effect": s.effect,
             "input_schema_id": s.req_schema_id}
            for s in contracts.TOOLS.values()
        ]
    }


def handle_request(conn: sqlite3.Connection, request: Any) -> dict[str, Any]:
    """Dispatch one JSON-RPC request object; return the response object."""
    if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
        return _err(None, _BAD_REQUEST, "not a JSON-RPC 2.0 request")
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if not isinstance(params, dict):
        return _err(req_id, _BAD_REQUEST, "params must be an object")
    try:
        if method == "tools/list":
            return _ok(req_id, _tools_list())
        if method == "tools/call":
            result = service.call_tool(
                conn, params.get("name"), params.get("arguments") or {},
                params.get("token", ""), job_id=params.get("job_id", ""),
            )
            return _ok(req_id, result)
        if method == "resources/read":
            result = service.read_resource(
                conn, params.get("resource_type"), params.get("resource_id"),
                params.get("token", ""), job_id=params.get("job_id", ""),
            )
            return _ok(req_id, result)
        return _err(req_id, _METHOD_NOT_FOUND, f"unknown method {method!r}")
    except MCPDenied as denied:
        return _err(req_id, _DENY_CODE, str(denied), {"deny_code": denied.code})


def serve_stdio(conn: sqlite3.Connection, stdin: TextIO | None = None,
                stdout: TextIO | None = None) -> None:
    """Read line-delimited JSON-RPC requests from ``stdin``, write responses to
    ``stdout``. Blocks until EOF. Local-only: no socket is ever opened."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps(_err(None, _BAD_REQUEST, "invalid JSON")) + "\n")
            stdout.flush()
            continue
        response = handle_request(conn, request)
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()
