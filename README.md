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

## The Arduino decides (two-way)

The Arduino's own code can choose the temperature, and the website shows what it chose.

1. Every 2 seconds the website sends the Arduino **your setting** (from the website
   or the schedule) and the **live weather**: the outside temperature now and in 5 hours.
2. `decide()` in `firmware/uno_usb/uno_usb.ino` picks the actual target, a
   PREPARE/NORMAL decision and a short reason. Out of the box it uses the project's
   original rule: *2 °C colder within 5 h → heat 1 °C more (PREPARE)*. Edit
   `decide()` to add your own rules.
3. The servo turns to the Arduino's choice. The website shows it ("The Arduino chose
   22 °C because: cold coming"), along with your setting, which you can still change.

To turn this off, untick **Let the Arduino decide** under Schedule → Smart rules, or
press **Use my setting instead** on the Overview. You can also set
`#define ARDUINO_DECIDES 0` in the sketch. If the Arduino goes offline, the website
falls back to your setting.

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

## Run it in VS Code (Windows, Mac or Linux)

1. Install Python 3 from https://www.python.org/downloads/. On Windows, tick
   **"Add python.exe to PATH"**.
2. In VS Code, choose **File → Open Folder…** and pick this project folder.
   Click **Install** when VS Code offers the recommended Python extensions.
3. **Terminal → Run Task… → Set up (first time)**. This installs everything
   into a `.venv` folder.
4. Press **Ctrl+Shift+P**, choose **Python: Select Interpreter**, and pick the
   one marked `.venv`.
5. Open the **Run and Debug** view (Ctrl+Shift+D), choose **Website (server)**
   and press **F5**. The terminal shows your password. Then open
   http://localhost:8000.
6. With the Uno plugged in (and the Arduino IDE's Serial Monitor closed), choose
   **USB bridge (Arduino Uno)** and press F5 again. Once the website has been
   started at least once, **Website + USB bridge** starts both together.

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
3. Double-click **`start_bridge.bat`**. It
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

## Put it on the internet (use it from anywhere)

Your Uno is plugged into your PC, so the PC runs the website and a free
**Cloudflare Tunnel** publishes it at a real `https://` address. You don't need
to change your router, and your data stays on your PC.

1. Double-click **`start_public.bat`**. In VS Code you can instead choose
   **Public website (website + Arduino + tunnel)** and press F5.
   - The first time, it downloads Cloudflare's small `cloudflared` program
     into `tools\`.
   - It starts the website, connects the Arduino if it's plugged in (and keeps
     retrying if not), and opens the tunnel.
2. It prints a box like:
   ```
     Your website is live at:  https://some-random-words.trycloudflare.com
   ```
   Open that address on your phone, on mobile data or any Wi-Fi, and sign in
   with your password. The address is also on the **Devices** page.
3. Keep the window open. Closing it takes the website offline, but the Arduino
   stays on its last setting.

**Keep the PC awake:** go to Settings → System → Power → Screen and sleep, and
set "When plugged in, put my device to sleep after" to **Never**. To start
automatically after a restart, add `start_public.bat` to Task Scheduler with
the trigger **At log on**.

**Change the password:** edit `data\password.txt`, then restart. Anyone with the
address and the password can control your heating, so use a strong one.

### A permanent address (your own domain)

The free `trycloudflare.com` address changes every time the tunnel starts. For
one that never changes, such as `https://heating.yourname.com`:

1. Get a domain (about $10 a year, for example from Cloudflare Registrar) and
   add it to a free Cloudflare account.
2. In the Cloudflare dashboard, go to **Zero Trust → Networks → Tunnels → Create
   a tunnel**, choose **Cloudflared**, and name it.
3. Copy the **token**: it's the long text after `--token` in the install
   command shown. Save it in a file named `data\tunnel_token.txt`.
4. Under **Public Hostname**, add `heating` + your domain, with service
   **HTTP** and URL `localhost:8000`.
5. Run `start_public.bat` again. It now uses your own address.

### Or host it in the cloud (later, with the Wi-Fi Arduino)

Once you switch to an Arduino UNO R4 WiFi, the PC is no longer needed. You can
run the website on a cloud host with a persistent disk (Render, Railway,
Fly.io…) using the included `Dockerfile`, with a disk mounted at `/data`. Set
these environment variables: `SHC_PASSWORD`, `SHC_SECURE_COOKIES=1`,
`SHC_DEVICE_TOKEN` and `SHC_SECRET_KEY`.

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
- **Indoor temperature (optional):** use any sensor from your kit. Open
  `firmware/uno_usb/uno_usb.ino`, change `#define SENSOR_TYPE SENSOR_NONE` to
  your sensor, and upload again. The comment above that line shows the wiring
  for each one:
  - DHT11 or DHT22: data pin on **D3**. Needs the "DHT sensor library" by
    Adafruit.
  - TMP36, LM35 or the kit's thermistor module: signal on **A0**.

  The room temperature then shows on the website and the LCD, updating every
  few seconds. Connect the sensor's + to the breadboard's 5V rail (shared with
  the LCD) and its − to GND.

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
