"""DEPLOY-2026 Compose package: config validity + PORT-4 secret scan.

AC-1 (compose config validates; the four services are present), AC-4 (committed
artifacts contain no secrets), and INV-7 (the image carries no DB/raw layer, no
published host port — private/loopback only, GOV-420).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
COMPOSE = DEPLOY / "docker-compose.yml"
DOCKERFILE = DEPLOY / "Dockerfile"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --- structure -------------------------------------------------------------

def test_deploy_package_files_exist():
    for name in ("Dockerfile", "docker-compose.yml", ".env.example", "README.md",
                 "mcp_stdio.py", ".gitignore"):
        assert (DEPLOY / name).exists(), f"missing deploy/{name}"


def test_compose_declares_the_four_services():
    text = _text(COMPOSE)
    for svc in ("ingress:", "worker:", "relay:", "mcp:"):
        assert svc in text, f"compose missing service {svc}"
    # scale-shape profile carries the managed-DB stand-in.
    assert "postgres:" in text and 'profiles: ["scale-shape"]' in text


def test_compose_publishes_no_host_ports():
    """GOV-420 / INV-4: private/loopback only — no `ports:` mapping anywhere."""
    for line in _text(COMPOSE).splitlines():
        stripped = line.strip()
        assert not stripped.startswith("ports:"), (
            "compose must not publish host ports (private/loopback only)")


def test_registry_and_raw_store_are_mounts_not_layers():
    df = _text(DOCKERFILE)
    # Inspect only the COPY/ADD instructions: none may bring a DB file or the raw
    # store into an image layer (INV-7). ENV/CMD naming the mount path is fine.
    copy_lines = [
        l.strip() for l in df.splitlines()
        if l.strip().upper().startswith(("COPY ", "ADD "))
    ]
    for line in copy_lines:
        for bad in (".db", "Source-Data", "Raw-PDFs", "Raw-Corpus", "gov_watchdog"):
            assert bad not in line, f"Dockerfile bakes {bad} into a layer: {line}"
    # The DB reaches the container only as a compose volume mount.
    assert "/data" in _text(COMPOSE) and "volumes:" in _text(COMPOSE)
    # .dockerignore keeps the registry/raw store out of the build context.
    dockerignore = _text(ROOT / ".dockerignore")
    assert "Database/*.db" in dockerignore and "Docs/Source-Data/" in dockerignore


# --- PORT-4 secret scan over committed artifacts ---------------------------

_SECRET_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[0-9A-Za-z-]+"
    r"|(?:password|secret|api[_-]?key|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+]{12,})",
    re.IGNORECASE,
)


@pytest.mark.parametrize("name", ["Dockerfile", "docker-compose.yml", ".env.example",
                                  "README.md", "mcp_stdio.py"])
def test_no_secret_shaped_values_in_committed_files(name):
    text = _text(DEPLOY / name)
    matches = _SECRET_RE.findall(text)
    assert not matches, f"deploy/{name} contains secret-shaped value(s): {matches}"


def test_env_example_uses_placeholders_only():
    text = _text(DEPLOY / ".env.example")
    # Placeholders, not real credentials.
    assert "CHANGE_ME" in text
    # A real .env must be gitignored.
    assert ".env" in _text(DEPLOY / ".gitignore")


# --- AC-1: docker-compose config validates (skips if the CLI is absent) -----

@pytest.mark.skipif(shutil.which("docker-compose") is None,
                    reason="docker-compose CLI not available in this environment")
def test_docker_compose_config_validates():
    for extra in ([], ["--profile", "scale-shape"]):
        proc = subprocess.run(
            ["docker-compose", "-f", str(COMPOSE), *extra, "config"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"docker-compose config failed:\n{proc.stderr}"


@pytest.mark.skipif(shutil.which("docker-compose") is None,
                    reason="docker-compose CLI not available in this environment")
def test_compose_config_lists_expected_services():
    proc = subprocess.run(
        ["docker-compose", "-f", str(COMPOSE), "--profile", "scale-shape",
         "config", "--services"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    services = set(proc.stdout.split())
    assert {"ingress", "worker", "relay", "mcp", "postgres"} <= services
