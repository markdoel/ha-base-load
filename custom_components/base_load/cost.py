"""Pure helpers to turn watts into money."""

from __future__ import annotations

from .const import HOURS_PER_YEAR

_CURRENCY_ALIASES = {"€": "EUR", "$": "USD", "£": "GBP", "fr": "CHF"}
# Units given in the minor unit of a currency, with the major unit they belong to.
_MINOR_UNITS = {"ct": "EUR", "cent": "EUR", "c": "EUR", "rp": "CHF", "p": "GBP"}


def price_unit(unit: str | None, default_currency: str) -> tuple[float, str]:
    """Factor to convert a price into <currency>/kWh, and that currency.

    "EUR/kWh" -> (1, "EUR"), "ct/kWh" -> (0.01, "EUR"), "€/MWh" -> (0.001, "EUR").
    Without a unit the price is assumed to be per kWh in the default currency.
    """
    if not unit or "/" not in unit:
        return 1.0, default_currency
    money, energy = (part.strip() for part in unit.split("/", 1))
    factor = 1.0
    key = money.lower()
    if key in _MINOR_UNITS:
        factor, currency = 0.01, _MINOR_UNITS[key]
    else:
        currency = _CURRENCY_ALIASES.get(money, money.upper() or default_currency)
    energy_key = energy.lower()
    if energy_key == "mwh":
        factor /= 1000
    elif energy_key == "wh":
        factor *= 1000
    return factor, currency


def annual_cost(watts: float, price_per_kwh: float) -> float:
    """Cost of a constant load over one year."""
    return watts / 1000 * HOURS_PER_YEAR * price_per_kwh


def blended(workday: float | None, weekend: float | None) -> float | None:
    """Weekly average of the two day types (5 workdays, 2 weekend days)."""
    if workday is None and weekend is None:
        return None
    if workday is None:
        return weekend
    if weekend is None:
        return workday
    return (5 * workday + 2 * weekend) / 7
