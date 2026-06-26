from __future__ import annotations

from pathlib import Path

from usage_dashboard.client.backlight import Backlight


def _make_device(base: Path, name: str = "panel_backlight@1",
                 brightness: int = 15, max_brightness: int = 31) -> Path:
    dev = base / name
    dev.mkdir(parents=True)
    (dev / "brightness").write_text(str(brightness))
    (dev / "max_brightness").write_text(str(max_brightness))
    return dev


def _brightness(dev: Path) -> str:
    return (dev / "brightness").read_text()


class TestBacklight:
    def test_no_base_dir_is_unavailable_and_noop(self, tmp_path: Path) -> None:
        bl = Backlight(base=tmp_path / "missing")
        assert bl.available is False
        bl.set_power(False)  # must not raise
        bl.set_power(True)

    def test_empty_base_is_unavailable(self, tmp_path: Path) -> None:
        assert Backlight(base=tmp_path).available is False

    def test_off_zeroes_brightness_on_restores_prior_level(self, tmp_path: Path) -> None:
        dev = _make_device(tmp_path, brightness=15)
        bl = Backlight(base=tmp_path)
        assert bl.available is True

        bl.set_power(False)
        assert _brightness(dev) == "0"
        bl.set_power(True)
        assert _brightness(dev) == "15"  # restored to the startup level

    def test_wake_level_falls_back_to_max_when_started_dark(self, tmp_path: Path) -> None:
        dev = _make_device(tmp_path, brightness=0, max_brightness=31)
        bl = Backlight(base=tmp_path)
        bl.set_power(True)
        assert _brightness(dev) == "31"  # never restore to off

    def test_redundant_writes_skipped(self, tmp_path: Path) -> None:
        dev = _make_device(tmp_path, brightness=15)
        bl = Backlight(base=tmp_path)
        bl.set_power(False)
        (dev / "brightness").write_text("tampered")  # external change we must not clobber
        bl.set_power(False)                           # same state -> skip the write
        assert _brightness(dev) == "tampered"
        bl.set_power(True)                            # state change -> writes
        assert _brightness(dev) == "15"

    def test_max_and_current_level_reported(self, tmp_path: Path) -> None:
        dev = _make_device(tmp_path, brightness=15, max_brightness=31)
        bl = Backlight(base=tmp_path)
        assert bl.max_level == 31
        assert bl.current_level == 15
        (dev / "brightness").write_text("7")
        assert bl.current_level == 7  # read fresh each time

    def test_set_level_writes_clamped_value(self, tmp_path: Path) -> None:
        dev = _make_device(tmp_path, brightness=15, max_brightness=31)
        bl = Backlight(base=tmp_path)
        bl.set_level(20)
        assert _brightness(dev) == "20"
        bl.set_level(999)  # above max -> clamped to max
        assert _brightness(dev) == "31"
        bl.set_level(0)  # never blanks via a level set (0 is sleep's job)
        assert _brightness(dev) == "1"

    def test_set_level_survives_a_sleep_wake_cycle(self, tmp_path: Path) -> None:
        # The whole point of updating the wake-restore level: a user-chosen
        # brightness must come back after the panel sleeps, not snap to startup.
        dev = _make_device(tmp_path, brightness=15)
        bl = Backlight(base=tmp_path)
        bl.set_level(8)
        bl.set_power(False)
        assert _brightness(dev) == "0"
        bl.set_power(True)
        assert _brightness(dev) == "8"  # restored the chosen level, not 15

    def test_set_level_noop_without_device(self, tmp_path: Path) -> None:
        Backlight(base=tmp_path / "missing").set_level(10)  # must not raise

    def test_write_error_is_swallowed(self, tmp_path: Path) -> None:
        # brightness as a directory makes write_text raise OSError; the loop must
        # not crash on a permissions/IO failure.
        dev = tmp_path / "panel_backlight@1"
        (dev / "brightness").mkdir(parents=True)
        (dev / "max_brightness").write_text("31")
        bl = Backlight(base=tmp_path)
        assert bl.available is True
        bl.set_power(False)  # must not raise
