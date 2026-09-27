"""USB bridge: lets the current Arduino Uno (no Wi-Fi) be controlled from the website.

Runs on the Windows PC, Mac or Raspberry Pi that the Uno is plugged into:

    pip install requests pyserial
    python bridge/serial_bridge.py --server https://my-heating.onrender.com --key DEVICE_KEY

The Arduino's port (COM3 on Windows, /dev/cu.usbmodem... on a Mac) is found
automatically; pass --port COM4 to choose one yourself.

Every 10 seconds it checks in with the website, then moves the servo and updates
the LCD through the Uno's USB serial port (firmware/uno_usb). If the website
can't be reached, it keeps following the last schedule it received.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import serial
from serial.tools import list_ports

SYNC_SECONDS = 10


def parse(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def scheduled_angle(sched: str, when: datetime):
    """Angle from the compact weekly schedule 'mask:minute:temp:angle,...'."""
    entries = []
    for item in sched.split(","):
        try:
            mask, start, _temp, angle = item.split(":")
            entries.append((int(mask), int(start), int(angle)))
        except ValueError:
            continue
    minute = when.hour * 60 + when.minute
    for back in range(8):
        day = (when.weekday() - back) % 7
        todays = [e for e in entries if e[0] & (1 << day) and (back > 0 or e[1] <= minute)]
        if todays:
            return max(todays, key=lambda e: e[1])[2]
    return None


class Arduino:
    def __init__(self, port: str):
        self.port = port
        self.conn = None
        self.angle = None

    def open(self):
        if self.conn:
            return
        self.conn = serial.Serial(self.port, 9600, timeout=3)
        time.sleep(2)  # the Uno restarts when the port opens
        self.conn.reset_input_buffer()
        print(f"Connected to Arduino on {self.port}")

    def send(self, line: str) -> str:
        try:
            self.open()
            self.conn.write((line + "\n").encode())
            reply = self.conn.readline().decode(errors="replace").strip()
        except (serial.SerialException, OSError) as exc:
            print(f"Arduino not reachable ({exc}); retrying next round")
            self.conn = None
            return ""
        if reply.startswith("OK ANGLE "):
            try:
                self.angle = int(reply.split()[-1])
            except ValueError:
                pass
        return reply


def find_arduino_port():
    """Pick the serial port that looks like an Arduino (COMx on Windows)."""
    ports = list(list_ports.comports())
    for p in ports:
        text = f"{p.description} {p.manufacturer or ''}".lower()
        if "arduino" in text or p.vid == 0x2341:
            return p.device
    for p in ports:
        text = f"{p.device} {p.description}".lower()
        if "usbmodem" in text or "usb serial" in text or "ch340" in text or "usb-serial" in text:
            return p.device
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://localhost:8000",
                    help="website address (default: the website running on this computer)")
    ap.add_argument("--key", help="device key from the website's Devices page "
                                  "(default: read from data/device_token.txt)")
    ap.add_argument("--port", help="Arduino serial port, e.g. COM3 (found automatically if left out)")
    args = ap.parse_args()
    if not args.key:
        token_file = Path(__file__).resolve().parent.parent / "data" / "device_token.txt"
        if not token_file.exists():
            sys.exit("No device key given. Start the website once first, or pass --key "
                     "(copy it from the website's Devices page).")
        args.key = token_file.read_text().strip()
    if not args.port:
        args.port = find_arduino_port()
        if not args.port:
            names = ", ".join(p.device for p in list_ports.comports()) or "none"
            sys.exit(f"Couldn't find the Arduino. Plug it in, or pass --port (ports seen: {names}). "
                     "Close the Arduino IDE's Serial Monitor first - only one program can use the port.")
        print(f"Using Arduino on {args.port}")

    arduino = Arduino(args.port)
    session = requests.Session()
    session.headers["X-Device-Token"] = args.key
    last = {}
    ack = None
    test_until = 0.0
    lcd = (None, None)

    while True:
        params = {"via": "usb", "fw": "uno-usb-1.0"}
        if arduino.angle is not None:
            params["angle"] = arduino.angle
        if ack is not None:
            params["ack"] = ack
        try:
            resp = session.get(args.server.rstrip("/") + "/api/device/sync", params=params, timeout=10)
            if resp.status_code == 401:
                sys.exit("The website rejected the device key. Copy it again from the Devices page.")
            resp.raise_for_status()
            data = parse(resp.text)
            online = data.get("ok") == "1"
        except requests.RequestException as exc:
            print(f"{datetime.now():%H:%M:%S} website unreachable ({exc.__class__.__name__}); using stored schedule")
            data, online = {}, False

        if online:
            last = data
            ack = None
            if "cmd" in data:  # servo test: "<id>:ANGLE:<angle>"
                cid, kind, value = data["cmd"].split(":")
                if kind == "ANGLE":
                    arduino.send(f"ANGLE {value}")
                    ack = cid
                    test_until = time.time() + 60
            wanted = (data.get("lcd1", ""), data.get("lcd2", ""))
        else:
            wanted = ("OFFLINE-SCHEDULE", "SERVER DOWN")

        if last and time.time() > test_until:
            angle = last.get("angle")
            if not online and last.get("mode") == "auto" and last.get("sched"):
                angle = scheduled_angle(last["sched"], datetime.now()) or angle
            if angle is not None:
                arduino.send(f"SET {angle} {last.get('min', 60)} {last.get('max', 120)}")

        if wanted != lcd:
            if arduino.send(f"LCD1 {wanted[0]}") and arduino.send(f"LCD2 {wanted[1]}"):
                lcd = wanted

        time.sleep(SYNC_SECONDS)


if __name__ == "__main__":
    main()
