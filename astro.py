"""Sun and moon data, computed locally from Home Assistant's home location."""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any

from homeassistant.const import SUN_EVENT_SUNRISE, SUN_EVENT_SUNSET
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date

SYNODIC_MONTH_DAYS = 29.530588861
REFERENCE_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)

# Upper bound of each phase, as a fraction of the synodic month. Names match
# the states of Home Assistant's own moon sensor.
MOON_PHASES = (
    (0.02, "new_moon"),
    (0.23, "waxing_crescent"),
    (0.27, "first_quarter"),
    (0.48, "waxing_gibbous"),
    (0.52, "full_moon"),
    (0.73, "waning_gibbous"),
    (0.77, "last_quarter"),
    (0.98, "waning_crescent"),
    (1.00, "new_moon"),
)


def sun_times(hass: HomeAssistant, day: date) -> dict[str, Any]:
    """Sunrise and sunset for a local date. None during polar day or night."""
    sunrise = get_astral_event_date(hass, SUN_EVENT_SUNRISE, day)
    sunset = get_astral_event_date(hass, SUN_EVENT_SUNSET, day)

    return {
        "sunrise": sunrise.isoformat() if sunrise else None,
        "sunset": sunset.isoformat() if sunset else None,
        "day_length": (
            round((sunset - sunrise).total_seconds()) if sunrise and sunset else None
        ),
    }


def moon_phase(now: datetime) -> dict[str, Any]:
    """Mean-lunation approximation: good to a few hours, which is plenty here."""
    days = (now - REFERENCE_NEW_MOON).total_seconds() / 86400
    fraction = (days % SYNODIC_MONTH_DAYS) / SYNODIC_MONTH_DAYS
    illumination = (1 - math.cos(2 * math.pi * fraction)) / 2
    phase = next(name for upper, name in MOON_PHASES if fraction <= upper)

    return {
        "phase": phase,
        "fraction": round(fraction, 3),
        "illumination": round(illumination, 3),
    }
