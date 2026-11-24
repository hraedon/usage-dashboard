"""The Pi-side plumbing for the cellular fallback (Plan 010).

Three small files stand between the worker and a unit: a root-owned wrapper
that sudoers points at, the worker's systemd unit, and an installer that puts
them — and nothing more — in place. Each one is where a mistake would be
expensive and quiet, so this module pins the properties that make them safe:

* the wrapper accepts one word from a fixed list, so "what the worker may ask
  root to do" is the same set in three places (wrapper, sudoers, helper);
* the unit gives the worker its own user and does not carry anything that
  would stop it calling sudo;
* the installer validates the sudoers rule before it can break sudo, never
  starts the worker, and never writes the settings file;
* the settings template documents every setting the code reads.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from usage_dashboard.client import cellular_worker as cw
from usage_dashboard.deploy import cellular_link as cl
from usage_dashboard.deploy import redeploy

REPO_ROOT = Path(__file__).resolve().parents[1]
PI_DIR = REPO_ROOT / "deploy" / "pi"
WRAPPER = PI_DIR / "usage-dashboard-cellular-link"
UNIT = PI_DIR / "usage-dashboard-cellular.service"
FIREWALL_UNIT = PI_DIR / "usage-dashboard-cellular-firewall.service"
INSTALLER = PI_DIR / "cellular-install.sh"
ENV_EXAMPLE = PI_DIR / "usage-dashboard-cellular.env.example"
# What the worker may ask root for: three verbs that manage the link, and one
# that only loads the deny policy (so it can be put in place at any time).
WORKER_VERBS = {"firewall", "route", "unroute", "down"}


def _bash(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, timeout=60, check=False
    )


def _settings_read(source: str) -> set[str]:
    """Every setting name a module reads from the environment."""
    return set(re.findall(r'"((?:CELLULAR_)?[A-Z][A-Z0-9_]{3,})"', source))


# -- the wrapper ------------------------------------------------------------------


class TestWrapper:
    def test_it_is_valid_bash(self) -> None:
        assert subprocess.run(["bash", "-n", str(WRAPPER)], check=False).returncode == 0

    def _rendered(self, tmp_path: Path, updater: Path) -> Path:
        rendered = tmp_path / "wrapper"
        text = WRAPPER.read_text().replace(
            'UPDATER="/usr/local/bin/usage-dashboard-update"', f'UPDATER="{updater.as_posix()}"'
        )
        assert f'UPDATER="{updater.as_posix()}"' in text
        rendered.write_text(text)
        return rendered

    def test_it_runs_the_module_from_the_venv_the_updater_names(
        self, tmp_path: Path
    ) -> None:
        venv = tmp_path / "app" / ".venv"
        (venv / "bin").mkdir(parents=True)
        # Stand-ins: an installed updater that names the venv, and the venv's
        # interpreter, which records how it was called.
        (tmp_path / "updater").write_text(f'APPDIR="{tmp_path / "app"}"\nVENV="{venv}"\n')
        python = venv / "bin" / "python"
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        python.chmod(0o755)
        proc = _bash(self._rendered(tmp_path, tmp_path / "updater"), "route")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == [
            "-I", "-m", "usage_dashboard.deploy.cellular_link", "route",
        ]

    @pytest.mark.parametrize(
        "args",
        [(), ("route", "extra"), ("--env-file", "x"), ("route", "unroute"),
         ("reboot",), ("Route",)],
    )
    def test_anything_but_one_worker_verb_is_a_usage_error(
        self, tmp_path: Path, args: tuple[str, ...]
    ) -> None:
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text("#!/bin/sh\necho RAN\n")
        python.chmod(0o755)
        (tmp_path / "updater").write_text(f'VENV="{venv}"\n')
        proc = _bash(self._rendered(tmp_path, tmp_path / "updater"), *args)
        assert proc.returncode == 2, proc
        assert "RAN" not in proc.stdout
        assert "usage:" in proc.stderr or "exactly one word" in proc.stderr

    def test_without_a_usable_venv_it_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "updater").write_text('VENV="/nonexistent"\n')
        proc = _bash(self._rendered(tmp_path, tmp_path / "updater"), "route")
        assert proc.returncode == 2 and "install.sh" in proc.stderr

    def test_operator_verbs_run_too_for_a_human_with_sudo(self, tmp_path: Path) -> None:
        venv = tmp_path / "venv"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        python.chmod(0o755)
        (tmp_path / "updater").write_text(f'VENV="{venv}"\n')
        wrapper = self._rendered(tmp_path, tmp_path / "updater")
        for verb in ("status", "check", "inspect"):
            assert _bash(wrapper, verb).stdout.split()[-1] == verb

    def test_the_three_lists_of_worker_verbs_agree(self) -> None:
        # Wrapper: the case pattern's first alternative group is the worker's.
        case = re.search(r"case \"\$\{1:-\}\" in\n\s+([a-z|]+)\)", WRAPPER.read_text())
        assert case is not None
        assert set(case.group(1).split("|")) == (
            set(cl.WORKER_VERBS) | set(cl.OPERATOR_VERBS)
        )
        assert set(cl.WORKER_VERBS) == WORKER_VERBS
        # Installer: the sudoers rule grants exactly those and no more.
        rule = re.search(r"NOPASSWD: (.*)", INSTALLER.read_text())
        assert rule is not None
        granted = {command.rsplit(" ", 1)[-1] for command in rule.group(1).split(", ")}
        assert granted == WORKER_VERBS
        # Worker: it only ever asks for those.
        asked = set(re.findall(r'self\._link\("([a-z]+)"\)', cw_source()))
        assert asked == WORKER_VERBS


def cw_source() -> str:
    return (REPO_ROOT / "src" / "usage_dashboard" / "client" / "cellular_worker.py").read_text()


# -- the unit ----------------------------------------------------------------------


class TestUnit:
    def test_placeholders(self) -> None:
        text = UNIT.read_text()
        assert "User=@WORKER_USER@\nGroup=@WORKER_USER@\n" in text
        assert "ExecStart=@VENV@/bin/usage-dashboard-cellular\n" in text
        assert sorted(set(re.findall(r"@[A-Z_]+@", text))) == ["@VENV@", "@WORKER_USER@"]

    def test_it_is_inert_until_provisioned(self) -> None:
        text = UNIT.read_text()
        assert "ConditionPathExists=/etc/usage-dashboard-cellular.env\n" in text
        assert "EnvironmentFile=/etc/usage-dashboard-cellular.env\n" in text

    def test_a_configuration_error_is_not_retried(self) -> None:
        text = UNIT.read_text()
        assert "Restart=on-failure\n" in text
        assert "RestartPreventExitStatus=78\n" in text
        assert cw.EXIT_CONFIG == 78

    def test_a_stopping_worker_always_takes_the_link_down(self) -> None:
        # As root, whatever User= is; and a failure here is not a failed stop.
        directives = [
            line for line in UNIT.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert (
            "ExecStopPost=+-/usr/local/bin/usage-dashboard-cellular-link down" in directives
        )

    def test_nothing_stops_the_worker_calling_sudo(self) -> None:
        # NoNewPrivileges (or an empty capability bounding set) would break the
        # one privileged call the worker makes. The sudoers rule bounds it.
        directives = [
            line for line in UNIT.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert not any(d.startswith(("NoNewPrivileges", "CapabilityBoundingSystem"))
                       for d in directives), directives
        assert "RestrictAddressFamilies=AF_UNIX AF_NETLINK AF_INET AF_INET6" in directives

    def test_the_cache_is_readable_by_the_gui_user(self) -> None:
        assert "UMask=0022" in UNIT.read_text()


# -- the installer -----------------------------------------------------------------


class TestInstaller:
    def test_it_is_valid_bash(self) -> None:
        assert subprocess.run(["bash", "-n", str(INSTALLER)], check=False).returncode == 0

    def test_it_refuses_to_run_as_root(self) -> None:
        proc = subprocess.run(
            ["bash", "-c", 'id() { echo 0; }; export -f id; source "$0"',
             INSTALLER.as_posix()],
            capture_output=True, text=True, check=False, timeout=60,
        )
        assert proc.returncode == 1
        assert "not root" in proc.stderr

    def test_it_needs_the_existing_install(self) -> None:
        text = INSTALLER.read_text()
        assert "Run deploy/pi/install.sh on this unit first." in text
        assert "s/^User=//p" in text and 's/^VENV="\\(.*\\)"$/\\1/p' in text

    @pytest.mark.parametrize("name", ["cell 0", "x;reboot", "Root", "a" * 40, ""])
    def test_the_worker_user_name_cannot_smuggle_syntax(self, name: str) -> None:
        proc = subprocess.run(
            ["bash", "-c", 'sudo() { echo "SUDO $*" >&2; exit 99; }; export -f sudo; '
             f'WORKER_USER="{name}" source "$0"',
             INSTALLER.as_posix()],
            capture_output=True, text=True, check=False, timeout=60,
        )
        if name == "":
            pytest.skip("an empty override falls back to the default name")
        assert proc.returncode == 1, proc
        assert "WORKER_USER must be a plain" in proc.stderr

    def test_the_sudoers_rule_is_validated_before_it_is_installed(self) -> None:
        text = INSTALLER.read_text()
        check = text.index("sudo visudo -cf")
        install = text.index('"$SUDOERS_FILE.new" "$SUDOERS_FILE"')
        assert check < install
        assert "-m 0440" in text
        assert "/etc/sudoers.d/usage-dashboard-cellular" in text

    def test_the_unit_is_verified_before_it_is_installed(self) -> None:
        text = INSTALLER.read_text()
        assert text.index("systemd-analyze verify") < text.index('"$UNIT_DIR/$UNIT_NAME"')

    def test_it_enables_both_units_but_never_starts_them(self) -> None:
        invoked = re.findall(r"^sudo systemctl (.+)$", INSTALLER.read_text(), re.M)
        assert invoked == [
            "daemon-reload", 'enable "$UNIT_NAME" "$FIREWALL_UNIT_NAME"',
        ]

    def test_both_units_are_verified_before_either_is_installed(self) -> None:
        text = INSTALLER.read_text()
        checks = [m.start() for m in re.finditer(r"systemd-analyze verify", text)]
        assert checks and max(checks) < text.index('"$UNIT_DIR/$UNIT_NAME"')
        assert text.index('"$UNIT_DIR/$UNIT_NAME"') < text.index(
            '"$UNIT_DIR/$FIREWALL_UNIT_NAME"'
        )
        assert "usage-dashboard-cellular-firewall.service" in text

    def test_it_does_not_write_the_settings_file(self) -> None:
        text = INSTALLER.read_text()
        assert "usage-dashboard-cellular.env.example" in text   # ...it points at it
        allowed = ("ENV_FILE=", "if [ -f \"$ENV_FILE\" ]", "echo ", "#")
        mentions = [
            line for line in text.splitlines()
            if "$ENV_FILE" in line or "/etc/usage-dashboard-cellular.env" in line
        ]
        assert mentions
        # Only the definition, an existence test, and instructions to the operator.
        unexpected = [line for line in mentions if not line.strip().startswith(allowed)]
        assert unexpected == [], unexpected

    def test_it_gives_the_worker_its_own_user_and_the_gui_the_signal_dir(self) -> None:
        text = INSTALLER.read_text()
        assert "useradd --system --user-group --no-create-home" in text
        assert '--shell /usr/sbin/nologin "$WORKER_USER"' in text
        assert 'install -d -o "$WORKER_USER" -g "$WORKER_USER" -m 0755 "$STATE_DIR"' in text
        assert 'install -d -o "$RUNUSER" -g "$RUNGROUP" -m 0755 "$STATE_DIR/request"' in text

    def test_the_helper_is_installed_executable_and_root_owned_by_rename(self) -> None:
        text = INSTALLER.read_text()
        assert 'install -m 0755 "$HERE/usage-dashboard-cellular-link" "$HELPER.new"' in text
        assert 'mv -f "$HELPER.new" "$HELPER"' in text

    def test_modemmanager_is_opt_in(self) -> None:
        text = INSTALLER.read_text()
        assert 'INSTALL_MODEM_MANAGER="${INSTALL_MODEM_MANAGER:-0}"' in text
        assert 'if [ "$INSTALL_MODEM_MANAGER" = "1" ]; then\n' in text
        assert "    sudo apt-get install -y modemmanager usb-modeswitch\n" in text

    def test_the_cellular_pieces_are_never_auto_redeployed(self) -> None:
        # A bad link configuration must not be able to reach a unit by itself.
        sources = {component.src for component in redeploy.COMPONENTS}
        assert not any("cellular" in name for name in sources)
        assert "NOT part of auto-update or auto-redeploy" in INSTALLER.read_text()


# -- the settings template and the entry point --------------------------------------


class TestSettingsTemplate:
    def test_every_setting_the_code_reads_is_documented(self) -> None:
        read = _settings_read(cw_source()) | _settings_read(
            (REPO_ROOT / "src" / "usage_dashboard" / "deploy" / "cellular_link.py").read_text()
        )
        read = {name for name in read if name.startswith("CELLULAR_")} | {"SERVER_URL"}
        pattern = r"^(#\s*)?((?:CELLULAR_)?[A-Z][A-Z0-9_]+)="
        documented = {name for _comment, name in re.findall(pattern, ENV_EXAMPLE.read_text(), re.M)}
        assert read <= documented, sorted(read - documented)

    def test_required_settings_are_live_and_marked_to_be_filled(self) -> None:
        live = dict(
            (name, value) for name, value in re.findall(
                r"^([A-Z][A-Z0-9_]+)=(\S+)\s*$", ENV_EXAMPLE.read_text(), re.M
            )
        )
        assert set(live) >= {
            "SERVER_URL", "CELLULAR_DISPLAY_KEY", "CELLULAR_DEST", "CELLULAR_IFACE",
            "CELLULAR_BUDGET_MB", "CELLULAR_CONNECTION",
        }
        assert live["CELLULAR_DISPLAY_KEY"] == "CHANGE-ME"
        # The template must not be usable as-is: its destination is a
        # placeholder, which the worker refuses before touching anything.
        with pytest.raises(cw.ConfigError, match="CELLULAR_DEST"):
            cw.WorkerConfig.from_env({**live, "CELLULAR_DISPLAY_KEY": "x"})

    def test_it_warns_about_inline_comments(self) -> None:
        text = ENV_EXAMPLE.read_text()
        assert "no inline" in text
        assert "does not create it" in text or "BY HAND" in text

    def test_the_worker_is_an_entry_point_and_the_helper_is_not(self) -> None:
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        entry = 'usage-dashboard-cellular = "usage_dashboard.client.cellular_worker:main"'
        assert entry in pyproject
        assert "cellular_link" not in pyproject


@pytest.mark.skipif(os.name == "nt", reason="the venv entry point is POSIX-only here")
def test_the_console_script_exits_with_the_config_code_when_unconfigured() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CELLULAR_")}
    env.pop("SERVER_URL", None)
    # The entry point beside this interpreter: CI runs the venv's pytest
    # without putting the venv's bin directory on PATH.
    script = Path(sys.executable).parent / "usage-dashboard-cellular"
    proc = subprocess.run(
        [str(script)], capture_output=True, text=True, check=False,
        timeout=60, env=env,
    )
    assert proc.returncode == cw.EXIT_CONFIG


# -- the firewall from boot (an independent review found it was missing) ---------------------


class TestBootTimeFirewall:
    def _directives(self) -> list[str]:
        return [
            line.strip() for line in FIREWALL_UNIT.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    def test_it_runs_before_anything_configures_the_network(self) -> None:
        directives = self._directives()
        # The hook systemd provides for "before networking": a unit ordered
        # before it runs first even with no default dependencies of its own.
        assert "Before=network-pre.target shutdown.target" in directives
        assert "Wants=network-pre.target" in directives
        assert "DefaultDependencies=no" in directives
        assert "After=local-fs.target" in directives

    def test_it_only_loads_the_policy(self) -> None:
        directives = self._directives()
        loads = "ExecStart=/usr/local/bin/usage-dashboard-cellular-link firewall"
        assert directives.count(loads) == 1
        assert not any(d.startswith(("ExecStop", "ExecStopPost", "ExecReload")) for d in directives)
        assert "Type=oneshot" in directives and "RemainAfterExit=yes" in directives

    def test_it_cannot_hold_up_the_boot(self) -> None:
        assert "TimeoutStartSec=30" in self._directives()

    def test_it_is_inert_until_provisioned_like_the_worker(self) -> None:
        assert "ConditionPathExists=/etc/usage-dashboard-cellular.env" in self._directives()

    def test_it_takes_no_placeholders(self) -> None:
        assert "@" not in FIREWALL_UNIT.read_text().replace("@@", "")
