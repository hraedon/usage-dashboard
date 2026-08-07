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
