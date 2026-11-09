"""End-to-end tests with a faked recorder statistics backend."""

from datetime import datetime, timedelta
from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.base_load.const import DOMAIN

GRID = "sensor.grid_power"
BATTERY = "sensor.battery_output"
WALLBOX = "sensor.wallbox_power"
HEATER = "sensor.heater_power"

ENTRY_DATA = {
    "grid_sensor": GRID,
    "feed_in_sensors": [BATTERY],
    "excluded_sensors": [WALLBOX],
    "candidate_sensors": [HEATER],
    "night_start": 1,
    "night_end": 5,
    "lookback_weeks": 8,
    "sensitivity": 3.0,
    "min_deviation": 60,
    "excluded_threshold": 50,
}


def fake_statistics(
    current_grid: float,
    charging_now: bool = False,
    heater_now: float = 20.0,
    charging_switches: bool = False,
):
    """Build a statistics_during_period replacement.

    History: grid 300 W at night (with small wobble), battery 50 W at night,
    the car charges every Tuesday night with 1850 W. Daytime 900 W.
    """

    def _impl(hass, start, end, statistic_ids, period, units, types):
        step = timedelta(hours=1) if period == "hour" else timedelta(minutes=5)
        rows = {sid: [] for sid in statistic_ids}
        moment = start.replace(minute=0, second=0, microsecond=0)
        recent_cutoff = end - timedelta(minutes=45)
        while moment < end:
            local = dt_util.as_local(moment)
            night = 1 <= local.hour < 5
            charging = night and local.weekday() == 1
            wobble = (local.day % 5) * 4
            grid = (250.0 + wobble if night else 900.0) + (1850.0 if charging else 0.0)
            battery = 50.0 if night else 0.0
            wallbox = 1850.0 if charging else 0.0
            heater = 20.0
            if period == "5minute" and moment >= recent_cutoff:
                grid, battery = current_grid, 50.0
                wallbox = 1850.0 if charging_now else 0.0
                heater = heater_now
            values = {GRID: grid, BATTERY: battery, WALLBOX: wallbox, HEATER: heater}
            lows = dict(values)
            highs = dict(values)
            if charging_switches and period == "5minute":
                values[WALLBOX], lows[WALLBOX], highs[WALLBOX] = 900.0, 0.0, 1850.0
            for sid in statistic_ids:
                rows[sid].append(
                    {
                        "start": moment.timestamp(),
                        "mean": values[sid],
                        "min": lows[sid],
                        "max": highs[sid],
                    }
                )
            moment += step
        return rows

    return _impl


async def _setup(
    hass: HomeAssistant,
    current_grid: float,
    charging_now: bool = False,
    heater_now: float = 20.0,
    charging_switches: bool = False,
):
    entry = MockConfigEntry(domain=DOMAIN, title="Base Load", data=ENTRY_DATA, unique_id=GRID)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.base_load.coordinator.statistics_during_period",
        side_effect=fake_statistics(current_grid, charging_now, heater_now, charging_switches),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


