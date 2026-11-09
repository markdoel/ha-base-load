"""Tests for the pure baseline logic."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.base_load.baseline import (
    MIN_SIGMA_W,
    MeteredSeries,
    NightLoad,
    age_weight,
    build_baseline,
    combine_house_load,
    coverage,
    day_type,
    evaluate,
    in_night_window,
    last_night_load,
    night_date,
    robust_stats,
    weighted_median,
    window_hours,
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


def test_combine_adds_sources_and_subtracts_full_charging_hours():
    grid = {at(3, 1): 300.0, at(3, 2): 2300.0, at(3, 3): -50.0}
    battery = MeteredSeries(mean={at(3, 1): 0.0, at(3, 2): 0.0, at(3, 3): 400.0})
    wallbox = MeteredSeries(
        mean={at(3, 1): 0.0, at(3, 2): 1850.0, at(3, 3): 0.0},
        min={at(3, 1): 0.0, at(3, 2): 1820.0, at(3, 3): 0.0},
        max={at(3, 1): 0.0, at(3, 2): 1880.0, at(3, 3): 0.0},
    )

    load = combine_house_load(grid, [battery], [wallbox], excluded_threshold=50)

    assert load == {at(3, 1): 300.0, at(3, 2): 450.0, at(3, 3): 350.0}


def test_combine_drops_hours_in_which_excluded_load_switched():
    grid = {at(3, 1): 1200.0}
    wallbox = MeteredSeries(mean={at(3, 1): 900.0}, min={at(3, 1): 0.0}, max={at(3, 1): 1850.0})
    assert combine_house_load(grid, [], [wallbox], excluded_threshold=50) == {}


def test_combine_without_min_max_drops_active_excluded_hours():
    grid = {at(3, 1): 2300.0, at(3, 2): 320.0}
    wallbox = MeteredSeries(mean={at(3, 1): 1850.0, at(3, 2): 10.0})
    assert combine_house_load(grid, [], [wallbox], excluded_threshold=50) == {at(3, 2): 310.0}


def test_combine_handles_missing_source_data():
    grid = {at(3, h): 300.0 for h in range(1, 7)}
    # Data starts at 02:00. Idle (0 W) at 02:00, silent at 03:00 and 04:00,
    # 150 W at 05:00, silent again at 06:00.
    battery = MeteredSeries(mean={at(3, 2): 0.0, at(3, 5): 150.0})

    load = combine_house_load(grid, [battery], [], excluded_threshold=50)

    assert load == {
        at(3, 1): 300.0,  # before the sensor existed
        at(3, 2): 300.0,
        at(3, 3): 300.0,  # bridged: last value was idle
        at(3, 4): 300.0,
        at(3, 5): 450.0,
        # 06:00 dropped: the battery was delivering before it went silent
    }


def test_combine_does_not_bridge_old_gaps():
    grid = {at(3, 1): 300.0, at(4, 1): 300.0}
    battery = MeteredSeries(mean={at(3, 1): 0.0})
    assert combine_house_load(grid, [battery], [], excluded_threshold=50) == {at(3, 1): 300.0}


def test_combine_drops_gap_of_a_charging_car():
    grid = {at(3, 1): 2150.0, at(3, 2): 2150.0}
    wallbox = MeteredSeries(mean={at(3, 1): 1850.0}, min={at(3, 1): 1840.0}, max={at(3, 1): 1860.0})
    assert combine_house_load(grid, [], [wallbox], excluded_threshold=50) == {at(3, 1): 300.0}


def test_combine_skips_hours_without_grid_value():
    battery = MeteredSeries(mean={at(3, 1): 100.0})
    assert combine_house_load({}, [battery], [], excluded_threshold=50) == {}


def test_weighted_median():
    assert weighted_median([(1, 1), (2, 1), (3, 1)]) == 2
    assert weighted_median([(1, 1), (2, 1), (3, 1), (4, 1)]) == 2.5
    assert weighted_median([(100, 1), (500, 3)]) == 500


def test_age_weight():
    now = at(8, 0)
    assert age_weight(now, now, 14) == 1
    assert age_weight(now - timedelta(days=14), now, 14) == pytest.approx(0.5)
    assert age_weight(now - timedelta(days=14), now, 0) == 1
    assert age_weight(now - timedelta(days=14), None, 14) == 1


def test_weighted_baseline_follows_recent_change():
    """Four weeks at 300 W, then ten days at 400 W (heating season)."""
    now = datetime(2026, 11, 8, 12, tzinfo=TZ)
    load = {}
    for days_ago in range(1, 39):
        day = now - timedelta(days=days_ago)
        value = 400.0 if days_ago <= 10 else 300.0
        for hour in range(1, 5):
            load[day.replace(hour=hour)] = value

    plain = build_baseline(load, 1, 5)
    weighted = build_baseline(load, 1, 5, now=now, half_life_days=7)

    assert plain["workday"].median == 300
    assert weighted["workday"].median == 400


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


def test_last_night_load_skips_running_night():
    load = {at(7, h): 400.0 for h in range(1, 5)}
    load |= {at(8, 1): 800.0, at(8, 2): 800.0}

    # Sunday 02:30: the current night is still running.
    running = datetime(2026, 11, 8, 2, 30, tzinfo=TZ)
    assert last_night_load(load, running, 1, 5) == NightLoad(date(2026, 11, 7), 400.0, 4)

    # Sunday 06:00: the night ending Sunday is complete.
    done = datetime(2026, 11, 8, 6, 0, tzinfo=TZ)
    assert last_night_load(load, done, 1, 5) == NightLoad(date(2026, 11, 8), 800.0, 2)


def test_last_night_load_before_window_on_same_day():
    load = {at(7, h): 400.0 for h in range(1, 5)}
    # Sunday 00:30, window 1-5: the night ending Sunday has not started.
    early = datetime(2026, 11, 8, 0, 30, tzinfo=TZ)
    assert last_night_load(load, early, 1, 5) == NightLoad(date(2026, 11, 7), 400.0, 4)


def test_last_night_load_without_data():
    now = datetime(2026, 11, 8, 12, tzinfo=TZ)
    assert last_night_load({}, now, 1, 5) == NightLoad(date(2026, 11, 8), None, 0)


def test_last_night_load_ignores_single_appliance_run():
    """Dishwasher at 01:00, otherwise a normal night."""
    load = {at(9, 1): 840.0, at(9, 2): 476.0, at(9, 3): 429.0, at(9, 4): 421.0}
    done = datetime(2026, 11, 9, 7, 0, tzinfo=TZ)
    assert last_night_load(load, done, 1, 5) == NightLoad(date(2026, 11, 9), 452.5, 4)


def test_last_night_does_not_fall_back_to_older_nights():
    load = {at(7, h): 400.0 for h in range(1, 5)}
    done = datetime(2026, 11, 8, 6, 0, tzinfo=TZ)
    assert last_night_load(load, done, 1, 5) == NightLoad(date(2026, 11, 8), None, 0)


def test_window_hours_and_coverage():
    assert window_hours(1, 5) == 4
    assert window_hours(23, 5) == 6
    load = {at(7, h): 400.0 for h in range(1, 5)} | {at(8, 1): 400.0}
    nights = [date(2026, 11, 7), date(2026, 11, 8)]
    assert coverage(load, nights, 1, 5) == 5 / 8
    assert coverage(load, [], 1, 5) is None
