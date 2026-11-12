"""Daily summaries, kept on disk indefinitely.

Built from Home Assistant's hourly statistics (never purged by the recorder)
and then kept in our own store, keyed by our JSON keys. That way the archive
survives a new station, renamed entities or a wiped recorder database.

It only ever appends: on first start it backfills as far back as the
recorder goes, then adds each finished day shortly after local midnight.

Wind direction gets, besides its daily circular mean, a wind rose per day:
how many of its hours the wind blew from each of 16 sectors, and how many
were calm. Adding up days gives a rose for any month, season or year.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_change
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter

from .const import DOMAIN
from .stats import Bucket, Sources, async_fetch, current_state, to_columns

_LOGGER = logging.getLogger(__name__)

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.daily"
# Archives stored before this lack the wind rose; it is backfilled once.
SCHEMA_WIND = 2

# 16 sectors of 22.5°, the first centred on north, then clockwise.
SECTORS = 16
# Beaufort 0: below this the direction means nothing.
CALM_KMH = 1.0

# Hourly statistics for the last hour of the day are compiled just after
# midnight; this leaves them plenty of margin.
DAILY_RUN = {"hour": 0, "minute": 30, "second": 0}
# After a restart the recorder first compiles any periods it missed.
STARTUP_DELAY = 300  # seconds


class DailyArchive:
    """Append-only archive of per-day min/mean/max (or change) per key."""

    def __init__(self, hass: HomeAssistant, *, entities: Sources) -> None:
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
        # Whether the days already archived have their wind rose.
        self._has_wind = True
        self._lock = asyncio.Lock()

    async def async_start(self) -> None:
        if stored := await self._store.async_load():
            self._days = stored["days"]
            self._last = date.fromisoformat(stored["last"]) if stored["last"] else None
            self._units = stored["units"]
            self._has_wind = stored.get("schema", 1) >= SCHEMA_WIND

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
            behind = self._last is None or self._last < yesterday
            if not behind and self._has_wind:
                return

            try:
                if not self._has_wind and self._last is not None and self._days:
                    await self._backfill_wind()
                self._has_wind = True

                if behind:
                    if self._last is None:
                        fetched = await self._fetch_everything(today)
                    else:
                        fetched = await self._fetch(self._last + timedelta(days=1), today)
                    self._days.update(fetched)
                    self._last = yesterday
                    _LOGGER.debug(
                        "wsjson: archived %d day(s) up to %s", len(fetched), yesterday
                    )
            except Exception:  # noqa: BLE001 - retried on the next run
                _LOGGER.exception("wsjson: daily statistics query failed")
                return

            self._update_units()
            await self._store.async_save(
                {
                    "schema": SCHEMA_WIND,
                    "last": self._last.isoformat() if self._last else None,
                    "units": self._units,
                    "days": self._days,
                }
            )

    async def _backfill_wind(self) -> None:
        """Add wind direction to days archived before it was included."""
        if "wind_bearing" not in self._entities:
            return

        first = date.fromisoformat(min(self._days))
        end = self._last + timedelta(days=1)
        bearing = {"wind_bearing": self._entities["wind_bearing"]}
        for year in range(first.year, end.year + 1):
            start = max(first, date(year, 1, 1))
            chunk_end = min(end, date(year + 1, 1, 1))
            if start >= chunk_end:
                continue
            for day, bucket in (await self._fetch(start, chunk_end, bearing)).items():
                self._days.setdefault(day, {}).update(bucket)

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

    async def _fetch(
        self, start: date, end: date, entities: Sources | None = None
    ) -> dict[str, Bucket]:
        """Whole local days from start (inclusive) to end (exclusive)."""
        entities = self._entities if entities is None else entities
        buckets = await async_fetch(
            self.hass,
            entities,
            dt_util.start_of_local_day(start),
            dt_util.start_of_local_day(end),
            "day",
        )
        days = {
            dt_util.as_local(t).date().isoformat(): bucket
            for t, bucket in buckets.items()
        }

        if "wind_bearing" in entities:
            for day, rose in (await self._fetch_wind_rose(start, end)).items():
                days.setdefault(day, {}).setdefault("wind_bearing", {}).update(rose)
        return days

    async def _fetch_wind_rose(self, start: date, end: date) -> dict[str, dict[str, Any]]:
        """Per local day, hours of wind from each sector, plus calm hours.

        Each hour counts once, by its mean direction and mean speed.
        """
        keys = ("wind_bearing", "wind_speed")
        if not all(k in self._entities for k in keys):
            return {}

        hours = await async_fetch(
            self.hass,
            {k: self._entities[k] for k in keys},
            dt_util.start_of_local_day(start),
            dt_util.start_of_local_day(end),
            "hour",
        )
        to_kmh = self._speed_to_kmh()

        out: dict[str, dict[str, Any]] = {}
        for t, bucket in hours.items():
            bearing = bucket.get("wind_bearing", {}).get("mean")
            speed = bucket.get("wind_speed", {}).get("mean")
            if bearing is None or speed is None:
                continue

            day = out.setdefault(
                dt_util.as_local(t).date().isoformat(),
                {"sectors": [0] * SECTORS, "calm": 0},
            )
            if to_kmh(speed) < CALM_KMH:
                day["calm"] += 1
            else:
                day["sectors"][round(bearing / (360 / SECTORS)) % SECTORS] += 1
        return out

    def _speed_to_kmh(self) -> Callable[[float], float]:
        """Converter from the wind speed sensor's unit; km/h if unknown."""
        state = current_state(self.hass, self._entities["wind_speed"])
        unit = state.attributes.get("unit_of_measurement") if state else None
        try:
            return SpeedConverter.converter_factory(unit, "km/h")
        except HomeAssistantError:
            return lambda value: value

    def _update_units(self) -> None:
        """Latest unit per key; kept as-is while its sensors are unavailable."""
        for key, sources in self._entities.items():
            if (state := current_state(self.hass, sources)) is not None:
                if unit := state.attributes.get("unit_of_measurement"):
                    self._units[key] = unit
