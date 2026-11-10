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
    heater_since: datetime | None = None,
    extra_since: datetime | None = None,
):
    """Build a statistics_during_period replacement.

    History: grid 300 W at night (with small wobble), battery 50 W at night,
    the car charges every Tuesday night with 1850 W. Daytime 900 W.
    """

    def _impl(hass, start, end, statistic_ids, period, units, types):
        step = timedelta(hours=1) if period == "hour" else timedelta(minutes=5)
        rows = {sid: [] for sid in statistic_ids if sid in (GRID, BATTERY, WALLBOX, HEATER)}
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
            if heater_since is not None and local >= heater_since:
                # Heating season: the heater runs at 90 W from then on.
                heater = 90.0
                grid += 70.0
            # An unmeasured load of about 300 W, 01:00 to 02:30.
            if (
                extra_since is not None
                and local >= extra_since
                and (local.hour == 1 or (local.hour == 2 and local.minute < 30))
            ):
                grid += 300.0
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
                if sid not in values:
                    continue
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
    data: dict | None = None,
    presence_history: list | None = None,
    **fake_kwargs,
):
    entry = MockConfigEntry(
        domain=DOMAIN, title="Base Load", data=data or ENTRY_DATA, unique_id=GRID
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.base_load.coordinator.statistics_during_period",
            side_effect=fake_statistics(
                current_grid, charging_now, heater_now, charging_switches, **fake_kwargs
            ),
        ),
        patch(
            "custom_components.base_load.coordinator.history.get_significant_states",
            return_value=_presence_states(presence_history or []),
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _presence_states(changes: list[tuple[datetime, str]]):
    """Fake recorder history for zone.home."""
    from homeassistant.core import State

    states = []
    for moment, value in changes:
        state = State("zone.home", value)
        object.__setattr__(state, "last_changed", moment)
        states.append(state)
    return {"zone.home": states}


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


BERLIN = dt_util.get_time_zone("Europe/Berlin")


async def test_away_night_is_skipped(hass: HomeAssistant, night) -> None:
    hass.states.async_set("zone.home", "0")
    data = {**ENTRY_DATA, "presence_entity": "zone.home"}
    history = [
        (datetime(2026, 10, 20, tzinfo=BERLIN), "2"),
        (datetime(2026, 11, 2, 18, tzinfo=BERLIN), "0"),
    ]
    await _setup(hass, current_grid=120.0, data=data, presence_history=history)

    anomaly = hass.states.get("binary_sensor.base_load_base_load_anomaly")
    last = hass.states.get("binary_sensor.base_load_last_night_anomaly")
    assert anomaly.state == "off"
    assert anomaly.attributes["status"] == "away"
    assert last.attributes["status"] == "away"
    assert last.state == "unknown"


async def test_cost_sensors(hass: HomeAssistant, night) -> None:
    hass.states.async_set("input_number.price", "0.40", {"unit_of_measurement": "EUR/kWh"})
    data = {**ENTRY_DATA, "price_entity": "input_number.price"}
    await _setup(hass, current_grid=262.0, data=data)

    cost = hass.states.get("sensor.base_load_base_load_cost_per_year")
    assert cost.attributes["unit_of_measurement"] == "EUR"
    assert cost.attributes["price_source"] == "current"
    # About 310 W all year at 0.40 EUR/kWh.
    assert 1000 < float(cost.state) < 1200
    assert hass.states.get("sensor.base_load_deviation_cost_per_year") is not None


async def test_data_quality_ok(hass: HomeAssistant, night) -> None:
    await _setup(hass, current_grid=262.0)
    quality = hass.states.get("sensor.base_load_data_quality")
    incomplete = hass.states.get("binary_sensor.base_load_data_incomplete")
    assert float(quality.state) > 95
    assert incomplete.state == "off"


async def test_failed_update_keeps_last_result(hass: HomeAssistant, night) -> None:
    entry = await _setup(hass, current_grid=262.0)
    coordinator = entry.runtime_data

    with patch(
        "custom_components.base_load.coordinator.statistics_during_period",
        side_effect=RuntimeError("recorder busy"),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    expected = hass.states.get("sensor.base_load_expected_base_load")
    incomplete = hass.states.get("binary_sensor.base_load_data_incomplete")
    assert expected.state not in ("unavailable", "unknown")
    assert incomplete.state == "on"
    assert "stale" in incomplete.attributes["reasons"]


async def test_missing_sensor_raises_repair(hass: HomeAssistant, night) -> None:
    from homeassistant.helpers import issue_registry as ir

    entry = await _setup(hass, current_grid=262.0)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"sensor_missing_{entry.entry_id}")
    # No test entity exists for the configured sensors.
    assert issue is not None
    assert GRID in issue.translation_placeholders["entities"]

    for entity_id in (GRID, BATTERY, WALLBOX, HEATER):
        hass.states.async_set(entity_id, "0")
    entry.runtime_data.check_entities()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"sensor_missing_{entry.entry_id}") is None


