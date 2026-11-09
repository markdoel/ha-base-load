"""Pure calculation logic for the base load baseline.

Deliberately free of Home Assistant imports so it can be tested directly.
All timestamps are local, timezone-aware datetimes marking the start of a period.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median

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


@dataclass(frozen=True, slots=True)
class MeteredSeries:
    """Hourly or 5-minute statistics of one sensor: mean, min and max per period."""

    mean: Series
    min: Series = field(default_factory=dict)
    max: Series = field(default_factory=dict)

    @property
    def first(self) -> datetime | None:
        """First period with data, the sensor did not exist before."""
        return min(self.mean) if self.mean else None


# A gap is bridged with the last known value only if that value was small and
# recent. Battery hubs often stop reporting at night while idle; a gap after
# real output (or after a charging car) stays unknown and drops the period.
GAP_FILL_MAX_W = 50.0
GAP_FILL_MAX_AGE = timedelta(hours=12)


def _fill_gaps(series: MeteredSeries, moments: Iterable[datetime]) -> MeteredSeries:
    """Complete a series on the given periods.

    - Before the first recorded period the sensor did not exist: 0 W.
    - A gap after a small, recent value: that value, assumed steady.
    - Any other gap: no value, the period will be dropped.
    """
    first = series.first
    mean: dict[datetime, float] = {}
    low: dict[datetime, float] = {}
    high: dict[datetime, float] = {}
    last: tuple[datetime, float] | None = None

    for moment in sorted(moments):
        value = series.mean.get(moment)
        if value is not None:
            mean[moment] = value
            if moment in series.min:
                low[moment] = series.min[moment]
            if moment in series.max:
                high[moment] = series.max[moment]
            last = (moment, value)
            continue
        if first is None or moment < first:
            fill = 0.0
        elif (
            last is not None
            and abs(last[1]) <= GAP_FILL_MAX_W
            and moment - last[0] <= GAP_FILL_MAX_AGE
        ):
            fill = last[1]
        else:
            continue
        mean[moment] = low[moment] = high[moment] = fill

    return MeteredSeries(mean=mean, min=low, max=high)


def _excluded_part(series: MeteredSeries, moment: datetime, threshold: float) -> float | None:
    """Watts to subtract for an excluded load, or None if the period must go.

    Fully off (max <= threshold) or fully on (min > threshold): the mean is
    subtracted. A load that switched within the period (min <= threshold < max)
    drops it, because sensor lag makes the subtraction unreliable at the edges.
    Without min and max a mean above the threshold drops the period, too.
    """
    mean = series.mean.get(moment)
    if mean is None:
        return None
    low = series.min.get(moment)
    high = series.max.get(moment)
    if low is None or high is None:
        return mean if mean <= threshold else None
    if low <= threshold < high:
        return None
    return mean


def combine_house_load(
    grid: Series,
    feed_ins: Iterable[MeteredSeries],
    excluded: Iterable[MeteredSeries],
    excluded_threshold: float,
) -> dict[datetime, float]:
    """Compute the household load per period.

    House load = grid import + local sources (PV, battery) - excluded loads.

    - Periods without a grid value are dropped.
    - Gaps in sources and excluded loads are handled by _fill_gaps; a gap
      that cannot be bridged drops the period, because a battery delivering
      400 W without data would otherwise look like 400 W of extra load.
    - Excluded loads are subtracted when they ran the whole period or not at
      all, periods in which they switched are dropped.
    """
    moments = list(grid)
    feed_in_list = [_fill_gaps(series, moments) for series in feed_ins]
    excluded_list = [_fill_gaps(series, moments) for series in excluded]
    result: dict[datetime, float] = {}

    for moment, grid_value in grid.items():
        feed_in = [series.mean.get(moment) for series in feed_in_list]
        if None in feed_in:
            continue
        parts = [_excluded_part(series, moment, excluded_threshold) for series in excluded_list]
        if None in parts:
            continue
        result[moment] = grid_value + sum(feed_in) - sum(parts)

    return result


def weighted_median(pairs: Iterable[tuple[float, float]]) -> float:
    """Median of (value, weight) pairs; plain median when all weights are equal."""
    ordered = sorted(pairs)
    total = sum(weight for _, weight in ordered)
    half = total / 2
    running = 0.0
    for index, (value, weight) in enumerate(ordered):
        running += weight
        if running > half:
            return value
        if running == half:
            # Exactly on the boundary: average with the next value, as median does.
            return (value + ordered[index + 1][0]) / 2
    return ordered[-1][0]


def robust_stats(
    values: Iterable[float], weights: Iterable[float] | None = None
) -> BaselineStats | None:
    """Median and median absolute deviation (MAD), optionally weighted."""
    data = list(values)
    if not data:
        return None
    weight_list = list(weights) if weights is not None else [1.0] * len(data)
    mid = weighted_median(zip(data, weight_list, strict=True))
    mad = weighted_median(
        (abs(value - mid), weight) for value, weight in zip(data, weight_list, strict=True)
    )
    return BaselineStats(median=mid, mad=mad, count=len(data))


def age_weight(moment: datetime, now: datetime | None, half_life_days: float) -> float:
    """Exponential decay: a value half_life_days old counts half."""
    if now is None or half_life_days <= 0:
        return 1.0
    age_days = max((now - moment).total_seconds() / 86400, 0.0)
    return 0.5 ** (age_days / half_life_days)


def build_baseline(
    house_load: Series,
    start: int,
    end: int,
    now: datetime | None = None,
    half_life_days: float = 0.0,
) -> dict[str, BaselineStats]:
    """Baseline per day type from all night hours.

    With a half life, recent nights weigh more, so the baseline follows lasting
    changes (heating season, a new device) faster without reacting to one night.
    """
    buckets: dict[str, list[tuple[float, float]]] = {
        DAY_TYPE_WORKDAY: [],
        DAY_TYPE_WEEKEND: [],
    }
    for moment, value in house_load.items():
        if in_night_window(moment.hour, start, end):
            weight = age_weight(moment, now, half_life_days)
            buckets[day_type(moment, start, end)].append((value, weight))

    baseline: dict[str, BaselineStats] = {}
    for key, pairs in buckets.items():
        stats = robust_stats([v for v, _ in pairs], [w for _, w in pairs])
        if stats is not None:
            baseline[key] = stats
    return baseline


@dataclass(frozen=True, slots=True)
class NightLoad:
    """Load of one completed night."""

    night: date
    load: float | None
    hours: int


def window_hours(start: int, end: int) -> int:
    """Number of hours in the night window."""
    return (end - start) % 24


def last_completed_night(now: datetime, start: int, end: int) -> date:
    """Morning date of the most recent night whose window is over."""
    current = night_date(now, start, end)
    finished = not in_night_window(now.hour, start, end) and (start > end or now.hour >= end)
    return current if finished else current - timedelta(days=1)


def last_night_load(house_load: Series, now: datetime, start: int, end: int) -> NightLoad:
    """Median load of the most recent completed night, with its valid hours.

    The median of the night hours ignores a single appliance run (a dishwasher
    started at midnight) and only reacts to load that lasts most of the night.
    The night is always the latest completed one: if it has too little data the
    caller has to say so instead of quietly showing an older night.
    """
    night = last_completed_night(now, start, end)
    values = [
        value
        for moment, value in house_load.items()
        if in_night_window(moment.hour, start, end) and night_date(moment, start, end) == night
    ]
    return NightLoad(night=night, load=median(values) if values else None, hours=len(values))


def coverage(house_load: Series, nights: Iterable[date], start: int, end: int) -> float | None:
    """Share of night hours with valid data, over the given nights."""
    wanted = set(nights)
    if not wanted:
        return None
    valid = sum(
        1
        for moment in house_load
        if in_night_window(moment.hour, start, end) and night_date(moment, start, end) in wanted
    )
    return min(valid / (len(wanted) * window_hours(start, end)), 1.0)


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
