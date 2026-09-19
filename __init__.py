"""Serve a public, cached JSON snapshot of a weather station."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ENTITIES,
    CONF_HISTORY,
    CONF_HISTORY_HOURS,
    CONF_HISTORY_TTL,
    CONF_URL_PATH,
    DEFAULT_HISTORY_HOURS,
    DEFAULT_HISTORY_TTL,
    DEFAULT_URL_PATH,
    DOMAIN,
)
from .view import WeatherStationView


def _validate_history_keys(conf: dict) -> dict:
    """History is a subset of the entity keys, not a second entity map."""
    unknown = sorted(set(conf[CONF_HISTORY]) - set(conf[CONF_ENTITIES]))
    if unknown:
        raise vol.Invalid(
            f"'{CONF_HISTORY}' refers to keys not defined in "
            f"'{CONF_ENTITIES}': {', '.join(unknown)}"
        )
    return conf


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.All(
            vol.Schema(
                {
                    # Maps a stable JSON key -> an entity_id, so the published
                    # shape does not change if you rename entities later.
                    vol.Required(CONF_ENTITIES): vol.Schema(
                        {cv.string: cv.entity_id}
                    ),
                    # Which of those keys also get a 24h series. Omit for none.
                    vol.Optional(CONF_HISTORY, default=list): vol.All(
                        cv.ensure_list, [cv.string]
                    ),
                    vol.Optional(
                        CONF_HISTORY_HOURS, default=DEFAULT_HISTORY_HOURS
                    ): vol.All(vol.Coerce(int), vol.Range(min=1, max=240)),
                    vol.Optional(
                        CONF_HISTORY_TTL, default=DEFAULT_HISTORY_TTL
                    ): cv.positive_int,
                    vol.Optional(CONF_URL_PATH, default=DEFAULT_URL_PATH): cv.string,
                }
            ),
            _validate_history_keys,
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the public view. No entities, no config flow, no state."""
    conf = config[DOMAIN]

    hass.http.register_view(
        WeatherStationView(
            hass,
            url_path=conf[CONF_URL_PATH],
            entities=conf[CONF_ENTITIES],
            history_keys=conf[CONF_HISTORY],
            history_hours=conf[CONF_HISTORY_HOURS],
            history_ttl=conf[CONF_HISTORY_TTL],
        )
    )
    return True
