"""Base Load Monitor: detects unusual night-time base load."""

from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import (
    EventEntityRegistryUpdatedData,
    async_track_entity_registry_updated_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import (
    CONF_CANDIDATE_SENSORS,
    CONF_EXCLUDED_SENSORS,
    CONF_FEED_IN_SENSORS,
    CONF_GRID_SENSOR,
    CONF_PRESENCE_ENTITY,
    CONF_PRICE_ENTITY,
    DOMAIN,
    SERVICE_LABEL_NIGHT,
    SERVICE_MARK_NORMAL,
    STORAGE_VERSION,
)
from .coordinator import BaseLoadConfigEntry, BaseLoadCoordinator
from .morning import async_setup_notifications

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_ENTRY = "config_entry_id"
_MARK_SCHEMA = vol.Schema({vol.Optional(_ENTRY): cv.string, vol.Optional("date"): cv.date})
_LABEL_SCHEMA = _MARK_SCHEMA.extend({vol.Required("label"): vol.All(cv.string, vol.Length(min=1))})


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list[BaseLoadCoordinator]:
    entries = [
        entry
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if _ENTRY not in call.data or entry.entry_id == call.data[_ENTRY]
    ]
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_found")
    return [entry.runtime_data for entry in entries]


def _night(coordinator: BaseLoadCoordinator, call: ServiceCall) -> date:
    if "date" in call.data:
        return call.data["date"]
    data = coordinator.data
    return data.last_night_date if data and data.last_night_date else dt_util.now().date()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the feedback services."""

    async def _mark_normal(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_mark_normal(_night(coordinator, call))

    async def _label_night(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            if not await coordinator.async_label_night(
                _night(coordinator, call), call.data["label"]
            ):
                raise ServiceValidationError(
                    translation_domain=DOMAIN, translation_key="nothing_to_label"
                )

    hass.services.async_register(DOMAIN, SERVICE_MARK_NORMAL, _mark_normal, _MARK_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_LABEL_NIGHT, _label_night, _LABEL_SCHEMA)
    return True


PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

_SINGLE_KEYS = (CONF_GRID_SENSOR, CONF_PRESENCE_ENTITY, CONF_PRICE_ENTITY)
_LIST_KEYS = (CONF_FEED_IN_SENSORS, CONF_EXCLUDED_SENSORS, CONF_CANDIDATE_SENSORS)


def replace_entity_id(settings: dict[str, Any], old: str, new: str) -> dict[str, Any] | None:
    """Settings with one entity ID swapped, or None if it was not used."""
    changed = False
    result = dict(settings)
    for key in _SINGLE_KEYS:
        if result.get(key) == old:
            result[key] = new
            changed = True
    for key in _LIST_KEYS:
        values = list(result.get(key, []))
        if old in values:
            result[key] = [new if value == old else value for value in values]
            changed = True
    return result if changed else None


async def async_setup_entry(hass: HomeAssistant, entry: BaseLoadConfigEntry) -> bool:
    """Set up Base Load Monitor from a config entry."""
    coordinator = BaseLoadCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.check_entities()

    @callback
    def _registry_updated(event: Event[EventEntityRegistryUpdatedData]) -> None:
        """Follow renamed entities, flag removed ones."""
        data = event.data
        if data["action"] == "update" and "old_entity_id" in data:
            settings = {**entry.data, **entry.options}
            updated = replace_entity_id(settings, data["old_entity_id"], data["entity_id"])
            if updated is not None:
                # Saving the options reloads the entry with the new entity ID.
                hass.config_entries.async_update_entry(entry, options=updated)
        elif data["action"] == "remove":
            coordinator.check_entities()

    entry.async_on_unload(
        async_track_entity_registry_updated_event(
            hass, coordinator.configured_entities, _registry_updated
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_notifications(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BaseLoadConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: BaseLoadConfigEntry) -> None:
    """Delete the stored baseline when the entry is removed."""
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()


async def _async_reload(hass: HomeAssistant, entry: BaseLoadConfigEntry) -> None:
    """Reload after the options changed."""
    await hass.config_entries.async_reload(entry.entry_id)
