"""The CLI entry module must import on any OS without POSIX-only stdlib modules.

`usage-dashboard capacity` is the command agents are documented to call, and
agents do not all run on Linux. Found 2026-11-07 during the PR 37 deployment
round: a module-level import chain through ``server.fetch_antigravity``
pulled ``fcntl``, so even ``--help`` died with ModuleNotFoundError on
Windows. The quarantine is now: enrolment commands import their pty/fcntl
backends lazily at call time, and the capacity command lives in the portable
``usage_dashboard.client.capacity``.

This test pins the quarantine in a fresh interpreter (a subprocess, because
any earlier test in the same process may legitimately have imported fcntl).
"""

from __future__ import annotations

import subprocess
import sys

POSIX_ONLY_MODULES = ("fcntl", "pty", "termios", "tty")


def _import_probe(module: str) -> subprocess.CompletedProcess[str]:
    probe = (
        "import sys\n"
        f"import {module}\n"
        f"leaked = sorted(set(sys.modules) & set({POSIX_ONLY_MODULES!r}))\n"
        "print(','.join(leaked))\n"
        "sys.exit(1 if leaked else 0)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_module_import_pulls_no_posix_only_modules() -> None:
    result = _import_probe("usage_dashboard.cli")
    assert result.returncode == 0, (
        f"usage_dashboard.cli leaked POSIX-only modules at import: "
        f"{result.stdout.strip() or result.stderr.strip()}"
    )


def test_capacity_client_module_import_pulls_no_posix_only_modules() -> None:
    result = _import_probe("usage_dashboard.client.capacity")
    assert result.returncode == 0, (
        f"usage_dashboard.client.capacity leaked POSIX-only modules at import: "
        f"{result.stdout.strip() or result.stderr.strip()}"
    )
