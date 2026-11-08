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
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .causes import Explanation
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
    entities: list[SensorEntity] = [
        BaseLoadSensor(coordinator, description) for description in SENSORS
    ]
    entities += [
        BaseLoadCauseSensor(coordinator, "cause", last_night=False),
        BaseLoadCauseSensor(coordinator, "last_night_cause", last_night=True),
    ]
    async_add_entities(entities)


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


_TEXT = {
    "de": {
        "none": "Keine Auffälligkeit",
        "unmeasured": "nicht gemessen",
        "constant": "dauerhaft",
        "intermittent": "zeitweise",
        "since": "ab",
    },
    "en": {
        "none": "Nothing unusual",
        "unmeasured": "unmeasured",
        "constant": "constant",
        "intermittent": "intermittent",
        "since": "since",
    },
}
# Unexplained rest is mentioned only if it is a real part of the deviation.
UNEXPLAINED_MIN_W = 30.0
UNEXPLAINED_MIN_SHARE = 0.25


def _watts(value: float) -> str:
    return f"{value:+.0f} W"


class BaseLoadCauseSensor(BaseLoadEntity, SensorEntity):
    """Plain-language guess at what caused a deviation."""

    def __init__(self, coordinator, key: str, last_night: bool) -> None:
        super().__init__(coordinator, key)
        self._last_night = last_night

    @property
    def _explanation(self) -> Explanation | None:
        data = self.coordinator.data
        return data.last_night_explanation if self._last_night else data.explanation

    def _text(self) -> dict[str, str]:
        language = (self.hass.config.language or "en").split("-")[0]
        return _TEXT.get(language, _TEXT["en"])

    def _name_of(self, entity_id: str) -> str:
        """Device name if the sensor belongs to one, else the entity name.

        Power sensors of plugs are usually called "<device> Power" or worse,
        the device name ("Heizung") reads much better in a sentence.
        """
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is not None and entry.device_id is not None:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            if device is not None and (name := device.name_by_user or device.name):
                return name
        state = self.hass.states.get(entity_id)
        return state.name if state is not None else entity_id

    @staticmethod
    def _show_unexplained(explanation: Explanation) -> bool:
        rest = abs(explanation.unexplained)
        return rest >= UNEXPLAINED_MIN_W and rest >= UNEXPLAINED_MIN_SHARE * abs(explanation.total)

    @property
    def native_value(self) -> str:
        text = self._text()
        explanation = self._explanation
        if explanation is None:
            return text["none"]

        parts = [
            f"{self._name_of(cause.entity_id)} {_watts(cause.deviation)}"
            for cause in explanation.causes
        ]
        if self._show_unexplained(explanation) or not parts:
            rest = f"{text['unmeasured']} {_watts(explanation.unexplained)}"
            pattern = explanation.pattern
            if pattern is not None and pattern.shape is not None:
                detail = text[pattern.shape]
                if pattern.onset is not None:
                    onset = dt_util.as_local(pattern.onset).strftime("%H:%M")
                    detail = f"{detail} {text['since']} {onset}"
                rest = f"{rest} ({detail})"
            parts.append(rest)
        value = ", ".join(parts)
        return value[0].upper() + value[1:]

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        explanation = self._explanation
        data = self.coordinator.data
        attrs: dict[str, Any] = {}
        if self._last_night:
            attrs["date"] = data.last_night_date
        if explanation is None:
            return attrs
        pattern = explanation.pattern
        attrs |= {
            "total_deviation": round(explanation.total),
            "causes": [
                {
                    "entity_id": cause.entity_id,
                    "name": self._name_of(cause.entity_id),
                    "deviation": round(cause.deviation),
                    "usual": round(cause.baseline),
                    "measured": round(cause.current),
                }
                for cause in explanation.causes
            ],
            "unexplained": round(explanation.unexplained),
            "pattern": pattern.shape if pattern else None,
            "onset": pattern.onset if pattern else None,
        }
        return attrs
