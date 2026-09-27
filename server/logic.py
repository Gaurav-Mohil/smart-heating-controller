"""Heating logic: schedules, the current target, servo angles and forecast analysis.

All functions here are pure (no I/O) so they are easy to test.
Times are timezone-aware datetimes in the home's local timezone.
"""
from __future__ import annotations

from datetime import datetime, timedelta

MODES = ("auto", "manual", "away")


def parse_hm(value: str) -> int:
    """'06:30' -> minutes after midnight."""
    hours, minutes = value.split(":")
    h, m = int(hours), int(minutes)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid time {value!r}")
    return h * 60 + m


def fmt_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def at_minute(day: datetime, minute: int) -> datetime:
    return day.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)


# ---- temperatures & servo angles -------------------------------------------------

def safe_range(calibration: dict):
    temps = [p["temp"] for p in calibration["points"]]
    return min(temps), max(temps)


def clamp_temp(temp: float, calibration: dict) -> float:
    lo, hi = safe_range(calibration)
    return max(lo, min(hi, temp))


def temp_to_angle(temp: float, calibration: dict) -> int:
    """Linear interpolation between calibration points, clamped to the safe angle limits."""
    points = sorted(calibration["points"], key=lambda p: p["temp"])
    if temp <= points[0]["temp"]:
        angle = points[0]["angle"]
    elif temp >= points[-1]["temp"]:
        angle = points[-1]["angle"]
    else:
        angle = points[0]["angle"]
        for a, b in zip(points, points[1:]):
            if a["temp"] <= temp <= b["temp"]:
                span = b["temp"] - a["temp"]
                frac = (temp - a["temp"]) / span if span else 0
                angle = a["angle"] + frac * (b["angle"] - a["angle"])
                break
    return int(round(max(calibration["min_angle"], min(calibration["max_angle"], angle))))


def is_calibrated(calibration: dict) -> bool:
    return all(p.get("measured") for p in calibration["points"])


# ---- schedule ------------------------------------------------------------------------

def scheduled_period_at(schedule: list, when: datetime):
    """The schedule period in effect at `when` (looks back up to a week)."""
    minute_now = when.hour * 60 + when.minute
    for back in range(8):
        day = when - timedelta(days=back)
        todays = [p for p in schedule if day.weekday() in p["days"]]
        if back == 0:
            todays = [p for p in todays if parse_hm(p["start"]) <= minute_now]
        if todays:
            return max(todays, key=lambda p: parse_hm(p["start"]))
    return None


def schedule_changes(schedule: list, start: datetime, end: datetime):
    """Every scheduled change in (start, end], in order: [(datetime, period)]."""
    out = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        for p in schedule:
            if day.weekday() in p["days"]:
                t = at_minute(day, parse_hm(p["start"]))
                if start < t <= end:
                    out.append((t, p))
        day = day + timedelta(days=1)
    out.sort(key=lambda item: item[0])
    return out


def next_schedule_change(schedule: list, now: datetime):
    changes = schedule_changes(schedule, now, now + timedelta(days=8))
    return changes[0][0] if changes else None


def active_event(events: list, when: datetime):
    for ev in events:
        if datetime.fromisoformat(ev["start"]) <= when < datetime.fromisoformat(ev["end"]):
            return ev
    return None


def effective_target(control: dict, schedule: list, events: list, calibration: dict, now: datetime,
                     default: float = 20):
    """What the heating should aim for right now, and why."""
    mode = control.get("mode", "auto")
    if mode == "manual":
        return {"temp": clamp_temp(control["manual_temp"], calibration), "reason": "Manual", "until": None}
    if mode == "away":
        return {"temp": clamp_temp(control["away_temp"], calibration), "reason": "Away", "until": None}

    hold = control.get("hold")
    if hold and datetime.fromisoformat(hold["until"]) > now:
        return {"temp": clamp_temp(hold["temp"], calibration), "reason": "Hold", "until": hold["until"]}

    period = scheduled_period_at(schedule, now)
    ev = active_event(events, now)
    # A preheat only ever raises the temperature; a warmer scheduled period wins.
    if ev and (not period or ev["temp"] > period["temp"]):
        return {"temp": clamp_temp(ev["temp"], calibration), "reason": "Preheat", "until": ev["end"]}

    if period:
        nxt = next_schedule_change(schedule, now)
        return {
            "temp": clamp_temp(period["temp"], calibration),
            "reason": period["name"],
            "until": nxt.isoformat() if nxt else None,
        }
    return {"temp": clamp_temp(default, calibration), "reason": "Default", "until": None}


