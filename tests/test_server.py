from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from server import energy, logic
from server.app import create_app
from server.controller import Controller
from server.store import DEFAULTS, Store
from server.weather import Weather, localize

TZ = ZoneInfo("America/Moncton")
# Sunday 27 Sep 2026, midnight — matches the forecast from the first working run.
NOW = datetime(2026, 9, 27, 0, 5, tzinfo=TZ)
TEMPS = [(12.5, 11.1), (11.8, 10.5), (11.4, 10.2), (10.8, 9.3), (10.2, 8.7), (9.8, 8.4), (9.5, 7.8), (9.2, 7.3)]


def hourly(start=NOW.replace(minute=0), temps=TEMPS):
    return [{"time": (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"), "temp": t, "feels": f}
            for i, (t, f) in enumerate(temps)]


@pytest.fixture
def ctl(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    weather = Weather(store, fetcher=lambda *a: hourly(), daily_fetcher=lambda *a: {})
    return Controller(store, weather, clock=lambda: NOW)


@pytest.fixture
def client(tmp_path, ctl, monkeypatch):
    monkeypatch.setenv("SHC_PASSWORD", "letmein")
    monkeypatch.setenv("SHC_DEVICE_TOKEN", "devtoken")
    app = create_app(tmp_path, controller=ctl)
    c = app.test_client()
    assert c.post("/api/login", json={"password": "letmein"}).status_code == 200
    c.environ_base["HTTP_X_REQUESTED_WITH"] = "shc"
    return c


# ---- logic -------------------------------------------------------------------------------

def test_angle_mapping_interpolates_and_clamps():
    cal = DEFAULTS["calibration"]
    assert logic.temp_to_angle(21, cal) == 90
    assert logic.temp_to_angle(21.5, cal) == 95
    assert logic.temp_to_angle(30, cal) == 120
    assert logic.temp_to_angle(10, cal) == 60


def test_forecast_analysis_matches_first_run():
    a = logic.analyze_forecast(localize(hourly(), "America/Moncton"), NOW, threshold=2.0, window=5)
    assert a["change"] == -2.7
    assert a["decision"] == "PREPARE"


def test_steady_forecast_is_normal():
    flat = [(10.0, 9.0)] * 8
    a = logic.analyze_forecast(localize(hourly(temps=flat), "America/Moncton"), NOW, threshold=2.0, window=5)
    assert a["decision"] == "NORMAL"


def test_schedule_period_lookup_wraps_to_previous_day():
    sched = DEFAULTS["schedule"]
    # 00:05 Sunday -> still "Overnight" from Saturday 22:30
    assert logic.scheduled_period_at(sched, NOW)["name"] == "Overnight"
    # Monday 09:00 -> Away (weekday); Sunday 09:00 -> Morning
    monday = datetime(2026, 9, 28, 9, 0, tzinfo=TZ)
    assert logic.scheduled_period_at(sched, monday)["name"] == "Away"
    assert logic.scheduled_period_at(sched, NOW.replace(hour=9))["name"] == "Morning"


def test_validate_schedule_rejects_bad_input():
    with pytest.raises(ValueError):
        logic.validate_schedule([{"name": "x", "start": "25:00", "temp": 20, "days": [0]}])
    with pytest.raises(ValueError):
        logic.validate_schedule([{"name": "x", "start": "08:00", "temp": 20, "days": []}])


# ---- controller ----------------------------------------------------------------------------

def test_plan_suggests_preheat(ctl):
    plan = ctl.plan()
    sg = plan["suggestion"]
    assert plan["decision"] == "PREPARE"
    assert sg["status"] == "pending"
    assert sg["temp"] == 20  # overnight 19 + 1
    assert plan["target"]["temp"] == 19


def test_accepting_suggestion_changes_target_when_it_starts(ctl):
    sg = ctl.plan()["suggestion"]
    ctl.decide_suggestion(sg["id"], "accept")
    start = datetime.fromisoformat(sg["start"])
    ctl._clock = lambda: start + timedelta(minutes=1)
    plan = ctl.plan()
    assert plan["target"]["reason"] == "Preheat"
    assert plan["target"]["temp"] == sg["temp"]


def test_hold_lasts_until_next_schedule_change(ctl):
    ctl.set_temperature(22)
    t = ctl.plan()["target"]
    assert (t["temp"], t["reason"]) == (22, "Hold")
    assert t["until"].startswith("2026-09-27T06:30")


def test_device_sync_returns_target_and_schedule(ctl):
    lines = ctl.device_sync({"angle": 70})
    assert lines["target"] == "19"
    assert lines["angle"] == 70
    assert lines["decision"] == "PREPARE"
    assert "127:390:21:90" in lines["sched"]  # Morning, every day, 06:30, 21 °C, 90°
    assert ctl.device_status()["online"]


def test_servo_test_respects_limits_and_is_acknowledged(ctl):
    with pytest.raises(ValueError):
        ctl.queue_servo_test(150)
    cmd = ctl.queue_servo_test(100)
    assert ctl.device_sync({})["cmd"] == f"{cmd['id']}:ANGLE:100"
    ctl.device_sync({"ack": str(cmd["id"]), "angle": 100})
    assert ctl.store.get("command") is None


# ---- energy ----------------------------------------------------------------------------------

def test_parse_hourly_usage_csv():
    text = "Account,123\nDate,Time,Usage (kWh)\n2026-09-01,00:00,1.5\n2026-09-01,01:00,2\n09/02/2026,00:00,\"1,000.25\"\nbad,row,x\n"
    totals, skipped = energy.parse_usage_csv(text)
    assert totals == {"2026-09-01": 3.5, "2026-09-02": 1000.25}
    assert skipped == 1


def test_spread_meter_reading():
    prev = {"ts": datetime(2026, 9, 1, 12, tzinfo=TZ).timestamp(), "kwh": 1000}
    now = datetime(2026, 9, 4, 12, tzinfo=TZ).timestamp()
    shares = energy.spread_reading(prev, 1060, now, TZ)
    assert shares == {"2026-09-02": 20, "2026-09-03": 20, "2026-09-04": 20}


def test_report_html_escapes_names():
    summary = energy.summarize([{"day": "2026-09-01", "kwh": 20, "source": "import"}], {"2026-09-01": 8.0}, 12.5)
    out = energy.report_html(summary, "September 2026", {"usage", "schedule", "weather"},
                             [{"name": "<script>", "start": "06:00", "temp": 21, "days": [0]}], "Home")
    assert "<script>" not in out.split("</style>")[1]
    assert summary["kwh_per_hdd"] == 2.0
    assert summary["estimated_cost"] == 2.5


# ---- API -------------------------------------------------------------------------------------------

def test_api_requires_login(tmp_path, ctl, monkeypatch):
    monkeypatch.setenv("SHC_PASSWORD", "letmein")
    c = create_app(tmp_path, controller=ctl).test_client()
    assert c.get("/api/state").status_code == 401
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401


def test_api_requires_custom_header_for_changes(client):
    del client.environ_base["HTTP_X_REQUESTED_WITH"]
    assert client.post("/api/control", json={"mode": "away"}).status_code == 400


def test_api_state_and_control(client):
    s = client.get("/api/state").get_json()
    assert s["decision"] == "PREPARE"
    assert len(s["forecast"]["hours"]) == 8
    s = client.post("/api/control", json={"mode": "manual"}).get_json()
    s = client.post("/api/control", json={"temp": 23}).get_json()
    assert s["target"] == {"temp": 23.0, "reason": "Manual", "until": None, "angle": 110,
                           "base": 23.0, "base_reason": "Manual"}


def test_api_schedule_roundtrip(client):
    periods = [{"name": "Day", "start": "07:00", "temp": 21, "days": [0, 1, 2, 3, 4, 5, 6]},
               {"name": "Night", "start": "22:00", "temp": 40, "days": [0, 1, 2, 3, 4, 5, 6]}]
    r = client.put("/api/schedule", json={"periods": periods}).get_json()
    assert [p["temp"] for p in r["periods"]] == [21, 24]  # clamped to the safe range


def test_device_endpoint(client):
    assert client.get("/api/device/sync?token=wrong").status_code == 401
    r = client.get("/api/device/sync?angle=80&rssi=-60", headers={"X-Device-Token": "devtoken"})
    assert r.status_code == 200
    assert "target=19\n" in r.get_data(as_text=True)


def test_energy_upload_and_export(client):
    from io import BytesIO
    data = {"file": (BytesIO(b"Date,kWh\n2026-09-01,20\n2026-09-02,22\n"), "usage.csv")}
    r = client.post("/api/energy/upload", data=data, content_type="multipart/form-data")
    assert r.get_json()["days"] == 2
    e = client.get("/api/energy?month=2026-09").get_json()
    assert e["summary"]["total_kwh"] == 42
    csv_out = client.get("/api/reports/export?month=2026-09&fmt=csv").get_data(as_text=True)
    assert "2026-09-02,22" in csv_out


def test_preheat_never_lowers_a_warmer_schedule(ctl):
    sg = ctl.plan()["suggestion"]
    ctl.decide_suggestion(sg["id"], "accept")
    morning = datetime.fromisoformat(sg["start"]).replace(hour=7, minute=0)
    ctl._clock = lambda: morning
    t = ctl.plan()["target"]
    assert (t["temp"], t["reason"]) == (21, "Morning")


def test_bridge_offline_schedule_matches_server(ctl):
    from bridge.serial_bridge import scheduled_angle
    sched = ctl.device_sync({})["sched"]
    monday_9am = datetime(2026, 9, 28, 9, 0)
    sunday_9am = datetime(2026, 9, 27, 9, 0)
    assert scheduled_angle(sched, monday_9am) == 60   # Away 18 °C
    assert scheduled_angle(sched, sunday_9am) == 90   # Morning 21 °C
    assert scheduled_angle(sched, datetime(2026, 9, 27, 0, 5)) == 70  # Overnight from Saturday


def test_login_lockout_ignores_spoofed_forwarded_for(tmp_path, ctl, monkeypatch):
    monkeypatch.setenv("SHC_PASSWORD", "letmein")
    monkeypatch.setattr("server.app.time.sleep", lambda s: None)
    c = create_app(tmp_path, controller=ctl).test_client()
    codes = [c.post("/api/login", json={"password": "x"},
                    headers={"X-Forwarded-For": f"10.0.0.{i}"}).status_code for i in range(10)]
    assert codes[-1] == 429
    assert c.post("/api/login", json={"password": "letmein"}).status_code == 429


def test_arduino_decision_drives_target_and_website_sets_base(ctl):
    lines = ctl.device_sync({})
    assert lines["base"] == "19" and lines["out"] == "12.5" and lines["ahead"] == "9.8" and lines["hours"] == 5
    # The Arduino's own code chooses 20 °C because cold is coming.
    lines = ctl.device_sync({"set": 20.0, "decision": "PREPARE", "note": "cold coming"})
    assert (lines["target"], lines["angle"], lines["decision"]) == ("20", 80, "PREPARE")
    t = ctl.plan()["target"]
    assert (t["temp"], t["reason"], t["base"], t["note"]) == (20, "Arduino", 19, "cold coming")
    # Changing the temperature on the website changes the base the Arduino decides from.
    ctl.set_temperature(22)
    assert ctl.device_sync({"set": 23.0, "decision": "PREPARE", "note": "cold coming"})["base"] == "22"
    assert ctl.plan()["target"]["temp"] == 23
    # "set=none": the Arduino follows the website again.
    ctl.device_sync({"set": None})
    assert ctl.plan()["target"]["temp"] == 22


def test_arduino_decision_ignored_when_off_or_offline(ctl):
    ctl.device_sync({"set": 24.0, "decision": "PREPARE", "note": "x"})
    rules = ctl.store.get("rules"); rules["arduino_decides"] = False; ctl.store.set("rules", rules)
    assert ctl.plan()["target"]["temp"] == 19
    rules["arduino_decides"] = True; ctl.store.set("rules", rules)
    device = ctl.store.get("device"); device["last_seen"] -= 3600; ctl.store.set("device", device)
    assert ctl.plan()["target"]["temp"] == 19


def test_device_endpoint_accepts_arduino_decision(client):
    r = client.get("/api/device/sync?set=21.5&decision=prepare&note=cold%20coming",
                   headers={"X-Device-Token": "devtoken"}).get_data(as_text=True)
    assert "target=21.5\n" in r and "decision=PREPARE\n" in r
    s = client.get("/api/state").get_json()
    assert s["target"]["reason"] == "Arduino" and s["device"]["note"] == "cold coming"


def test_bridge_parses_arduino_report():
    from bridge.serial_bridge import parse_report
    assert parse_report("OK REPORT 22.0 PREPARE cold_coming") == {"set": 22.0, "decision": "PREPARE", "note": "cold coming"}
    assert parse_report("OK REPORT NONE") == {"set": "none"}
    assert parse_report("ERR UNKNOWN") is None
