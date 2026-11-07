"""The CLI entry module must import on any OS, Windows included.

Found 2026-11-07 during the PR 37 deployment round: ``usage-dashboard
capacity`` — the command agents are documented to call — could not even be
imported on Windows, because ``cli.py`` pulled ``server.token_store`` and
``server.fetch_antigravity`` at module level and both import ``fcntl``. The
enrolment commands are POSIX-bound by design (pty ceremonies, fcntl-locked
homes, run on the server pod); the capacity command is not.

Two pins, because CI runs on Linux and cannot feel a Windows failure:

- Runtime: both modules import cleanly in a fresh interpreter. On a Windows
  checkout this is the regression test. (Transitive ``fcntl`` via stdlib
  ``subprocess`` is fine and POSIX-only: CPython's subprocess imports fcntl
  on POSIX — the invariant is that OUR imports never name a POSIX-only
  module, not that sys.modules stays clean.)
- Static: no module-level import in ``cli.py`` or ``client/capacity.py``
  names a POSIX-only stdlib module. Function-local imports inside the
  enrolment commands are the sanctioned escape hatch.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

POSIX_ONLY_MODULES = frozenset({"fcntl", "pty", "termios", "tty"})

CLI_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "src" / "usage_dashboard"
PORTABLE_MODULES = ("cli.py", "client/capacity.py")


def test_portable_cli_modules_import_on_this_platform() -> None:
    probe = "import usage_dashboard.cli, usage_dashboard.client.capacity"
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, (
        "a portable CLI module failed to import on this platform: "
        f"{result.stderr.strip()}"
    )


def test_portable_cli_modules_have_no_module_level_posix_imports() -> None:
    offenders: list[str] = []
    for rel in PORTABLE_MODULES:
        tree = ast.parse((CLI_PACKAGE_ROOT / rel).read_text(encoding="utf-8"))
        for node in tree.body:  # top-level statements only: lazy imports are
            if isinstance(node, ast.Import):  # nested in functions on purpose
                offenders.extend(
                    f"{rel}: import {alias.name}"
                    for alias in node.names
                    if alias.name.split(".")[0] in POSIX_ONLY_MODULES
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in POSIX_ONLY_MODULES:
                    offenders.append(f"{rel}: from {node.module} import ...")
    assert not offenders, f"module-level POSIX-only imports re-introduced: {offenders}"
