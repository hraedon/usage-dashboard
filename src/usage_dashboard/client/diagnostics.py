"""Unit diagnostics for the touch GUI's status overlay.

Gathers the few things you actually want when standing in front of a wall-mounted
panel and wondering why it's stale: how to *reach* this unit (hostname + IPs),
which server it points at, what code it's running, and whether the auto-updater
is healthy. The gathering touches the environment (sockets, files, a `git`
call); the parsing and formatting are pure so they're unit-tested without it.

Update health comes from two tiny files the updater (`deploy/pi/update.sh`)
writes on every run — there's no daemon to query, so the updater leaves a
breadcrumb and we read it:

* ``update-last-check``  — ``<iso8601> <result> <short-commit>`` every run
  (``result`` ∈ up-to-date / updated / pip-failed / import-failed)
* ``update-last-change`` — ``<iso8601> <old-short> <new-short>`` only when the
  code actually moved.

Both live under the same state dir as the persisted brightness level.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_RESULT_LABELS = {
    "up-to-date": "ok",
    "updated": "updated",
    "pip-failed": "pip failed",
    "import-failed": "import failed",
}
_FAILED_RESULTS = frozenset({"pip-failed", "import-failed"})


@dataclass(frozen=True)
class UpdateCheck:
    when: datetime | None
    result: str
    commit: str


@dataclass(frozen=True)
class UpdateChange:
    when: datetime | None
    old: str
    new: str


@dataclass(frozen=True)
class Diagnostics:
    hostname: str
    addresses: list[str]
    server_host: str
    running_commit: str
    check: UpdateCheck | None
    change: UpdateChange | None


@dataclass(frozen=True)
class DiagLine:
    label: str
    value: str
    warn: bool = False


def default_state_dir() -> Path | None:
    """The shared per-unit state dir (``$XDG_STATE_HOME`` or ``~/.local/state``),
    or None if even the home dir can't be resolved. The updater writes its
    status files here and the GUI reads them, so the two must agree — keep this
    in lockstep with the ``${XDG_STATE_HOME:-$HOME/.local/state}`` in
    ``deploy/pi/update.sh``."""
    base = os.environ.get("XDG_STATE_HOME")
    try:
        root = Path(base) if base else Path.home() / ".local" / "state"
    except (RuntimeError, OSError):
        return None
    return root / "usage-dashboard"


# -- parsing (pure) ---------------------------------------------------------


def _parse_dt(token: str) -> datetime | None:
    try:
        return datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_check(path: Path) -> UpdateCheck | None:
    """Parse ``update-last-check``; None if absent/empty/unreadable."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    parts = raw.split()
    return UpdateCheck(
        when=_parse_dt(parts[0]) if parts else None,
        result=parts[1] if len(parts) > 1 else "unknown",
        commit=parts[2] if len(parts) > 2 else "",
    )


def parse_change(path: Path) -> UpdateChange | None:
    """Parse ``update-last-change``; None if absent/empty/unreadable."""
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    parts = raw.split()
    return UpdateChange(
        when=_parse_dt(parts[0]) if parts else None,
        old=parts[1] if len(parts) > 1 else "",
        new=parts[2] if len(parts) > 2 else "",
    )


def format_ago(when: datetime | None, now: datetime) -> str:
    """A compact 'how long ago': just now / Nm / Nh / Nd ago."""
    if when is None:
        return "never"
    delta = (now - when).total_seconds()
    if delta < 60:
        return "just now"
    minutes = int(delta // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def diagnostic_lines(diag: Diagnostics, now: datetime) -> list[DiagLine]:
    """Label/value rows for the overlay's left column. An unhealthy updater (a
    rolled-back install, or no record at all) is flagged so the GUI can colour
    it — that's the line worth noticing on a stale panel."""
    lines = [DiagLine("Host", diag.hostname or "—")]
    if diag.addresses:
        lines.append(DiagLine("IP", diag.addresses[0]))
        lines.extend(DiagLine("", ip) for ip in diag.addresses[1:])
    else:
        lines.append(DiagLine("IP", "—"))
    if diag.server_host:
        lines.append(DiagLine("Server", diag.server_host))

    if diag.check is not None:
        failed = diag.check.result in _FAILED_RESULTS
        state = "(rolled back)" if failed else "(current)"
        lines.append(
            DiagLine("Commit", f"{diag.running_commit or '—'} {state}".strip())
        )
        label = _RESULT_LABELS.get(diag.check.result, diag.check.result)
        lines.append(
            DiagLine("Update", f"{label} · {format_ago(diag.check.when, now)}",
                     warn=failed)
        )
    else:
        lines.append(DiagLine("Commit", diag.running_commit or "—"))
        lines.append(DiagLine("Update", "no record", warn=True))

    if diag.change is not None and diag.change.when is not None:
        lines.append(DiagLine("Changed", format_ago(diag.change.when, now)))
    return lines


# -- gathering (touches the environment) ------------------------------------


def _url_host(server_url: str) -> str:
    try:
        return urlsplit(server_url).hostname or ""
    except ValueError:
        return ""


def _primary_ip() -> str | None:
    """The source IP the kernel would use to reach the internet, via a UDP
    *connect* (sets a default peer; sends nothing) so there's no DNS lookup and
    no traffic — fast and safe to call on the render thread."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]  # type: ignore[no-any-return]
    except OSError:
        return None
    finally:
        sock.close()


def _is_loopback_or_linklocal(ip: str) -> bool:
    return ip.startswith("127.") or ip == "::1" or ip.lower().startswith("fe80")


def local_addresses() -> list[str]:
    """This unit's reachable IPs, primary first, loopback/link-local dropped."""
    addrs: list[str] = []
    primary = _primary_ip()
    if primary:
        addrs.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = str(info[4][0])
            if ip not in addrs and not _is_loopback_or_linklocal(ip):
                addrs.append(ip)
    except OSError:
        pass
    return addrs


def _git_commit() -> str:
    """Short SHA of the running checkout (editable install lives in the repo),
    or '' off a git tree / on failure."""
    try:
        repo = Path(__file__).resolve().parents[3]
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def gather(state_dir: Path | None, server_url: str = "") -> Diagnostics:
    """Snapshot the unit's diagnostics. Cheap enough to call when the overlay
    opens; every piece degrades to a blank/`—` rather than raising. Time-relative
    formatting (e.g. 'updated 4m ago') is applied later by :func:`diagnostic_lines`
    against the draw-time clock, so the snapshot itself carries raw timestamps."""
    check = parse_check(state_dir / "update-last-check") if state_dir else None
    change = parse_change(state_dir / "update-last-change") if state_dir else None
    running = check.commit if (check and check.commit) else _git_commit()
    return Diagnostics(
        hostname=socket.gethostname(),
        addresses=local_addresses(),
        server_host=_url_host(server_url),
        running_commit=running,
        check=check,
        change=change,
    )
