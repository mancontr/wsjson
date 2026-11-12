"""Constants for the wsjson integration."""

from datetime import datetime

DOMAIN = "wsjson"

CONF_ENTITIES = "entities"
CONF_TIDES = "tides"
CONF_STATION_ID = "station_id"
CONF_SPRING_RANGE = "spring_range"

# All endpoints live under this path.
BASE_PATH = "/api/wsjson"

# The published JSON keys. Fixed, so the payload shape is a stable contract;
# which of them are present depends on which sensors the station has.
ENTITY_KEYS = (
    "temperature",
    "dewpoint",
    "humidity",
    "pressure",
    "wind_speed",
    "wind_gust",
    "wind_bearing",
    "precipitation_rate",
    "precipitation_today",
    "uv",
    "solar_radiation",
)
# Keys that also get history and daily summaries: all but wind_bearing, whose
# min/mean/max are meaningless on a circle.
SERIES_KEYS = tuple(k for k in ENTITY_KEYS if k != "wind_bearing")

# States that should serialise as null rather than as a string.
UNAVAILABLE_STATES = frozenset({"unknown", "unavailable", "none", ""})


def iso_seconds(value: datetime) -> str:
    """ISO 8601 timestamp truncated to whole seconds."""
    return value.replace(microsecond=0).isoformat()
