from __future__ import annotations

from pathlib import Path

import pytest

from usage_dashboard.client.brightness import (
    level_for_step,
    load_level,
    save_level,
    step_for_level,
)


class TestStepMath:
    def test_rails_map_to_visible_min_and_max(self) -> None:
        assert level_for_step(10, 10, 31) == 31  # top step -> max
        assert level_for_step(1, 10, 31) >= 1    # bottom step is visible
        assert level_for_step(1, 10, 31) == round(31 / 10)

    def test_step_clamped_into_range(self) -> None:
        assert level_for_step(0, 10, 31) == level_for_step(1, 10, 31)
        assert level_for_step(99, 10, 31) == 31

    def test_step_for_level_is_rough_inverse(self) -> None:
        for step in range(1, 11):
            level = level_for_step(step, 10, 31)
            assert step_for_level(level, 10, 31) == step

    def test_step_for_level_clamps_and_floors_at_one(self) -> None:
        assert step_for_level(0, 10, 31) == 1     # off reads as the lowest step
        assert step_for_level(31, 10, 31) == 10
        assert step_for_level(999, 10, 31) == 10  # above max clamps

    def test_degenerate_inputs_dont_blow_up(self) -> None:
        assert level_for_step(5, 0, 31) == 31     # no steps -> full
        assert level_for_step(5, 10, 0) == 1      # no range -> visible floor
        assert step_for_level(5, 0, 31) == 1
        assert step_for_level(5, 10, 0) == 1

    @pytest.mark.parametrize("steps", [9, 10, 11])
    def test_count_is_tunable(self, steps: int) -> None:
        # Whatever the configured granularity, the rails still hit floor and max.
        assert level_for_step(steps, steps, 31) == 31
        assert step_for_level(31, steps, 31) == steps


class TestPersistence:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "state" / "brightness"  # parent created on save
        save_level(path, 17)
        assert load_level(path) == 17

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert load_level(tmp_path / "absent") is None

    def test_garbage_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "brightness"
        path.write_text("not-a-number")
        assert load_level(path) is None

    def test_save_swallows_unwritable_path(self, tmp_path: Path) -> None:
        # A path whose parent is a file (can't be a dir) makes mkdir fail; save
        # must degrade silently rather than crash the GUI.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        save_level(blocker / "child" / "brightness", 5)  # must not raise
