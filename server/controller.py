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
        # Backward-compatible defaults for projects created before gradual Auto existed.
        control.setdefault("ramp", None)
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

        # Auto ramp: move toward the user's final target over 1–6 hours instead of
        # jumping there. The DHT room sensor gently advances/holds the ramp when the
        # room is materially behind/ahead of the expected warming path.
        ramp = control.get("ramp") if control.get("mode") == "auto" else None
        if ramp:
            try:
                start = datetime.fromisoformat(ramp["start"])
                end = datetime.fromisoformat(ramp["end"])
                start_temp = float(ramp["start_temp"])
                final_temp = float(ramp["target_temp"])
                total = max(1.0, (end - start).total_seconds())
                frac = max(0.0, min(1.0, (now - start).total_seconds() / total))
                linear = start_temp + (final_temp - start_temp) * frac
                room = (self.store.get("device") or {}).get("indoor_temp")
                start_room = ramp.get("start_room")
                feedback = 0.0
                if room is not None and start_room is not None and final_temp > start_temp:
                    expected_room = float(start_room) + (final_temp - float(start_room)) * frac
                    lag = expected_room - float(room)
                    if lag > 0.8:
                        feedback = 0.5
                    elif lag < -0.8:
                        feedback = -0.5
                requested = linear + feedback
                if final_temp >= start_temp:
                    requested = max(start_temp, min(final_temp, requested))
                else:
                    requested = min(start_temp, max(final_temp, requested))
                target = {"temp": requested, "reason": "Auto ramp", "until": end.isoformat()}
                if frac >= 1.0:
                    target["temp"] = final_temp
                    control["ramp"] = None
                    control["hold"] = {"temp": final_temp, "until": (now + timedelta(hours=4)).isoformat()}
                    self.store.set("control", control)
            except (KeyError, TypeError, ValueError):
                control["ramp"] = None
                self.store.set("control", control)

        target["temp"] = round(target["temp"] * 2) / 2
        # "base" is your setting (website/schedule); the Arduino may choose something else from it.
        target["base"] = target["temp"]
        target["base_reason"] = target["reason"]
        decision = analysis["decision"] if analysis else "NORMAL"

        choice = None if control.get("ramp") else self.arduino_choice(rules)
        if choice:
            target.update(temp=round(logic.clamp_temp(choice["set"], calibration) * 2) / 2,
                          reason="Arduino", note=choice.get("note") or "")
            decision = choice.get("decision") or decision
        target["angle"] = logic.temp_to_angle(target["temp"], calibration)

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

    def arduino_choice(self, rules: dict):
        """The target the Arduino's own code chose, if it is deciding and online."""
        if not rules.get("arduino_decides", True):
            return None
        device = self.store.get("device") or {}
        if device.get("set") is None or not device.get("last_seen"):
            return None
        if time.time() - device["last_seen"] > DEVICE_ONLINE_SECONDS:
            return None  # offline: fall back to your setting
        return {"set": device["set"], "decision": device.get("decision"), "note": device.get("note")}

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
        if mode != "auto":
            control["ramp"] = None
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

    def start_auto_ramp(self, temp: float, hours: int) -> None:
        """Gradually move from the current target to temp over 1–6 hours."""
        hours = int(hours)
        if hours < 1 or hours > 6:
            raise ValueError("Auto duration must be between 1 and 6 hours.")
        calibration = self.store.get("calibration")
        final_temp = round(logic.clamp_temp(float(temp), calibration) * 2) / 2
        now = self.now()
        control = self.store.get("control")
        control.setdefault("ramp", None)
        # Use the currently effective website target as the physical starting setpoint.
        current = logic.effective_target(control, self.store.get("schedule"), self.store.get("events"), calibration, now)["temp"]
        device = self.store.get("device") or {}
        control["mode"] = "auto"
        control["hold"] = None
        control["ramp"] = {
            "start": now.isoformat(),
            "end": (now + timedelta(hours=hours)).isoformat(),
            "hours": hours,
            "start_temp": round(float(current) * 2) / 2,
            "target_temp": final_temp,
            "start_room": device.get("indoor_temp"),
        }
        self.store.set("control", control)
        self.store.log("user", f"Auto ramp: {current:g} → {final_temp:g} °C over {hours} h")

    def cancel_auto_ramp(self) -> None:
        control = self.store.get("control")
        control["ramp"] = None
        self.store.set("control", control)
        self.store.log("user", "Auto ramp cancelled")

    def resume_schedule(self) -> None:
        control = self.store.get("control")
        control["hold"] = None
        control["ramp"] = None
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
        for key in ("angle", "rssi", "fw", "indoor_temp", "humidity", "via"):
            if report.get(key) is not None:
                device[key] = report[key]

        # The Arduino's own decision (sent when its code decides the temperature).
        if "set" in report:
            new = (report.get("set"), report.get("decision"), report.get("note"))
            old = (device.get("set"), device.get("decision"), device.get("note"))
            if new != old:
                if new[0] is None:
                    self.store.log("in", "Arduino: following your setting")
                else:
                    why = f" ({new[2]})" if new[2] else ""
                    self.store.log("in", f"Arduino chose {new[0]:g} °C · {new[1] or 'NORMAL'}{why}")
            device["set"], device["decision"], device["note"] = new

        cmd = self.store.get("command")
        if cmd and report.get("ack") is not None and str(report["ack"]) == str(cmd["id"]):
            self.store.log("in", f"Controller: servo at {cmd['angle']}° (test done)")
            self.store.set("command", None)
            device["test_until"] = now_ts + 60  # hold the test angle for a minute
            cmd = None

        self.store.set("device", device)  # save first so the plan sees the Arduino's latest decision
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
        # Inputs for the Arduino's own decision: your setting and the live weather.
        lines["base"] = f"{target['base']:g}"
        analysis = plan["analysis"]
        if analysis:
            lines["out"] = f"{analysis['current']['temp']:g}"
            lines["ahead"] = f"{analysis['ahead']['temp']:g}"
            lines["hours"] = analysis["window"]
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
            "humidity": device.get("humidity"),
            "set": device.get("set"),
            "decision": device.get("decision"),
            "note": device.get("note"),
            "command": self.store.get("command"),
        }
