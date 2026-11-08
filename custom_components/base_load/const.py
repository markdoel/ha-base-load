"""Constants for the Base Load Monitor integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "base_load"

CONF_GRID_SENSOR: Final = "grid_sensor"
CONF_FEED_IN_SENSORS: Final = "feed_in_sensors"
CONF_EXCLUDED_SENSORS: Final = "excluded_sensors"
CONF_NIGHT_START: Final = "night_start"
CONF_NIGHT_END: Final = "night_end"
CONF_LOOKBACK_WEEKS: Final = "lookback_weeks"
CONF_SENSITIVITY: Final = "sensitivity"
CONF_MIN_DEVIATION: Final = "min_deviation"
CONF_EXCLUDED_THRESHOLD: Final = "excluded_threshold"

DEFAULT_NAME: Final = "Base Load"
DEFAULT_NIGHT_START: Final = 1
DEFAULT_NIGHT_END: Final = 5
DEFAULT_LOOKBACK_WEEKS: Final = 8
DEFAULT_SENSITIVITY: Final = 3.0
DEFAULT_MIN_DEVIATION: Final = 60
DEFAULT_EXCLUDED_THRESHOLD: Final = 50

# Minimum number of valid night hours per day type before the baseline is trusted.
MIN_BASELINE_HOURS: Final = 24

# Window for the current reading (mean of 5-minute statistics).
CURRENT_WINDOW: Final = timedelta(minutes=30)

UPDATE_INTERVAL: Final = timedelta(minutes=5)
# The baseline changes slowly, so it is recomputed less often.
BASELINE_REFRESH: Final = timedelta(hours=6)

DAY_TYPE_WORKDAY: Final = "workday"
DAY_TYPE_WEEKEND: Final = "weekend"

DIRECTION_HIGH: Final = "high"
DIRECTION_LOW: Final = "low"
