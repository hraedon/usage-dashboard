from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from usage_dashboard.deploy import redeploy
from usage_dashboard.deploy.redeploy import apply, render

REPO = Path(__file__).resolve().parents[1]
SUBS = {
    "RUNUSER": "pi",
    "APPDIR": "/home/pi/usage-dashboard",
    "VENV": "/home/pi/usage-dashboard/.venv",
    "XRANDR_ROTATE": "right",
}


def _ok_verifier(text: str, suffix: str) -> tuple[bool, str]:
    return True, ""


class FakeRunner:
    """Records systemctl calls; returns scripted ``is-active`` codes per service
    (default: active)."""

    def __init__(self, active: dict[str, list[int]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._active: dict[str, list[int]] = defaultdict(list)
        if active:
            self._active.update(active)

    def __call__(self, cmd: list[str]) -> CompletedProcess[str]:
        self.calls.append(cmd)
        rc = 0
        if cmd[:2] == ["systemctl", "is-active"]:
            seq = self._active.get(cmd[2])
            rc = seq.pop(0) if seq else 0
        return CompletedProcess(cmd, rc, "", "")

    def issued(self, verb: str) -> list[str]:
        """Service names restarted/etc. for ``systemctl <verb> <svc>`` calls."""
        return [c[2] for c in self.calls if c[:2] == ["systemctl", verb]]


@pytest.fixture
def appdir(tmp_path: Path) -> Path:
    """A throwaway app checkout carrying the real deploy/pi templates."""
    dst = tmp_path / "app"
    shutil.copytree(REPO / "deploy" / "pi", dst / "deploy" / "pi")
    return dst


class TestRender:
    def test_replaces_known_tokens(self) -> None:
        out = render("U=@RUNUSER@ A=@APPDIR@", SUBS)
        assert out == "U=pi A=/home/pi/usage-dashboard"

    def test_unknown_tokens_untouched(self) -> None:
        assert render("keep @WAT@", SUBS) == "keep @WAT@"


class TestApply:
    def test_first_apply_installs_everything_then_no_op(
        self, appdir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"
        runner = FakeRunner()
        rep = apply(str(appdir), SUBS, root=str(root), runner=runner,
                    verifier=_ok_verifier)
        assert rep.changed  # something installed
        # The updater + units actually landed under the fake root...
        assert (root / "usr/local/bin/usage-dashboard-update").exists()
        assert (root / "etc/systemd/system/usage-dashboard-gui.service").exists()
        # ...with the templated values rendered in.
        updater = (root / "usr/local/bin/usage-dashboard-update").read_text()
        assert 'APPDIR="/home/pi/usage-dashboard"' in updater
        # A unit change triggers exactly one daemon-reload.
        assert runner.calls.count(["systemctl", "daemon-reload"]) == 1

        # Second apply against the now-populated root is a pure no-op.
        runner2 = FakeRunner()
        rep2 = apply(str(appdir), SUBS, root=str(root), runner=runner2,
                     verifier=_ok_verifier)
        assert not rep2.changed
        assert rep2.to_lines() == ["no changes (in sync)"]
        assert runner2.calls == []  # no reload, no restart when nothing moved

    def test_drift_updates_and_backs_up(self, appdir: Path, tmp_path: Path) -> None:
        root = tmp_path / "root"
        apply(str(appdir), SUBS, root=str(root), runner=FakeRunner(),
              verifier=_ok_verifier)
        # Operator edits a template in the checkout (an infra-only change).
        xsession = appdir / "deploy/pi/usage-dashboard-xsession"
        xsession.write_text(xsession.read_text() + "\n# tweak\n")

        runner = FakeRunner()
        rep = apply(str(appdir), SUBS, root=str(root), runner=runner,
                    verifier=_ok_verifier)
        dest = "usr/local/bin/usage-dashboard-xsession"
        assert dest in rep.changed_scripts
        assert (root / (dest + ".bak")).exists()        # prior version preserved
        assert "# tweak" in (root / dest).read_text()    # new version live
        # A script-only change does NOT daemon-reload, but does bounce the GUI.
        assert ["systemctl", "daemon-reload"] not in runner.calls
        assert "usage-dashboard-gui.service" in runner.issued("restart")
        # No leftover temp file from the atomic write.
        assert not (root / (dest + ".tmp")).exists()

    def test_unit_failing_verify_is_skipped_not_installed(
        self, appdir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "root"

        def bad_gui(text: str, suffix: str) -> tuple[bool, str]:
            return ("Conflicts=getty@tty1" not in text), "simulated bad unit"

        rep = apply(str(appdir), SUBS, root=str(root), runner=FakeRunner(),
                    verifier=bad_gui)
        gui = "etc/systemd/system/usage-dashboard-gui.service"
        assert gui in rep.skipped
        assert gui not in rep.changed_units
        assert not (root / gui).exists()            # the bad unit never landed
        assert any("verify" in e for e in rep.errors)
        # Other components still installed.
        assert (root / "etc/systemd/system/usage-dashboard-update.timer").exists()

    def test_gui_not_running_is_not_started(self, appdir: Path, tmp_path: Path) -> None:
        root = tmp_path / "root"
        runner = FakeRunner(active={"usage-dashboard-gui.service": [1]})  # inactive
        rep = apply(str(appdir), SUBS, root=str(root), runner=runner,
                    verifier=_ok_verifier)
        assert "usage-dashboard-gui.service" in rep.skipped_restart
        assert "usage-dashboard-gui.service" not in runner.issued("restart")

    def test_gui_failing_restart_rolls_back(self, appdir: Path, tmp_path: Path) -> None:
        root = tmp_path / "root"
        # Seed an installed baseline so there's a backup to roll back to.
        apply(str(appdir), SUBS, root=str(root), runner=FakeRunner(),
              verifier=_ok_verifier)
        gui_src = appdir / "deploy/pi/usage-dashboard-gui.service"
        gui_src.write_text(gui_src.read_text() + "\n# bad change\n")
        dest = root / "etc/systemd/system/usage-dashboard-gui.service"

        # is-active: running before, dead after the bounce -> rollback path.
        runner = FakeRunner(active={"usage-dashboard-gui.service": [0, 1]})
        rep = apply(str(appdir), SUBS, root=str(root), runner=runner,
                    verifier=_ok_verifier)
        assert "usage-dashboard-gui.service" in rep.rolled_back
        assert "# bad change" not in dest.read_text()  # prior unit restored

    def test_plan_reports_without_writing(self, appdir: Path, tmp_path: Path) -> None:
        root = tmp_path / "root"
        runner = FakeRunner()
        rep = apply(str(appdir), SUBS, root=str(root), runner=runner,
                    verifier=_ok_verifier, dry_run=True)
        assert rep.changed
        assert runner.calls == []                      # nothing executed
        assert not (root / "usr/local/bin/usage-dashboard-update").exists()


def test_cli_plan_runs(appdir: Path, tmp_path: Path, capsys, monkeypatch) -> None:
    # Stub the unit verifier: systemd-analyze (when present) checks ExecStart
    # paths exist, which they don't under a tmp root — irrelevant to CLI plumbing.
    monkeypatch.setattr(redeploy, "_verify_unit", _ok_verifier)
    root = tmp_path / "root"
    rc = redeploy.main([
        "plan", "--appdir", str(appdir), "--runuser", "pi",
        "--venv", "/v", "--rotate", "right", "--root", str(root),
    ])
    assert rc == 0
    assert "redeploy:" in capsys.readouterr().out
