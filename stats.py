"""Recorder statistics, shaped as aligned columns.

History and the daily archive share one shape: a start, an interval, and per
key a set of equal-length arrays, one value per bucket (null where missing):

    {"start": ..., "interval": "PT5M",
     "temperature": {"min": [...], "mean": [...], "max": [...]},
     "precipitation_today": {"change": [...]}}

Which fields a key gets follows from its statistics, not from config:
measurements give min/mean/max, counters (total_increasing) give change.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from functools import partial
from typing import Any, Literal

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

STAT_FIELDS = ("min", "mean", "max", "change")

# One bucket: key -> field -> value. Only fields that have a value are present.
Bucket = dict[str, dict[str, float]]


async def async_fetch(
    hass: HomeAssistant,
    entities: dict[str, str],
    start: datetime,
    end: datetime,
    period: Literal["5minute", "day"],
) -> dict[datetime, Bucket]:
    """Statistics for start <= bucket < end, keyed by bucket start (UTC)."""
    raw = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            start,
            end,
            set(entities.values()),
            period,
            None,  # units: keep whatever the sensor reports
            set(STAT_FIELDS),
        )
    )

    buckets: dict[datetime, Bucket] = {}
    for key, entity_id in entities.items():
        for row in raw.get(entity_id, []):
            values = {
                f: round(row[f], 2) for f in STAT_FIELDS if row.get(f) is not None
            }
            if values:
                buckets.setdefault(_row_time(row["start"]), {})[key] = values
    return buckets


def to_columns(buckets: Iterable[Bucket | None]) -> dict[str, dict[str, list]]:
    """Turn a sequence of buckets into one array per key and field."""
    buckets = list(buckets)
    columns: dict[str, dict[str, list]] = {}
    for i, bucket in enumerate(buckets):
        for key, values in (bucket or {}).items():
            fields = columns.setdefault(key, {})
            for field, value in values.items():
                fields.setdefault(field, [None] * len(buckets))[i] = value

    # Stable field order, whatever order they first showed up in.
    return {
        key: {f: fields[f] for f in STAT_FIELDS if f in fields}
        for key, fields in sorted(columns.items())
    }


def _row_time(value: Any) -> datetime:
    """Statistics rows carry epoch floats on recent HA, datetimes on older ones."""
    if isinstance(value, (int, float)):
        return dt_util.utc_from_timestamp(value)
    return dt_util.as_utc(value)
