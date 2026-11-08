"""Diagnostics for Base Load Monitor."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import BaseLoadConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BaseLoadConfigEntry
) -> dict[str, Any]:
    """Return configuration, baseline and the latest evaluation.

    Nothing here is secret: only sensor IDs and power values.
    """
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "config": dict(entry.data),
        "options": dict(entry.options),
        "last_update_success": coordinator.last_update_success,
        "baseline": {key: asdict(stats) for key, stats in data.baseline.items()},
        "baseline_updated": data.baseline_updated,
        "current_load": data.current_load,
        "excluded_active": data.excluded_active,
        "in_night": data.in_night,
        "day_type": data.day_type,
        "evaluation": asdict(data.evaluation) if data.evaluation else None,
        "last_night_date": data.last_night_date,
        "last_night_load": data.last_night_load,
        "last_night_evaluation": (
            asdict(data.last_night_evaluation) if data.last_night_evaluation else None
        ),
        "explanation": asdict(data.explanation) if data.explanation else None,
        "last_night_explanation": (
            asdict(data.last_night_explanation) if data.last_night_explanation else None
        ),
        "candidate_sensors": coordinator.candidate_sensors,
    }
