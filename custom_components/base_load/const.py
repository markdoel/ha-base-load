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
CONF_CANDIDATE_SENSORS: Final = "candidate_sensors"
CONF_HALF_LIFE_DAYS: Final = "half_life_days"

DEFAULT_NAME: Final = "Base Load"
DEFAULT_NIGHT_START: Final = 1
DEFAULT_NIGHT_END: Final = 5
DEFAULT_LOOKBACK_WEEKS: Final = 8
DEFAULT_SENSITIVITY: Final = 3.0
DEFAULT_MIN_DEVIATION: Final = 60
DEFAULT_EXCLUDED_THRESHOLD: Final = 50
DEFAULT_HALF_LIFE_DAYS: Final = 14

# Minimum number of valid night hours per day type before the baseline is trusted.
MIN_BASELINE_HOURS: Final = 24

# Window for the current reading (median of 5-minute statistics). An hour with
# a median means an appliance must run more than 30 minutes to move the value.
CURRENT_WINDOW: Final = timedelta(minutes=60)

UPDATE_INTERVAL: Final = timedelta(minutes=5)
# The baseline changes slowly, so it is recomputed less often.
BASELINE_REFRESH: Final = timedelta(hours=6)

DAY_TYPE_WORKDAY: Final = "workday"
DAY_TYPE_WEEKEND: Final = "weekend"

DIRECTION_HIGH: Final = "high"
DIRECTION_LOW: Final = "low"

# How far before the night window the onset of a deviation is searched.
ONSET_LOOKBACK: Final = timedelta(hours=3)

CONF_PRESENCE_ENTITY: Final = "presence_entity"
CONF_PRICE_ENTITY: Final = "price_entity"

# Domains accepted for the presence entity and how "away" is read from them.
PRESENCE_DOMAINS: Final = [
    "zone",
    "person",
    "group",
    "device_tracker",
    "input_boolean",
    "binary_sensor",
]
# A night counts as away if nobody was home for at least this share of it.
AWAY_SHARE: Final = 0.5

# Average price over this period, from long-term statistics if available.
PRICE_LOOKBACK: Final = timedelta(days=30)
HOURS_PER_YEAR: Final = 8760

# A night needs at least this share of its hours with valid data to be judged.
MIN_NIGHT_COVERAGE: Final = 0.5
# Below this share of valid night hours in the learning period, data is flagged.
MIN_DATA_QUALITY: Final = 0.5
# Consecutive failed updates that are bridged with the last good result.
MAX_FAILED_UPDATES: Final = 6

STORAGE_VERSION: Final = 1
ISSUE_SENSOR_MISSING: Final = "sensor_missing"

CONF_NOTIFY_SERVICE: Final = "notify_service"
CONF_NOTIFY_TIME: Final = "notify_time"
DEFAULT_NOTIFY_TIME: Final = "06:30:00"

EVENT_LEVEL_SHIFT: Final = "base_load_level_shift"
ACTION_NORMAL: Final = "BASE_LOAD_NORMAL"
ACTION_LABEL: Final = "BASE_LOAD_LABEL"
SERVICE_MARK_NORMAL: Final = "mark_normal"
SERVICE_LABEL_NIGHT: Final = "label_night"

CONF_DETECT_APPLIANCES: Final = "detect_appliances"
CONF_MIN_STEP: Final = "min_step"
DEFAULT_MIN_STEP: Final = 80
# Runs of an unnamed cluster before the user is asked what it is.
ASK_AFTER_RUNS: Final = 10
ACTION_NAME_APPLIANCE: Final = "BASE_LOAD_APPLIANCE"
SERVICE_NAME_APPLIANCE: Final = "name_appliance"
SERVICE_FORGET_APPLIANCE: Final = "forget_appliance"
SIGNAL_APPLIANCES_UPDATED: Final = "base_load_appliances_updated_{}"
SIGNAL_APPLIANCE_NAMED: Final = "base_load_appliance_named_{}"
