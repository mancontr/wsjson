"""Tide predictions from the IHM (Instituto Hidrográfico de la Marina) API.

IHM publishes predictions a year or more ahead, a month per request, so whole
months are fetched once and kept on disk. In practice that is about one
request a month; requests to our view never touch the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

IHM_URL = "https://ideihm.covam.es/api-ihm/getmarea"
IHM_TYPES = {"pleamar": "high", "bajamar": "low"}

STORE_VERSION = 1
STORE_KEY = f"{DOMAIN}.tides"

# How far ahead the cache must reach before the next month is fetched.
PREFETCH_DAYS = 14
# Coverage is checked this often; it is a no-op unless a month is missing,
# and doubles as the retry interval when IHM is down.
CHECK_INTERVAL = timedelta(hours=1)
FETCH_TIMEOUT = 30  # seconds

# Rough tide coefficient bounds, and where spring ("vivas") tides start.
COEFFICIENT_MIN = 20
COEFFICIENT_MAX = 120
SPRING_THRESHOLD = 70

# Without a configured spring range, it is estimated as the cached months'
# mean range times this: roughly 1 + S2/M2, typical of semidiurnal Atlantic
# coasts. Any whole month spans two spring-neap cycles, so its mean range is
# stable to within a few percent.
SPRING_TO_MEAN_RANGE = 1.35


@dataclass(frozen=True, slots=True)
class TideEvent:
    t: datetime
    type: str
    height: float

    def as_dict(self) -> dict[str, Any]:
        return {"t": self.t.isoformat(), "type": self.type, "height": self.height}


class TideTable:
    """Month-by-month cache of IHM predictions for a single station."""

    def __init__(
        self, hass: HomeAssistant, *, station_id: str, spring_range: float | None
    ) -> None:
        self.hass = hass
        self._station_id = station_id
        self._spring_range = spring_range
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)

        # "YYYY-MM" (UTC month, as IHM files them) -> that month's events.
        self._months: dict[str, list[dict[str, Any]]] = {}
        self._events: list[TideEvent] = []
        self._estimated_spring_range: float | None = None
        self._lock = asyncio.Lock()

    async def async_start(self) -> None:
        """Load the disk cache, then keep it topped up in the background."""
        stored = await self._store.async_load()
        # A different station in the config invalidates everything stored.
        if stored and stored.get("station_id") == self._station_id:
            self._set_months(stored["months"])

        self.hass.async_create_background_task(
            self._async_refresh(), "wsjson tides refresh"
        )
        async_track_time_interval(self.hass, self._async_refresh, CHECK_INTERVAL)

    # ------------------------------------------------------------------
    # Request path: in-memory only.
    # ------------------------------------------------------------------

    def today(self, now: datetime) -> dict[str, Any]:
        start = dt_util.start_of_local_day(dt_util.as_local(now).date())
        end = dt_util.start_of_local_day(start.date() + timedelta(days=1))

        events = [e for e in self._events if start <= e.t < end]

        # A high/low pair counts for the day its midpoint falls in, so a tide
        # cut off at midnight still contributes its range to one of the days.
        ranges = [
            abs(b.height - a.height)
            for a, b in pairwise(self._events)
            if start <= a.t + (b.t - a.t) / 2 < end
        ]
        spring_range = self._spring_range or self._estimated_spring_range
        coefficient = None
        if ranges and spring_range:
            average = sum(ranges) / len(ranges)
            coefficient = round(
                min(COEFFICIENT_MAX, max(COEFFICIENT_MIN, average / spring_range * 100))
            )

        return {
            "coefficient": coefficient,
            "kind": (
                None
                if coefficient is None
                else "spring" if coefficient >= SPRING_THRESHOLD else "neap"
            ),
            "events": [e.as_dict() for e in events],
        }

    # ------------------------------------------------------------------
    # Background refresh.
    # ------------------------------------------------------------------

    async def _async_refresh(self, _now: datetime | None = None) -> None:
        if self._lock.locked():
            return

        async with self._lock:
            today = dt_util.now().date()
            # The day before covers local days that start in the previous UTC
            # month (Spain is ahead of UTC).
            wanted = _months_between(today - timedelta(days=1), today + timedelta(days=PREFETCH_DAYS))
            missing = [m for m in wanted if m not in self._months]
            if not missing:
                return

            fetched: dict[str, list[dict[str, Any]]] = {}
            for month in missing:
                try:
                    fetched[month] = await self._fetch_month(month)
                except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError) as err:
                    _LOGGER.warning("wsjson: could not fetch tides for %s: %s", month, err)
                    break  # The rest wait for the next check.

            if not fetched:
                return

            months = {**self._months, **fetched}
            self._set_months({m: e for m, e in months.items() if m >= wanted[0]})
            await self._store.async_save(
                {"station_id": self._station_id, "months": self._months}
            )

    async def _fetch_month(self, month: str) -> list[dict[str, Any]]:
        session = async_get_clientsession(self.hass)
        params = {
            "request": "gettide",
            "format": "json",
            "id": self._station_id,
            "month": month.replace("-", ""),
        }
        async with asyncio.timeout(FETCH_TIMEOUT):
            async with session.get(IHM_URL, params=params) as resp:
                resp.raise_for_status()
                # Served as JSON but not always labelled as such.
                payload = json.loads(await resp.text())

        events = [_parse_row(row) for row in payload["mareas"]["datos"]["marea"]]
        if not events:
            raise ValueError("empty month")
        return events

    def _set_months(self, months: dict[str, list[dict[str, Any]]]) -> None:
        self._months = months
        self._events = sorted(
            (
                TideEvent(datetime.fromisoformat(e["t"]), e["type"], e["height"])
                for events in months.values()
                for e in events
            ),
            key=lambda e: e.t,
        )

        ranges = [abs(b.height - a.height) for a, b in pairwise(self._events)]
        self._estimated_spring_range = (
            sum(ranges) / len(ranges) * SPRING_TO_MEAN_RANGE if ranges else None
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _parse_row(row: dict[str, str]) -> dict[str, Any]:
    """IHM rows carry an unlabelled UTC date and time, and Spanish tide types."""
    t = datetime.strptime(f"{row['fecha']} {row['hora']}", "%Y-%m-%d %H:%M")
    return {
        "t": t.replace(tzinfo=timezone.utc).isoformat(),
        "type": IHM_TYPES[row["tipo"]],
        "height": round(float(row["altura"]), 2),
    }


def _months_between(start: date, end: date) -> list[str]:
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months
