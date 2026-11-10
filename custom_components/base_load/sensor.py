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
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .causes import Explanation
from .const import DAY_TYPE_WEEKEND, DAY_TYPE_WORKDAY
from .coordinator import BaseLoadConfigEntry, BaseLoadData
from .cost import annual_cost
from .entity import BaseLoadEntity
from .morning import entity_name


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
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda data: data.last_night_load,
        attrs_fn=_last_night_attrs,
    ),
    BaseLoadSensorDescription(
        key="data_quality",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=0,
        value_fn=lambda data: (
            None if data.data_quality is None else round(data.data_quality * 100, 1)
        ),
        attrs_fn=lambda data: {
            "last_night_hours": data.last_night_hours,
            "window_hours": data.window_hours,
            "stale": data.stale,
        },
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
    entities += [BaseLoadUnmeasuredSensor(coordinator), BaseLoadShiftSensor(coordinator)]
    if coordinator.price_entity:
        entities += [
            BaseLoadCostSensor(coordinator, "annual_cost", deviation=False),
            BaseLoadCostSensor(coordinator, "deviation_cost", deviation=True),
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
        "confirmed": "Als normal bestätigt",
    },
    "en": {
        "none": "Nothing unusual",
        "unmeasured": "unmeasured",
        "constant": "constant",
        "intermittent": "intermittent",
        "since": "since",
        "confirmed": "Confirmed as normal",
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
        if explanation.label and (self._show_unexplained(explanation) or not parts):
            parts.append(f"{explanation.label} {_watts(explanation.unexplained)}")
        elif self._show_unexplained(explanation) or not parts:
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
        value = value[0].upper() + value[1:]
        if self._last_night and self.coordinator.data.last_night_marked_normal:
            value = f"{text['confirmed']}: {value}"
        return value

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
            "label": explanation.label,
            "pattern": pattern.shape if pattern else None,
            "onset": pattern.onset if pattern else None,
        }
        return attrs


class BaseLoadCostSensor(BaseLoadEntity, SensorEntity):
    """Yearly cost of the base load, or of last night's deviation if it stayed."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator, key: str, deviation: bool) -> None:
        super().__init__(coordinator, key)
        self._deviation = deviation

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self.coordinator.data.currency or self.hass.config.currency

    def _watts(self) -> float | None:
        data = self.coordinator.data
        if not self._deviation:
            return data.weekly_base_load
        evaluation = data.last_night_evaluation
        if evaluation is None:
            return None
        # Only extra load costs money; missing load is reported, not credited.
        return max(evaluation.deviation, 0.0)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        watts = self._watts()
        if watts is None or data.price is None:
            return None
        return round(annual_cost(watts, data.price), 2)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        data = self.coordinator.data
        watts = self._watts()
        attrs: dict[str, Any] = {
            "power": None if watts is None else round(watts),
            "price_per_kwh": None if data.price is None else round(data.price, 4),
            "price_source": data.price_source,
        }
        if self._deviation:
            attrs["date"] = data.last_night_date
            if watts is not None and data.price is not None:
                # What last night's extra load cost during the night window.
                attrs["last_night_cost"] = round(watts / 1000 * data.window_hours * data.price, 3)
        return attrs


class BaseLoadUnmeasuredSensor(BaseLoadEntity, SensorEntity):
    """Base load no selected device explains, with a breakdown per device."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "unmeasured")

    @property
    def native_value(self) -> float | None:
        value = self.coordinator.data.unmeasured_load
        return None if value is None else round(value)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        data = self.coordinator.data
        total = data.weekly_base_load
        price = data.price

        def row(name: str, watts: float, entity_id: str | None = None) -> dict[str, Any]:
            item: dict[str, Any] = {"name": name, "power": round(watts)}
            if entity_id:
                item["entity_id"] = entity_id
            if total:
                item["share"] = round(watts / total * 100)
            if price is not None:
                item["cost_per_year"] = round(annual_cost(watts, price))
            return item

        rows = [
            row(entity_name(self.hass, sensor), watts, sensor)
            for sensor, watts in sorted(
                data.device_loads.items(), key=lambda item: item[1], reverse=True
            )
            if round(watts) > 0
        ]
        return {
            "total": None if total is None else round(total),
            "measured": round(sum(data.device_loads.values())),
            "devices": rows,
            "currency": data.currency if price is not None else None,
        }


class BaseLoadShiftSensor(BaseLoadEntity, SensorEntity):
    """Lasting changes of the night load, newest first."""

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "level_shift")

    def _name(self, entity_id: str) -> str:
        if entity_id == self.coordinator.grid_sensor:
            language = (self.hass.config.language or "en").split("-")[0]
            return "Haus gesamt" if language == "de" else "Whole house"
        return entity_name(self.hass, entity_id)

    @property
    def native_value(self) -> str:
        shifts = self.coordinator.data.shifts
        if not shifts:
            language = (self.hass.config.language or "en").split("-")[0]
            return _TEXT.get(language, _TEXT["en"])["none"]
        language = (self.hass.config.language or "en").split("-")[0]
        since = "seit" if language == "de" else "since"
        return ", ".join(
            f"{self._name(s.entity_id)} {_watts(s.delta)} {since} {s.since.strftime('%d.%m.')}"
            for s in shifts
        )

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        data = self.coordinator.data
        return {
            "shifts": [
                {
                    "entity_id": s.entity_id,
                    "name": self._name(s.entity_id),
                    "delta": round(s.delta),
                    "before": round(s.before),
                    "after": round(s.after),
                    "since": s.since,
                    "cost_per_year": (
                        round(annual_cost(s.delta, data.price)) if data.price is not None else None
                    ),
                }
                for s in data.shifts
            ]
        }
