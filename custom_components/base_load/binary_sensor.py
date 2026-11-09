"""Binary sensors for Base Load Monitor."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MIN_BASELINE_HOURS, MIN_DATA_QUALITY
from .coordinator import BaseLoadConfigEntry, BaseLoadCoordinator
from .entity import BaseLoadEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BaseLoadConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            BaseLoadAnomaly(coordinator),
            BaseLoadLastNightAnomaly(coordinator),
            BaseLoadDataIncomplete(coordinator),
        ]
    )


class BaseLoadAnomaly(BaseLoadEntity, BinarySensorEntity):
    """On while the current night load deviates clearly from the baseline."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: BaseLoadCoordinator) -> None:
        super().__init__(coordinator, "anomaly")

    @property
    def is_on(self) -> bool:
        evaluation = self.coordinator.data.evaluation
        return bool(evaluation and evaluation.is_anomaly)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        data = self.coordinator.data
        evaluation = data.evaluation
        return {
            "status": self._status(),
            "direction": evaluation.direction if evaluation else None,
            "deviation": round(evaluation.deviation) if evaluation else None,
            "score": round(evaluation.score, 2) if evaluation else None,
        }

    def _status(self) -> str:
        """Why the sensor is (not) evaluating right now."""
        data = self.coordinator.data
        expected = data.expected
        if expected is None or expected.count < MIN_BASELINE_HOURS:
            return "learning"
        if not data.in_night:
            return "outside_night_window"
        if data.away_now:
            return "away"
        if data.current_load is None:
            return "excluded_load_active" if data.excluded_active else "no_data"
        return "monitoring"


class BaseLoadLastNightAnomaly(BaseLoadEntity, BinarySensorEntity):
    """Verdict for the last completed night, handy for a morning notification."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: BaseLoadCoordinator) -> None:
        super().__init__(coordinator, "last_night_anomaly")

    @property
    def is_on(self) -> bool | None:
        evaluation = self.coordinator.data.last_night_evaluation
        return None if evaluation is None else evaluation.is_anomaly

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        data = self.coordinator.data
        evaluation = data.last_night_evaluation
        return {
            "date": data.last_night_date,
            "status": self._status(),
            "hours": data.last_night_hours,
            "load": round(data.last_night_load) if data.last_night_load is not None else None,
            "direction": evaluation.direction if evaluation else None,
            "deviation": round(evaluation.deviation) if evaluation else None,
            "score": round(evaluation.score, 2) if evaluation else None,
        }

    def _status(self) -> str:
        data = self.coordinator.data
        if data.last_night_away:
            return "away"
        if data.last_night_insufficient:
            return "no_data"
        if data.last_night_evaluation is None:
            return "learning"
        return "evaluated"


class BaseLoadDataIncomplete(BaseLoadEntity, BinarySensorEntity):
    """On if the results cannot be trusted right now."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BaseLoadCoordinator) -> None:
        super().__init__(coordinator, "data_incomplete")

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.data_incomplete

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        data = self.coordinator.data
        reasons = []
        if data.stale:
            reasons.append("stale")
        if not data.baseline:
            reasons.append("no_baseline")
        if data.data_quality is not None and data.data_quality < MIN_DATA_QUALITY:
            reasons.append("low_quality")
        if data.last_night_insufficient and not data.last_night_away:
            reasons.append("last_night_missing")
        return {
            "reasons": reasons,
            "data_quality": (
                None if data.data_quality is None else round(data.data_quality * 100, 1)
            ),
            "last_night_hours": data.last_night_hours,
            "window_hours": data.window_hours,
            "failed_updates": data.failed_updates,
        }
