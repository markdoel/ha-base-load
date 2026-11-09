"""Pure helpers to tell nights at home from nights away.

When nobody is home the base load drops. Without this, a holiday would look
like missing consumption, and holiday nights would drag the baseline down.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, tzinfo

from .causes import window_bounds

HOME_STATES = {"home", "on"}


def is_away(entity_id: str, state: str | None) -> bool | None:
    """Read "nobody home" from a presence entity. None if unknown.

    - zone: number of persons in the zone, away at 0 (zone.home)
    - person, group, device_tracker: away unless "home" (groups of persons
      report "home" if anyone is home)
    - input_boolean, binary_sensor: "on" means away (a holiday switch)
    """
    if state is None or state in {"unknown", "unavailable", ""}:
        return None
    domain = entity_id.split(".", 1)[0]
    if domain == "zone":
        try:
            return int(float(state)) == 0
        except ValueError:
            return None
    if domain in {"input_boolean", "binary_sensor"}:
        return state == "on"
    return state not in HOME_STATES


def away_share(
    entity_id: str,
    timeline: Sequence[tuple[datetime, str]],
    start: datetime,
    end: datetime,
) -> float | None:
    """Share of [start, end) during which nobody was home.

    timeline: (moment, state) pairs, sorted, including the state that was
    active at `start` (a change before the window). Unknown periods are left
    out of the share. None if nothing is known about the window.
    """
    if end <= start:
        return None
    away = known = 0.0
    for index, (moment, state) in enumerate(timeline):
        seg_start = max(moment, start)
        seg_end = timeline[index + 1][0] if index + 1 < len(timeline) else end
        seg_end = min(seg_end, end)
        if seg_end <= seg_start:
            continue
        flag = is_away(entity_id, state)
        if flag is None:
            continue
        length = (seg_end - seg_start).total_seconds()
        known += length
        if flag:
            away += length
    return away / known if known else None


def away_nights(
    entity_id: str,
    timeline: Sequence[tuple[datetime, str]],
    nights: Iterable[date],
    night_start: int,
    night_end: int,
    tz: tzinfo,
    threshold: float,
) -> set[date]:
    """Nights in which nobody was home for at least `threshold` of the window."""
    result: set[date] = set()
    for night in nights:
        start, end = window_bounds(night, night_start, night_end, tz)
        share = away_share(entity_id, timeline, start, end)
        if share is not None and share >= threshold:
            result.add(night)
    return result
