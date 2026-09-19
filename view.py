"""Public HTTP view serving the weather station snapshot."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import UNAVAILABLE_STATES

_LOGGER = logging.getLogger(__name__)

CACHE_CONTROL = "public, max-age=60, stale-while-revalidate=300, stale-if-error=86400"


class WeatherStationView(HomeAssistantView):
    """Unauthenticated, read-only, cached snapshot endpoint."""

    requires_auth = False

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        url_path: str,
        entities: dict[str, str],
        history_keys: list[str],
        history_hours: int,
        history_ttl: int,
    ) -> None:
        self.hass = hass
        self.url = url_path
        self.name = "api:wsjson:data"

        self._entities = entities
        # Only these keys hit the database; everything else is instant-only.
        self._history_entities = {k: entities[k] for k in history_keys}
        self._history_hours = history_hours
        self._history_ttl = timedelta(seconds=history_ttl)

        self._history_cache: dict[str, list[dict[str, Any]]] | None = None
        self._history_cached_at: datetime | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    async def get(self, request: web.Request) -> web.Response:
        now = dt_util.utcnow()

        payload = {
            "generated_at": now.isoformat(),
            "history_from": (now - timedelta(hours=self._history_hours)).isoformat(),
            "current": self._current_values(),
            "history": await self._cached_history(now),
        }

        return web.json_response(
            payload,
            headers={
                "Cache-Control": CACHE_CONTROL,
                "Access-Control-Allow-Origin": "*",
            },
        )

    # ------------------------------------------------------------------
    # Current values: read straight from the in-memory state machine.
    # Free, always live, never cached.
    # ------------------------------------------------------------------

    def _current_values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}

        for key, entity_id in self._entities.items():
            state = self.hass.states.get(entity_id)

            if state is None or state.state.lower() in UNAVAILABLE_STATES:
                out[key] = {"value": None, "unit": None, "last_updated": None}
                continue

            out[key] = {
                "value": _as_number(state.state),
                "unit": state.attributes.get("unit_of_measurement"),
                "last_updated": state.last_updated.isoformat(),
            }

        return out

    # ------------------------------------------------------------------
    # History: expensive, so cached behind a TTL and a lock.
    # ------------------------------------------------------------------

    async def _cached_history(self, now: datetime) -> dict[str, list[dict[str, Any]]]:
        if not self._history_entities:
            return {}

        if self._is_fresh(now):
            return self._history_cache  # type: ignore[return-value]

        async with self._lock:
            # A concurrent request may have refreshed while we waited.
            if self._is_fresh(dt_util.utcnow()):
                return self._history_cache  # type: ignore[return-value]

            try:
                fresh = await self._fetch_history(now)
            except Exception:  # noqa: BLE001 - never 500 a public page
                _LOGGER.exception("wsjson: statistics query failed")
                return self._history_cache or {}

            self._history_cache = fresh
            self._history_cached_at = dt_util.utcnow()
            return fresh

    def _is_fresh(self, now: datetime) -> bool:
        return (
            self._history_cache is not None
            and self._history_cached_at is not None
            and now - self._history_cached_at < self._history_ttl
        )

    async def _fetch_history(self, now: datetime) -> dict[str, list[dict[str, Any]]]:
        """Query the 5-minute statistics table, off the event loop."""
        start = now - timedelta(hours=self._history_hours)
        statistic_ids = set(self._history_entities.values())

        raw = await get_instance(self.hass).async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                start,
                now,
                statistic_ids,
                "5minute",
                None,  # units: keep whatever the sensor reports
                {"mean", "min", "max"},
            )
        )

        out: dict[str, list[dict[str, Any]]] = {}
        for key, entity_id in self._history_entities.items():
            out[key] = [
                {
                    "t": _row_time(row["start"]),
                    "mean": _round(row.get("mean")),
                    "min": _round(row.get("min")),
                    "max": _round(row.get("max")),
                }
                for row in raw.get(entity_id, [])
            ]

        return out


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _as_number(raw: str) -> float | str:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def _row_time(value: Any) -> str:
    """Statistics rows carry epoch floats on recent HA, datetimes on older ones."""
    if isinstance(value, (int, float)):
        return dt_util.utc_from_timestamp(value).isoformat()
    return value.isoformat()
