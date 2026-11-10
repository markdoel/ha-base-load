"""Live appliance detection: listens to the meter and builds appliances."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_NAME_APPLIANCE,
    ASK_AFTER_RUNS,
    CONF_MIN_STEP,
    CONF_NOTIFY_SERVICE,
    DEFAULT_MIN_STEP,
    DOMAIN,
    SIGNAL_APPLIANCE_NAMED,
    SIGNAL_APPLIANCES_UPDATED,
    STORAGE_VERSION,
)
from .nilm import (
    CLUSTER_POWER_TOLERANCE,
    ApplianceCatalog,
    Cluster,
    EventPairer,
    Run,
    SteadyStateDetector,
    describe_duration,
)

if TYPE_CHECKING:
    from .coordinator import BaseLoadConfigEntry, BaseLoadCoordinator

_LOGGER = logging.getLogger(__name__)

# At most one question per this period, so the phone is not flooded.
QUESTION_INTERVAL = timedelta(hours=6)
# Values older than this are not trusted when combining the signal.
MAX_VALUE_AGE = timedelta(minutes=2)

TEXT = {
    "de": {
        "title": "Unbekanntes Gerät erkannt",
        "message": (
            "Etwa {power} W für meist {duration}, {count}× seit {since}, "
            "oft gegen {hour} Uhr. Was ist das?"
        ),
        "button": "Benennen",
        "save": "Speichern",
        "placeholder": "z. B. Wasserkocher",
    },
    "en": {
        "title": "Unknown appliance detected",
        "message": (
            "About {power} W for usually {duration}, {count}× since {since}, "
            "often around {hour}:00. What is it?"
        ),
        "button": "Name it",
        "save": "Save",
        "placeholder": "e.g. kettle",
    },
}


def _float(state: str | None) -> float | None:
    try:
        return float(state) if state not in (None, "unknown", "unavailable") else None
    except (TypeError, ValueError):
        return None


class ApplianceDetector:
    """Feeds the live house load through the NILM pipeline."""

    def __init__(
        self, hass: HomeAssistant, entry: BaseLoadConfigEntry, coordinator: BaseLoadCoordinator
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.appliances"
        )
        min_step = float(coordinator.opt(CONF_MIN_STEP, DEFAULT_MIN_STEP))
        self.detector = SteadyStateDetector(min_step)
        self.pairer = EventPairer()
        self.catalog = ApplianceCatalog()
        self.running: dict[str, tuple[datetime, float]] = {}
        self.runs_total = 0
        self.house_load: float | None = None
        self._values: dict[str, tuple[datetime, float]] = {}
        self._last_question: datetime | None = None
        self._unsubs: list = []

    # Lifecycle ----------------------------------------------------------

    async def async_start(self) -> None:
        stored = await self._store.async_load() or {}
        self.catalog = ApplianceCatalog.load(stored.get("clusters", []))
        if last := stored.get("last_question"):
            self._last_question = dt_util.parse_datetime(last)
        self.runs_total = int(stored.get("runs_total", 0))

        c = self.coordinator
        watched = [c.grid_sensor, *c.feed_in_sensors, *c.excluded_sensors]
        for entity_id in watched:
            state = self.hass.states.get(entity_id)
            if state is not None and (value := self._watts(entity_id, state)) is not None:
                self._values[entity_id] = (dt_util.utcnow(), value)
        self._unsubs.append(async_track_state_change_event(self.hass, watched, self._state_changed))
        self._unsubs.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._action)
        )

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._save()

    def _save(self) -> None:
        def payload() -> dict[str, Any]:
            return {
                "clusters": self.catalog.dump(),
                "last_question": (self._last_question.isoformat() if self._last_question else None),
                "runs_total": self.runs_total,
            }

        self._store.async_delay_save(payload, 30)

    # Signal -------------------------------------------------------------

    def _watts(self, entity_id: str, state) -> float | None:
        value = _float(state.state)
        if value is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        return value * 1000 if unit == "kW" else value

    def combined(self, now: datetime) -> float | None:
        """House load = grid + local sources - excluded loads, from live values."""
        c = self.coordinator
        grid = self._values.get(c.grid_sensor)
        if grid is None:
            return None
        total = grid[1]
        for entity_id in c.feed_in_sensors:
            value = self._values.get(entity_id)
            # A source that stopped reporting while idle counts as 0.
            if value is not None and now - value[0] <= MAX_VALUE_AGE:
                total += value[1]
            elif value is not None and abs(value[1]) > 50:
                return None
        for entity_id in c.excluded_sensors:
            value = self._values.get(entity_id)
            if value is not None:
                if now - value[0] > MAX_VALUE_AGE and abs(value[1]) > 50:
                    return None
                total -= value[1]
        return total

    @callback
    def _state_changed(self, event: Event[EventStateChangedData]) -> None:
        state = event.data["new_state"]
        entity_id = event.data["entity_id"]
        if state is None:
            return
        value = self._watts(entity_id, state)
        if value is None:
            self._values.pop(entity_id, None)
            return
        now = state.last_updated
        self._values[entity_id] = (now, value)
        # Sources often update faster than the meter; the meter clocks the signal.
        if entity_id != self.coordinator.grid_sensor:
            return
        house = self.combined(now)
        if house is None:
            return
        self.house_load = house
        self.process(now, house)

    def process(self, now: datetime, house: float) -> None:
        """Run one sample through detector, pairer and catalog."""
        edge = self.detector.feed(now, house)
        if edge is None:
            return
        if edge.delta > 0:
            self._mark_running(edge.time, edge.delta)
        run = self.pairer.feed(edge)
        if run is None:
            if edge.delta < 0:
                self._signal()
            return
        self._finish(run)

    def _mark_running(self, start: datetime, power: float) -> None:
        for cluster in self.catalog.named():
            if abs(power / max(cluster.power, 1) - 1) <= CLUSTER_POWER_TOLERANCE * 1.2:
                self.running[cluster.id] = (start, power)
                self._signal()
                return

    def _finish(self, run: Run) -> None:
        self.runs_total += 1
        cluster = self.catalog.add(run)
        for cluster_id, (start, _) in list(self.running.items()):
            if start == run.start:
                del self.running[cluster_id]
        self._save()
        self._signal()
        self._maybe_ask(cluster)

    @callback
    def _signal(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_APPLIANCES_UPDATED.format(self.entry.entry_id))

    def power_of(self, cluster_id: str) -> float:
        running = self.running.get(cluster_id)
        return running[1] if running else 0.0

    # Naming -------------------------------------------------------------

    def _language(self) -> str:
        return (self.hass.config.language or "en").split("-")[0]

    def _maybe_ask(self, cluster: Cluster) -> None:
        service = self.coordinator.opt(CONF_NOTIFY_SERVICE, None)
        if not service or cluster.name or cluster.asked or cluster.count < ASK_AFTER_RUNS:
            return
        now = dt_util.utcnow()
        if self._last_question and now - self._last_question < QUESTION_INTERVAL:
            return
        question = self.catalog.to_question(ASK_AFTER_RUNS)
        if question is None:
            return
        question.asked = True
        self._last_question = now
        self._save()
        self.hass.async_create_task(self._send_question(service, question))

    async def _send_question(self, service: str, cluster: Cluster) -> None:
        language = self._language()
        text = TEXT.get(language, TEXT["en"])
        since = dt_util.parse_datetime(cluster.first_seen) if cluster.first_seen else None
        message = text["message"].format(
            power=round(cluster.power),
            duration=describe_duration(cluster.duration_s, language),
            count=cluster.count,
            since=dt_util.as_local(since).strftime("%d.%m.") if since else "?",
            hour=cluster.typical_hour if cluster.typical_hour is not None else "?",
        )
        domain, name = service.split(".", 1)
        await self.hass.services.async_call(
            domain,
            name,
            {
                "title": text["title"],
                "message": message,
                "data": {
                    "tag": f"base_load_appliance_{cluster.id}",
                    "actions": [
                        {
                            "action": f"{ACTION_NAME_APPLIANCE}|{self.entry.entry_id}|{cluster.id}",
                            "title": text["button"],
                            "behavior": "textInput",
                            "textInputButtonTitle": text["save"],
                            "textInputPlaceholder": text["placeholder"],
                        }
                    ],
                },
            },
            blocking=False,
        )

    @callback
    def _action(self, event: Event) -> None:
        parts = str(event.data.get("action", "")).split("|")
        if len(parts) != 3 or parts[0] != ACTION_NAME_APPLIANCE or parts[1] != self.entry.entry_id:
            return
        name = str(event.data.get("reply_text") or "").strip()
        if name:
            self.name(parts[2], name)

    def name(self, cluster_id: str, name: str) -> bool:
        cluster = self.catalog.get(cluster_id)
        if cluster is None or not name.strip():
            return False
        first_time = cluster.name is None
        cluster.name = name.strip()
        self._save()
        if first_time:
            async_dispatcher_send(
                self.hass, SIGNAL_APPLIANCE_NAMED.format(self.entry.entry_id), cluster.id
            )
        self._signal()
        return True

    def forget(self, cluster_id: str) -> bool:
        cluster = self.catalog.get(cluster_id)
        if cluster is None:
            return False
        self.catalog.clusters.remove(cluster)
        self.running.pop(cluster_id, None)
        registry = er.async_get(self.hass)
        for kind in ("power", "energy"):
            unique_id = f"{self.entry.entry_id}_appliance_{cluster_id}_{kind}"
            if entity_id := registry.async_get_entity_id("sensor", DOMAIN, unique_id):
                registry.async_remove(entity_id)
        self._save()
        self._signal()
        return True
