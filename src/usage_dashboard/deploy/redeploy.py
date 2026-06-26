"""Self-redeploy of the Pi's installer-managed components (WI-016).

The auto-updater (``deploy/pi/update.sh``) only swaps the Python app. The pieces
``install.sh`` lays down as root — the updater script itself, the systemd units,
the X-session launcher, the touch-rebind helper — otherwise change only when
someone re-runs ``install.sh`` on each unit by hand, so an infra fix silently
fails to reach the fleet. This module re-applies just those pieces from the
already-pulled checkout, idempotently and safely:

* **content-addressed drift** — a component is touched only when the rendered
  source differs from what's installed, so a redeploy is a no-op in steady state;
* **WI-011-safe writes** — every file is written to a temp sibling and
  ``os.replace``-d into place (atomic rename), so the running updater/wrapper that
  triggered this can be replaced mid-run without corrupting its own execution
  (the live process keeps its descriptor to the old inode);
* **validate before install** — unit files are run through ``systemd-analyze
  verify``; a unit that fails is skipped, never installed;
* **rollback** — the long-running GUI service (the one with no console to fall
  back to) is health-checked after a bounce and its prior unit/launcher restored
  if it doesn't come active.

The privileged file writes and ``systemctl`` calls run as root via the thin
``deploy/pi/usage-dashboard-redeploy`` wrapper; this module holds the logic so it
is unit-tested without root (drive everything against a ``--root`` prefix and an
injected command runner).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess

Runner = Callable[[list[str]], "CompletedProcess[str]"]
Verifier = Callable[[str, str], "tuple[bool, str]"]

# The long-running service with no console behind it: bounce it only if it was
# already running, health-check the bounce, and roll back if it won't come up.
GUI_SERVICE = "usage-dashboard-gui.service"


@dataclass(frozen=True)
class Component:
    """One installer-managed file: where it comes from, where it goes, and what
    (if anything) to restart when it changes."""

    src: str            # filename under deploy/pi/
    dest: str           # install path, relative to the apply root (no leading /)
    kind: str           # "unit" | "script"
    mode: int
    restart: str | None  # service to restart on change, or None


# Order is install order; restarts are de-duplicated so two changes that both
# want the GUI bounced only bounce it once.
COMPONENTS: tuple[Component, ...] = (
    Component("update.sh", "usr/local/bin/usage-dashboard-update", "script", 0o755, None),
    Component("usage-dashboard-redeploy", "usr/local/bin/usage-dashboard-redeploy",
              "script", 0o755, None),
    Component("usage-dashboard-xsession", "usr/local/bin/usage-dashboard-xsession",
              "script", 0o755, GUI_SERVICE),
    Component("goodix-touch-rebind.sh", "usr/local/bin/goodix-touch-rebind.sh",
              "script", 0o755, "goodix-touch-rebind.service"),
    Component("goodix-touch-rebind.service",
              "etc/systemd/system/goodix-touch-rebind.service", "unit", 0o644,
              "goodix-touch-rebind.service"),
    Component("usage-dashboard-gui.service",
              "etc/systemd/system/usage-dashboard-gui.service", "unit", 0o644,
              GUI_SERVICE),
    Component("usage-dashboard-update.service",
              "etc/systemd/system/usage-dashboard-update.service", "unit", 0o644, None),
    Component("usage-dashboard-update.timer",
              "etc/systemd/system/usage-dashboard-update.timer", "unit", 0o644,
              "usage-dashboard-update.timer"),
)


@dataclass
class ApplyReport:
    changed_units: list[str] = field(default_factory=list)
    changed_scripts: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)
    skipped_restart: list[str] = field(default_factory=list)  # wasn't running
    skipped: list[str] = field(default_factory=list)          # failed verify
    errors: list[str] = field(default_factory=list)
    backups: dict[str, str] = field(default_factory=dict)     # dest -> backup path

    @property
    def changed(self) -> bool:
        return bool(self.changed_units or self.changed_scripts)

    def to_lines(self) -> list[str]:
        out: list[str] = []
        for label, items in (
            ("updated unit", self.changed_units),
            ("updated script", self.changed_scripts),
            ("restarted", self.restarted),
            ("rolled back", self.rolled_back),
            ("left stopped", self.skipped_restart),
            ("skipped (verify)", self.skipped),
            ("error", self.errors),
        ):
            for item in items:
                out.append(f"{label}: {item}")
        if not out:
            out.append("no changes (in sync)")
        return out


def render(text: str, subs: dict[str, str]) -> str:
    """Replace ``@VAR@`` placeholders. Absent placeholders are left untouched, so
    the same subs map applies to every component regardless of which tokens it
    actually uses (matching ``install.sh``'s ``render_unit``)."""
    for key, value in subs.items():
        text = text.replace(f"@{key}@", value)
    return text


def _real_runner(cmd: list[str]) -> CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _verify_unit(text: str, suffix: str) -> tuple[bool, str]:
    """Run a unit body through ``systemd-analyze verify``. If the tool is absent
    (dev/CI), don't block — the value is catching typos on a real Pi, not gating
    the test host."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        res = subprocess.run(
            ["systemd-analyze", "verify", path],
            capture_output=True, text=True, check=False,
        )
        return res.returncode == 0, (res.stderr or res.stdout).strip()
    except OSError as exc:
        return True, f"verify skipped ({exc})"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _atomic_install(dest: Path, text: str, mode: int) -> Path | None:
    """Write *text* to *dest* via a temp sibling + atomic rename, backing up any
    existing file to ``<dest>.bak`` first. Returns the backup path, or None when
    the file is new. The rename is what makes replacing the running script safe."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if dest.exists():
        backup = dest.with_name(dest.name + ".bak")
        shutil.copy2(dest, backup)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, dest)  # atomic on the same filesystem
    return backup


