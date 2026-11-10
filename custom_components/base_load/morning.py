"""Morning notification with feedback actions."""

from __future__ import annotations

from datetime import date, time
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_track_time_change

from .const import (
    ACTION_LABEL,
    ACTION_NORMAL,
    CONF_NOTIFY_SERVICE,
    CONF_NOTIFY_TIME,
    DEFAULT_NOTIFY_TIME,
)
from .cost import annual_cost

if TYPE_CHECKING:
    from .coordinator import BaseLoadConfigEntry, BaseLoadCoordinator, BaseLoadData

_LOGGER = logging.getLogger(__name__)

TEXT = {
    "de": {
        "title_night": "Grundlast letzte Nacht {delta}",
        "title_shift": "Neues Grundlast-Niveau",
        "night": "Nacht auf den {date}: {load} W statt {usual} W ({delta}).",
        "cause": "Vermutete Ursache: {cause}.",
        "cost": "Bliebe das so, wären das etwa {cost} pro Jahr.",
        "shift": "{name}: {delta} seit {since} (etwa {cost} pro Jahr).",
        "shift_no_cost": "{name}: {delta} seit {since}.",
        "normal": "Normal",
        "label": "Benennen",
        "label_button": "Speichern",
        "label_placeholder": "z. B. Geschirrspüler",
        "house": "Haus gesamt",
    },
    "en": {
        "title_night": "Base load last night {delta}",
        "title_shift": "New base load level",
        "night": "Night to {date}: {load} W instead of {usual} W ({delta}).",
        "cause": "Probable cause: {cause}.",
        "cost": "If it stayed, that would be about {cost} per year.",
        "shift": "{name}: {delta} since {since} (about {cost} per year).",
        "shift_no_cost": "{name}: {delta} since {since}.",
        "normal": "Normal",
        "label": "Name it",
        "label_button": "Save",
        "label_placeholder": "e.g. dishwasher",
        "house": "Whole house",
    },
}


def texts(hass: HomeAssistant) -> dict[str, str]:
    language = (hass.config.language or "en").split("-")[0]
    return TEXT.get(language, TEXT["en"])


def entity_name(hass: HomeAssistant, entity_id: str) -> str:
    """Device name if the entity belongs to one, else its name."""
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.device_id is not None:
        device = dr.async_get(hass).async_get(entry.device_id)
        if device is not None and (name := device.name_by_user or device.name):
            return name
    state = hass.states.get(entity_id)
    return state.name if state is not None else entity_id


def _money(value: float, currency: str | None) -> str:
    return f"{value:.0f} {currency or ''}".strip()


def _date(day: date) -> str:
    return day.strftime("%d.%m.")


def build_message(
    hass: HomeAssistant, coordinator: BaseLoadCoordinator, data: BaseLoadData, cause: str | None
) -> tuple[str, str, bool] | None:
    """Title, message and whether the night can be given feedback. None: nothing to say."""
    text = texts(hass)
    lines: list[str] = []
    title: str | None = None
    feedback = False

    evaluation = data.last_night_evaluation
    if (
        evaluation is not None
        and data.last_night_date is not None
        and not data.last_night_marked_normal
        and (evaluation.is_anomaly or abs(evaluation.deviation) >= coordinator.min_deviation)
    ):
        stats = data.baseline.get("weekend" if data.last_night_date.weekday() >= 5 else "workday")
        delta = f"{evaluation.deviation:+.0f} W"
        title = text["title_night"].format(delta=delta)
        lines.append(
            text["night"].format(
                date=_date(data.last_night_date),
                load=round(data.last_night_load or 0),
                usual=round(stats.median) if stats else "?",
                delta=delta,
            )
        )
        if cause:
            lines.append(text["cause"].format(cause=cause))
        if data.price is not None and evaluation.deviation > 0:
            cost = annual_cost(evaluation.deviation, data.price)
            lines.append(text["cost"].format(cost=_money(cost, data.currency)))
        feedback = True

    shifts = coordinator.unnotified_shifts()
    for shift in shifts:
        name = (
            text["house"]
            if shift.entity_id == coordinator.grid_sensor
            else entity_name(hass, shift.entity_id)
        )
        delta = f"{shift.delta:+.0f} W"
        if data.price is not None:
            cost = annual_cost(abs(shift.delta), data.price)
            lines.append(
                text["shift"].format(
                    name=name,
                    delta=delta,
                    since=_date(shift.since),
                    cost=_money(cost, data.currency),
                )
            )
        else:
            lines.append(
                text["shift_no_cost"].format(name=name, delta=delta, since=_date(shift.since))
            )
    if shifts and title is None:
        title = text["title_shift"]

    if title is None:
        return None
    return title, "\n".join(lines), feedback


