"""Data coordinator: reads recorder statistics and evaluates the base load."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
from statistics import fmean
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .baseline import (
    BaselineStats,
    Evaluation,
    build_baseline,
    combine_house_load,
    day_type,
    day_type_of,
    evaluate,
    in_night_window,
    last_night_mean,
    night_date,
)
from .const import (
    BASELINE_REFRESH,
    CONF_EXCLUDED_SENSORS,
    CONF_EXCLUDED_THRESHOLD,
    CONF_FEED_IN_SENSORS,
    CONF_GRID_SENSOR,
    CONF_LOOKBACK_WEEKS,
    CONF_MIN_DEVIATION,
    CONF_NIGHT_END,
    CONF_NIGHT_START,
    CONF_SENSITIVITY,
    CURRENT_WINDOW,
    DEFAULT_EXCLUDED_THRESHOLD,
    DEFAULT_LOOKBACK_WEEKS,
    DEFAULT_MIN_DEVIATION,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_SENSITIVITY,
    DOMAIN,
    MIN_BASELINE_HOURS,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type BaseLoadConfigEntry = ConfigEntry[BaseLoadCoordinator]


@dataclass(slots=True)
class BaseLoadData:
    """Result of one update."""

    baseline: dict[str, BaselineStats] = field(default_factory=dict)
    baseline_updated: datetime | None = None
    current_load: float | None = None
    excluded_active: bool = False
    in_night: bool = False
    day_type: str | None = None
    evaluation: Evaluation | None = None
    last_night_date: date | None = None
    last_night_load: float | None = None
    last_night_evaluation: Evaluation | None = None

    @property
    def expected(self) -> BaselineStats | None:
        """Baseline for the current day type."""
        if self.day_type is None:
            return None
        return self.baseline.get(self.day_type)


class BaseLoadCoordinator(DataUpdateCoordinator[BaseLoadData]):
    """Fetches statistics and computes baseline and evaluation."""

    config_entry: BaseLoadConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BaseLoadConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self._house_load: dict[datetime, float] = {}
        self._baseline: dict[str, BaselineStats] = {}
        self._baseline_updated: datetime | None = None

    # Configuration ------------------------------------------------------

    def _opt(self, key: str, default: Any) -> Any:
        """Read an option, falling back to entry data and the default."""
        entry = self.config_entry
        return entry.options.get(key, entry.data.get(key, default))

    @property
    def grid_sensor(self) -> str:
        return self._opt(CONF_GRID_SENSOR, "")

    @property
    def feed_in_sensors(self) -> list[str]:
        return list(self._opt(CONF_FEED_IN_SENSORS, []))

    @property
    def excluded_sensors(self) -> list[str]:
        return list(self._opt(CONF_EXCLUDED_SENSORS, []))

    @property
    def night_start(self) -> int:
        return int(self._opt(CONF_NIGHT_START, DEFAULT_NIGHT_START))

    @property
    def night_end(self) -> int:
        return int(self._opt(CONF_NIGHT_END, DEFAULT_NIGHT_END))

    @property
    def excluded_threshold(self) -> float:
        return float(self._opt(CONF_EXCLUDED_THRESHOLD, DEFAULT_EXCLUDED_THRESHOLD))

    @property
    def all_sensors(self) -> set[str]:
        return {self.grid_sensor, *self.feed_in_sensors, *self.excluded_sensors}

    # Statistics ---------------------------------------------------------

    async def _fetch(
        self, start: datetime, end: datetime, period: str
    ) -> dict[str, dict[datetime, float]]:
        """Mean per sensor and period, in watts, keyed by local start time."""
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            self.all_sensors,
            period,
            {"power": UnitOfPower.WATT},
            {"mean"},
        )
        result: dict[str, dict[datetime, float]] = {}
        for statistic_id, entries in rows.items():
            series: dict[datetime, float] = {}
            for row in entries:
                mean = row.get("mean")
                if mean is None:
                    continue
                raw_start = row["start"]
                moment = (
                    dt_util.utc_from_timestamp(raw_start)
                    if isinstance(raw_start, (int, float))
                    else raw_start
                )
                series[dt_util.as_local(moment)] = float(mean)
            result[statistic_id] = series
        return result

    def _house_load_from(self, data: dict[str, dict[datetime, float]]) -> dict[datetime, float]:
        return combine_house_load(
            grid=data.get(self.grid_sensor, {}),
            feed_ins=[data.get(sensor, {}) for sensor in self.feed_in_sensors],
            excluded=[data.get(sensor, {}) for sensor in self.excluded_sensors],
            excluded_threshold=self.excluded_threshold,
        )

    async def _refresh_baseline(self, now: datetime) -> None:
        """Load hourly statistics for the learning period and build the baseline."""
        weeks = int(self._opt(CONF_LOOKBACK_WEEKS, DEFAULT_LOOKBACK_WEEKS))
        data = await self._fetch(now - timedelta(weeks=weeks), now, "hour")
        if not data.get(self.grid_sensor):
            raise UpdateFailed(f"No long-term statistics found for {self.grid_sensor}")

        self._house_load = self._house_load_from(data)
        start, end = self.night_start, self.night_end
        today = now.date()
        # Only nights that ended before today go into the baseline, so the night
        # being evaluated never teaches itself to be normal.
        history = {
            moment: value
            for moment, value in self._house_load.items()
            if night_date(moment, start, end) < today
        }
        self._baseline = build_baseline(history, start, end)
        self._baseline_updated = now
        _LOGGER.debug("Baseline recomputed: %s", self._baseline)

    def _baseline_due(self, now: datetime) -> bool:
        updated = self._baseline_updated
        return updated is None or now - updated >= BASELINE_REFRESH or updated.date() != now.date()

    # Update -------------------------------------------------------------

    async def _async_update_data(self) -> BaseLoadData:
        now = dt_util.now()
        start, end = self.night_start, self.night_end
        sensitivity = float(self._opt(CONF_SENSITIVITY, DEFAULT_SENSITIVITY))
        min_deviation = float(self._opt(CONF_MIN_DEVIATION, DEFAULT_MIN_DEVIATION))

        if self._baseline_due(now):
            await self._refresh_baseline(now)

        # Current value from 5-minute statistics. A period is only written after
        # it has ended, so the query reaches a little further back.
        recent = await self._fetch(now - CURRENT_WINDOW - timedelta(minutes=10), now, "5minute")
        cutoff = now - CURRENT_WINDOW - timedelta(minutes=5)
        recent_load = self._house_load_from(recent)
        window_values = [value for moment, value in recent_load.items() if moment >= cutoff]
        current = fmean(window_values) if window_values else None

        excluded_active = any(
            value > self.excluded_threshold
            for sensor in self.excluded_sensors
            for moment, value in recent.get(sensor, {}).items()
            if moment >= cutoff
        )

        result = BaseLoadData(
            baseline=self._baseline,
            baseline_updated=self._baseline_updated,
            current_load=current,
            excluded_active=excluded_active,
            in_night=in_night_window(now.hour, start, end),
            day_type=day_type(now, start, end),
        )

        expected = result.expected
        if (
            expected is not None
            and expected.count >= MIN_BASELINE_HOURS
            and current is not None
            and result.in_night
            and not excluded_active
        ):
            result.evaluation = evaluate(current, expected, sensitivity, min_deviation)

        last = last_night_mean(self._house_load, now, start, end)
        if last is not None:
            result.last_night_date, result.last_night_load = last
            last_stats = self._baseline.get(day_type_of(result.last_night_date))
            if last_stats is not None and last_stats.count >= MIN_BASELINE_HOURS:
                result.last_night_evaluation = evaluate(
                    result.last_night_load, last_stats, sensitivity, min_deviation
                )

        return result
