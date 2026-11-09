"""Base Load Monitor: detects unusual night-time base load."""

from __future__ import annotations

from typing import Any

from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    EventEntityRegistryUpdatedData,
    async_track_entity_registry_updated_event,
)
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CANDIDATE_SENSORS,
    CONF_EXCLUDED_SENSORS,
    CONF_FEED_IN_SENSORS,
    CONF_GRID_SENSOR,
    CONF_PRESENCE_ENTITY,
    CONF_PRICE_ENTITY,
    DOMAIN,
    STORAGE_VERSION,
)
from .coordinator import BaseLoadConfigEntry, BaseLoadCoordinator

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