def target_at(schedule: list, events: list, calibration: dict, when: datetime, default: float = 20):
    """Planned target at a future time in auto mode (ignores holds)."""
    period = scheduled_period_at(schedule, when)
    temp = period["temp"] if period else default
    ev = active_event(events, when)
    if ev:
        temp = max(temp, ev["temp"])
    return clamp_temp(temp, calibration)


def upcoming(schedule: list, events: list, now: datetime, hours: int = 24, limit: int = 5):
    items = [
        {"at": t.isoformat(), "name": p["name"], "temp": p["temp"], "suggested": False}
        for t, p in schedule_changes(schedule, now, now + timedelta(hours=hours))
    ]
    for ev in events:
        start = datetime.fromisoformat(ev["start"])
        if now < start <= now + timedelta(hours=hours):
            items.append({"at": ev["start"], "name": "Preheat", "temp": ev["temp"], "suggested": True})
    items.sort(key=lambda i: i["at"])
    return items[:limit]


def prune_events(events: list, now: datetime) -> list:
    return [ev for ev in events if datetime.fromisoformat(ev["end"]) > now]


def validate_schedule(periods) -> list:
    if not isinstance(periods, list) or not periods:
        raise ValueError("The schedule needs at least one period.")
    if len(periods) > 24:
        raise ValueError("Too many periods (24 at most).")
    clean = []
    seen = set()
    for i, p in enumerate(periods):
        name = str(p.get("name", "")).strip()[:40] or f"Period {i + 1}"
        start = fmt_hm(parse_hm(str(p.get("start", ""))))
        temp = float(p.get("temp"))
        days = sorted({int(d) for d in p.get("days", []) if 0 <= int(d) <= 6})
        if not days:
            raise ValueError(f"“{name}” has no days selected.")
        pid = str(p.get("id") or f"p{i + 1}")[:20]
        if pid in seen:
            pid = f"{pid}-{i}"
        seen.add(pid)
        clean.append({"id": pid, "name": name, "start": start, "temp": round(temp * 2) / 2, "days": days})
    clean.sort(key=lambda p: parse_hm(p["start"]))
    return clean


# ---- forecast analysis ---------------------------------------------------------------

def analyze_forecast(hourly: list, now: datetime, threshold: float, window: int):
    """Compare the temperature now with `window` hours ahead.

    `hourly` is a list of {"time": iso local, "temp": float, "feels": float}.
    Returns None when there is not enough data.
    """
    floor = now.replace(minute=0, second=0, microsecond=0)
    future = [h for h in hourly if datetime.fromisoformat(h["time"]) >= floor]
    if len(future) < 2:
        return None
    window = max(1, min(window, len(future) - 1))
    current, ahead = future[0], future[window]
    change = round(ahead["temp"] - current["temp"], 1)
    coldest = min(future[1:window + 1], key=lambda h: h["temp"])
    return {
        "current": current,
        "ahead": ahead,
        "window": window,
        "change": change,
        "decision": "PREPARE" if change <= -threshold else "NORMAL",
        "coldest": coldest,
    }


def build_suggestion(analysis, rules: dict, schedule: list, events: list, calibration: dict,
                     now: datetime):
    """Suggest a preheat before a forecast temperature drop, or None."""
    if not analysis or not rules.get("preheat_suggestions") or analysis["decision"] != "PREPARE":
        return None
    coldest_at = datetime.fromisoformat(analysis["coldest"]["time"])
    start = coldest_at - timedelta(minutes=15)
    earliest = now + timedelta(minutes=15 - now.minute % 15)
    start = max(start, earliest.replace(second=0, microsecond=0))
    end = start + timedelta(hours=3)
    base = target_at(schedule, [], calibration, start)
    _, hi = safe_range(calibration)
    temp = min(hi, base + float(rules.get("preheat_boost", 1)))
    if temp <= base:
        return None
    return {
        "id": "preheat-" + coldest_at.strftime("%Y%m%d%H"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "temp": temp,
        "drop": -analysis["change"],
        "hours": analysis["window"],
    }