@pytest.fixture
async def night(hass: HomeAssistant, freezer):
    """Wednesday 4 Nov 2026, 03:30 in Berlin."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(datetime(2026, 11, 4, 3, 30, tzinfo=dt_util.get_time_zone("Europe/Berlin")))


async def test_normal_night(hass: HomeAssistant, night) -> None:
    await _setup(hass, current_grid=262.0)

    expected = hass.states.get("sensor.base_load_expected_base_load")
    current = hass.states.get("sensor.base_load_current_base_load")
    anomaly = hass.states.get("binary_sensor.base_load_base_load_anomaly")

    # House load = grid + battery, charging nights excluded.
    assert 300 <= float(expected.state) <= 320
    assert expected.attributes["day_type"] == "workday"
    assert float(current.state) == pytest.approx(312.0)
    assert anomaly.state == "off"
    assert anomaly.attributes["status"] == "monitoring"


async def test_high_load_is_flagged(hass: HomeAssistant, night) -> None:
    await _setup(hass, current_grid=600.0)

    anomaly = hass.states.get("binary_sensor.base_load_base_load_anomaly")
    deviation = hass.states.get("sensor.base_load_base_load_deviation")
    assert anomaly.state == "on"
    assert anomaly.attributes["direction"] == "high"
    assert float(deviation.state) > 300


async def test_full_charging_is_subtracted(hass: HomeAssistant, night) -> None:
    # Car charges the whole window: 262 W house + 1850 W charger on the grid.
    await _setup(hass, current_grid=2112.0, charging_now=True)

    anomaly = hass.states.get("binary_sensor.base_load_base_load_anomaly")
    current = hass.states.get("sensor.base_load_current_base_load")
    assert anomaly.state == "off"
    assert anomaly.attributes["status"] == "monitoring"
    assert float(current.state) == pytest.approx(312.0)


async def test_switching_charger_suppresses_evaluation(hass: HomeAssistant, night) -> None:
    await _setup(hass, current_grid=1162.0, charging_switches=True)

    anomaly = hass.states.get("binary_sensor.base_load_base_load_anomaly")
    assert anomaly.state == "off"
    assert anomaly.attributes["status"] == "excluded_load_active"


async def test_last_night_verdict(hass: HomeAssistant, night) -> None:
    await _setup(hass, current_grid=262.0)

    last = hass.states.get("binary_sensor.base_load_last_night_anomaly")
    # The night ending Tuesday 3 Nov was a charging night. Full charging hours
    # are subtracted now, so it counts as the last completed night.
    assert last.state == "off"
    assert str(last.attributes["date"]) == "2026-11-03"


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Haus", **ENTRY_DATA, "excluded_sensors": [GRID]}
    )
    assert result["errors"] == {"base": "duplicate_sensor"}

    with patch("custom_components.base_load.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "Haus", **ENTRY_DATA}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Haus"
    assert result["data"]["grid_sensor"] == GRID
    assert "name" not in result["data"]


async def test_options_flow(hass: HomeAssistant, night) -> None:
    entry = await _setup(hass, current_grid=262.0)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.base_load.coordinator.statistics_during_period",
        side_effect=fake_statistics(262.0),
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**ENTRY_DATA, "sensitivity": 2.5}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["sensitivity"] == 2.5


async def test_options_flow_rejects_empty_window(hass: HomeAssistant, night) -> None:
    entry = await _setup(hass, current_grid=262.0)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**ENTRY_DATA, "night_start": 3, "night_end": 3}
    )
    assert result["errors"] == {"night_end": "empty_window"}


async def test_diagnostics(hass: HomeAssistant, night) -> None:
    from custom_components.base_load.diagnostics import async_get_config_entry_diagnostics

    entry = await _setup(hass, current_grid=600.0)
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["config"]["grid_sensor"] == GRID
    assert diag["evaluation"]["is_anomaly"] is True
    assert set(diag["baseline"]) == {"workday", "weekend"}


async def test_cause_measured_device(hass: HomeAssistant, night) -> None:
    hass.states.async_set(HEATER, "320", {"friendly_name": "Heater"})
    await _setup(hass, current_grid=562.0, heater_now=320.0)

    cause = hass.states.get("sensor.base_load_probable_cause")
    assert cause.state == "Heater +300 W"
    assert cause.attributes["causes"][0]["entity_id"] == HEATER
    assert abs(cause.attributes["unexplained"]) < 30


async def test_cause_unmeasured(hass: HomeAssistant, night) -> None:
    await _setup(hass, current_grid=600.0)

    cause = hass.states.get("sensor.base_load_probable_cause")
    assert cause.state.startswith("Unmeasured +")
    assert "constant since" in cause.state
    assert cause.attributes["causes"] == []
    assert cause.attributes["pattern"] == "constant"


async def test_no_cause_on_normal_night(hass: HomeAssistant, night) -> None:
    await _setup(hass, current_grid=262.0)
    cause = hass.states.get("sensor.base_load_probable_cause")
    assert cause.state == "Nothing unusual"


async def test_diagnostics_include_explanation(hass: HomeAssistant, night) -> None:
    from custom_components.base_load.diagnostics import async_get_config_entry_diagnostics

    entry = await _setup(hass, current_grid=600.0)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["explanation"]["pattern"]["shape"] == "constant"
    assert diag["candidate_sensors"] == [HEATER]
