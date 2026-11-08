"""Config flow for Base Load Monitor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME, UnitOfPower
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_CANDIDATE_SENSORS,
    CONF_EXCLUDED_SENSORS,
    CONF_EXCLUDED_THRESHOLD,
    CONF_FEED_IN_SENSORS,
    CONF_GRID_SENSOR,
    CONF_LOOKBACK_WEEKS,
    CONF_MIN_DEVIATION,
    CONF_NIGHT_END,
    CONF_NIGHT_START,
    CONF_SENSITIVITY,
    DEFAULT_EXCLUDED_THRESHOLD,
    DEFAULT_LOOKBACK_WEEKS,
    DEFAULT_MIN_DEVIATION,
    DEFAULT_NAME,
    DEFAULT_NIGHT_END,
    DEFAULT_NIGHT_START,
    DEFAULT_SENSITIVITY,
    DOMAIN,
)

_POWER_SENSOR = selector.EntitySelectorConfig(domain="sensor", device_class=SensorDeviceClass.POWER)
_HOUR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="h"
    )
)


def _watts(maximum: int) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            max=maximum,
            step=5,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=UnitOfPower.WATT,
        )
    )


def _sources_schema(defaults: dict[str, Any]) -> dict[vol.Marker, Any]:
    """Sensor selection, shared by setup and options."""
    return {
        vol.Required(
            CONF_GRID_SENSOR, default=defaults.get(CONF_GRID_SENSOR, vol.UNDEFINED)
        ): selector.EntitySelector(_POWER_SENSOR),
        vol.Optional(
            CONF_FEED_IN_SENSORS, default=defaults.get(CONF_FEED_IN_SENSORS, [])
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="sensor", device_class=SensorDeviceClass.POWER, multiple=True
            )
        ),
        vol.Optional(
            CONF_EXCLUDED_SENSORS, default=defaults.get(CONF_EXCLUDED_SENSORS, [])
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="sensor", device_class=SensorDeviceClass.POWER, multiple=True
            )
        ),
        vol.Optional(
            CONF_CANDIDATE_SENSORS, default=defaults.get(CONF_CANDIDATE_SENSORS, [])
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain="sensor", device_class=SensorDeviceClass.POWER, multiple=True
            )
        ),
    }


def _tuning_schema(defaults: dict[str, Any]) -> dict[vol.Marker, Any]:
    """Night window and sensitivity, shared by setup and options."""
    return {
        vol.Required(
            CONF_NIGHT_START, default=defaults.get(CONF_NIGHT_START, DEFAULT_NIGHT_START)
        ): _HOUR,
        vol.Required(
            CONF_NIGHT_END, default=defaults.get(CONF_NIGHT_END, DEFAULT_NIGHT_END)
        ): _HOUR,
        vol.Required(
            CONF_LOOKBACK_WEEKS,
            default=defaults.get(CONF_LOOKBACK_WEEKS, DEFAULT_LOOKBACK_WEEKS),
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=2, max=26, step=1, mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Required(
            CONF_SENSITIVITY, default=defaults.get(CONF_SENSITIVITY, DEFAULT_SENSITIVITY)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=10, step=0.5, mode=selector.NumberSelectorMode.SLIDER
            )
        ),
        vol.Required(
            CONF_MIN_DEVIATION,
            default=defaults.get(CONF_MIN_DEVIATION, DEFAULT_MIN_DEVIATION),
        ): _watts(2000),
        vol.Required(
            CONF_EXCLUDED_THRESHOLD,
            default=defaults.get(CONF_EXCLUDED_THRESHOLD, DEFAULT_EXCLUDED_THRESHOLD),
        ): _watts(1000),
    }


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    grid = user_input[CONF_GRID_SENSOR]
    sources = set(user_input.get(CONF_FEED_IN_SENSORS, []))
    excluded = set(user_input.get(CONF_EXCLUDED_SENSORS, []))
    candidates = set(user_input.get(CONF_CANDIDATE_SENSORS, []))
    if (
        grid in sources | excluded | candidates
        or sources & excluded
        or candidates & (sources | excluded)
    ):
        errors["base"] = "duplicate_sensor"
    if int(user_input[CONF_NIGHT_START]) == int(user_input[CONF_NIGHT_END]):
        errors[CONF_NIGHT_END] = "empty_window"
    return errors


class BaseLoadConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the setup flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_GRID_SENSOR])
                self._abort_if_unique_id_configured()
                name = user_input.pop(CONF_NAME)
                return self.async_create_entry(title=name, data=user_input)

        defaults = user_input or {}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME, default=defaults.get(CONF_NAME, DEFAULT_NAME)
                ): selector.TextSelector(),
                **_sources_schema(defaults),
                **_tuning_schema(defaults),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> BaseLoadOptionsFlow:
        return BaseLoadOptionsFlow()


class BaseLoadOptionsFlow(OptionsFlow):
    """Change sensors and tuning after setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                return self.async_create_entry(data=user_input)

        defaults = user_input or {**self.config_entry.data, **self.config_entry.options}
        schema = vol.Schema({**_sources_schema(defaults), **_tuning_schema(defaults)})
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
