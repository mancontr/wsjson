"""Recorder statistics, shaped as aligned columns.

History and the daily archive share one shape: a start, an interval, and per
key a set of equal-length arrays, one value per bucket (null where missing):

    {"start": ..., "interval": "PT5M",
     "temperature": {"min": [...], "mean": [...], "max": [...]},
     "precipitation_today": {"change": [...]}}

Which fields a key gets follows from its statistics, not from config:
measurements give min/mean/max, counters (total_increasing) give change, and
angles (wind_bearing) give just a circular mean.

A key can have several sensors. Counters (rain) take, for each local day,
the sensor that measured the most that day: a failed one, offline, clogged or
stuck, measures less. Anything else takes, bucket by bucket, the first sensor
with data.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from functools import partial
from typing import Any, Literal

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from .const import ANGLE_KEYS, UNAVAILABLE_STATES

STAT_FIELDS = ("min", "mean", "max", "change")
# Published field order: the statistics, then the daily wind summary.
FIELD_ORDER = (*STAT_FIELDS, "sectors", "calm")

# One bucket: key -> field -> value. Only fields that have a value are present.
Bucket = dict[str, dict[str, Any]]
# Key -> its sensors, in order of preference.
Sources = dict[str, list[str]]


COUNTER_STATE_CLASSES = frozenset({"total", "total_increasing"})


def current_state(hass: HomeAssistant, sources: list[str]) -> State | None:
    """The state to publish for a key: the counter reading the most, or else
    the first sensor that is available."""
    available = [
        state
        for entity_id in sources
        if (state := hass.states.get(entity_id)) is not None
        and state.state.lower() not in UNAVAILABLE_STATES
    ]
    if len(available) > 1 and all(
        s.attributes.get("state_class") in COUNTER_STATE_CLASSES for s in available
    ):
        try:
            return max(available, key=lambda s: float(s.state))
        except ValueError:
            pass
    return available[0] if available else None


async def async_fetch(
    hass: HomeAssistant,
    entities: Sources,
    start: datetime,
    end: datetime,
    period: Literal["5minute", "hour", "day"],
) -> dict[datetime, Bucket]:
    """Statistics for start <= bucket < end, keyed by bucket start (UTC)."""
    entities = await _without_linear_angles(hass, entities)
    if not entities:
        return {}

    raw = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            start,
            end,
            {e for sources in entities.values() for e in sources},
            period,
            None,  # units: keep whatever the sensor reports
            set(STAT_FIELDS),
        )
    )

    rows: dict[str, dict[datetime, dict[str, float]]] = {}
    for entity_id, entity_rows in raw.items():
        for row in entity_rows:
            values = {
                f: round(row[f], 2) for f in STAT_FIELDS if row.get(f) is not None
            }
            if values:
                rows.setdefault(entity_id, {})[_row_time(row["start"])] = values

    buckets: dict[datetime, Bucket] = {}
    for key, sources in entities.items():
        series = [rows.get(e, {}) for e in sources]
        picked = _largest_per_day(series) if _is_counter(series) else _first(series)
        for t, values in picked.items():
            buckets.setdefault(t, {})[key] = values
    return buckets


Series = dict[datetime, dict[str, float]]


def _is_counter(series: list[Series]) -> bool:
    return any("change" in v for rows in series for v in rows.values())


def _first(series: list[Series]) -> Series:
    """Bucket by bucket, the first sensor with data."""
    out: Series = {}
    for rows in reversed(series):
        out.update(rows)
    return out


def _largest_per_day(series: list[Series]) -> Series:
    """For each local day, every bucket from the sensor that measured the most.

    Per day rather than per bucket: two sensors log the same rain a few
    minutes apart, and taking the larger of each bucket would count it twice.
    """
    days: dict[date, list[datetime]] = {}
    for t in set().union(*series):
        days.setdefault(dt_util.as_local(t).date(), []).append(t)

    out: Series = {}
    for times in days.values():
        totals = [sum(rows[t].get("change", 0) for t in times if t in rows) for rows in series]
        best = series[totals.index(max(totals))]
        out.update({t: best[t] for t in times if t in best})
    return out


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
        key: {f: fields[f] for f in FIELD_ORDER if f in fields}
        for key, fields in sorted(columns.items())
    }


async def _without_linear_angles(hass: HomeAssistant, entities: Sources) -> Sources:
    """Drop angle sensors whose statistics average them as plain numbers.

    An arithmetic mean of 350° and 10° is 180°: worse than nothing.
    """
    angle_ids = {e for k, sources in entities.items() if k in ANGLE_KEYS for e in sources}
    if not angle_ids:
        return entities

    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids=angle_ids)
    )
    circular = {
        e
        for e in angle_ids
        if e in metadata and metadata[e][1]["mean_type"] == StatisticMeanType.CIRCULAR
    }
    out = {}
    for key, sources in entities.items():
        if key in ANGLE_KEYS:
            sources = [e for e in sources if e in circular]
        if sources:
            out[key] = sources
    return out


def _row_time(value: Any) -> datetime:
    """Statistics rows carry epoch floats on recent HA, datetimes on older ones."""
    if isinstance(value, (int, float)):
        return dt_util.utc_from_timestamp(value)
    return dt_util.as_utc(value)
