"""Sensors for Base Load Monitor."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DAY_TYPE_WEEKEND, DAY_TYPE_WORKDAY
from .coordinator import BaseLoadConfigEntry, BaseLoadData
from .entity import BaseLoadEntity


def _round(value: float | None, digits: int = 0) -> float | None:
    return None if value is None else round(value, digits)


def _expected_attrs(data: BaseLoadData) -> dict[str, Any]:
    workday = data.baseline.get(DAY_TYPE_WORKDAY)
    weekend = data.baseline.get(DAY_TYPE_WEEKEND)
    expected = data.expected
    return {
        "day_type": data.day_type,
        "spread": _round(expected.sigma if expected else None),
        "workday_median": _round(workday.median if workday else None),
        "workday_hours": workday.count if workday else 0,
        "weekend_median": _round(weekend.median if weekend else None),
        "weekend_hours": weekend.count if weekend else 0,
        "baseline_updated": data.baseline_updated,
    }


def _deviation_attrs(data: BaseLoadData) -> dict[str, Any]:
    evaluation = data.evaluation
    return {
        "score": _round(evaluation.score if evaluation else None, 2),
        "direction": evaluation.direction if evaluation else None,
    }


def _last_night_attrs(data: BaseLoadData) -> dict[str, Any]:
    evaluation = data.last_night_evaluation
    return {
        "date": data.last_night_date,
        "deviation": _round(evaluation.deviation if evaluation else None),
        "score": _round(evaluation.score if evaluation else None, 2),
        "direction": evaluation.direction if evaluation else None,
    }


@dataclass(frozen=True, kw_only=True)
class BaseLoadSensorDescription(SensorEntityDescription):
    """Describes a Base Load Monitor sensor."""

    value_fn: Callable[[BaseLoadData], float | None]
    attrs_fn: Callable[[BaseLoadData], Mapping[str, Any]] | None = None


SENSORS: tuple[BaseLoadSensorDescription, ...] = (
    BaseLoadSensorDescription(
        key="expected",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.expected.median if data.expected else None,
        attrs_fn=_expected_attrs,
    ),
    BaseLoadSensorDescription(
        key="current",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.current_load,
        attrs_fn=lambda data: {
            "in_night_window": data.in_night,
            "excluded_load_active": data.excluded_active,
        },
    ),
    BaseLoadSensorDescription(
        key="deviation",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.evaluation.deviation if data.evaluation else None,
        attrs_fn=_deviation_attrs,
    ),
    BaseLoadSensorDescription(
        key="last_night",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=lambda data: data.last_night_load,
        attrs_fn=_last_night_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BaseLoadConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(BaseLoadSensor(coordinator, description) for description in SENSORS)


class BaseLoadSensor(BaseLoadEntity, SensorEntity):
    """Power value derived from the base load analysis."""

    entity_description: BaseLoadSensorDescription

    def __init__(self, coordinator, description: BaseLoadSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
