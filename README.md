# Base Load Monitor

Custom integration for Home Assistant that learns the normal night-time base load
of a household from long-term statistics and reports unusual deviations, in both
directions:

- **high**: something keeps running that normally does not (a forgotten heater, a stuck inverter, a pump)
- **low**: something that should run has stopped (fridge, freezer, network gear)

No extra data storage and no learning phase: the baseline is computed directly from
the recorder's hourly statistics, so years of existing data are usable on day one.

## How it works

```
house load = grid import + local sources (PV, battery) - excluded loads
```

1. Hourly means of all selected sensors are read from the recorder for the learning
   period (default 8 weeks).
2. Hours inside the night window (default 01:00 to 05:00) are grouped into workday
   and weekend nights. The day type is that of the morning after.
3. Hours in which an excluded load (e.g. the EV charger) ran above its threshold
   are dropped. Subtracting a charging session hour by hour is too inaccurate.
4. Per day type the **median** and **MAD** are computed. Both are robust against
   outliers, so a few unusual nights do not shift the baseline.
5. During the night the mean of the last 30 minutes (5-minute statistics) is compared
   with the baseline. An anomaly needs both a robust score above the sensitivity and
   an absolute deviation above the minimum.

The night being evaluated never enters its own baseline.

## Entities

| Entity | Description |
|---|---|
| `sensor.<name>_expected_base_load` | Baseline median for the current day type, attributes for both day types |
| `sensor.<name>_current_base_load` | House load, mean of the last 30 minutes |
| `sensor.<name>_base_load_deviation` | Deviation from the baseline during the night window, with `score` and `direction` |
| `sensor.<name>_last_night_base_load` | Mean house load of the last completed night |
| `binary_sensor.<name>_base_load_anomaly` | On while the current night deviates. Attribute `status`: `monitoring`, `learning`, `outside_night_window`, `excluded_load_active`, `no_data` |
| `binary_sensor.<name>_last_night_anomaly` | Verdict for the last completed night, ideal for a morning notification |

## Requirements

- All selected sensors need `state_class: measurement` and `device_class: power`,
  otherwise the recorder keeps no long-term statistics for them.
- The grid sensor may be negative during export.

## Installation

HACS: add this repository as a custom repository (category *Integration*), install,
restart Home Assistant, then add **Base Load Monitor** under
*Settings → Devices & services*.

Manual: copy `custom_components/base_load` into your `config/custom_components`.

## Development

```bash
pip install -r requirements_test.txt
pytest
ruff check custom_components tests
```
