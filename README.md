# Smart Heating Controller

A university prototype that heats a home based on the live weather forecast. You
control it from a website that works on your phone or laptop from anywhere. It
works with **old mechanical thermostats** (an SG90 servo turns the dial) and with
**modern Wi-Fi thermostats** (through Home Assistant).

```
 Phone / laptop ──https──▶  Website + API (server/)  ◀──https── Arduino UNO R4 WiFi (firmware/uno_r4_wifi)
                                  │  ▲                              or
                  Open-Meteo ◀────┘  └──── USB bridge (bridge/) ──USB── current Arduino Uno (firmware/uno_usb)
                  Home Assistant ◀── (optional, modern thermostats)
```

## What it does

- **Overview**: shows the target temperature (you can change it or switch between
  Auto, Manual and Away), the outside temperature, an 8-hour forecast, what the
  controller is doing, and the forecast decision (PREPARE/NORMAL).
- **Preheat suggestions**: when the forecast drops by 2 °C or more within 5 hours
  (you can change both numbers), it suggests something like *"Preheat to 20 °C at
  5:45 AM?"*. Nothing happens until you accept, unless you turn on automatic
  acceptance. A preheat only ever raises the temperature.
- **Schedule**: set a temperature for each part of the day and pick which days
  it applies to. If you change the temperature in Auto mode, it holds until the
  next scheduled change.
- **Energy & reports**: import the usage file (CSV) from your NB Power online
  account, or type in readings from the meter display. You get daily kWh, a cost
  estimate from your rate, and kWh per heating degree-day (usage adjusted for
  weather). You can download a report as CSV or printable/PDF, or email it to
  NB Power or anyone else.
- **Devices**: controller status, what the LCD should show, the device key,
  servo tests, safe angle limits, temperature→angle calibration, the Home
  Assistant connection, and an optional indoor sensor reading.

**About NB Power:** the meter is never touched. Usage data only comes from files
or numbers you give the site yourself. A report is something *you* send. It does
not change your bill or sign you up for any program.

## Keeping it running ("always works")

- **The controller carries on alone.** Every check-in sends it the whole weekly
  schedule. The Wi-Fi firmware also saves that schedule to EEPROM. If the
  internet, the Wi-Fi or the website goes down, it keeps following the schedule
  and the LCD shows `OFFLINE-SCHEDULE`. The USB bridge does the same.
- **The controller checks in with the website, not the other way round** (every
  10 seconds). That means no port forwarding or changes to your home router.
  Changes you make while it's offline are picked up when it reconnects.
