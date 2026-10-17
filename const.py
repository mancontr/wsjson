"""Constants for the wsjson integration."""

DOMAIN = "wsjson"

CONF_ENTITIES = "entities"
CONF_HISTORY = "history"
CONF_HISTORY_HOURS = "history_hours"
CONF_HISTORY_TTL = "history_ttl"
CONF_URL_PATH = "url_path"
CONF_TIDES = "tides"
CONF_STATION_ID = "station_id"
CONF_SPRING_RANGE = "spring_range"

DEFAULT_HISTORY_HOURS = 24
DEFAULT_HISTORY_TTL = 300  # seconds; matches the 5-minute statistics cadence
DEFAULT_URL_PATH = "/api/wsjson/data.json"

# States that should serialise as null rather than as a string.
UNAVAILABLE_STATES = frozenset({"unknown", "unavailable", "none", ""})
