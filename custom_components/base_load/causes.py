"""Pure logic that suggests what caused a base load anomaly.

Two parts:
1. Measured devices: each candidate gets its own night baseline. Devices whose
   deviation points the same way as the total and stands out from their own
   spread are ranked by size.
2. The rest: whatever the measured devices do not explain is reported as
   unmeasured, together with its onset and whether it was constant or
   intermittent. That shape is often the best hint for an unmeasured device.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import median

from .baseline import MAD_TO_SIGMA, BaselineStats, Series

# A device must move at least this much to count as a cause.
DEVICE_MIN_DEVIATION_W = 10.0
# Robust score a device needs against its own spread.
DEVICE_MIN_SCORE = 3.0
# Spread floor for devices; small plugs can be very steady.
DEVICE_MIN_SIGMA_W = 2.0
MAX_CAUSES = 3

PATTERN_CONSTANT = "constant"
PATTERN_INTERMITTENT = "intermittent"
# Share of periods above the threshold for each pattern.
CONSTANT_SHARE = 0.75
INTERMITTENT_SHARE = 0.15


@dataclass(frozen=True, slots=True)
class Cause:
    """A measured device that explains part of the deviation."""

    entity_id: str
    deviation: float
    baseline: float
    current: float


@dataclass(frozen=True, slots=True)
class Pattern:
    """Shape of the part of the deviation that no device explains."""

    onset: datetime | None
    shape: str | None
    share: float


@dataclass(frozen=True, slots=True)
class Explanation:
    """Suggested causes for one anomaly."""

    total: float
    causes: list[Cause] = field(default_factory=list)
    pattern: Pattern | None = None

    @property
    def explained(self) -> float:
        return sum(cause.deviation for cause in self.causes)

    @property
    def unexplained(self) -> float:
        return self.total - self.explained


def device_sigma(stats: BaselineStats) -> float:
    """Robust spread of a single device, with a small floor."""
    return max(stats.mad * MAD_TO_SIGMA, DEVICE_MIN_SIGMA_W)


def rank_causes(
    total_deviation: float,
    device_values: Mapping[str, float],
    device_baselines: Mapping[str, BaselineStats],
) -> list[Cause]:
    """Devices that deviate in the same direction as the total, largest first."""
    sign = 1.0 if total_deviation >= 0 else -1.0
    causes: list[Cause] = []
    for entity_id, value in device_values.items():
        stats = device_baselines.get(entity_id)
        if stats is None:
            continue
        deviation = value - stats.median
        if deviation * sign < DEVICE_MIN_DEVIATION_W:
            continue
        if abs(deviation) / device_sigma(stats) < DEVICE_MIN_SCORE:
            continue
        causes.append(
            Cause(
                entity_id=entity_id,
                deviation=deviation,
                baseline=stats.median,
                current=value,
            )
        )
    causes.sort(key=lambda cause: abs(cause.deviation), reverse=True)
    return causes[:MAX_CAUSES]


def residual_series(
    house: Series,
    devices: Mapping[str, Series],
    device_baselines: Mapping[str, BaselineStats],
    causes: Sequence[Cause],
) -> dict[datetime, float]:
    """House load with the excess of the identified causes removed."""
    result: dict[datetime, float] = {}
    for moment, value in house.items():
        excess = 0.0
        for cause in causes:
            device_value = devices.get(cause.entity_id, {}).get(moment)
            if device_value is not None:
                excess += device_value - device_baselines[cause.entity_id].median
        result[moment] = value - excess
    return result


def describe_pattern(
    series: Series,
    baseline: float,
    threshold: float,
    window_start: datetime,
    window_end: datetime,
    direction: float,
) -> Pattern | None:
    """Onset and shape of a deviation within the night window.

    The onset may lie before the window: the search walks back from the first
    deviating period in the window for as long as the deviation persists.
    """
    ordered = sorted(series.items())
    if not ordered:
        return None

    def above(value: float) -> bool:
        return (value - baseline) * direction >= threshold

    in_window = [(m, v) for m, v in ordered if window_start <= m < window_end]
    if not in_window:
        return None

    flags = [above(value) for _, value in in_window]
    # A single deviating period is noise, not a pattern.
    if sum(flags) < 2:
        return Pattern(onset=None, shape=None, share=sum(flags) / len(flags))

    # The shape is judged from the first deviating period on, so a load that
    # switched on late in the night still counts as constant.
    first = flags.index(True)
    tail = flags[first:]
    share = sum(tail) / len(tail)
    if share >= CONSTANT_SHARE:
        shape: str | None = PATTERN_CONSTANT
    elif share >= INTERMITTENT_SHARE:
        shape = PATTERN_INTERMITTENT
    else:
        return Pattern(onset=None, shape=None, share=share)

    onset = in_window[first][0]
    if first == 0:
        before = [(m, v) for m, v in ordered if m < window_start]
        for moment, value in reversed(before):
            if not above(value):
                break
            onset = moment
    return Pattern(onset=onset, shape=shape, share=share)


def window_bounds(night: date, start: int, end: int, tz) -> tuple[datetime, datetime]:
    """Start and end of the night window that ends on the morning of `night`."""
    end_dt = datetime(night.year, night.month, night.day, end, tzinfo=tz)
    start_day = night - timedelta(days=1) if start > end else night
    start_dt = datetime(start_day.year, start_day.month, start_day.day, start, tzinfo=tz)
    return start_dt, end_dt


def median_in(series: Series, start: datetime, end: datetime) -> float | None:
    """Median of all values with start <= moment < end."""
    values = [value for moment, value in series.items() if start <= moment < end]
    return median(values) if values else None