- **The website keeps the last forecast** if Open-Meteo is briefly unreachable
  (it's marked "offline copy"). The web app shows a banner if *it* can't reach
  the server.
- **Host the website somewhere that is always on.** See Deploy below.

## Run it on Windows (5 minutes)

1. Install Python 3 from https://www.python.org/downloads/. In the installer,
   tick **"Add python.exe to PATH"**.
2. Download this project: on GitHub, choose **Code → Download ZIP**, then
   unzip it. Or use `git clone`.
3. Double-click **`start_server.bat`**. The first run installs everything. Then
   open http://localhost:8000. The window shows the website password on the
   first start; it's also saved in `data\password.txt`. Keep the window open
   while you use the site.

If Windows asks whether Python may use the network, allow **Private networks**.
Your phone on the same Wi-Fi can then open `http://<your PC's IP>:8000`. Run
`ipconfig` to find the IPv4 address.

To choose your own password, run these in PowerShell from the project folder:

```powershell
$env:SHC_PASSWORD = "yourpassword"
.venv\Scripts\python -m server.app
```

### Use your current Arduino Uno right away (USB)

1. In the Arduino IDE, upload `firmware\uno_usb\uno_usb.ino` to the Uno (same
   LCD wiring; servo on D9; D2 not used). It still understands `PREPARE` /
   `NORMAL` / `PREHEAT`.
2. **Close the IDE's Serial Monitor.** Only one program can use the COM port at a time.
3. Double-click **`start_bridge.bat`** and press Enter to accept the address. It
   finds the Arduino's COM port (e.g. `COM3`) automatically and reads the
   device key from `data\device_token.txt`. The Devices page should switch to
   **Online · usb**.

   If it picks the wrong port, run it by hand:
   `.venv\Scripts\python bridge\serial_bridge.py --server http://localhost:8000 --key <KEY> --port COM4`
   (Arduino IDE → Tools → Port shows the right one).

This replaces `weather_controller.py`. The forecast check, the PREPARE/NORMAL
decision and the serial commands now live in the server and the bridge.

### On a Mac or Linux instead

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r bridge/requirements.txt
python -m server.app
python bridge/serial_bridge.py --server http://localhost:8000 --key <DEVICE KEY>
```

### Go wireless (no computer needed)

The Uno has no Wi-Fi. Use an **Arduino UNO R4 WiFi**: it has the same pins and
shape, so the LCD and servo wiring stays exactly the same.

1. In the Arduino IDE, install the "Arduino UNO R4 Boards" package.
2. Copy `firmware/uno_r4_wifi/arduino_secrets.example.h` to `arduino_secrets.h`
   and fill in your Wi-Fi, the website address and the device key.
3. Upload `firmware/uno_r4_wifi/uno_r4_wifi.ino`.

## Deploy so you can use it from anywhere

Pick one:

- **Your Windows PC (or a Raspberry Pi) at home + Cloudflare Tunnel (free).**
  Keep `start_server.bat` running and install `cloudflared`
  (`winget install Cloudflare.cloudflared`). Then run
  `cloudflared tunnel --url http://localhost:8000` for a quick test address, or
  set up a named tunnel with your own domain so the address never changes. The
  data stays at home, but it only works while the PC is on and not asleep. To
  start it automatically, add `start_server.bat` and `start_bridge.bat` to
  Task Scheduler with the trigger "At log on".
- **A cloud host with a persistent disk** (Render, Railway, Fly.io…). Use the
  included `Dockerfile` and mount a disk at `/data`. Set these environment
  variables:
  - `SHC_PASSWORD`: the website password.
  - `SHC_SECURE_COOKIES=1`: required on https.
  - `SHC_DEVICE_TOKEN` (optional): set this so the device key survives redeploys.
  - `SHC_SECRET_KEY` (optional): keeps you signed in across redeploys.

  Without a persistent disk, the schedule and usage data are lost on every
  redeploy.

On an iPhone or Android phone, open the site and choose **Add to Home Screen**
to get an app icon.

### Emailing reports (optional)

Set `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASSWORD` and
`SMTP_FROM` (for example, a Gmail app password). Until these are set, the site
offers **Download** only.

### Modern Wi-Fi thermostats (optional)

In Home Assistant, add your thermostat's integration and create a long-lived
access token (Profile → Security). Then fill in **Devices → Wi-Fi thermostat**
with the Home Assistant address, the `climate.…` entity and the token. The
server must be able to reach Home Assistant, which means Nabu Casa or a tunnel
if the server is in the cloud. Every target change is sent to the thermostat
too.

## Hardware notes

- **Servo power:** the SG90 can pull more current than the Uno's 5 V pin
  comfortably gives, especially with the LCD sharing it. If the LCD flickers or
  the board resets when the servo moves, power the servo from a separate 5 V
  supply and **connect its GND to the Arduino GND**.
- **Testing the servo:** test it on its own first. Keep it detached from the
  dial until you know the dial's end stops. The firmware never moves past the
  safe limits set on the Devices page (60°–120° by default), and it releases
  the servo after each move, so it doesn't buzz or keep drawing power.
- **Calibration:** on Devices, move the servo until the dial reads each
  temperature, then press **Use current angle**. Until every point is measured,
  the site marks the mapping as uncalibrated.
- **Indoor sensor (optional):** a DS18B20 or DHT22 on a free pin (D3 or D8).
  Send its reading as `&temp=21.3` on the sync request and the site shows it.

## Development

```bash
pip install -r requirements.txt -r bridge/requirements.txt pytest
python -m pytest
```

Project layout:

```
server/     Flask app: API, heating logic, weather, energy/reports, Home Assistant
web/        the website (plain HTML/CSS/JS, no build step)
firmware/   uno_r4_wifi (Wi-Fi) and uno_usb (USB, with bridge/)
bridge/     serial_bridge.py for the USB Uno
tests/      pytest suite
```

Device API (for your own hardware): `GET /api/device/sync` with header
`X-Device-Token: <key>`. Optional query parameters are `angle`, `rssi`, `temp`,
`ack`, `fw` and `via`. The response is plain `key=value` lines: `target`,
`angle`, `decision`, `mode`, `min`, `max`, `now`, `dow`, `sched`, `lcd1`,
`lcd2`, and an optional `cmd`.
