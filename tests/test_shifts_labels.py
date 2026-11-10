"""Tests for level shifts and labelled patterns."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.base_load.causes import (
    PATTERN_CONSTANT,
    PATTERN_INTERMITTENT,
    Pattern,
    label_from,
    match_label,
)
from custom_components.base_load.shifts import detect_shift, night_medians

TZ = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 11, 10)


def nights(values: dict[int, float]) -> dict[date, float]:
    """Days ago -> value."""
    return {TODAY - timedelta(days=ago): value for ago, value in values.items()}


def test_shift_detected_with_start_date():
    data = nights({ago: (88.0 if ago <= 8 else 12.0 + ago % 3) for ago in range(0, 40)})
    shift = detect_shift("sensor.heizung", data, TODAY, 10, 3, 2)
    assert shift is not None
    assert shift.since == TODAY - timedelta(days=8)
    assert round(shift.delta) == 75
    assert shift.key == f"sensor.heizung|{shift.since.isoformat()}"


def test_shift_down():
    data = nights({ago: (0.0 if ago <= 3 else 60.0) for ago in range(0, 30)})
    shift = detect_shift("sensor.freezer", data, TODAY, 10, 3, 2)
    assert shift is not None and shift.delta < 0


def test_no_shift_for_a_single_high_night_or_noise():
    single = nights({ago: (300.0 if ago == 0 else 20.0) for ago in range(0, 30)})
    assert detect_shift("sensor.x", single, TODAY, 10, 3, 2) is None
    noisy = nights({ago: 20.0 + (ago % 4) * 5 for ago in range(0, 30)})
    assert detect_shift("sensor.x", noisy, TODAY, 10, 3, 2) is None


def test_no_shift_without_enough_reference():
    data = nights({ago: (90.0 if ago <= 3 else 10.0) for ago in range(0, 12)})
    assert detect_shift("sensor.x", data, TODAY, 10, 3, 2) is None


def test_night_medians_respects_valid_hours():
    base = datetime(2026, 11, 9, tzinfo=TZ)
    series = {base.replace(hour=h): float(h * 10) for h in range(1, 5)}
    assert night_medians(series, 1, 5) == {date(2026, 11, 9): 25.0}
    valid = [base.replace(hour=1), base.replace(hour=2)]
    assert night_medians(series, 1, 5, valid) == {date(2026, 11, 9): 15.0}


def pattern(onset_hour: int, onset_minute: int, magnitude: float, minutes: int, shape=None):
    return Pattern(
        onset=datetime(2026, 11, 9, onset_hour, onset_minute, tzinfo=TZ),
        shape=shape or PATTERN_INTERMITTENT,
        share=0.4,
        magnitude=magnitude,
        duration=timedelta(minutes=minutes),
    )


def test_label_round_trip_and_tolerances():
    dishwasher = label_from(" Dishwasher ", pattern(0, 10, 1800, 80))
    assert dishwasher is not None and dishwasher.name == "Dishwasher"
    heater = label_from("Heater", pattern(23, 30, 900, 300, PATTERN_CONSTANT))
    labels = [dishwasher, heater]

    assert match_label(pattern(0, 40, 1700, 90), labels) == "Dishwasher"
    # Onset across midnight still matches: 23:50 vs 23:30.
    assert match_label(pattern(23, 50, 950, 280, PATTERN_CONSTANT), labels) == "Heater"
    # Too weak, too late, too long.
    assert match_label(pattern(0, 10, 800, 80), labels) is None
    assert match_label(pattern(4, 0, 1800, 80), labels) is None
    assert match_label(pattern(0, 10, 1800, 200), labels) is None
    assert match_label(None, labels) is None


def test_label_needs_a_pattern():
    vague = Pattern(onset=None, shape=None, share=0.0)
    assert label_from("x", vague) is None
