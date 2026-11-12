"""Public HTTP views: current conditions, recent history and the daily archive."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .astro import moon_phase, sun_times
from .const import BASE_PATH, iso_seconds
from .daily import DailyArchive
from .stats import Sources, async_fetch, current_state, to_columns
from .tides import TideTable

_LOGGER = logging.getLogger(__name__)

# Current conditions and history: cheap to serve, worth keeping fresh.
LIVE_CACHE_CONTROL = (
    "public, max-age=60, stale-while-revalidate=300, stale-if-error=86400"
)
# The archive grows once a day, shortly after midnight.
DAILY_CACHE_CONTROL = (
    "public, max-age=3600, stale-while-revalidate=86400, stale-if-error=604800"
)
# A finished year never changes again.
FINAL_CACHE_CONTROL = "public, max-age=604800"

HISTORY_SPAN = timedelta(hours=24)
HISTORY_INTERVAL = timedelta(minutes=5)  # the recorder's short-term statistics
# New 5-minute statistics appear every 5 minutes; no point querying more often.
HISTORY_TTL = timedelta(minutes=5)


class CurrentView(HomeAssistantView):
    """Current values, plus today's sun, moon and tides. Never touches the DB."""

    requires_auth = False
    url = f"{BASE_PATH}/data.json"
    name = "api:wsjson:data"

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entities: Sources,
        tides: TideTable | None,
    ) -> None:
        self.hass = hass
        self._entities = entities
        self._tides = tides
        # Sun times only change with the local date.
        self._sun_cache: tuple[date, dict[str, Any]] | None = None

    async def get(self, request: web.Request) -> web.Response:
        now = dt_util.utcnow()
        current, last_updated = self._current_values()

        payload = {
            "generated_at": iso_seconds(now),
            "last_updated": iso_seconds(last_updated) if last_updated else None,
            "current": current,
            "sun": self._sun_today(now),
            "moon": moon_phase(now),
            "tide": self._tides.today(now) if self._tides else None,
        }
        return _json(payload, LIVE_CACHE_CONTROL)

    def _current_values(self) -> tuple[dict[str, Any], datetime | None]:
        """Current values, plus when the station last reported any of them.

        All values arrive together from the station, so a single timestamp
        covers them. last_reported advances even when a value is unchanged,
        which last_updated does not.
        """
        out: dict[str, Any] = {}
        latest: datetime | None = None

        for key, sources in self._entities.items():
            state = current_state(self.hass, sources)
            if state is None:
                out[key] = {"value": None, "unit": None}
                continue

            out[key] = {
                "value": _as_number(state.state),
                "unit": state.attributes.get("unit_of_measurement"),
            }
            if latest is None or state.last_reported > latest:
                latest = state.last_reported

        return out, latest

    def _sun_today(self, now: datetime) -> dict[str, Any]:
        today = dt_util.as_local(now).date()
        if self._sun_cache is None or self._sun_cache[0] != today:
            self._sun_cache = (today, sun_times(self.hass, today))
        return self._sun_cache[1]


class HistoryView(HomeAssistantView):
    """The last 24 hours of 5-minute statistics, cached behind a TTL and a lock."""

    requires_auth = False
    url = f"{BASE_PATH}/history.json"
    name = "api:wsjson:history"

    def __init__(self, hass: HomeAssistant, *, entities: Sources) -> None:
        self.hass = hass
        self._entities = entities

        self._cache: dict[str, Any] | None = None
        self._cached_at: datetime | None = None
        self._lock = asyncio.Lock()

    async def get(self, request: web.Request) -> web.Response:
        payload = await self._cached()
        if payload is None:
            return _json(None, "no-store", status=503)
        return _json(payload, LIVE_CACHE_CONTROL)

    async def _cached(self) -> dict[str, Any] | None:
        if self._is_fresh():
            return self._cache

        async with self._lock:
            # A concurrent request may have refreshed while we waited.
            if self._is_fresh():
                return self._cache

            try:
                fresh = await self._fetch(dt_util.utcnow())
            except Exception:  # noqa: BLE001 - serve the last good copy
                _LOGGER.exception("wsjson: statistics query failed")
                return self._cache

            self._cache = fresh
            self._cached_at = dt_util.utcnow()
            return fresh

    def _is_fresh(self) -> bool:
        return (
            self._cache is not None
            and self._cached_at is not None
            and dt_util.utcnow() - self._cached_at < HISTORY_TTL
        )

    async def _fetch(self, now: datetime) -> dict[str, Any]:
        """The 5-minute statistics, as columns on a fixed 5-minute grid."""
        step = HISTORY_INTERVAL.total_seconds()
        start = _ceil(now - HISTORY_SPAN, step)
        end = _floor(now, step)  # buckets after this one are still open
        slots = [
            start + i * HISTORY_INTERVAL
            for i in range(int((end - start) / HISTORY_INTERVAL))
        ]

        buckets = {}
        if self._entities and slots:
            buckets = await async_fetch(self.hass, self._entities, start, end, "5minute")

        # The newest bucket may not be compiled yet; drop it rather than
        # publish a trailing gap.
        while slots and slots[-1] not in buckets:
            slots.pop()

        columns = to_columns(buckets.get(t) for t in slots)
        return {
            "start": iso_seconds(start),
            "interval": "PT5M",
            "units": {key: self._unit(key) for key in columns},
            **columns,
        }

    def _unit(self, key: str) -> str | None:
        state = current_state(self.hass, self._entities[key])
        return state.attributes.get("unit_of_measurement") if state else None


class DailyIndexView(HomeAssistantView):
    """Which days and years the daily archive covers."""

    requires_auth = False
    url = f"{BASE_PATH}/daily.json"
    name = "api:wsjson:daily"

    def __init__(self, archive: DailyArchive) -> None:
        self._archive = archive

    async def get(self, request: web.Request) -> web.Response:
        return _json(self._archive.index(), DAILY_CACHE_CONTROL)


class DailyYearView(HomeAssistantView):
    """One year of daily summaries."""

    requires_auth = False
    url = BASE_PATH + r"/daily/{year:\d{4}}.json"
    name = "api:wsjson:daily:year"

    def __init__(self, archive: DailyArchive) -> None:
        self._archive = archive

    async def get(self, request: web.Request, year: str) -> web.Response:
        payload = self._archive.year(int(year))
        if payload is None:
            return _json(None, DAILY_CACHE_CONTROL, status=404)

        final = self._archive.is_final(int(year))
        return _json(payload, FINAL_CACHE_CONTROL if final else DAILY_CACHE_CONTROL)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _as_number(raw: str) -> float | str:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw


def _json(payload: Any, cache_control: str, *, status: int = 200) -> web.Response:
    return web.json_response(
        payload,
        status=status,
        headers={
            "Cache-Control": cache_control,
            "Access-Control-Allow-Origin": "*",
        },
    )


def _floor(value: datetime, step: float) -> datetime:
    ts = value.timestamp()
    return dt_util.utc_from_timestamp(ts - ts % step)


def _ceil(value: datetime, step: float) -> datetime:
    floor = _floor(value, step)
    return floor if floor == value else floor + timedelta(seconds=step)
