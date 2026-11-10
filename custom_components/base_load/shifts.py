"""Pure logic to detect lasting level shifts in night load.

A level shift is not an anomaly: a device or the house runs at a new, stable
level for several nights in a row (heating season, a new server). The weighted
baseline absorbs such changes silently after a while; this module makes them
visible, with the date they started.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from statistics import median

from .baseline import MAD_TO_SIGMA, in_night_window, night_date, robust_stats

# Nights that must all sit on the new level.
RECENT_NIGHTS = 3
# Reference nights must be at least this old, so the new level is not part of it.
REFERENCE_MIN_AGE_DAYS = 10
MIN_REFERENCE_NIGHTS = 7
# How far back the start of a shift is searched.
MAX_SHIFT_AGE_DAYS = 21


@dataclass(frozen=True, slots=True)
class LevelShift:
    """A stable change of the night load of one series."""

    entity_id: str
    delta: float
    since: date
    before: float
    after: float

    @property
    def key(self) -> str:
        """Stable identity, used to report each shift only once."""
        return f"{self.entity_id}|{self.since.isoformat()}"


def night_medians(
    series: Mapping[datetime, float],
    start: int,
    end: int,
    valid: Iterable[datetime] | None = None,
) -> dict[date, float]:
    """Median of the night hours per night, optionally only on valid hours."""
    allowed = set(valid) if valid is not None else None
    nights: dict[date, list[float]] = {}
    for moment, value in series.items():
        if allowed is not None and moment not in allowed:
            continue
        if in_night_window(moment.hour, start, end):
            nights.setdefault(night_date(moment, start, end), []).append(value)
    return {night: median(values) for night, values in nights.items()}


def detect_shift(
    entity_id: str,
    nights: Mapping[date, float],
    today: date,
    min_delta: float,
    min_score: float,
    min_sigma: float,
) -> LevelShift | None:
    """A shift if the last nights all sit clearly off the older reference level."""
    completed = sorted(night for night in nights if night <= today)
    recent = completed[-RECENT_NIGHTS:]
    if len(recent) < RECENT_NIGHTS:
        return None
    reference = [
        nights[night] for night in completed if (today - night).days >= REFERENCE_MIN_AGE_DAYS
    ]
    stats = robust_stats(reference)
    if stats is None or stats.count < MIN_REFERENCE_NIGHTS:
        return None

    threshold = max(min_delta, min_score * max(stats.mad * MAD_TO_SIGMA, min_sigma))
    deltas = [nights[night] - stats.median for night in recent]
    if not (all(d >= threshold for d in deltas) or all(d <= -threshold for d in deltas)):
        return None
    sign = 1.0 if deltas[0] > 0 else -1.0

    # Walk back to the first night of the current run on the new level.
    since = recent[0]
    for night in reversed(completed[: completed.index(recent[0])]):
        if (today - night).days > MAX_SHIFT_AGE_DAYS:
            break
        if (nights[night] - stats.median) * sign < threshold:
            break
        since = night

    after = median(nights[night] for night in recent)
    return LevelShift(
        entity_id=entity_id,
        delta=after - stats.median,
        since=since,
        before=stats.median,
        after=after,
    )
