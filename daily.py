"""Daily summaries, kept on disk indefinitely.

Built from Home Assistant's hourly statistics (never purged by the recorder)
and then kept in our own store, keyed by our JSON keys. That way the archive
survives a new station, renamed entities or a wiped recorder database.

It only ever appends: on first start it backfills as far back as the
recorder goes, then adds each finished day shortly after local midnight.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .stats import Bucket, async_fetch, to_columns

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.daily"

# Hourly statistics for the last hour of the day are compiled just after
# midnight; this leaves them plenty of margin.
DAILY_RUN = {"hour": 0, "minute": 30, "second": 0}
# After a restart the recorder first compiles any periods it missed.
STARTUP_DELAY = 300  # seconds


class DailyArchive:
    """Append-only archive of per-day min/mean/max (or change) per key."""

    def __init__(self, hass: HomeAssistant, *, entities: dict[str, str]) -> None:
        self.hass = hass
        self._entities = entities
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)

        # "YYYY-MM-DD" (local date) -> that day's bucket. Days without data
        # are simply absent.
        self._days: dict[str, Bucket] = {}
        # Last day processed, with or without data. Nothing before it is
        # ever fetched again.
        self._last: date | None = None
        self._units: dict[str, str | None] = {}
        self._lock = asyncio.Lock()

    async def async_start(self) -> None:
        if stored := await self._store.async_load():
            self._days = stored["days"]
            self._last = date.fromisoformat(stored["last"]) if stored["last"] else None
            self._units = stored["units"]

        if not self._entities:
            return

        @callback
        def _schedule_catch_up(_hass: HomeAssistant) -> None:
            async_call_later(self.hass, STARTUP_DELAY, self._async_update)

        async_at_started(self.hass, _schedule_catch_up)
        async_track_time_change(self.hass, self._async_update, **DAILY_RUN)

    # ------------------------------------------------------------------
    # Request path: in-memory only.
    # ------------------------------------------------------------------

    def index(self) -> dict[str, Any]:
        first = min(self._days) if self._days else None
        return {
            "first": first,
            "last": self._last.isoformat() if self._last and first else None,
            "years": sorted({int(d[:4]) for d in self._days}),
        }

    def year(self, year: int) -> dict[str, Any] | None:
        """One year of daily columns, or None if there is nothing for it."""
        days = sorted(d for d in self._days if d.startswith(f"{year:04d}-"))
        if not days or self._last is None:
            return None

        start = date.fromisoformat(days[0])
        end = min(date(year, 12, 31), self._last)
        span = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        columns = to_columns(self._days.get(d.isoformat()) for d in span)

        return {
            "start": start.isoformat(),
            "interval": "P1D",
            "units": {key: self._units.get(key) for key in columns},
            **columns,
        }

    def is_final(self, year: int) -> bool:
        """Whether a year's data can no longer change."""
        return self._last is not None and self._last >= date(year, 12, 31)

    # ------------------------------------------------------------------
    # Background update.
    # ------------------------------------------------------------------

    async def _async_update(self, _now: datetime | None = None) -> None:
        if self._lock.locked():
            return

        async with self._lock:
            today = dt_util.now().date()
            yesterday = today - timedelta(days=1)
            if self._last is not None and self._last >= yesterday:
                return

            try:
                if self._last is None:
                    fetched = await self._fetch_everything(today)
                else:
                    fetched = await self._fetch(self._last + timedelta(days=1), today)
            except Exception:  # noqa: BLE001 - retried on the next run
                _LOGGER.exception("wsjson: daily statistics query failed")
                return

            self._days.update(fetched)
            self._last = yesterday
            self._update_units()
            await self._store.async_save(
                {
                    "last": self._last.isoformat(),
                    "units": self._units,
                    "days": self._days,
                }
            )
            _LOGGER.debug("wsjson: archived %d day(s) up to %s", len(fetched), yesterday)

    async def _fetch_everything(self, today: date) -> dict[str, Bucket]:
        """Backfill a year at a time, newest first, until a year comes back empty."""
        out: dict[str, Bucket] = {}
        end = today
        while True:
            # January 1st of the year the chunk's last day falls in.
            start = date((end - timedelta(days=1)).year, 1, 1)
            chunk = await self._fetch(start, end)
            if not chunk:
                return out
            out.update(chunk)
            end = start

    async def _fetch(self, start: date, end: date) -> dict[str, Bucket]:
        """Whole local days from start (inclusive) to end (exclusive)."""
        buckets = await async_fetch(
            self.hass,
            self._entities,
            dt_util.start_of_local_day(start),
            dt_util.start_of_local_day(end),
            "day",
        )
        return {
            dt_util.as_local(t).date().isoformat(): bucket
            for t, bucket in buckets.items()
        }

    def _update_units(self) -> None:
        """Latest unit per key; kept as-is while a sensor is unavailable."""
        for key, entity_id in self._entities.items():
            if (state := self.hass.states.get(entity_id)) is not None:
                if unit := state.attributes.get("unit_of_measurement"):
                    self._units[key] = unit
