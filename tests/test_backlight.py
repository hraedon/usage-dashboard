from __future__ import annotations

from pathlib import Path

from usage_dashboard.client.backlight import Backlight


def _make_device(base: Path, name: str = "10-0045") -> Path:
    dev = base / name
    dev.mkdir(parents=True)
    node = dev / "bl_power"
    node.write_text("0")
    return node


class TestBacklight:
    def test_no_base_dir_is_unavailable_and_noop(self, tmp_path: Path) -> None:
        bl = Backlight(base=tmp_path / "missing")
        assert bl.available is False
        bl.set_power(False)  # must not raise
        bl.set_power(True)

    def test_empty_base_is_unavailable(self, tmp_path: Path) -> None:
        assert Backlight(base=tmp_path).available is False

    def test_discovers_device_and_toggles(self, tmp_path: Path) -> None:
        node = _make_device(tmp_path)
        bl = Backlight(base=tmp_path)
        assert bl.available is True

        bl.set_power(False)
        assert node.read_text() == "1"  # blanked
        bl.set_power(True)
        assert node.read_text() == "0"  # unblanked

    def test_redundant_writes_skipped(self, tmp_path: Path) -> None:
        node = _make_device(tmp_path)
        bl = Backlight(base=tmp_path)
        bl.set_power(False)
        node.write_text("tampered")  # external change we should NOT overwrite
        bl.set_power(False)          # same state -> skip the write
        assert node.read_text() == "tampered"
        bl.set_power(True)           # state change -> writes
        assert node.read_text() == "0"

    def test_write_error_is_swallowed(self, tmp_path: Path) -> None:
        # bl_power as a directory makes write_text raise OSError; the loop must
        # not crash on a permissions/IO failure.
        dev = tmp_path / "10-0045"
        (dev / "bl_power").mkdir(parents=True)
        bl = Backlight(base=tmp_path)
        assert bl.available is True
        bl.set_power(False)  # must not raise
