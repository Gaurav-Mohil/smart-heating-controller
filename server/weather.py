"""Live weather from Open-Meteo (no API key needed).

Results are cached in the store so the site keeps working — showing the last
known forecast, marked as stale — if Open-Meteo is briefly unreachable.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
REFRESH_SECONDS = 15 * 60


def fetch_hourly(lat: float, lon: float, tz: str) -> list:
    resp = requests.get(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,apparent_temperature",
            "forecast_days": 2,
            "timezone": tz,
        },
        timeout=10,
    )
    resp.raise_for_status()
    hourly = resp.json()["hourly"]
    return [
        {"time": t, "temp": temp, "feels": feels}
        for t, temp, feels in zip(hourly["time"], hourly["temperature_2m"], hourly["apparent_temperature"])
        if temp is not None
    ]


def fetch_daily_means(lat: float, lon: float, tz: str, start: date, end: date) -> dict:
    """Daily mean temperatures for a past date range: {"YYYY-MM-DD": °C}."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_mean",
        "timezone": tz,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }
    out = {}
    # The forecast API covers roughly the last three months; the archive covers older dates.
    for url in (FORECAST_URL, ARCHIVE_URL):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            daily = resp.json()["daily"]
        except (requests.RequestException, KeyError, ValueError):
            continue
        for d, t in zip(daily["time"], daily["temperature_2m_mean"]):
            if t is not None:
                out.setdefault(d, t)
    return out


def localize(hours: list, tz: str) -> list:
    """Open-Meteo returns local times without an offset; make them explicit ISO times."""
    zone = ZoneInfo(tz)
    out = []
    for h in hours:
        t = datetime.fromisoformat(h["time"])
        if t.tzinfo is None:
            t = t.replace(tzinfo=zone)
        out.append({**h, "time": t.isoformat()})
    return out


class Weather:
    def __init__(self, store, fetcher=fetch_hourly, daily_fetcher=fetch_daily_means):
        self.store = store
        self.fetcher = fetcher
        self.daily_fetcher = daily_fetcher

    def hourly(self, force: bool = False) -> dict:
        """{"hours": [...], "fetched_at": epoch, "stale": bool, "error": str|None}"""
        cache = self.store.get("forecast_cache")
        fresh = cache and time.time() - cache["fetched_at"] < REFRESH_SECONDS
        if fresh and not force:
            return {**cache, "stale": False, "error": None}
        loc = self.store.get("settings")["location"]
        try:
            hours = localize(self.fetcher(loc["lat"], loc["lon"], loc["tz"]), loc["tz"])
            cache = {"hours": hours, "fetched_at": time.time()}
            self.store.set("forecast_cache", cache)
            return {**cache, "stale": False, "error": None}
        except Exception as exc:  # network down, API error, bad JSON...
            if cache:
                return {**cache, "stale": True, "error": str(exc)[:200]}
            return {"hours": [], "fetched_at": None, "stale": True, "error": str(exc)[:200]}

    def daily_means(self, start: date, end: date) -> dict:
        cached = self.store.get("weather_daily") or {}
        wanted = {date.fromordinal(o).isoformat() for o in range(start.toordinal(), end.toordinal() + 1)}
        # Only ask for completed days.
        wanted = {d for d in wanted if d < date.today().isoformat()}
        missing = wanted - set(cached)
        if missing:
            loc = self.store.get("settings")["location"]
            try:
                got = self.daily_fetcher(loc["lat"], loc["lon"], loc["tz"],
                                         date.fromisoformat(min(missing)), date.fromisoformat(max(missing)))
            except Exception:
                got = {}
            if got:
                cached.update(got)
                self.store.set("weather_daily", cached)
        return {d: cached[d] for d in wanted if d in cached}
