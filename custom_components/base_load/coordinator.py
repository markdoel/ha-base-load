"""Data coordinator: reads recorder statistics and evaluates the base load."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
import logging
import math
from statistics import fmean, median
from typing import Any

from homeassistant.components.recorder import get_instance, history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .baseline import (
    BaselineStats,
    Evaluation,
    MeteredSeries,
    build_baseline,
    combine_house_load,
    coverage,
    day_type,
    day_type_of,
    evaluate,
    in_night_window,
    last_night_load,
    night_date,
    window_hours,
)
from .causes import (
    Explanation,
    Label,
    describe_pattern,
    label_from,
    match_label,
    median_in,
    rank_causes,
    residual_series,
    window_bounds,
)
from .const import (
    AWAY_SHARE,
    BASELINE_REFRESH,
    CONF_CANDIDATE_SENSORS,
    CONF_EXCLUDED_SENSORS,
    CONF_EXCLUDED_THRESHOLD,
    CONF_FEED_IN_SENSORS,
    CONF_GRID_SENSOR,
    CONF_HALF_LIFE_DAYS,
    CONF_LOOKBACK_WEEKS,
    CONF_MIN_DEVIATION,
    CONF_NIGHT_END,
    CONF_NIGHT_START,
    CONF_PRESENCE_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_SENSITIVITY,
    CURRENT_WINDOW,
    DAY_TYPE_WEEKEND,
    DAY_TYPE_WORKDAY,
    DEFAULT_EXCLUDED_THRESHOLD,
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_LOOKBACK_WEEKS,
    DEFAULT_MIN_DEVIATION,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_SENSITIVITY,
    DOMAIN,
    EVENT_LEVEL_SHIFT,
    ISSUE_SENSOR_MISSING,
    MAX_FAILED_UPDATES,
    MIN_BASELINE_HOURS,
    MIN_DATA_QUALITY,
    MIN_NIGHT_COVERAGE,
    ONSET_LOOKBACK,
    PRICE_LOOKBACK,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
)
from .cost import blended, price_unit
from .presence import away_nights, is_away
from .shifts import LevelShift, detect_shift, night_medians

_LOGGER = logging.getLogger(__name__)

type BaseLoadConfigEntry = ConfigEntry[BaseLoadCoordinator]
type SeriesById = dict[str, MeteredSeries]

# Short-term (5-minute) statistics and state history are kept about 10 days by
# default, so presence can only be read back that far.
PRESENCE_HISTORY = timedelta(days=10)


@dataclass(slots=True)
class BaseLoadData:
    """Result of one update."""

    baseline: dict[str, BaselineStats] = field(default_factory=dict)
    baseline_updated: datetime | None = None
    current_load: float | None = None
    excluded_active: bool = False
    in_night: bool = False
    away_now: bool = False
    day_type: str | None = None
    evaluation: Evaluation | None = None
    explanation: Explanation | None = None
    last_night_date: date | None = None
    last_night_load: float | None = None
    last_night_hours: int = 0
    last_night_away: bool = False
    last_night_evaluation: Evaluation | None = None
    last_night_explanation: Explanation | None = None
    data_quality: float | None = None
    window_hours: int = 0
    stale: bool = False
    failed_updates: int = 0
    price: float | None = None
    currency: str | None = None
    price_source: str | None = None
    last_night_marked_normal: bool = False
    shifts: list[LevelShift] = field(default_factory=list)
    device_loads: dict[str, float] = field(default_factory=dict)

    @property
    def expected(self) -> BaselineStats | None:
        """Baseline for the current day type."""
        if self.day_type is None:
            return None
        return self.baseline.get(self.day_type)

    @property
    def last_night_insufficient(self) -> bool:
        """The last night had too few valid hours to be judged."""
        return self.last_night_hours < math.ceil(self.window_hours * MIN_NIGHT_COVERAGE)

    @property
    def data_incomplete(self) -> bool:
        """Something keeps the results from being trustworthy."""
        if self.stale or not self.baseline:
            return True
        if self.data_quality is not None and self.data_quality < MIN_DATA_QUALITY:
            return True
        return self.last_night_insufficient and not self.last_night_away

    @property
    def unmeasured_load(self) -> float | None:
        """Part of the weekly base load that no selected device explains."""
        total = self.weekly_base_load
        if total is None:
            return None
        return total - sum(self.device_loads.values())

    @property
    def weekly_base_load(self) -> float | None:
        """Baseline averaged over a week, for cost estimates."""
        workday = self.baseline.get(DAY_TYPE_WORKDAY)
        weekend = self.baseline.get(DAY_TYPE_WEEKEND)
        return blended(workday.median if workday else None, weekend.median if weekend else None)


def _stats_to_json(stats: dict[str, BaselineStats]) -> dict[str, list[float]]:
    return {kind: [s.median, s.mad, s.count] for kind, s in stats.items()}


def _stats_from_json(raw: dict[str, list[float]]) -> dict[str, BaselineStats]:
    return {
        kind: BaselineStats(median=float(v[0]), mad=float(v[1]), count=int(v[2]))
        for kind, v in raw.items()
    }


class BaseLoadCoordinator(DataUpdateCoordinator[BaseLoadData]):
    """Fetches statistics and computes baseline, evaluation and causes."""

    config_entry: BaseLoadConfigEntry

    def __init__(self, hass: HomeAssistant, entry: BaseLoadConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._loaded = False
        self._house_load: dict[datetime, float] = {}
        self._devices: dict[str, dict[datetime, float]] = {}
        self._baseline: dict[str, BaselineStats] = {}
        self._device_baselines: dict[str, dict[str, BaselineStats]] = {}
        self._baseline_updated: datetime | None = None
        self._away_nights: set[date] = set()
        self._data_quality: float | None = None
        self._price: tuple[float, str, str] | None = None
        self._failed_updates = 0
        self._normal_nights: set[date] = set()
        self._labels: list[Label] = []
        self._event_shifts: set[str] = set()
        self._notified_shifts: set[str] = set()
        self._shifts: list[LevelShift] = []
        # The explanation of a finished night does not change, so it is cached.
        self._last_night_cache: tuple[date, Explanation | None] | None = None

    # Configuration ------------------------------------------------------

    def opt(self, key: str, default: Any) -> Any:
        """Read a setting. Once options exist they are complete and win."""
        entry = self.config_entry
        if entry.options:
            return entry.options.get(key, default)
        return entry.data.get(key, default)

    @property
    def grid_sensor(self) -> str:
        return self.opt(CONF_GRID_SENSOR, "")

    @property
    def feed_in_sensors(self) -> list[str]:
        return list(self.opt(CONF_FEED_IN_SENSORS, []))

    @property
    def excluded_sensors(self) -> list[str]:
        return list(self.opt(CONF_EXCLUDED_SENSORS, []))

    @property
    def candidate_sensors(self) -> list[str]:
        return list(self.opt(CONF_CANDIDATE_SENSORS, []))

    @property
    def presence_entity(self) -> str | None:
        return self.opt(CONF_PRESENCE_ENTITY, None) or None

    @property
    def price_entity(self) -> str | None:
        return self.opt(CONF_PRICE_ENTITY, None) or None

    @property
    def night_start(self) -> int:
        return int(self.opt(CONF_NIGHT_START, DEFAULT_NIGHT_START))

    @property
    def night_end(self) -> int:
        return int(self.opt(CONF_NIGHT_END, DEFAULT_NIGHT_END))

    @property
    def excluded_threshold(self) -> float:
        return float(self.opt(CONF_EXCLUDED_THRESHOLD, DEFAULT_EXCLUDED_THRESHOLD))

    @property
    def half_life_days(self) -> float:
        return float(self.opt(CONF_HALF_LIFE_DAYS, DEFAULT_HALF_LIFE_DAYS))

    @property
    def min_deviation(self) -> float:
        return float(self.opt(CONF_MIN_DEVIATION, DEFAULT_MIN_DEVIATION))

    @property
    def sensitivity(self) -> float:
        return float(self.opt(CONF_SENSITIVITY, DEFAULT_SENSITIVITY))

    @property
    def power_sensors(self) -> set[str]:
        return {
            self.grid_sensor,
            *self.feed_in_sensors,
            *self.excluded_sensors,
            *self.candidate_sensors,
        }

    @property
    def configured_entities(self) -> list[str]:
        """Every entity the configuration refers to."""
        entities = [
            self.grid_sensor,
            *self.feed_in_sensors,
            *self.excluded_sensors,
            *self.candidate_sensors,
        ]
        entities += [e for e in (self.presence_entity, self.price_entity) if e]
        return entities

    # Repairs ------------------------------------------------------------

    def check_entities(self) -> list[str]:
        """Raise or clear a repair issue for configured entities that are gone."""
        registry = er.async_get(self.hass)
        missing = [
            entity_id
            for entity_id in self.configured_entities
            if self.hass.states.get(entity_id) is None and registry.async_get(entity_id) is None
        ]
        issue_id = f"{ISSUE_SENSOR_MISSING}_{self.config_entry.entry_id}"
        if missing:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_SENSOR_MISSING,
                translation_placeholders={
                    "title": self.config_entry.title,
                    "entities": ", ".join(missing),
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        return missing

    # Storage ------------------------------------------------------------

    async def _load(self) -> None:
        """Restore baselines and away nights from the last run."""
        self._loaded = True
        stored = await self._store.async_load()
        if not stored:
            return
        try:
            self._baseline = _stats_from_json(stored.get("baseline", {}))
            self._device_baselines = {
                sensor: _stats_from_json(raw)
                for sensor, raw in stored.get("device_baselines", {}).items()
            }
            # The restored baseline bridges outages; the first successful
            # update always recomputes it (baseline_updated stays None).
            _LOGGER.debug("Restored baseline from %s", stored.get("baseline_updated"))
            self._away_nights = {date.fromisoformat(d) for d in stored.get("away_nights", [])}
            self._normal_nights = {date.fromisoformat(d) for d in stored.get("normal_nights", [])}
            self._labels = [Label(**raw) for raw in stored.get("labels", [])]
            self._event_shifts = set(stored.get("event_shifts", []))
            self._notified_shifts = set(stored.get("notified_shifts", []))
        except (KeyError, TypeError, ValueError) as err:
            _LOGGER.warning("Ignoring unreadable stored baseline: %s", err)

    def _save(self) -> None:
        weeks = int(self.opt(CONF_LOOKBACK_WEEKS, DEFAULT_LOOKBACK_WEEKS))
        oldest = dt_util.now().date() - timedelta(weeks=weeks)
        self._away_nights = {d for d in self._away_nights if d >= oldest}
        payload = {
            "baseline": _stats_to_json(self._baseline),
            "device_baselines": {
                sensor: _stats_to_json(stats) for sensor, stats in self._device_baselines.items()
            },
            "baseline_updated": (
                self._baseline_updated.isoformat() if self._baseline_updated else None
            ),
            "away_nights": sorted(d.isoformat() for d in self._away_nights),
            "normal_nights": sorted(d.isoformat() for d in self._normal_nights if d >= oldest),
            "labels": [asdict(label) for label in self._labels],
            "event_shifts": sorted(self._event_shifts),
            "notified_shifts": sorted(self._notified_shifts),
        }
        self._store.async_delay_save(lambda: payload, 10)

    # Statistics ---------------------------------------------------------

    async def _fetch(
        self, start: datetime, end: datetime, period: str, sensors: set[str] | None = None
    ) -> SeriesById:
        """Mean, min and max per sensor and period, keyed by local start time."""
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            sensors if sensors is not None else self.power_sensors,
            period,
            {"power": UnitOfPower.WATT},
            {"mean", "min", "max"},
        )
        result: SeriesById = {}
        for statistic_id, entries in rows.items():
            series = MeteredSeries(mean={}, min={}, max={})
            for row in entries:
                mean = row.get("mean")
                if mean is None:
                    continue
                raw_start = row["start"]
                moment = dt_util.as_local(
                    dt_util.utc_from_timestamp(raw_start)
                    if isinstance(raw_start, (int, float))
                    else raw_start
                )
                series.mean[moment] = float(mean)
                if (low := row.get("min")) is not None:
                    series.min[moment] = float(low)
                if (high := row.get("max")) is not None:
                    series.max[moment] = float(high)
            result[statistic_id] = series
        return result

    def _house_load_from(self, data: SeriesById) -> dict[datetime, float]:
        empty = MeteredSeries(mean={})
        return combine_house_load(
            grid=data.get(self.grid_sensor, empty).mean,
            feed_ins=[data.get(sensor, empty) for sensor in self.feed_in_sensors],
            excluded=[data.get(sensor, empty) for sensor in self.excluded_sensors],
            excluded_threshold=self.excluded_threshold,
        )

    # Presence -----------------------------------------------------------

    async def _update_away_nights(self, now: datetime) -> None:
        """Read presence history back about 10 days and mark nights away."""
        entity_id = self.presence_entity
        if entity_id is None:
            return
        start = now - PRESENCE_HISTORY
        states = await get_instance(self.hass).async_add_executor_job(
            history.get_significant_states,
            self.hass,
            start,
            now,
            [entity_id],
            None,
            True,  # include the state active at the start
            False,  # all changes, not only significant ones
        )
        timeline = [
            (state.last_changed, state.state)
            for state in states.get(entity_id, [])
            if hasattr(state, "last_changed")
        ]
        if not timeline:
            return
        timeline.sort()
        last = night_date(now, self.night_start, self.night_end)
        nights = [last - timedelta(days=offset) for offset in range(0, 11)]
        found = away_nights(
            entity_id,
            timeline,
            nights,
            self.night_start,
            self.night_end,
            dt_util.get_default_time_zone(),
            AWAY_SHARE,
        )
        # Nights inside the readable history are decided by it, older ones keep
        # what was stored before.
        readable = {night for night in nights if night > start.date()}
        self._away_nights = (self._away_nights - readable) | found

    def _away_now(self) -> bool:
        entity_id = self.presence_entity
        if entity_id is None:
            return False
        state = self.hass.states.get(entity_id)
        return bool(is_away(entity_id, state.state if state else None))

    # Price --------------------------------------------------------------

    async def _update_price(self, now: datetime) -> None:
        """Average price over 30 days from statistics, else the current value."""
        entity_id = self.price_entity
        self._price = None
        if entity_id is None:
            return
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get("unit_of_measurement") if state else None
        factor, currency = price_unit(unit, self.hass.config.currency)

        data = await self._fetch(now - PRICE_LOOKBACK, now, "hour", {entity_id})
        values = list(data[entity_id].mean.values()) if entity_id in data else []
        if values:
            self._price = (fmean(values) * factor, currency, "statistics")
            return
        try:
            current = float(state.state) if state else None
        except ValueError:
            current = None
        if current is not None:
            self._price = (current * factor, currency, "current")

    # Baseline -----------------------------------------------------------

    async def _refresh_baseline(self, now: datetime) -> None:
        """Load hourly statistics for the learning period and build the baselines."""
        weeks = int(self.opt(CONF_LOOKBACK_WEEKS, DEFAULT_LOOKBACK_WEEKS))
        data = await self._fetch(now - timedelta(weeks=weeks), now, "hour")
        if self.grid_sensor not in data or not data[self.grid_sensor].mean:
            raise UpdateFailed(f"No long-term statistics found for {self.grid_sensor}")

        await self._update_away_nights(now)
        await self._update_price(now)

        self._house_load = self._house_load_from(data)
        self._devices = {
            sensor: data[sensor].mean if sensor in data else {} for sensor in self.candidate_sensors
        }
        start, end = self.night_start, self.night_end
        today = now.date()
        # Only nights that ended before today and in which someone was home go
        # into the baseline. The evaluated night never teaches itself to be
        # normal, and holidays do not drag the baseline down.
        history_load = {
            moment: value
            for moment, value in self._house_load.items()
            if (night := night_date(moment, start, end)) < today and night not in self._away_nights
        }
        half_life = self.half_life_days
        self._baseline = build_baseline(history_load, start, end, now, half_life)
        # Devices learn on exactly the hours the house baseline uses.
        self._device_baselines = {
            sensor: build_baseline(
                {m: v for m, v in series.items() if m in history_load},
                start,
                end,
                now,
                half_life,
            )
            for sensor, series in self._devices.items()
        }
        first_night = (now - timedelta(weeks=weeks)).date() + timedelta(days=1)
        nights = [
            first_night + timedelta(days=offset)
            for offset in range((today - first_night).days)
            if first_night + timedelta(days=offset) not in self._away_nights
        ]
        self._data_quality = coverage(self._house_load, nights, start, end)
        self._detect_shifts(today, history_load)
        self._baseline_updated = now
        self.check_entities()
        self._save()
        _LOGGER.debug("Baseline recomputed: %s", self._baseline)

    def _detect_shifts(self, today: date, history_load: dict[datetime, float]) -> None:
        """Find lasting level changes of the house and each device."""
        start, end = self.night_start, self.night_end
        # Include last night: a shift is about the newest nights.
        valid = {
            moment: value
            for moment, value in self._house_load.items()
            if night_date(moment, start, end) not in self._away_nights
        }
        shifts: list[LevelShift] = []
        house = detect_shift(
            self.grid_sensor,
            night_medians(valid, start, end),
            today,
            min_delta=self.min_deviation,
            min_score=3.0,
            min_sigma=10.0,
        )
        if house is not None:
            shifts.append(house)
        for sensor, series in self._devices.items():
            shift = detect_shift(
                sensor,
                night_medians(series, start, end, valid),
                today,
                min_delta=10.0,
                min_score=3.0,
                min_sigma=2.0,
            )
            if shift is not None:
                shifts.append(shift)
        shifts.sort(key=lambda shift: abs(shift.delta), reverse=True)
        self._shifts = shifts
        for shift in shifts:
            if shift.key not in self._event_shifts:
                self._event_shifts.add(shift.key)
                self.hass.bus.async_fire(
                    EVENT_LEVEL_SHIFT,
                    {
                        "entry_id": self.config_entry.entry_id,
                        "entity_id": shift.entity_id,
                        "delta": round(shift.delta),
                        "before": round(shift.before),
                        "after": round(shift.after),
                        "since": shift.since.isoformat(),
                    },
                )

    def unnotified_shifts(self) -> list[LevelShift]:
        """Current shifts not yet sent in a notification; marks them as sent."""
        fresh = [s for s in self._shifts if s.key not in self._notified_shifts]
        self._notified_shifts |= {s.key for s in fresh}
        if fresh:
            self._save()
        return fresh

    # Feedback -----------------------------------------------------------

    async def async_mark_normal(self, night: date) -> None:
        """The user says this night was fine."""
        self._normal_nights.add(night)
        self._save()
        await self.async_refresh()

    async def async_label_night(self, night: date, name: str) -> bool:
        """Name the unexplained pattern of a night. False if there is none."""
        data = self.data
        if data is None or data.last_night_date != night:
            return False
        explanation = data.last_night_explanation
        if explanation is None or explanation.pattern is None:
            return False
        label = label_from(name, explanation.pattern)
        if label is None or not label.name:
            return False
        self._labels = [item for item in self._labels if item.name != label.name] + [label]
        self._normal_nights.add(night)
        self._last_night_cache = None
        self._save()
        await self.async_refresh()
        return True

    async def async_refresh_now(self) -> None:
        """Recompute everything, including the baseline."""
        self._baseline_updated = None
        await self.async_refresh()

    def _baseline_due(self, now: datetime) -> bool:
        updated = self._baseline_updated
        return updated is None or now - updated >= BASELINE_REFRESH or updated.date() != now.date()

    # Causes -------------------------------------------------------------

    def _notable(self, evaluation: Evaluation) -> bool:
        """Explain anomalies and also clear deviations just below the score."""
        return evaluation.is_anomaly or abs(evaluation.deviation) >= self.min_deviation

    def _device_baselines_for(self, kind: str) -> dict[str, BaselineStats]:
        return {
            sensor: stats[kind] for sensor, stats in self._device_baselines.items() if kind in stats
        }

    def _explain(
        self,
        evaluation: Evaluation,
        house_baseline: float,
        kind: str,
        device_values: dict[str, float],
        five_minute: SeriesById,
        window: tuple[datetime, datetime],
    ) -> Explanation:
        """Rank measured devices, then describe what is left over."""
        baselines = self._device_baselines_for(kind)
        causes = rank_causes(evaluation.deviation, device_values, baselines)
        residual = residual_series(
            self._house_load_from(five_minute),
            {
                sensor: five_minute[sensor].mean if sensor in five_minute else {}
                for sensor in self.candidate_sensors
            },
            baselines,
            causes,
        )
        pattern = describe_pattern(
            residual,
            baseline=house_baseline,
            threshold=self.min_deviation,
            window_start=window[0],
            window_end=window[1],
            direction=1.0 if evaluation.deviation >= 0 else -1.0,
        )
        return Explanation(
            total=evaluation.deviation,
            causes=causes,
            pattern=pattern,
            label=match_label(pattern, self._labels),
        )

    async def _explain_last_night(
        self, night: date, evaluation: Evaluation, stats: BaselineStats
    ) -> Explanation | None:
        if self._last_night_cache and self._last_night_cache[0] == night:
            return self._last_night_cache[1]

        tz = dt_util.get_default_time_zone()
        window = window_bounds(night, self.night_start, self.night_end, tz)
        device_values = {
            sensor: value
            for sensor, series in self._devices.items()
            if (value := median_in(series, *window)) is not None
        }
        five_minute = await self._fetch(window[0] - ONSET_LOOKBACK, window[1], "5minute")
        explanation = self._explain(
            evaluation, stats.median, day_type_of(night), device_values, five_minute, window
        )
        self._last_night_cache = (night, explanation)
        return explanation

    # Update -------------------------------------------------------------

    async def _async_update_data(self) -> BaseLoadData:
        """Compute a result; bridge short outages with the last good one."""
        try:
            result = await self._compute()
        except Exception as err:
            self._failed_updates += 1
            if self.data is not None and self._failed_updates <= MAX_FAILED_UPDATES:
                _LOGGER.warning(
                    "Update failed (%s of %s bridged), keeping last result: %s",
                    self._failed_updates,
                    MAX_FAILED_UPDATES,
                    err,
                )
                return replace(self.data, stale=True, failed_updates=self._failed_updates)
            if self._baseline and self.data is None:
                # First update after a restart: show the stored baseline.
                _LOGGER.warning("Update failed, using the stored baseline: %s", err)
                return BaseLoadData(
                    baseline=self._baseline,
                    window_hours=window_hours(self.night_start, self.night_end),
                    stale=True,
                    failed_updates=self._failed_updates,
                )
            if isinstance(err, UpdateFailed):
                raise
            raise UpdateFailed(str(err)) from err
        self._failed_updates = 0
        return result

    def _weekly_device_loads(self) -> dict[str, float]:
        """Weekly average night load per selected device."""
        loads: dict[str, float] = {}
        for sensor, stats in self._device_baselines.items():
            workday = stats.get(DAY_TYPE_WORKDAY)
            weekend = stats.get(DAY_TYPE_WEEKEND)
            value = blended(
                workday.median if workday else None, weekend.median if weekend else None
            )
            if value is not None:
                loads[sensor] = value
        return loads

    async def _compute(self) -> BaseLoadData:
        if not self._loaded:
            await self._load()

        now = dt_util.now()
        start, end = self.night_start, self.night_end

        if self._baseline_due(now):
            await self._refresh_baseline(now)

        in_night = in_night_window(now.hour, start, end)
        tz = dt_util.get_default_time_zone()
        window = window_bounds(night_date(now, start, end), start, end, tz)

        # During the night the whole window so far is loaded, which the cause
        # analysis needs for onset and shape. Otherwise the last hour does.
        # A 5-minute period is only written after it ended, hence the margin.
        fetch_start = (
            window[0] - ONSET_LOOKBACK if in_night else now - CURRENT_WINDOW - timedelta(minutes=10)
        )
        recent = await self._fetch(fetch_start, now, "5minute")
        cutoff = now - CURRENT_WINDOW - timedelta(minutes=5)
        recent_load = self._house_load_from(recent)
        window_values = [value for moment, value in recent_load.items() if moment >= cutoff]
        # At least half of the periods of the last hour must be usable.
        enough = len(window_values) >= CURRENT_WINDOW / timedelta(minutes=5) / 2
        current = median(window_values) if window_values and enough else None

        # Informational: an excluded load ran in the window. It only blocks the
        # evaluation if it left too few usable periods.
        excluded_active = any(
            value > self.excluded_threshold
            for sensor in self.excluded_sensors
            if sensor in recent
            for moment, value in (recent[sensor].max or recent[sensor].mean).items()
            if moment >= cutoff
        )

        away_now = self._away_now()
        if in_night and away_now:
            tonight = night_date(now, start, end)
            if tonight not in self._away_nights:
                self._away_nights.add(tonight)
                self._save()

        price = self._price
        result = BaseLoadData(
            baseline=self._baseline,
            baseline_updated=self._baseline_updated,
            current_load=current,
            excluded_active=excluded_active,
            in_night=in_night,
            away_now=away_now,
            day_type=day_type(now, start, end),
            data_quality=self._data_quality,
            window_hours=window_hours(start, end),
            price=price[0] if price else None,
            currency=price[1] if price else None,
            price_source=price[2] if price else None,
            shifts=list(self._shifts),
            device_loads=self._weekly_device_loads(),
        )

        expected = result.expected
        if (
            expected is not None
            and expected.count >= MIN_BASELINE_HOURS
            and current is not None
            and in_night
            and not away_now
        ):
            result.evaluation = evaluate(current, expected, self.sensitivity, self.min_deviation)
            if self._notable(result.evaluation) and result.day_type is not None:
                device_values = {
                    sensor: value
                    for sensor in self.candidate_sensors
                    if sensor in recent
                    and (value := median_in(recent[sensor].mean, cutoff, now)) is not None
                }
                result.explanation = self._explain(
                    result.evaluation,
                    expected.median,
                    result.day_type,
                    device_values,
                    recent,
                    (window[0], min(window[1], now)),
                )

        last = last_night_load(self._house_load, now, start, end)
        result.last_night_date = last.night
        result.last_night_load = last.load
        result.last_night_hours = last.hours
        result.last_night_away = last.night in self._away_nights
        result.last_night_marked_normal = last.night in self._normal_nights
        last_stats = self._baseline.get(day_type_of(last.night))
        if (
            last.load is not None
            and not result.last_night_insufficient
            and not result.last_night_away
            and last_stats is not None
            and last_stats.count >= MIN_BASELINE_HOURS
        ):
            result.last_night_evaluation = evaluate(
                last.load, last_stats, self.sensitivity, self.min_deviation
            )
            if self._notable(result.last_night_evaluation):
                result.last_night_explanation = await self._explain_last_night(
                    last.night, result.last_night_evaluation, last_stats
                )

        return result
