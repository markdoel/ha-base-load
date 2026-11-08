"""Pure calculation logic for the base load baseline.

Deliberately free of Home Assistant imports so it can be tested directly.
All timestamps are local, timezone-aware datetimes marking the start of a period.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import fmean, median

from .const import DAY_TYPE_WEEKEND, DAY_TYPE_WORKDAY, DIRECTION_HIGH, DIRECTION_LOW

# Scale factor turning a MAD into a standard deviation for normal data.
MAD_TO_SIGMA = 1.4826
# Lower bound for the spread so very quiet nights do not produce absurd scores.
MIN_SIGMA_W = 10.0

Series = Mapping[datetime, float]


@dataclass(frozen=True, slots=True)
class BaselineStats:
    """Robust statistics of the night load for one day type."""

    median: float
    mad: float
    count: int

    @property
    def sigma(self) -> float:
        """Robust spread in watts."""
        return max(self.mad * MAD_TO_SIGMA, MIN_SIGMA_W)


@dataclass(frozen=True, slots=True)
class Evaluation:
    """Result of comparing a reading with the baseline."""

    deviation: float
    score: float
    is_anomaly: bool

    @property
    def direction(self) -> str:
        """'high' for extra consumption, 'low' for missing consumption."""
        return DIRECTION_HIGH if self.deviation >= 0 else DIRECTION_LOW


def in_night_window(hour: int, start: int, end: int) -> bool:
    """Return whether an hour lies in the night window (may wrap midnight)."""
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def night_date(moment: datetime, start: int, end: int) -> date:
    """Date of the morning on which the night containing this moment ends."""
    if start > end and moment.hour >= start:
        return (moment + timedelta(days=1)).date()
    return moment.date()


def day_type_of(day: date) -> str:
    """Workday or weekend for the morning after a night."""
    return DAY_TYPE_WEEKEND if day.weekday() >= 5 else DAY_TYPE_WORKDAY


def day_type(moment: datetime, start: int, end: int) -> str:
    """Day type of the night a moment belongs to."""
    return day_type_of(night_date(moment, start, end))


def combine_house_load(
    grid: Series,
    feed_ins: Iterable[Series],
    excluded: Iterable[Series],
    excluded_threshold: float,
) -> dict[datetime, float]:
    """Compute the household load per period.

    House load = grid import + local sources (PV, battery) - excluded loads.
    Periods without a grid value are dropped. Missing sources count as 0.
    Periods in which an excluded load ran above the threshold (for example the
    car charging) are dropped entirely: subtracting it is too inaccurate at the
    edges of a charging session.
    """
    feed_in_list = list(feed_ins)
    excluded_list = list(excluded)
    result: dict[datetime, float] = {}

    for moment, grid_value in grid.items():
        excluded_values = [series.get(moment, 0.0) for series in excluded_list]
        if any(value > excluded_threshold for value in excluded_values):
            continue
        feed_in = sum(series.get(moment, 0.0) for series in feed_in_list)
        result[moment] = grid_value + feed_in - sum(excluded_values)

    return result


def robust_stats(values: Iterable[float]) -> BaselineStats | None:
    """Median and median absolute deviation (MAD)."""
    data = list(values)
    if not data:
        return None
    mid = median(data)
    mad = median(abs(value - mid) for value in data)
    return BaselineStats(median=mid, mad=mad, count=len(data))


def build_baseline(house_load: Series, start: int, end: int) -> dict[str, BaselineStats]:
    """Baseline per day type from all night hours."""
    buckets: dict[str, list[float]] = {DAY_TYPE_WORKDAY: [], DAY_TYPE_WEEKEND: []}
    for moment, value in house_load.items():
        if in_night_window(moment.hour, start, end):
            buckets[day_type(moment, start, end)].append(value)

    baseline: dict[str, BaselineStats] = {}
    for key, values in buckets.items():
        stats = robust_stats(values)
        if stats is not None:
            baseline[key] = stats
    return baseline


def last_night_mean(
    house_load: Series, now: datetime, start: int, end: int
) -> tuple[date, float] | None:
    """Mean load of the most recent fully completed night."""
    nights: dict[date, list[float]] = {}
    for moment, value in house_load.items():
        if in_night_window(moment.hour, start, end):
            nights.setdefault(night_date(moment, start, end), []).append(value)

    current = night_date(now, start, end)
    current_finished = not in_night_window(now.hour, start, end) and (
        start > end or now.hour >= end
    )
    candidates = [day for day in nights if day < current or (day == current and current_finished)]
    if not candidates:
        return None
    latest = max(candidates)
    return latest, fmean(nights[latest])


def evaluate(
    current: float,
    stats: BaselineStats,
    sensitivity: float,
    min_deviation: float,
) -> Evaluation:
    """Evaluate a reading. Anomalies count in both directions.

    Extra load hints at devices left running, missing load for example at a
    fridge or freezer that stopped working.
    """
    deviation = current - stats.median
    score = deviation / stats.sigma
    is_anomaly = abs(deviation) >= min_deviation and abs(score) >= sensitivity
    return Evaluation(deviation=deviation, score=score, is_anomaly=is_anomaly)
