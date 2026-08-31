"""update.sh must read its knobs from the environment, not by grepping the
root-owned env file.

`/etc/usage-dashboard-gui.env` is 600 root:root because it carries API_KEY, but
`usage-dashboard-update.service` runs as the unprivileged run user. Grepping the
file therefore fails with "Permission denied"; `|| true` swallows the error and
the knob reads empty. Observed live on mpmusage01 2026-08-07:

    grep: /etc/usage-dashboard-gui.env: Permission denied

Two silent consequences: AUTO_REDEPLOY could never arm (so WI-016's remote
redeploy was unreachable), and UPDATE_REF had never taken effect on any unit —
every Pi tracked `main` regardless of what the file said.

Fix: the unit carries `EnvironmentFile=`, so systemd reads the file as root and
injects the values before dropping privileges; update.sh prefers the environment
and falls back to the file only for a root-run invocation.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATE_SH = REPO_ROOT / "deploy" / "pi" / "update.sh"
UPDATE_UNIT = REPO_ROOT / "deploy" / "pi" / "usage-dashboard-update.service"


def _env_or_file(env: dict[str, str], env_file: Path | None, name: str) -> str:
    """Run update.sh's env_or_file helper in isolation."""
    body = UPDATE_SH.read_text()
    start = body.index("env_or_file() {")
    end = body.index("\n}", start) + 2
    harness = (
        f'ENV_FILE="{env_file if env_file else "/nonexistent"}"\n'
        + body[start:end]
        + f'\nenv_or_file {name}\n'
    )
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, env=env
    ).stdout


class TestKnobResolution:
    def test_environment_wins(self, tmp_path):
        f = tmp_path / "env"
        f.write_text("UPDATE_REF=from-file\n")
        assert _env_or_file({"UPDATE_REF": "from-env", "PATH": "/usr/bin:/bin"}, f,
                            "UPDATE_REF") == "from-env"

    def test_falls_back_to_a_readable_file(self, tmp_path):
        f = tmp_path / "env"
        f.write_text("UPDATE_REF=from-file\n")
        assert _env_or_file({"PATH": "/usr/bin:/bin"}, f, "UPDATE_REF") == "from-file"

    def test_unreadable_file_yields_empty_not_an_error(self, tmp_path):
        # The live failure mode: file exists but the run user cannot read it.
        f = tmp_path / "env"
        f.write_text("AUTO_REDEPLOY=1\n")
        f.chmod(0o000)
        assert _env_or_file({"PATH": "/usr/bin:/bin"}, f, "AUTO_REDEPLOY") == ""

    def test_unreadable_file_is_survivable_when_the_env_carries_the_value(self, tmp_path):
        # ...and this is why the fix works: systemd injects it, so the
        # unreadable file no longer matters.
        f = tmp_path / "env"
        f.write_text("AUTO_REDEPLOY=1\n")
        f.chmod(0o000)
        assert _env_or_file({"AUTO_REDEPLOY": "1", "PATH": "/usr/bin:/bin"}, f,
                            "AUTO_REDEPLOY") == "1"


