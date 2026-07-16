"""DEPLOY-2026 lock-in lint (PORT-3, plan D2).

A static import-graph walk: no civic-domain module may import a provider SDK.
The one exemption is the adapter package (``scripts/deploy_adapters/``), the sole
place backend-specific code is allowed to live. The lint is also self-tested
against synthetic source so it can never silently degrade into a no-op.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# Adapter package: the ONLY location provider-specific code may live.
EXEMPT_DIRS = {SCRIPTS / "deploy_adapters"}

# Provider SDK top-level module names forbidden in civic-domain code.
FORBIDDEN_ROOTS = {
    "boto3", "botocore", "psycopg", "psycopg2", "google", "googleapiclient",
    "azure", "redis", "pymysql", "mysql", "mysqlclient", "snowflake",
    "pymongo", "aioboto3", "s3transfer", "minio",
}


def _forbidden_imports_in_source(src: str) -> set[str]:
    """Return the set of forbidden provider-SDK roots imported by ``src``."""
    found: set[str] = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_ROOTS:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import — never a provider SDK.
                continue
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_ROOTS:
                found.add(root)
    return found


def _is_exempt(path: Path) -> bool:
    return any(exempt in path.parents for exempt in EXEMPT_DIRS)


def _civic_domain_files():
    return sorted(p for p in SCRIPTS.rglob("*.py") if not _is_exempt(p))


def test_there_are_civic_domain_files():
    files = _civic_domain_files()
    assert len(files) > 50, "expected the full scripts/ tree to be scanned"


@pytest.mark.parametrize(
    "path", _civic_domain_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_no_provider_sdk_import(path):
    found = _forbidden_imports_in_source(path.read_text(encoding="utf-8"))
    assert not found, f"{path.name} imports provider SDK(s): {sorted(found)}"


def test_lint_detects_a_forbidden_import():
    """Self-check: the lint is not a no-op."""
    assert _forbidden_imports_in_source("import boto3\nx = 1\n") == {"boto3"}
    assert _forbidden_imports_in_source(
        "from google.cloud import storage\n") == {"google"}
    assert _forbidden_imports_in_source("import sqlite3\nimport json\n") == set()


def test_adapter_package_is_the_exemption():
    """The Postgres adapter is allowed backend-specific code, but even it uses
    ``psql`` via subprocess — it imports no provider SDK today."""
    pg = SCRIPTS / "deploy_adapters" / "postgres_adapter.py"
    assert _is_exempt(pg)
    # Confirm it genuinely carries no forbidden import either (subprocess only).
    assert _forbidden_imports_in_source(pg.read_text(encoding="utf-8")) == set()
