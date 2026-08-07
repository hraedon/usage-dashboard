"""The install.sh re-exec must not lose its script directory.

install.sh re-execs from a copy in /tmp before `git reset --hard` so a moved ref
cannot rewrite the running script mid-run (WI-011; bash reads lazily by byte
offset). That fix silently broke a second thing: `HERE` and the `APPDIR` derived
from it are computed from `$0`, which the re-exec replaces with the /tmp path.
Every later `"$HERE/<file>"` read then resolves under /tmp.

It failed exactly that way on mpmusage01 on 2026-08-07 — `install: cannot stat
'/tmp/goodix-touch-rebind.sh'` — which blocked deploying the remote-redeploy
helper (WI-028). The abort was clean thanks to `set -euo pipefail`, but the
installer was unusable on any existing checkout.

These reproduce the mechanism with a miniature script rather than grepping
install.sh, so they fail if the contract breaks however it is re-expressed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "deploy" / "pi" / "install.sh"

# The two halves of the contract, lifted verbatim in shape from install.sh.
_HARNESS = """\
set -euo pipefail
HERE="${_INSTALL_SH_HERE:-$(cd "$(dirname "$0")" && pwd)}"
if [ -z "${_INSTALL_SH_REEXECED:-}" ]; then
    _STABLE_COPY="$(mktemp --suffix=.sh)"
    cp "$0" "$_STABLE_COPY"
    chmod +x "$_STABLE_COPY"
    export _STABLE_COPY _INSTALL_SH_REEXECED=1 _INSTALL_SH_HERE="$HERE"
    exec "$_STABLE_COPY" "$@"
fi
trap 'rm -f "${_STABLE_COPY:-}"' EXIT
echo "HERE=$HERE"
"""

# The pre-fix version: HERE recomputed from $0 and not carried across.
_HARNESS_BROKEN = _HARNESS.replace(
    'HERE="${_INSTALL_SH_HERE:-$(cd "$(dirname "$0")" && pwd)}"',
    'HERE="$(cd "$(dirname "$0")" && pwd)"',
).replace(' _INSTALL_SH_HERE="$HERE"', "")


def _run(script_text: str, tmp_path: Path) -> str:
    script = tmp_path / "harness.sh"
    script.write_text(script_text)
    script.chmod(0o755)
    out = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


class TestReexecPreservesScriptDir:
    def test_here_survives_the_reexec(self, tmp_path):
        assert _run(_HARNESS, tmp_path) == f"HERE={tmp_path}"

    def test_the_pre_fix_version_really_did_lose_it(self, tmp_path):
        # Proves the test is not vacuous: without the carry-through, HERE
        # resolves to the /tmp copy's directory, not the script's own.
        got = _run(_HARNESS_BROKEN, tmp_path)
        assert got != f"HERE={tmp_path}"
        assert got.startswith("HERE=/tmp")


class TestInstallShWiresItUp:
    def test_install_sh_carries_here_across_the_exec(self):
        text = INSTALL_SH.read_text()
        assert '_INSTALL_SH_HERE="$HERE"' in text, (
            "install.sh re-execs without exporting HERE; the re-exec'd run will "
            "resolve HERE to the /tmp copy and every $HERE/<file> read will fail"
        )
        assert 'HERE="${_INSTALL_SH_HERE:-' in text, (
            "install.sh does not honour the carried-through HERE"
        )
