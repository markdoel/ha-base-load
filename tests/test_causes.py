"""Tests for the cause analysis logic."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.base_load.baseline import robust_stats
from custom_components.base_load.causes import (
    PATTERN_CONSTANT,
    PATTERN_INTERMITTENT,
    Cause,
    Explanation,
    describe_pattern,
    mean_in,
    rank_causes,
    residual_series,
    window_bounds,
)

TZ = ZoneInfo("Europe/Berlin")
HEATER = "sensor.heater"
FRIDGE = "sensor.fridge"
FREEZER = "sensor.freezer"


def at(hour: int, minute: int = 0, day: int = 7) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def test_rank_causes_picks_devices_in_same_direction():
    baselines = {
        HEATER: robust_stats([20, 21, 19, 20]),
        FRIDGE: robust_stats([30, 10, 40, 20]),  # cycling, wide spread
        FREEZER: robust_stats([15, 15, 16, 15]),
    }
    values = {HEATER: 320.0, FRIDGE: 45.0, FREEZER: 0.0}

    causes = rank_causes(300.0, values, baselines)

    # Fridge moved but within its own spread, freezer moved the other way.
    assert [cause.entity_id for cause in causes] == [HEATER]
    assert causes[0].deviation == 300


def test_rank_causes_for_missing_load():
    baselines = {FREEZER: robust_stats([60, 62, 58, 60])}
    causes = rank_causes(-60.0, {FREEZER: 0.0}, baselines)
    assert causes[0].entity_id == FREEZER
    assert causes[0].deviation == -60


def test_rank_causes_ignores_tiny_changes_and_unknown_devices():
    baselines = {HEATER: robust_stats([2, 2, 2, 2])}
    assert rank_causes(100.0, {HEATER: 8.0, "sensor.unknown": 500.0}, baselines) == []


def test_explanation_rest():
    explanation = Explanation(
        total=240.0, causes=[Cause(HEATER, deviation=90.0, baseline=20, current=110)]
    )
    assert explanation.explained == 90
    assert explanation.unexplained == 150


def test_residual_removes_cause_excess():
    house = {at(2): 700.0, at(3): 400.0}
    devices = {HEATER: {at(2): 320.0, at(3): 20.0}}
    baselines = {HEATER: robust_stats([20, 20, 20])}
    causes = [Cause(HEATER, 300.0, 20.0, 320.0)]

    assert residual_series(house, devices, baselines, causes) == {
        at(2): 400.0,
        at(3): 400.0,
    }


def five_minute(start: datetime, end: datetime, value_fn) -> dict[datetime, float]:
    series, moment = {}, start
    while moment < end:
        series[moment] = value_fn(moment)
        moment += timedelta(minutes=5)
    return series


def test_pattern_constant_with_onset_before_window():
    # +240 W from 23:40 on, window 01:00 to 05:00.
    onset = at(23, 40, day=6)
    series = five_minute(at(22, 0, day=6), at(5), lambda m: 600.0 if m >= onset else 360.0)
    pattern = describe_pattern(series, 360.0, 60.0, at(1), at(5), direction=1.0)
    assert pattern.shape == PATTERN_CONSTANT
    assert pattern.onset == onset


def test_pattern_intermittent():
    series = five_minute(at(1), at(5), lambda m: 600.0 if m.minute < 20 else 360.0)
    pattern = describe_pattern(series, 360.0, 60.0, at(1), at(5), direction=1.0)
    assert pattern.shape == PATTERN_INTERMITTENT
    assert pattern.onset == at(1)


def test_pattern_none_when_nothing_stands_out():
    series = five_minute(at(1), at(5), lambda m: 370.0)
    pattern = describe_pattern(series, 360.0, 60.0, at(1), at(5), direction=1.0)
    assert pattern.shape is None


def test_window_bounds_and_mean():
    assert window_bounds(date(2026, 11, 7), 1, 5, TZ) == (at(1), at(5))
    assert window_bounds(date(2026, 11, 7), 23, 5, TZ) == (at(23, day=6), at(5))
    series = {at(0): 100.0, at(1): 200.0, at(4): 400.0, at(5): 900.0}
    assert mean_in(series, at(1), at(5)) == 300
    assert mean_in({}, at(1), at(5)) is None
