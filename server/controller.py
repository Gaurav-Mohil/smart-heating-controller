"""The controller ties storage, weather and heating logic together.

Both the web app and the Arduino talk to this one object, so they always agree
on what the heating should be doing.
"""
from __future__ import annotations

import itertools
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import homeassistant, logic
from .weather import Weather

DEVICE_ONLINE_SECONDS = 60
_command_ids = itertools.count(int(time.time()) % 100000)


class Controller:
    def __init__(self, store, weather: Weather = None, clock=None):
        self.store = store
        self.weather = weather or Weather(store)
        self._clock = clock

    # ---- time ---------------------------------------------------------------------
    @property
    def tz(self):
        return ZoneInfo(self.store.get("settings")["location"]["tz"])

    def now(self) -> datetime:
        if self._clock:
            return self._clock().astimezone(self.tz)
        return datetime.now(self.tz)

    # ---- the plan -------------------------------------------------------------------
    def plan(self) -> dict:
        """Work out the current target, forecast decision and suggestion."""
        now = self.now()
        control = self.store.get("control")
        schedule = self.store.get("schedule")
        calibration = self.store.get("calibration")
        rules = self.store.get("rules")

        events = logic.prune_events(self.store.get("events"), now)
        forecast = self.weather.hourly()
        analysis = logic.analyze_forecast(forecast["hours"], now, rules["drop_threshold"], rules["window_hours"])

        suggestion = logic.build_suggestion(analysis, rules, schedule, events, calibration, now)
        if suggestion:
            decisions = self.store.get("suggestions")
            status = decisions.get(suggestion["id"])
            if status is None and rules.get("auto_apply"):
                status = self._accept(suggestion, events, decisions, auto=True)
            suggestion["status"] = status or "pending"
        self.store.set("events", events)

        target = logic.effective_target(control, schedule, events, calibration, now)
        target["temp"] = round(target["temp"] * 2) / 2
        target["angle"] = logic.temp_to_angle(target["temp"], calibration)
        decision = analysis["decision"] if analysis else "NORMAL"

        self._note_changes(target, decision, analysis)
        return {
            "now": now,
            "control": control,
            "schedule": schedule,
            "events": events,
            "calibration": calibration,
            "rules": rules,
            "forecast": forecast,
            "analysis": analysis,
            "suggestion": suggestion,
            "target": target,
            "decision": decision,
        }

    def _note_changes(self, target: dict, decision: str, analysis) -> None:
        runtime = self.store.get("runtime")
        changed = False
        if runtime.get("last_decision") != decision:
            if analysis:
                self.store.log("info", f"Forecast {analysis['change']:+.1f} °C in {analysis['window']} h → {decision}")
            runtime["last_decision"] = decision
            changed = True
        key = [target["temp"], target["angle"]]
        if runtime.get("last_target") != key:
            self.store.log("out", f"Target {target['temp']:g} °C ({target['reason']}) · servo {target['angle']}°")
            runtime["last_target"] = key
            changed = True
            self._push_homeassistant(target["temp"])
        if changed:
            self.store.set("runtime", runtime)

    def _push_homeassistant(self, temp: float) -> None:
        ha = self.store.get("homeassistant")
        if not (ha.get("enabled") and ha.get("url") and ha.get("token") and ha.get("entity_id")):
            return
        try:
            homeassistant.set_temperature(ha["url"], ha["token"], ha["entity_id"], temp)
            ha.update(last_pushed={"temp": temp, "at": time.time()}, last_error=None)
            self.store.log("out", f"Wi-Fi thermostat set to {temp:g} °C")
        except Exception as exc:
            ha["last_error"] = str(exc)[:200]
            self.store.log("error", "Could not reach Home Assistant")
        self.store.set("homeassistant", ha)

    # ---- user actions ---------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in logic.MODES:
            raise ValueError("Unknown mode.")
        control = self.store.get("control")
        control["mode"] = mode
        control["hold"] = None
        self.store.set("control", control)
        self.store.log("user", f"Mode changed to {mode.capitalize()}")

    def set_temperature(self, temp: float) -> None:
        """Set the target. In Auto mode it holds until the next scheduled change."""
        calibration = self.store.get("calibration")
        temp = round(logic.clamp_temp(float(temp), calibration) * 2) / 2
        control = self.store.get("control")
        mode = control["mode"]
        if mode == "manual":
            control["manual_temp"] = temp
        elif mode == "away":
            control["away_temp"] = temp
        else:
            now = self.now()
            until = logic.next_schedule_change(self.store.get("schedule"), now) or now + timedelta(hours=4)
            control["hold"] = {"temp": temp, "until": until.isoformat()}
        self.store.set("control", control)
        self.store.log("user", f"Target set to {temp:g} °C")

    def resume_schedule(self) -> None:
        control = self.store.get("control")
        control["hold"] = None
        control["mode"] = "auto"
        self.store.set("control", control)
        self.store.log("user", "Resumed schedule")

    def _accept(self, suggestion: dict, events: list, decisions: dict, auto: bool = False) -> str:
        events[:] = [ev for ev in events if ev["id"] != suggestion["id"]]
        events.append({k: suggestion[k] for k in ("id", "start", "end", "temp")})
        decisions[suggestion["id"]] = "accepted"
        self.store.set("events", events)
        self.store.set("suggestions", self._trim(decisions))
        start = datetime.fromisoformat(suggestion["start"]).strftime("%I:%M %p").lstrip("0")
        who = "Automatically scheduled" if auto else "Accepted"
        self.store.log("user", f"{who}: preheat to {suggestion['temp']:g} °C at {start}")
        return "accepted"

    def decide_suggestion(self, suggestion_id: str, action: str) -> None:
        plan = self.plan()
        suggestion = plan["suggestion"]
        decisions = self.store.get("suggestions")
        events = self.store.get("events")
        if action == "undo":
            decisions.pop(suggestion_id, None)
            self.store.set("events", [ev for ev in events if ev["id"] != suggestion_id])
            self.store.set("suggestions", decisions)
            self.store.log("user", "Suggestion reopened")
            return
        if not suggestion or suggestion["id"] != suggestion_id:
            raise LookupError("That suggestion is no longer current.")
        if action == "accept":
            self._accept(suggestion, events, decisions)
        elif action == "dismiss":
            decisions[suggestion_id] = "dismissed"
            self.store.set("suggestions", self._trim(decisions))
            self.store.log("user", "Preheat suggestion skipped")
        else:
            raise ValueError("Unknown action.")

    @staticmethod
    def _trim(decisions: dict) -> dict:
        keys = sorted(decisions)[-50:]
        return {k: decisions[k] for k in keys}

    # ---- servo & calibration --------------------------------------------------------
    def queue_servo_test(self, angle: int) -> dict:
        cal = self.store.get("calibration")
        angle = int(angle)
        if not cal["min_angle"] <= angle <= cal["max_angle"]:
            raise ValueError(f"Angle must be between the safe limits ({cal['min_angle']}°–{cal['max_angle']}°).")
        cmd = {"id": next(_command_ids), "type": "ANGLE", "angle": angle, "created": time.time(), "done": False}
        self.store.set("command", cmd)
        self.store.log("out", f"Servo test → {angle}° (queued)")
        return cmd

    def clear_command(self) -> None:
        self.store.set("command", None)

    # ---- the Arduino ----------------------------------------------------------------
    def device_sync(self, report: dict) -> dict:
        """Called by the controller every few seconds. Returns what it should do."""
        now_ts = time.time()
        device = self.store.get("device") or {}
        was_online = device.get("last_seen") and now_ts - device["last_seen"] < DEVICE_ONLINE_SECONDS
        if not was_online:
            self.store.log("in", "Controller connected")
        device["last_seen"] = now_ts
        for key in ("angle", "rssi", "fw", "indoor_temp", "via"):
            if report.get(key) is not None:
                device[key] = report[key]

        cmd = self.store.get("command")
        if cmd and report.get("ack") is not None and str(report["ack"]) == str(cmd["id"]):
            self.store.log("in", f"Controller: servo at {cmd['angle']}° (test done)")
            self.store.set("command", None)
            device["test_until"] = now_ts + 60  # hold the test angle for a minute
            cmd = None

        plan = self.plan()
        target = plan["target"]
        if report.get("angle") is not None and report["angle"] == target["angle"] \
                and device.get("confirmed_angle") != target["angle"]:
            device["confirmed_angle"] = target["angle"]
            self.store.log("in", f"Controller: dial at {target['angle']}° ({target['temp']:g} °C)")
        self.store.set("device", device)

        cal = plan["calibration"]
        now = plan["now"]
        schedule = plan["schedule"]
        lines = {
            "ok": 1,
            "target": f"{target['temp']:g}",
            "angle": target["angle"],
            "decision": plan["decision"],
            "mode": plan["control"]["mode"],
            "min": cal["min_angle"],
            "max": cal["max_angle"],
            "now": now.hour * 60 + now.minute,
            "dow": now.weekday(),
            # Weekly schedule so the controller can carry on alone if the internet drops:
            # days bitmask (bit 0 = Monday) : minute of day : temperature : servo angle
            "sched": ",".join(
                f"{sum(1 << d for d in p['days'])}:{logic.parse_hm(p['start'])}:{p['temp']:g}:"
                f"{logic.temp_to_angle(p['temp'], cal)}"
                for p in schedule
            ),
            "lcd1": f"SET {target['temp']:g}C {plan['control']['mode'].upper()}"[:16],
            "lcd2": (f"IN {device['indoor_temp']:.1f}C {plan['decision']}" if device.get("indoor_temp") is not None
                     else f"MODE: {plan['decision']}")[:16],
        }
        if cmd:
            lines["cmd"] = f"{cmd['id']}:ANGLE:{cmd['angle']}"
        return lines

    def device_status(self) -> dict:
        device = self.store.get("device") or {}
        last = device.get("last_seen")
        age = None if last is None else max(0, int(time.time() - last))
        return {
            "online": age is not None and age < DEVICE_ONLINE_SECONDS,
            "seen_seconds_ago": age,
            "angle": device.get("angle"),
            "rssi": device.get("rssi"),
            "fw": device.get("fw"),
            "via": device.get("via"),
            "indoor_temp": device.get("indoor_temp"),
            "command": self.store.get("command"),
        }
