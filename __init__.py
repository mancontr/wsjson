"""Serve a public, cached JSON snapshot of a weather station."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ENTITIES,
    CONF_SPRING_RANGE,
    CONF_STATION_ID,
    CONF_TIDES,
    DOMAIN,
    ENTITY_KEYS,
    SERIES_KEYS,
)
from .daily import DailyArchive
from .tides import TideTable
from .view import CurrentView, DailyIndexView, DailyYearView, HistoryView


CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                # Maps each published JSON key -> an entity_id. The keys
                # are fixed; include the ones your station has.
                vol.Required(CONF_ENTITIES): vol.All(
                    vol.Schema({vol.Optional(k): cv.entity_id for k in ENTITY_KEYS}),
                    vol.Length(min=1),
                ),
                # IHM port to take tide predictions from. Omit for no tides.
                vol.Optional(CONF_TIDES): vol.Schema(
                    {
                        vol.Required(CONF_STATION_ID): cv.string,
                        # Mean spring range at that port, in metres: the
                        # yardstick for the rough tide coefficient.
                        # Estimated from the cached predictions if omitted.
                        vol.Optional(CONF_SPRING_RANGE): vol.All(
                            vol.Coerce(float), vol.Range(min=0, min_included=False)
                        ),
                    }
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the public views. No entities, no config flow."""
    conf = config[DOMAIN]

    tides = None
    if tides_conf := conf.get(CONF_TIDES):
        tides = TideTable(
            hass,
            station_id=tides_conf[CONF_STATION_ID],
            spring_range=tides_conf.get(CONF_SPRING_RANGE),
        )
        await tides.async_start()

    entities = conf[CONF_ENTITIES]
    # Everything with a meaningful min/mean/max gets history and daily data.
    series_entities = {k: v for k, v in entities.items() if k in SERIES_KEYS}

    archive = DailyArchive(hass, entities=series_entities)
    await archive.async_start()

    hass.http.register_view(CurrentView(hass, entities=entities, tides=tides))
    hass.http.register_view(HistoryView(hass, entities=series_entities))
    hass.http.register_view(DailyIndexView(archive))
    hass.http.register_view(DailyYearView(archive))
    return True
