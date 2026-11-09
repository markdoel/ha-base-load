"""Tests for presence and cost helpers."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.base_load.cost import annual_cost, blended, price_unit
from custom_components.base_load.presence import away_nights, away_share, is_away

TZ = ZoneInfo("Europe/Berlin")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


@pytest.mark.parametrize(
    ("entity_id", "state", "expected"),
    [
        ("zone.home", "0", True),
        ("zone.home", "2", False),
        ("person.markus", "not_home", True),
        ("person.markus", "Arbeit", True),
        ("person.markus", "home", False),
        ("group.family", "home", False),
        ("input_boolean.holiday", "on", True),
        ("input_boolean.holiday", "off", False),
        ("zone.home", "unknown", None),
        ("person.markus", None, None),
    ],
)
def test_is_away(entity_id, state, expected):
    assert is_away(entity_id, state) is expected


def test_away_share_and_nights():
    # Home until Friday 6 Nov 18:00, away until Sunday 8 Nov 12:00.
    timeline = [(at(1, 0), "2"), (at(6, 18), "0"), (at(8, 12), "1")]
    assert away_share("zone.home", timeline, at(7, 1), at(7, 5)) == 1
    assert away_share("zone.home", timeline, at(6, 1), at(6, 5)) == 0
    # Left at 03:00 in a 01:00 to 05:00 window: half the night away.
    leave = [(at(1, 0), "2"), (at(9, 3), "0")]
    assert away_share("zone.home", leave, at(9, 1), at(9, 5)) == 0.5

    nights = [date(2026, 11, d) for d in range(5, 10)]
    found = away_nights("zone.home", timeline, nights, 1, 5, TZ, 0.5)
    assert found == {date(2026, 11, 7), date(2026, 11, 8)}


def test_away_share_unknown():
    assert away_share("zone.home", [(at(1, 0), "unknown")], at(7, 1), at(7, 5)) is None
    assert away_share("zone.home", [], at(7, 1), at(7, 5)) is None


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("EUR/kWh", (1.0, "EUR")),
        ("€/kWh", (1.0, "EUR")),
        ("ct/kWh", (0.01, "EUR")),
        ("EUR/MWh", (0.001, "EUR")),
        (None, (1.0, "CHF")),
    ],
)
def test_price_unit(unit, expected):
    factor, currency = price_unit(unit, "CHF")
    assert factor == pytest.approx(expected[0])
    assert currency == expected[1]


def test_annual_cost_and_blend():
    # 100 W all year at 0.40 EUR/kWh: 876 kWh, 350.40 EUR.
    assert annual_cost(100, 0.40) == pytest.approx(350.40)
    assert blended(400, 470) == pytest.approx(420)
    assert blended(None, 470) == 470
    assert blended(None, None) is None
