"""Plain-language descriptions shared by sensors and notifications."""

from __future__ import annotations

from homeassistant.const import MAX_LENGTH_STATE_STATE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .causes import Explanation

CAUSE_TEXT = {
    "de": {
        "none": "Keine Auffälligkeit",
        "unmeasured": "nicht gemessen",
        "constant": "dauerhaft",
        "intermittent": "zeitweise",
        "since": "ab",
        "confirmed": "Als normal bestätigt",
    },
    "en": {
        "none": "Nothing unusual",
        "unmeasured": "unmeasured",
        "constant": "constant",
        "intermittent": "intermittent",
        "since": "since",
        "confirmed": "Confirmed as normal",
    },
}
# Unexplained rest is mentioned only if it is a real part of the deviation.
UNEXPLAINED_MIN_W = 30.0
UNEXPLAINED_MIN_SHARE = 0.25


def language(hass: HomeAssistant) -> str:
    """Two-letter language of the instance."""
    return (hass.config.language or "en").split("-")[0]


def cause_text(hass: HomeAssistant) -> dict[str, str]:
    return CAUSE_TEXT.get(language(hass), CAUSE_TEXT["en"])


def entity_name(hass: HomeAssistant, entity_id: str) -> str:
    """Device name if the entity belongs to one, else its name.

    Power sensors of plugs are usually called "<device> Power" or worse,
    the device name ("Heizung") reads much better in a sentence.
    """
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.device_id is not None:
        device = dr.async_get(hass).async_get(entry.device_id)
        if device is not None and (name := device.name_by_user or device.name):
            return name
    state = hass.states.get(entity_id)
    return state.name if state is not None else entity_id


def _watts(value: float) -> str:
    return f"{value:+.0f} W"


def show_unexplained(explanation: Explanation) -> bool:
    rest = abs(explanation.unexplained)
    return rest >= UNEXPLAINED_MIN_W and rest >= UNEXPLAINED_MIN_SHARE * abs(explanation.total)


def describe_cause(hass: HomeAssistant, explanation: Explanation) -> str:
    """E.g. "Heizung +70 W, nicht gemessen +40 W (dauerhaft ab 01:10)"."""
    text = cause_text(hass)
    parts = [
        f"{entity_name(hass, cause.entity_id)} {_watts(cause.deviation)}"
        for cause in explanation.causes
    ]
    rest_shown = show_unexplained(explanation) or not parts
    if explanation.label and rest_shown:
        parts.append(f"{explanation.label} {_watts(explanation.unexplained)}")
    elif rest_shown:
        rest = f"{text['unmeasured']} {_watts(explanation.unexplained)}"
        pattern = explanation.pattern
        if pattern is not None and pattern.shape is not None:
            detail = text[pattern.shape]
            if pattern.onset is not None:
                onset = dt_util.as_local(pattern.onset).strftime("%H:%M")
                detail = f"{detail} {text['since']} {onset}"
            rest = f"{rest} ({detail})"
        parts.append(rest)
    value = ", ".join(parts)
    return value[0].upper() + value[1:]


def fit_state(value: str) -> str:
    """Shorten a text to the maximum length of a state."""
    if len(value) <= MAX_LENGTH_STATE_STATE:
        return value
    return value[: MAX_LENGTH_STATE_STATE - 1].rstrip(" ,") + "…"