async def test_renamed_sensor_is_followed(hass: HomeAssistant, night) -> None:
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "test", "heater", suggested_object_id="heater_power")
    entry = await _setup(hass, current_grid=262.0)

    with patch(
        "custom_components.base_load.coordinator.statistics_during_period",
        side_effect=fake_statistics(262.0),
    ):
        registry.async_update_entity(HEATER, new_entity_id="sensor.heater_renamed")
        await hass.async_block_till_done()

    assert entry.options["candidate_sensors"] == ["sensor.heater_renamed"]


async def test_unmeasured_breakdown(hass: HomeAssistant, night) -> None:
    hass.states.async_set(HEATER, "20", {"friendly_name": "Heater"})
    await _setup(hass, current_grid=262.0)

    unmeasured = hass.states.get("sensor.base_load_unmeasured_base_load")
    # House about 308 W, heater 20 W measured.
    assert 270 < float(unmeasured.state) < 300
    assert unmeasured.attributes["devices"][0]["name"] == "Heater"
    assert unmeasured.attributes["devices"][0]["power"] == 20


async def test_level_shift_detected(hass: HomeAssistant, night) -> None:
    events = []
    hass.bus.async_listen("base_load_level_shift", events.append)
    hass.states.async_set(HEATER, "90", {"friendly_name": "Heater"})
    since = datetime(2026, 10, 30, 12, tzinfo=BERLIN)
    await _setup(hass, current_grid=332.0, heater_now=90.0, heater_since=since)

    shift = hass.states.get("sensor.base_load_new_base_load_level")
    entries = {item["entity_id"]: item for item in shift.attributes["shifts"]}
    assert entries[HEATER]["delta"] == 70
    assert str(entries[HEATER]["since"]) == "2026-10-31"
    assert any(event.data["entity_id"] == HEATER for event in events)


async def test_no_level_shift_on_stable_data(hass: HomeAssistant, night) -> None:
    await _setup(hass, current_grid=262.0)
    shift = hass.states.get("sensor.base_load_new_base_load_level")
    assert shift.attributes["shifts"] == []
    assert shift.state == "Nothing unusual"


async def _labelled_setup(hass: HomeAssistant):
    data = {**ENTRY_DATA, "min_deviation": 30}
    return await _setup(
        hass,
        current_grid=262.0,
        data=data,
        extra_since=datetime(2026, 11, 3, 0, tzinfo=BERLIN),
    )


async def test_mark_normal_service(hass: HomeAssistant, night) -> None:
    await _labelled_setup(hass)
    last = hass.states.get("binary_sensor.base_load_last_night_anomaly")
    assert last.attributes["deviation"] > 30

    await hass.services.async_call("base_load", "mark_normal", {}, blocking=True)
    await hass.async_block_till_done()

    last = hass.states.get("binary_sensor.base_load_last_night_anomaly")
    cause = hass.states.get("sensor.base_load_last_night_probable_cause")
    assert last.attributes["status"] == "marked_normal"
    assert last.state == "off"
    assert cause.state.startswith("Confirmed as normal")


async def test_label_night_and_recognise(hass: HomeAssistant, night) -> None:
    entry = await _labelled_setup(hass)
    coordinator = entry.runtime_data
    pattern = coordinator.data.last_night_explanation.pattern
    assert pattern is not None and pattern.shape is not None

    extra = datetime(2026, 11, 3, 0, tzinfo=BERLIN)
    with patch(
        "custom_components.base_load.coordinator.statistics_during_period",
        side_effect=fake_statistics(262.0, extra_since=extra),
    ):
        await hass.services.async_call(
            "base_load", "label_night", {"label": "Dishwasher"}, blocking=True
        )
        await hass.async_block_till_done()

    assert [label.name for label in coordinator._labels] == ["Dishwasher"]
    cause = hass.states.get("sensor.base_load_last_night_probable_cause")
    assert "Dishwasher" in cause.state


async def test_morning_notification(hass: HomeAssistant, night) -> None:
    from pytest_homeassistant_custom_component.common import async_mock_service

    from custom_components.base_load.morning import async_send_morning

    calls = async_mock_service(hass, "notify", "mobile_app_test")
    data = {**ENTRY_DATA, "min_deviation": 30, "notify_service": "notify.mobile_app_test"}
    entry = await _setup(
        hass,
        current_grid=262.0,
        data=data,
        extra_since=datetime(2026, 11, 3, 0, tzinfo=BERLIN),
    )
    with (
        patch(
            "custom_components.base_load.coordinator.statistics_during_period",
            side_effect=fake_statistics(262.0, extra_since=datetime(2026, 11, 3, 0, tzinfo=BERLIN)),
        ),
        patch(
            "custom_components.base_load.coordinator.history.get_significant_states",
            return_value={},
        ),
    ):
        await async_send_morning(hass, entry)
        await hass.async_block_till_done()

    assert len(calls) == 1
    payload = calls[0].data
    assert payload["title"].startswith("Base load last night +")
    actions = [action["action"] for action in payload["data"]["actions"]]
    assert actions[0].startswith("BASE_LOAD_NORMAL|")
    assert payload["data"]["actions"][1]["behavior"] == "textInput"

    # The notification action marks the night as normal.
    hass.bus.async_fire("mobile_app_notification_action", {"action": actions[0]})
    await hass.async_block_till_done()
    assert entry.runtime_data.data.last_night_marked_normal
