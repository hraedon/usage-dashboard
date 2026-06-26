from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from usage_dashboard.client import diagnostics as diag
from usage_dashboard.client.diagnostics import (
    Diagnostics,
    UpdateCheck,
    diagnostic_lines,
    format_ago,
    gather,
    local_addresses,
    parse_change,
    parse_check,
)

_NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


class TestParseCheck:
    def test_full_line(self, tmp_path: Path) -> None:
        p = tmp_path / "c"
        p.write_text("2026-06-26T11:50:00Z up-to-date a1b2c3d4\n")
        check = parse_check(p)
        assert check is not None
        assert check.result == "up-to-date"
        assert check.commit == "a1b2c3d4"
        assert check.when == datetime(2026, 6, 26, 11, 50, tzinfo=timezone.utc)

    def test_missing_file(self, tmp_path: Path) -> None:
        assert parse_check(tmp_path / "absent") is None

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "c"
        p.write_text("")
        assert parse_check(p) is None

    def test_partial_line_degrades(self, tmp_path: Path) -> None:
        p = tmp_path / "c"
        p.write_text("not-a-timestamp")
        check = parse_check(p)
        assert check is not None
        assert check.when is None       # unparseable timestamp -> None
        assert check.result == "unknown"
        assert check.commit == ""


class TestParseChange:
    def test_full_line(self, tmp_path: Path) -> None:
        p = tmp_path / "ch"
        p.write_text("2026-06-26T10:00:00Z aaaaaaaa bbbbbbbb")
        change = parse_change(p)
        assert change is not None
        assert (change.old, change.new) == ("aaaaaaaa", "bbbbbbbb")

    def test_missing(self, tmp_path: Path) -> None:
        assert parse_change(tmp_path / "absent") is None


class TestFormatAgo:
    def test_buckets(self) -> None:
        def at(**kw: int) -> datetime:
            from datetime import timedelta
            return _NOW - timedelta(**kw)

        assert format_ago(None, _NOW) == "never"
        assert format_ago(at(seconds=10), _NOW) == "just now"
        assert format_ago(at(minutes=5), _NOW) == "5m ago"
        assert format_ago(at(hours=3), _NOW) == "3h ago"
        assert format_ago(at(days=2), _NOW) == "2d ago"


class TestDiagnosticLines:
    def _diag(self, **over: object) -> Diagnostics:
        base: dict[str, object] = {
            "hostname": "mpmusage01",
            "addresses": ["192.168.1.50"],
            "server_host": "server.lan",
            "running_commit": "a1b2c3d4",
            "check": UpdateCheck(_NOW, "up-to-date", "a1b2c3d4"),
            "change": None,
        }
        base.update(over)
        return Diagnostics(**base)  # type: ignore[arg-type]

    def test_healthy_has_no_warn(self) -> None:
        lines = diagnostic_lines(self._diag(), _NOW)
        labels = {ln.label for ln in lines}
        assert {"Host", "IP", "Server", "Commit", "Update"} <= labels
        assert all(not ln.warn for ln in lines)
        commit = next(ln for ln in lines if ln.label == "Commit")
        assert "(current)" in commit.value

    def test_failed_update_warns_and_marks_rollback(self) -> None:
        lines = diagnostic_lines(
            self._diag(check=UpdateCheck(_NOW, "import-failed", "a1b2c3d4")), _NOW
        )
        update = next(ln for ln in lines if ln.label == "Update")
        commit = next(ln for ln in lines if ln.label == "Commit")
        assert update.warn is True
        assert "import failed" in update.value
        assert "(rolled back)" in commit.value

    def test_no_record_warns(self) -> None:
        lines = diagnostic_lines(self._diag(check=None), _NOW)
        update = next(ln for ln in lines if ln.label == "Update")
        assert update.value == "no record"
        assert update.warn is True

    def test_extra_addresses_get_label_less_rows(self) -> None:
        lines = diagnostic_lines(
            self._diag(addresses=["10.0.0.1", "10.0.0.2"]), _NOW
        )
        ip_rows = [ln for ln in lines if ln.label in ("IP", "")]
        assert ip_rows[0].label == "IP" and ip_rows[0].value == "10.0.0.1"
        assert ip_rows[1].label == "" and ip_rows[1].value == "10.0.0.2"


class TestGather:
    def test_reads_status_files_and_host(self, tmp_path: Path) -> None:
        (tmp_path / "update-last-check").write_text(
            "2026-06-26T11:00:00Z updated e5f6a7b8"
        )
        (tmp_path / "update-last-change").write_text(
            "2026-06-26T11:00:00Z a1b2c3d4 e5f6a7b8"
        )
        d = gather(tmp_path, "https://srv.example:8080/")
        assert d.server_host == "srv.example"
        assert d.running_commit == "e5f6a7b8"   # from the check file, not git
        assert d.check is not None and d.check.result == "updated"
        assert d.change is not None and d.change.new == "e5f6a7b8"
        assert isinstance(d.hostname, str) and d.hostname

    def test_no_state_dir_is_fine(self) -> None:
        d = gather(None, "")
        assert d.check is None and d.change is None
        assert d.server_host == ""

    def test_local_addresses_returns_list(self) -> None:
        # Environment-dependent, but must never raise and never include loopback.
        addrs = local_addresses()
        assert isinstance(addrs, list)
        assert all(not a.startswith("127.") for a in addrs)


def test_url_host_handles_garbage() -> None:
    assert diag._url_host("") == ""
    assert diag._url_host("not a url") == ""
    assert diag._url_host("http://h:8080/x") == "h"


def test_default_state_dir_honors_xdg(monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/xdg-x")
    assert diag.default_state_dir() == Path("/tmp/xdg-x/usage-dashboard")