def apply(
    appdir: str,
    subs: dict[str, str],
    *,
    root: str = "/",
    runner: Runner | None = None,
    verifier: Verifier | None = None,
    dry_run: bool = False,
) -> ApplyReport:
    """Re-apply every drifted installer-managed component, then daemon-reload and
    restart what changed. Safe to call every cycle: a no-op when in sync."""
    # Resolve at call time (not as bound defaults) so the real runner/verifier
    # stay patchable in tests.
    if runner is None:
        runner = _real_runner
    if verifier is None:
        verifier = _verify_unit
    report = ApplyReport()
    src_dir = Path(appdir) / "deploy" / "pi"
    restart_targets: set[str] = set()

    for comp in COMPONENTS:
        try:
            rendered = render((src_dir / comp.src).read_text(), subs)
        except OSError as exc:
            report.errors.append(f"read {comp.src}: {exc}")
            continue
        dest = Path(root) / comp.dest
        try:
            current: str | None = dest.read_text() if dest.exists() else None
        except OSError as exc:
            report.errors.append(f"read installed {comp.dest}: {exc}")
            continue
        if current == rendered:
            continue

        if comp.kind == "unit":
            ok, msg = verifier(rendered, "." + comp.dest.rsplit(".", 1)[-1])
            if not ok:
                report.skipped.append(comp.dest)
                report.errors.append(f"verify {comp.src}: {msg}")
                continue

        bucket = report.changed_units if comp.kind == "unit" else report.changed_scripts
        if dry_run:
            bucket.append(comp.dest)
        else:
            try:
                backup = _atomic_install(dest, rendered, comp.mode)
            except OSError as exc:
                report.errors.append(f"install {comp.dest}: {exc}")
                continue
            if backup is not None:
                report.backups[comp.dest] = str(backup)
            bucket.append(comp.dest)
        if comp.restart:
            restart_targets.add(comp.restart)

    if dry_run:
        report.restarted = sorted(restart_targets)  # "would restart"
        return report

    if report.changed_units:
        runner(["systemctl", "daemon-reload"])
    for service in sorted(restart_targets):
        if service == GUI_SERVICE:
            _bounce_gui(service, runner, root, report)
        else:
            res = runner(["systemctl", "restart", service])
            if res.returncode == 0:
                report.restarted.append(service)
            else:
                report.errors.append(f"restart {service}: rc={res.returncode}")
    return report


def _bounce_gui(service: str, runner: Runner, root: str, report: ApplyReport) -> None:
    """Restart the GUI only if it was already running, then confirm it came back
    active; if not, restore the backed-up unit/launcher and restart again. This
    is the one service with no console to recover from by hand."""
    if runner(["systemctl", "is-active", service]).returncode != 0:
        report.skipped_restart.append(service)  # don't start a stopped panel
        return
    runner(["systemctl", "restart", service])
    if runner(["systemctl", "is-active", service]).returncode == 0:
        report.restarted.append(service)
        return
    report.errors.append(f"{service} not active after restart")
    if _restore_for(service, root, report):
        runner(["systemctl", "daemon-reload"])
        runner(["systemctl", "restart", service])
        report.rolled_back.append(service)
    else:
        report.restarted.append(service)  # nothing to roll back to


def _restore_for(service: str, root: str, report: ApplyReport) -> bool:
    """Restore the backups of every just-changed component that bounces *service*.
    Returns True if anything was restored."""
    restored = False
    for comp in COMPONENTS:
        if comp.restart != service:
            continue
        backup = report.backups.get(comp.dest)
        if not backup:
            continue
        dest = Path(root) / comp.dest
        try:
            shutil.copy2(backup, dest)
            os.chmod(dest, comp.mode)
            restored = True
        except OSError as exc:
            report.errors.append(f"restore {comp.dest}: {exc}")
    return restored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="usage-dashboard-redeploy")
    parser.add_argument("action", choices=["apply", "plan"])
    parser.add_argument("--appdir", required=True)
    parser.add_argument("--runuser", default="")
    parser.add_argument("--venv", default="")
    parser.add_argument("--rotate", default="right")
    parser.add_argument("--root", default="/")
    args = parser.parse_args(argv)

    subs = {
        "RUNUSER": args.runuser,
        "APPDIR": args.appdir,
        "VENV": args.venv,
        "XRANDR_ROTATE": args.rotate,
    }
    report = apply(args.appdir, subs, root=args.root, dry_run=(args.action == "plan"))
    for line in report.to_lines():
        print(f"redeploy: {line}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
