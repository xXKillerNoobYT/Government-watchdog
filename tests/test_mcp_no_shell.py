"""§6.2 no-shell guard (RED): the package exposes no exec/shell/eval tool and
statically never imports a subprocess primitive.

This is a *static* source scan, not a runtime check — it catches a dangerous
import even on a code path that never runs. The tool registry is also asserted to
contain only the six typed tools.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mcp_service import contracts

PKG = Path(__file__).resolve().parent.parent / "scripts" / "mcp_service"

FORBIDDEN_MODULES = {"subprocess", "pty", "shlex", "socket", "socketserver", "asyncio"}
FORBIDDEN_CALLS = {"system", "popen", "exec", "eval", "execve", "spawn", "fork"}


def _py_files():
    return sorted(PKG.rglob("*.py"))


def test_package_has_python_files():
    assert _py_files(), "no package files found"


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.name)
def test_no_forbidden_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_MODULES, (
                    f"{path.name} imports forbidden module {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in FORBIDDEN_MODULES, (
                f"{path.name} imports from forbidden module {node.module!r}")


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.name)
def test_no_os_system_or_eval_calls(path):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # os.system(...) / os.popen(...) / subprocess.*  → attribute calls
            if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_CALLS:
                pytest.fail(f"{path.name} calls forbidden {func.attr!r}")
            # bare eval(...) / exec(...)
            if isinstance(func, ast.Name) and func.id in {"eval", "exec"}:
                pytest.fail(f"{path.name} calls builtin {func.id!r}")


def test_tool_registry_is_the_six_typed_tools():
    assert set(contracts.TOOLS) == {
        "list_job_inputs", "get_statement", "get_segment", "get_provenance",
        "get_policy_pack", "submit_output"}
    # Exactly one write tool; it lands in staging, never a canonical table.
    writes = [t for t in contracts.TOOLS.values() if t.effect == "write"]
    assert [t.name for t in writes] == ["submit_output"]
