"""Web server: serves the website, the JSON API used by it, and the device API
used by the Arduino (or the USB bridge).

Run locally:   python -m server.app
Production:    gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 "server.app:create_app()"
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_from_directory, session

from . import energy, homeassistant, logic
from .controller import Controller
from .store import Store

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


def _secret(data_dir: Path, name: str, env: str, nbytes: int = 24) -> str:
    """Read a secret from the environment, or create one once and keep it in the data folder."""
    if os.environ.get(env):
        return os.environ[env]
    path = data_dir / name
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(nbytes)
    path.write_text(value)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value


def create_app(data_dir=None, controller: Controller = None) -> Flask:
    data_dir = Path(data_dir or os.environ.get("SHC_DATA_DIR", ROOT / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__, static_folder=None)
    app.secret_key = _secret(data_dir, ".secret_key", "SHC_SECRET_KEY", 32)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SHC_SECURE_COOKIES", "0") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    )
    password = _secret(data_dir, "password.txt", "SHC_PASSWORD", 9)
    device_token = _secret(data_dir, "device_token.txt", "SHC_DEVICE_TOKEN", 18)
    if not os.environ.get("SHC_PASSWORD"):
        print(f"[smart-heating] Website password: {password}  (stored in {data_dir / 'password.txt'})")

    store = controller.store if controller else Store(str(data_dir / "heating.db"))
    ctl = controller or Controller(store)
    app.extensions["controller"] = ctl
    failed_logins: dict = {}

    # ---- helpers ---------------------------------------------------------------------
    def err(message: str, status: int = 400):
        return jsonify({"error": message}), status

    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("auth"):
                return err("Please sign in.", 401)
            # Every state-changing call must come from our own page (basic CSRF protection).
            if request.method != "GET" and request.headers.get("X-Requested-With") != "shc":
                return err("Bad request.", 400)
            return fn(*args, **kwargs)
        return wrapper

    def body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def iso_ts(ts):
        return None if ts is None else datetime.fromtimestamp(ts, ctl.tz).isoformat()

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        if resp.mimetype == "text/html" and request.path != "/api/reports/export":
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'",
            )
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- website -----------------------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/<path:path>")
    def static_files(path):
        if path.startswith("api/"):
            abort(404)
        return send_from_directory(WEB_DIR, path)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ---- sign in -----------------------------------------------------------------------
    @app.post("/api/login")
    def login():
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
        recent = [t for t in failed_logins.get(ip, []) if time.time() - t < 300]
        if len(recent) >= 8:
            return err("Too many attempts. Wait a few minutes and try again.", 429)
        given = str(body().get("password", ""))
        if not hmac.compare_digest(given.encode(), password.encode()):
            failed_logins[ip] = recent + [time.time()]
            time.sleep(0.5)
            return err("That password isn't right.", 401)
        failed_logins.pop(ip, None)
        session.clear()
        session["auth"] = True
        session.permanent = True
        return {"ok": True}

    @app.post("/api/logout")
    def logout():
        session.clear()
        return {"ok": True}

    @app.get("/api/session")
    def session_info():
        return {"signed_in": bool(session.get("auth"))}

    # ---- overview ----------------------------------------------------------------------
    @app.get("/api/state")
    @login_required
    def state():
        plan = ctl.plan()
        now = plan["now"]
        forecast = plan["forecast"]
        floor = now.replace(minute=0, second=0, microsecond=0)
        hours = [h for h in forecast["hours"] if datetime.fromisoformat(h["time"]) >= floor][:8]
        analysis = plan["analysis"]
        cal = plan["calibration"]
        lo, hi = logic.safe_range(cal)
        settings = store.get("settings")
        return {
            "now": now.isoformat(),
            "location": settings["location"]["name"],
            "tz": settings["location"]["tz"],
            "calibration": {k: cal[k] for k in ("min_angle", "max_angle", "points")},
            "mode": plan["control"]["mode"],
            "target": plan["target"],
            "safe_range": [lo, hi],
            "calibrated": logic.is_calibrated(cal),
            "decision": plan["decision"],
            "analysis": None if not analysis else {
                "now": analysis["current"],
                "ahead": analysis["ahead"],
                "change": analysis["change"],
                "window": analysis["window"],
            },
            "forecast": {
                "hours": hours,
                "stale": forecast["stale"],
                "fetched_at": iso_ts(forecast["fetched_at"]),
            },
            "suggestion": plan["suggestion"],
            "upcoming": logic.upcoming(plan["schedule"], plan["events"], now),
            "device": ctl.device_status(),
            "homeassistant": _ha_public(),
            "log": [{**e, "ts": iso_ts(e["ts"])} for e in store.recent_log(8)],
            "energy": _energy_snapshot(now),
        }

    def _energy_snapshot(now):
        start = now.date().replace(day=1).isoformat()
        rows = store.usage_between(start, now.date().isoformat())
        return {"month_kwh": round(sum(r["kwh"] for r in rows), 1) if rows else None,
                "days": len(rows), "latest_day": store.latest_usage_day()}

    def _ha_public():
        ha = store.get("homeassistant")
        return {"enabled": ha["enabled"], "url": ha["url"], "entity_id": ha["entity_id"],
                "has_token": bool(ha["token"]), "last_pushed": ha.get("last_pushed"),
                "last_error": ha.get("last_error")}

    @app.post("/api/control")
    @login_required
    def control():
        data = body()
        try:
            if "mode" in data:
                ctl.set_mode(str(data["mode"]))
            if "temp" in data:
                ctl.set_temperature(float(data["temp"]))
            if data.get("resume"):
                ctl.resume_schedule()
        except (TypeError, ValueError) as exc:
            return err(str(exc))
        return state()

    @app.post("/api/suggestion/<sid>/<action>")
    @login_required
    def suggestion(sid, action):
        try:
            ctl.decide_suggestion(sid, action)
        except LookupError as exc:
            return err(str(exc), 409)
        except ValueError as exc:
            return err(str(exc))
        return state()

    # ---- schedule ----------------------------------------------------------------------
    @app.get("/api/schedule")
    @login_required
    def get_schedule():
        plan = ctl.plan()
        return {"periods": plan["schedule"], "rules": plan["rules"], "events": plan["events"],
                "suggestion": plan["suggestion"], "safe_range": list(logic.safe_range(plan["calibration"])),
                "now": plan["now"].isoformat()}

    @app.put("/api/schedule")
    @login_required
    def put_schedule():
        try:
            periods = logic.validate_schedule(body().get("periods"))
        except (TypeError, ValueError, AttributeError) as exc:
            return err(str(exc) or "Check the schedule and try again.")
        cal = store.get("calibration")
        for p in periods:
            p["temp"] = logic.clamp_temp(p["temp"], cal)
        store.set("schedule", periods)
        store.log("user", "Schedule updated")
        return get_schedule()

    @app.put("/api/rules")
    @login_required
    def put_rules():
        data = body()
        rules = store.get("rules")
        for key in ("preheat_suggestions", "auto_apply"):
            if key in data:
                rules[key] = bool(data[key])
        try:
            if "drop_threshold" in data:
                rules["drop_threshold"] = max(0.5, min(10.0, float(data["drop_threshold"])))
            if "window_hours" in data:
                rules["window_hours"] = max(1, min(24, int(data["window_hours"])))
            if "preheat_boost" in data:
                rules["preheat_boost"] = max(0.5, min(4.0, float(data["preheat_boost"])))
        except (TypeError, ValueError):
            return err("Enter a number.")
        store.set("rules", rules)
        return get_schedule()

    # ---- devices -----------------------------------------------------------------------
    @app.get("/api/devices")
    @login_required
    def devices():
        cal = store.get("calibration")
        return {
            "device": ctl.device_status(),
            "calibration": cal,
            "calibrated": logic.is_calibrated(cal),
            "device_token": device_token,
            "homeassistant": _ha_public(),
        }

    @app.post("/api/servo/test")
    @login_required
    def servo_test():
        try:
            ctl.queue_servo_test(int(body().get("angle")))
        except (TypeError, ValueError) as exc:
            return err(str(exc) or "Enter an angle.")
        return devices()

    @app.post("/api/servo/cancel")
    @login_required
    def servo_cancel():
        ctl.clear_command()
        return devices()

    @app.put("/api/calibration")
    @login_required
    def put_calibration():
        data = body()
        cal = store.get("calibration")
        try:
            lo = int(data.get("min_angle", cal["min_angle"]))
            hi = int(data.get("max_angle", cal["max_angle"]))
            if not (0 <= lo < hi <= 180):
                raise ValueError("Limits must satisfy 0 ≤ min < max ≤ 180.")
            points = data.get("points", cal["points"])
            clean = []
            for p in points:
                clean.append({"temp": float(p["temp"]), "angle": int(p["angle"]),
                              "measured": bool(p.get("measured"))})
            temps = [p["temp"] for p in clean]
            if len(clean) < 2 or len(set(temps)) != len(temps):
                raise ValueError("Need at least two different temperatures.")
            for p in clean:
                if not lo <= p["angle"] <= hi:
                    raise ValueError(f"{p['temp']:g} °C maps to {p['angle']}°, outside the safe limits.")
        except (TypeError, ValueError, KeyError) as exc:
            return err(str(exc) or "Check the calibration values.")
        cal.update(min_angle=lo, max_angle=hi, points=sorted(clean, key=lambda p: p["temp"]))
        store.set("calibration", cal)
        store.log("user", "Servo calibration saved")
        return devices()

    @app.put("/api/homeassistant")
    @login_required
    def put_homeassistant():
        data = body()
        ha = store.get("homeassistant")
        for key in ("url", "entity_id"):
            if key in data:
                ha[key] = str(data[key]).strip()[:300]
        if data.get("token"):
            ha["token"] = str(data["token"]).strip()[:500]
        if "enabled" in data:
            ha["enabled"] = bool(data["enabled"])
        if ha["enabled"]:
            if not (ha["url"].startswith(("http://", "https://")) and ha["entity_id"].startswith("climate.")
                    and ha["token"]):
                return err("Enter the Home Assistant address, a climate.* entity and an access token.")
            try:
                info = homeassistant.check(ha["url"], ha["token"], ha["entity_id"])
                ha["last_error"] = None
                store.log("info", f"Connected to Home Assistant thermostat “{info['name']}”")
            except Exception as exc:
                ha["enabled"] = False
                ha["last_error"] = str(exc)[:200]
                store.set("homeassistant", ha)
                return err("Couldn't reach that thermostat through Home Assistant. Check the address, "
                           "entity and token. The server must be able to reach Home Assistant.")
        store.set("homeassistant", ha)
        store.set("runtime", {**store.get("runtime"), "last_target": None})  # push current target next time
        return devices()

    # ---- energy ------------------------------------------------------------------------
    def _period_from_args(args):
        month = args.get("month")
        if not month:
            month = ctl.now().strftime("%Y-%m")
        try:
            start, end = energy.month_bounds(month)
        except ValueError:
            raise ValueError("Pick a month.")
        return month, start, end

    def _summary(start: date, end: date):
        rows = store.usage_between(start.isoformat(), end.isoformat())
        means = ctl.weather.daily_means(start, end) if rows else {}
        return energy.summarize(rows, means, store.get("settings").get("rate_cents_per_kwh"))

    @app.get("/api/energy")
    @login_required
    def get_energy():
        try:
            month, start, end = _period_from_args(request.args)
        except ValueError as exc:
            return err(str(exc))
        return {
            "month": month,
            "summary": _summary(start, end),
            "rate_cents_per_kwh": store.get("settings").get("rate_cents_per_kwh"),
            "last_reading": store.last_meter_reading(),
            "latest_day": store.latest_usage_day(),
            "reports": [{**r, "ts": iso_ts(r["ts"])} for r in store.recent_reports()],
            "email_configured": energy.email_configured(),
        }

    @app.post("/api/energy/upload")
    @login_required
    def upload_usage():
        f = request.files.get("file")
        if not f:
            return err("Choose a CSV file.")
        try:
            text = f.read().decode("utf-8-sig", errors="replace")
        except Exception:
            return err("Couldn't read that file.")
        totals, skipped = energy.parse_usage_csv(text)
        if not totals:
            return err("No dates with kWh values were found. Is this the usage export from your NB Power account?")
        for day, kwh in totals.items():
            store.upsert_usage(day, kwh, "import")
        store.log("user", f"Imported usage for {len(totals)} days")
        return {"days": len(totals), "skipped": skipped, "first": min(totals), "last": max(totals)}

    @app.post("/api/energy/reading")
    @login_required
    def add_reading():
        try:
            kwh = float(str(body().get("kwh", "")).replace(",", ""))
            if kwh < 0:
                raise ValueError
        except ValueError:
            return err("Enter the kWh number shown on the meter.")
        now_ts = time.time()
        prev = store.last_meter_reading()
        if prev:
            try:
                shares = energy.spread_reading(prev, kwh, now_ts, ctl.tz)
            except ValueError as exc:
                return err(str(exc))
            for day, value in shares.items():
                existing = store.usage_between(day, day)
                if existing and existing[0]["source"] == "import":
                    continue  # imported data is more precise; keep it
                store.upsert_usage(day, value + (existing[0]["kwh"] if existing else 0), "meter")
        store.add_meter_reading(now_ts, kwh)
        store.log("user", f"Meter reading {kwh:g} kWh")
        return {"ok": True, "first": prev is None}

    @app.put("/api/settings")
    @login_required
    def put_settings():
        data = body()
        settings = store.get("settings")
        if "rate_cents_per_kwh" in data:
            raw = data["rate_cents_per_kwh"]
            try:
                settings["rate_cents_per_kwh"] = None if raw in (None, "") else max(0.0, float(raw))
            except (TypeError, ValueError):
                return err("Enter the rate in cents per kWh.")
        store.set("settings", settings)
        return {"ok": True}

    def _report(args_or_body):
        month, start, end = _period_from_args(args_or_body)
        include = {p for p in str(args_or_body.get("include", "usage,schedule,weather")).split(",")
                   if p in energy.REPORT_PARTS}
        summary = _summary(start, end)
        label = start.strftime("%B %Y")
        loc = store.get("settings")["location"]["name"]
        schedule = store.get("schedule")
        return month, label, include, summary, loc, schedule

    @app.get("/api/reports/export")
    @login_required
    def export_report():
        fmt = request.args.get("fmt", "csv")
        try:
            month, label, include, summary, loc, schedule = _report(request.args)
        except ValueError as exc:
            return err(str(exc))
        store.add_report(label, fmt.upper(), "Downloaded")
        if fmt == "html":
            page = energy.report_html(summary, label, include, schedule, loc, auto_print=True)
            return Response(page, mimetype="text/html")
        data = energy.report_csv(summary, label, include, schedule)
        return Response(data, mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=energy-report-{month}.csv"})

    @app.post("/api/reports/send")
    @login_required
    def send_report():
        data = body()
        to = str(data.get("to", "")).strip()
        if "@" not in to or len(to) > 200 or "\n" in to:
            return err("Enter the email address to send it to.")
        if not energy.email_configured():
            return err("Email isn't set up on the server yet. Download the report instead.", 409)
        try:
            month, label, include, summary, loc, schedule = _report(data)
        except ValueError as exc:
            return err(str(exc))
        attachments = [
            (f"energy-report-{month}.csv", "text/csv", energy.report_csv(summary, label, include, schedule)),
            (f"energy-report-{month}.html", "text/html", energy.report_html(summary, label, include, schedule, loc)),
        ]
        try:
            energy.send_report_email(
                to, f"Household energy report — {label}",
                f"Attached is the household heating and energy report for {label} ({loc}).\n\n"
                "Usage comes from the home owner's own NB Power usage data; the meter was not modified.",
                attachments)
        except Exception:
            return err("The email couldn't be sent. Check the server's email settings.", 502)
        store.add_report(label, "CSV + HTML", f"Sent to {to}")
        store.log("user", f"Energy report for {label} emailed")
        return {"ok": True}

    # ---- device API (Arduino / USB bridge) -------------------------------------------------
    @app.route("/api/device/sync", methods=["GET", "POST"])
    def device_sync():
        given = request.headers.get("X-Device-Token") or request.args.get("token", "")
        if not hmac.compare_digest(given.encode(), device_token.encode()):
            return Response("error=unauthorized\n", status=401, mimetype="text/plain")

        def num(name, cast=float):
            try:
                return cast(request.args[name])
            except (KeyError, ValueError):
                return None

        report = {
            "angle": num("angle", int),
            "rssi": num("rssi", int),
            "indoor_temp": num("temp"),
            "ack": request.args.get("ack"),
            "fw": request.args.get("fw", "")[:32] or None,
            "via": request.args.get("via", "")[:16] or None,
        }
        lines = ctl.device_sync(report)
        text = "".join(f"{k}={v}\n" for k, v in lines.items())
        return Response(text, mimetype="text/plain")

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    create_app().run(host="0.0.0.0", port=port, debug=False)
