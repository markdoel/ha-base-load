"""Tests for the pure baseline logic."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.base_load.baseline import (
    MIN_SIGMA_W,
    build_baseline,
    combine_house_load,
    day_type,
    evaluate,
    in_night_window,
    last_night_mean,
    night_date,
    robust_stats,
)

TZ = ZoneInfo("Europe/Berlin")


def at(day: int, hour: int) -> datetime:
    """November 2026: the 2nd is a Monday, the 7th a Saturday."""
    return datetime(2026, 11, day, hour, tzinfo=TZ)


@pytest.mark.parametrize(
    ("hour", "start", "end", "expected"),
    [
        (1, 1, 5, True),
        (4, 1, 5, True),
        (5, 1, 5, False),
        (0, 1, 5, False),
        (23, 23, 5, True),
        (2, 23, 5, True),
        (12, 23, 5, False),
        (3, 3, 3, False),
    ],
)
def test_in_night_window(hour, start, end, expected):
    assert in_night_window(hour, start, end) is expected


def test_night_date_wraps_to_next_morning():
    assert night_date(at(6, 23), 23, 5) == date(2026, 11, 7)
    assert night_date(at(7, 2), 23, 5) == date(2026, 11, 7)
    assert night_date(at(7, 2), 1, 5) == date(2026, 11, 7)


def test_day_type_uses_morning_after():
    # Friday 23:00 belongs to the night ending Saturday morning.
    assert day_type(at(6, 23), 23, 5) == "weekend"
    assert day_type(at(6, 2), 1, 5) == "workday"
    assert day_type(at(8, 2), 1, 5) == "weekend"


def test_combine_adds_sources_and_drops_excluded_hours():
    grid = {at(3, 1): 300.0, at(3, 2): 2300.0, at(3, 3): -50.0}
    battery = {at(3, 3): 400.0}
    wallbox = {at(3, 1): 0.0, at(3, 2): 1850.0}

    load = combine_house_load(grid, [battery], [wallbox], excluded_threshold=50)

    assert load == {at(3, 1): 300.0, at(3, 3): 350.0}


def test_combine_skips_hours_without_grid_value():
    load = combine_house_load({}, [{at(3, 1): 100.0}], [], excluded_threshold=50)
    assert load == {}


def test_robust_stats_ignores_outliers():
    stats = robust_stats([400, 410, 420, 430, 440, 2500])
    assert stats.median == 425
    assert stats.mad == 15
    assert stats.count == 6


def test_robust_stats_empty():
    assert robust_stats([]) is None


def test_build_baseline_splits_day_types_and_ignores_daytime():
    load = {}
    for day in range(2, 9):
        for hour in range(24):
            load[at(day, hour)] = 900.0
        for hour in range(1, 5):
            load[at(day, hour)] = 300.0 if day < 7 else 500.0

    baseline = build_baseline(load, 1, 5)

    assert baseline["workday"].median == 300
    assert baseline["workday"].count == 20
    assert baseline["weekend"].median == 500
    assert baseline["weekend"].count == 8


def test_evaluate_flags_both_directions():
    stats = robust_stats([400, 410, 420, 430, 440])
    high = evaluate(600, stats, sensitivity=3, min_deviation=60)
    low = evaluate(250, stats, sensitivity=3, min_deviation=60)
    normal = evaluate(445, stats, sensitivity=3, min_deviation=60)

    assert high.is_anomaly and high.direction == "high"
    assert low.is_anomaly and low.direction == "low"
    assert not normal.is_anomaly


def test_evaluate_respects_min_deviation_on_quiet_baseline():
    stats = robust_stats([400, 400, 400, 400])
    assert stats.sigma == MIN_SIGMA_W
    # 40 W is 4 sigma but below the minimum deviation.
    assert not evaluate(440, stats, sensitivity=3, min_deviation=60).is_anomaly


def test_last_night_mean_skips_running_night():
    load = {at(7, h): 400.0 for h in range(1, 5)}
    load |= {at(8, 1): 800.0, at(8, 2): 800.0}

    # Sunday 02:30: the current night is still running.
    running = datetime(2026, 11, 8, 2, 30, tzinfo=TZ)
    assert last_night_mean(load, running, 1, 5) == (date(2026, 11, 7), 400.0)

    # Sunday 06:00: the night ending Sunday is complete.
    done = datetime(2026, 11, 8, 6, 0, tzinfo=TZ)
    assert last_night_mean(load, done, 1, 5) == (date(2026, 11, 8), 800.0)


def test_last_night_mean_before_window_on_same_day():
    load = {at(7, h): 400.0 for h in range(1, 5)}
    # Sunday 00:30, window 1-5: the night ending Sunday has not started.
    early = datetime(2026, 11, 8, 0, 30, tzinfo=TZ)
    assert last_night_mean(load, early, 1, 5) == (date(2026, 11, 7), 400.0)


def test_last_night_mean_without_data():
    now = datetime(2026, 11, 8, 12, tzinfo=TZ)
    assert last_night_mean({}, now, 1, 5) is None