def _run_updater(
    tmp_path,
    ref: str,
    *,
    auto_redeploy: str = "1",
    tag=None,
    transport_failure: bool = False,
):
    """Run the real update.sh against a throwaway git repo.

    Renders the @APPDIR@/@VENV@ placeholders like install.sh does, and shims
    `sudo` so the redeploy hand-off is observable without root. *tag* is an
    optional ``(name, annotated)`` pair created on origin before cloning.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    git = ["git", "-C", str(origin)]
    subprocess.run(git + ["init", "--quiet", "-b", "main"], check=True)
    subprocess.run(git + ["config", "user.email", "t@t"], check=True)
    subprocess.run(git + ["config", "user.name", "t"], check=True)
    (origin / "f").write_text("x")
    subprocess.run(git + ["add", "f"], check=True)
    subprocess.run(git + ["commit", "--quiet", "-m", "init"], check=True)
    if tag is not None:
        name, annotated = tag
        subprocess.run(
            git + (["tag", "-a", name, "-m", name] if annotated else ["tag", name]),
            check=True,
        )

    appdir = tmp_path / "app"
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(appdir)], check=True
    )
    if transport_failure:
        subprocess.run(
            ["git", "-C", str(appdir), "remote", "set-url", "origin", str(tmp_path / "gone")],
            check=True,
        )

    # A `sudo` shim on PATH records the redeploy hand-off; a `usage-dashboard-
    # redeploy` stub makes the -x test pass.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "redeploy-ran"
    (bin_dir / "sudo").write_text(
        f'#!/bin/sh\necho ran >> "{marker}"\nexit 0\n'
    )
    (bin_dir / "sudo").chmod(0o755)
    helper = tmp_path / "usage-dashboard-redeploy"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)

    script = tmp_path / "update.sh"
    body = UPDATE_SH.read_text().replace("@APPDIR@", str(appdir)).replace(
        "@VENV@", str(tmp_path / "venv")
    )
    # Point the helper -x check at our stub rather than /usr/local/bin.
    body = body.replace("/usr/local/bin/usage-dashboard-redeploy", str(helper))
    script.write_text(body)
    script.chmod(0o755)

    state = tmp_path / "state"
    proc = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "XDG_STATE_HOME": str(state),
            "UPDATE_REF": ref,
            "AUTO_REDEPLOY": auto_redeploy,
        },
    )
    check_file = state / "usage-dashboard" / "update-last-check"
    return proc, (check_file.read_text().strip() if check_file.exists() else ""), marker


class TestUnresolvableRefIsLoudNotSilent:
    """WI-031: a pinned ref that no longer exists must not stall the unit
    invisibly. Merged PR branches are deleted, so this is the normal end state
    of a staged rollout, not an exotic typo."""

    def test_bad_ref_writes_a_breadcrumb_and_fails(self, tmp_path):
        proc, check, _ = _run_updater(tmp_path, "no-such-branch")
        assert proc.returncode != 0, "a bad pin must not look like success"
        assert "no-such-branch" in proc.stdout, (
            "the log must name the unresolvable ref"
        )
        assert "STALLED" in proc.stdout
        # The breadcrumb is what lets the panel say "stalled" not just "stale".
        assert check.split()[1] == "bad-ref", (
            f"expected a bad-ref breadcrumb, got {check!r}"
        )

    def test_bad_ref_still_runs_auto_redeploy(self, tmp_path):
        """Infra self-correction must not die with a bad app pin."""
        _, _, marker = _run_updater(tmp_path, "no-such-branch")
        assert marker.exists(), (
            "auto-redeploy must still run when the app ref cannot be resolved"
        )

    def test_bad_ref_does_not_silently_fall_back_to_main(self, tmp_path):
        proc, check, _ = _run_updater(tmp_path, "no-such-branch")
        assert "up to date (main" not in proc.stdout
        assert "updating main" not in proc.stdout

    def test_good_ref_still_reports_up_to_date(self, tmp_path):
        proc, check, _ = _run_updater(tmp_path, "main")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "up to date (main" in proc.stdout
        assert check.split()[1] == "up-to-date"

    def test_transport_failure_is_not_reported_as_a_bad_ref(self, tmp_path):
        proc, check, _ = _run_updater(tmp_path, "main", transport_failure=True)
        assert proc.returncode != 0
        assert "transport" in proc.stdout or "transport" in proc.stderr
        assert check.split()[1] == "fetch-failed"


class TestTagPinning:
    """deploy/pi/README.md and the env example both advertise pinning a fleet
    to a tag. Resolving via "origin/$REF" only ever worked for branches — no
    remote-tracking ref exists for a tag — so the documented workflow would
    have killed the updater at rev-parse. Resolution goes through FETCH_HEAD,
    which `git fetch origin <ref>` sets for branches, tags and HEAD alike."""

    def test_lightweight_tag_resolves(self, tmp_path):
        proc, check, _ = _run_updater(tmp_path, "v0.2.0", tag=("v0.2.0", False))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "up to date (v0.2.0" in proc.stdout
        assert check.split()[1] == "up-to-date"

    def test_annotated_tag_resolves_to_its_commit(self, tmp_path):
        # An annotated tag is its own object; without ^{commit} the rev-parse
        # returns the tag SHA, which never equals HEAD, so the updater would
        # "update" on every single run.
        proc, check, _ = _run_updater(tmp_path, "v0.3.0", tag=("v0.3.0", True))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "up to date (v0.3.0" in proc.stdout, (
            "an annotated tag must dereference to its commit, else the unit "
            "re-updates forever"
        )


class TestMissingCheckoutIsLoudNotSilent:
    """Same silent-stall family as WI-031, one layer earlier: if the checkout
    is missing or not a git repo, a bare ``cd``/``rev-parse`` under ``set -e``
    aborted UPSTREAM of ``write_check``, so the panel kept showing the last
    good timestamp and the unit looked healthy. It must fail loud and leave a
    breadcrumb instead."""

    def test_missing_checkout_writes_a_breadcrumb_and_fails(self, tmp_path):
        # Point APPDIR at a path that does not exist.
        body = UPDATE_SH.read_text().replace(
            "@APPDIR@", str(tmp_path / "gone")
        ).replace("@VENV@", str(tmp_path / "venv"))
        script = tmp_path / "update-gone.sh"
        script.write_text(body)
        script.chmod(0o755)
        state = tmp_path / "state"
        gone = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(tmp_path),
                "XDG_STATE_HOME": str(state),
                "UPDATE_REF": "main",
            },
        )
        assert gone.returncode != 0, "a missing checkout must not look like success"
        assert "STALLED" in gone.stdout
        check_file = state / "usage-dashboard" / "update-last-check"
        assert check_file.exists(), "a breadcrumb must be written"
        assert check_file.read_text().split()[1] == "no-checkout"


class TestUnitInjectsTheEnvFile:
    def test_update_unit_has_environmentfile(self):
        text = UPDATE_UNIT.read_text()
        assert "EnvironmentFile=-/etc/usage-dashboard-gui.env" in text, (
            "usage-dashboard-update.service must inject the env file; without it "
            "the unprivileged updater cannot see AUTO_REDEPLOY or UPDATE_REF and "
            "both silently read empty"
        )

    def test_update_sh_prefers_the_environment(self):
        assert "env_or_file" in UPDATE_SH.read_text()