async def async_send_morning(hass: HomeAssistant, entry: BaseLoadConfigEntry) -> None:
    """Refresh everything and send the morning notification if there is news."""
    coordinator = entry.runtime_data
    service = coordinator.opt(CONF_NOTIFY_SERVICE, None)
    if not service:
        return
    await coordinator.async_refresh_now()
    data = coordinator.data
    if data is None:
        return

    cause_state = hass.states.get(_cause_entity(hass, entry))
    cause = cause_state.state if cause_state else None
    built = build_message(hass, coordinator, data, cause)
    if built is None:
        return
    title, message, feedback = built

    payload: dict[str, Any] = {"title": title, "message": message}
    if feedback and data.last_night_date is not None:
        text = texts(hass)
        night = data.last_night_date.isoformat()
        payload["data"] = {
            "tag": f"base_load_{entry.entry_id}",
            "actions": [
                {"action": f"{ACTION_NORMAL}|{entry.entry_id}|{night}", "title": text["normal"]},
                {
                    "action": f"{ACTION_LABEL}|{entry.entry_id}|{night}",
                    "title": text["label"],
                    "behavior": "textInput",
                    "textInputButtonTitle": text["label_button"],
                    "textInputPlaceholder": text["label_placeholder"],
                },
            ],
        }
    domain, name = service.split(".", 1)
    await hass.services.async_call(domain, name, payload, blocking=False)


def _cause_entity(hass: HomeAssistant, entry: BaseLoadConfigEntry) -> str:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", "base_load", f"{entry.entry_id}_last_night_cause"
    )
    return entity_id or ""


def parse_time(value: str | None) -> time:
    try:
        return time.fromisoformat(value or DEFAULT_NOTIFY_TIME)
    except ValueError:
        return time.fromisoformat(DEFAULT_NOTIFY_TIME)


def async_setup_notifications(hass: HomeAssistant, entry: BaseLoadConfigEntry) -> None:
    """Schedule the morning notification and listen for its actions."""
    coordinator = entry.runtime_data
    if not coordinator.opt(CONF_NOTIFY_SERVICE, None):
        return
    at = parse_time(coordinator.opt(CONF_NOTIFY_TIME, DEFAULT_NOTIFY_TIME))

    async def _morning(_now) -> None:
        try:
            await async_send_morning(hass, entry)
        except Exception:
            _LOGGER.exception("Sending the morning notification failed")

    entry.async_on_unload(
        async_track_time_change(hass, _morning, hour=at.hour, minute=at.minute, second=0)
    )

    @callback
    def _action(event: Event) -> None:
        action = str(event.data.get("action", ""))
        parts = action.split("|")
        if len(parts) != 3 or parts[1] != entry.entry_id:
            return
        kind, _, night_raw = parts
        try:
            night = date.fromisoformat(night_raw)
        except ValueError:
            return
        if kind == ACTION_NORMAL:
            hass.async_create_task(coordinator.async_mark_normal(night))
        elif kind == ACTION_LABEL:
            name = str(event.data.get("reply_text") or "").strip()
            if name:
                hass.async_create_task(coordinator.async_label_night(night, name))

    entry.async_on_unload(hass.bus.async_listen("mobile_app_notification_action", _action))
