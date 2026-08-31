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


def _install_checkout_function() -> str:
    """Extract the production checkout function for an isolated shell run.

    The tag tests execute the same function that install.sh calls, rather than
    maintaining a second hand-written copy of its git block. The marker after
    the function is deliberately part of the seam: a refactor that removes or
    stops calling this function makes these tests fail instead of silently
    testing stale fixture code.
    """
    text = INSTALL_SH.read_text()
    start = text.index("update_existing_checkout() {")
    end = text.index("\n}\n\nif [ -d \"$APPDIR/.git\" ]; then", start) + 2
    return text[start:end]


def _install_checkout_dispatch() -> str:
    """Extract the production existing-checkout dispatch, including its call.

    This is intentionally separate from ``_install_checkout_function``. The
    tag tests exercise the real update function; this seam guards the caller
    at install.sh's checkout boundary too. It runs against a fake ``.git``
    directory and overrides the update function, so it performs no network or
    privileged operation.
    """
    text = INSTALL_SH.read_text()
    start = text.index('if [ -d "$APPDIR/.git" ]; then')
    end = text.index("\nfi\n\n# --- 5. venv + install", start) + 3
    return text[start:end]


def _run_checkout_dispatch(
    dispatch: str, tmp_path: Path
) -> tuple[subprocess.CompletedProcess, bool]:
    appdir = tmp_path / "existing"
    (appdir / ".git").mkdir(parents=True)
    marker = tmp_path / "update-called"
    script = tmp_path / "dispatch.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'APPDIR="{appdir}"\n'
        'REPO_URL="unused"\n'
        'UPDATE_REF="main"\n'
        f'MARKER="{marker}"\n'
        'update_existing_checkout() { printf \'called\\n\' > "$MARKER"; }\n'
        f"{dispatch}\n"
    )
    script.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    return proc, marker.exists()


def _make_origin_with_tag(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    git = ["git", "-C", str(origin)]
    subprocess.run(git + ["init", "--quiet", "-b", "main"], check=True)
    subprocess.run(git + ["config", "user.email", "t@t"], check=True)
    subprocess.run(git + ["config", "user.name", "t"], check=True)
    (origin / "f").write_text("x")
    subprocess.run(git + ["add", "f"], check=True)
    subprocess.run(git + ["commit", "--quiet", "-m", "init"], check=True)
    subprocess.run(git + ["tag", "v0.2.0"], check=True)
    return origin


def _run_git_block(
    function: str, tmp_path: Path, appdir: Path, ref: str
) -> subprocess.CompletedProcess:
    script = tmp_path / "gitblock.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'APPDIR="{appdir}"\n'
        f'UPDATE_REF="{ref}"\n'
        f"{function}\n"
        "update_existing_checkout\n"
    )
    script.chmod(0o755)
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "APPDIR": str(appdir),
            "UPDATE_REF": ref,
        },
    )


class TestInstallShTagRerun:
    """Re-running install.sh against an existing checkout pinned to a tag used
    to ``git reset --hard origin/<tag>`` — but a remote-tracking ref only exists
    for branches, so the documented tag-pinning workflow died at the reset. The
    fix resolves via FETCH_HEAD^{commit}, exactly like update.sh."""

    def test_tag_rerun_resolves_to_the_tag_commit(self, tmp_path):
        origin = _make_origin_with_tag(tmp_path)
        appdir = tmp_path / "app"
        subprocess.run(
            ["git", "clone", "--quiet", str(origin), str(appdir)], check=True
        )
        proc = _run_git_block(
            _install_checkout_function(), tmp_path, appdir, "v0.2.0"
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        head = subprocess.run(
            ["git", "-C", str(appdir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        tag_commit = subprocess.run(
            ["git", "-C", str(appdir), "rev-parse", "v0.2.0^{commit}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert head == tag_commit

    def test_the_pre_fix_block_really_did_fail_on_tags(self, tmp_path):
        # Proves the test is not vacuous: origin/<tag> does not resolve.
        origin = _make_origin_with_tag(tmp_path)
        appdir = tmp_path / "app"
        subprocess.run(
            ["git", "clone", "--quiet", str(origin), str(appdir)], check=True
        )
        broken = _install_checkout_function().replace(
            'git -C "$APPDIR" reset --hard --quiet "FETCH_HEAD^{commit}"',
            'git -C "$APPDIR" reset --hard --quiet "origin/$UPDATE_REF"',
        )
        proc = _run_git_block(broken, tmp_path, appdir, "v0.2.0")
        assert proc.returncode != 0

    def test_transport_failure_is_not_reported_as_a_missing_ref(self, tmp_path):
        origin = _make_origin_with_tag(tmp_path)
        appdir = tmp_path / "app"
        subprocess.run(
            ["git", "clone", "--quiet", str(origin), str(appdir)], check=True
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(appdir),
                "remote",
                "set-url",
                "origin",
                str(tmp_path / "gone"),
            ],
            check=True,
        )
        proc = _run_git_block(
            _install_checkout_function(), tmp_path, appdir, "v0.2.0"
        )
        assert proc.returncode != 0
        assert "network/transport failure" in proc.stderr

    def test_install_sh_uses_fetch_head_not_origin_ref(self):
        text = INSTALL_SH.read_text()
        assert 'reset --hard --quiet "FETCH_HEAD^{commit}"' in text, (
            "install.sh must resolve the fetched ref via FETCH_HEAD^{commit}; "
            "origin/$UPDATE_REF only exists for branches and kills tag re-runs"
        )
        assert 'reset --hard --quiet "origin/$UPDATE_REF"' not in text

    def test_existing_checkout_dispatch_calls_production_update(self, tmp_path):
        # The real if/else dispatch from install.sh must invoke the production
        # update function. This catches deleting/bypassing the call at the
        # checkout boundary without running apt, sudo, or network operations.
        proc, called = _run_checkout_dispatch(
            _install_checkout_dispatch(), tmp_path
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert called, (
            "install.sh's existing-checkout branch must call "
            "update_existing_checkout"
        )
