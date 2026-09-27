"""Put the Smart Heating website on the internet from this computer.

Starts three things and keeps them running:
  1. the website (server/)
  2. the USB bridge to the Arduino (bridge/), if an Arduino is plugged in
  3. a Cloudflare Tunnel, which gives the website a public https address

Run it with start_public.bat (Windows) or:  python run_public.py

With no setup, Cloudflare gives a free random address like
https://some-words.trycloudflare.com that changes each time this starts.
For an address that never changes, create a Cloudflare tunnel for your own
domain and put its token in data/tunnel_token.txt (see README).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def use_project_environment():
    """Make sure we run inside the project's .venv with everything installed.

    Whatever Python started this script (VS Code's pick, a double-click...), this
    creates .venv if needed, installs the requirements if any are missing, and
    then re-runs this script with the .venv's Python.
    """
    root = Path(__file__).resolve().parent
    venv = root / ".venv"
    venv_py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not venv_py.exists():
        print("Setting up the project for the first time...", flush=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
    missing = subprocess.call([str(venv_py), "-c", "import flask, requests, serial, waitress"
                               if os.name == "nt" else "import flask, requests, serial"],
                              stderr=subprocess.DEVNULL) != 0
    if missing:
        print("Installing what the website needs (one time, about a minute)...", flush=True)
        subprocess.check_call([str(venv_py), "-m", "pip", "install", "--disable-pip-version-check",
                               "-r", str(root / "requirements.txt"), "-r", str(root / "bridge" / "requirements.txt")])
    if Path(sys.executable).resolve() != venv_py.resolve():
        try:
            sys.exit(subprocess.call([str(venv_py), str(Path(__file__).resolve())] + sys.argv[1:]))
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    use_project_environment()

import requests  # noqa: E402  (after the environment check above)

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("SHC_DATA_DIR", ROOT / "data"))
TOOLS = ROOT / "tools"
PORT = os.environ.get("PORT", "8000")
PY = sys.executable
URL_FILE = DATA / "public_url.txt"

DOWNLOADS = {
    ("Windows", "AMD64"): "cloudflared-windows-amd64.exe",
    ("Windows", "ARM64"): "cloudflared-windows-amd64.exe",
    ("Windows", "x86"): "cloudflared-windows-386.exe",
    ("Linux", "x86_64"): "cloudflared-linux-amd64",
    ("Linux", "aarch64"): "cloudflared-linux-arm64",
    ("Linux", "armv7l"): "cloudflared-linux-arm",
}
RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def find_cloudflared() -> str:
    """Use an installed cloudflared, or download it once into tools/."""
    found = shutil.which("cloudflared")
    if found:
        return found
    for guess in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                  r"C:\Program Files\cloudflared\cloudflared.exe"):
        if Path(guess).exists():
            return guess
    exe = TOOLS / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if exe.exists():
        return str(exe)
    if platform.system() == "Darwin":
        sys.exit("On a Mac, install it first:  brew install cloudflared")
    name = DOWNLOADS.get((platform.system(), platform.machine()))
    if not name:
        sys.exit("Please install cloudflared: https://developers.cloudflare.com/cloudflare-one/"
                 "connections/connect-networks/downloads/")
    say("Downloading Cloudflare Tunnel (one time, about 30 MB)...")
    TOOLS.mkdir(exist_ok=True)
    tmp = exe.with_suffix(".part")
    with requests.get(RELEASES + name, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                f.write(chunk)
    tmp.replace(exe)
    if os.name != "nt":
        exe.chmod(0o755)
    return str(exe)


def wait_for_website(timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if requests.get(f"http://127.0.0.1:{PORT}/healthz", timeout=2).ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


class Keeper(threading.Thread):
    """Runs a program and starts it again if it stops."""

    def __init__(self, name, args, env=None, on_line=None, retry_seconds=10):
        super().__init__(daemon=True)
        self.name, self.args, self.env = name, args, env
        self.on_line, self.retry_seconds = on_line, retry_seconds
        self.proc = None
        self.stopping = False

    def run(self):
        while not self.stopping:
            try:
                self.proc = subprocess.Popen(
                    self.args, cwd=ROOT, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1)
            except OSError as exc:
                say(f"{self.name}: could not start ({exc})")
                time.sleep(self.retry_seconds)
                continue
            for line in self.proc.stdout:
                line = line.rstrip()
                if self.on_line:
                    self.on_line(line)
                else:
                    print(f"  {self.name}: {line}", flush=True)
            self.proc.wait()
            if not self.stopping:
                say(f"{self.name} stopped; starting it again in {self.retry_seconds} s")
                time.sleep(self.retry_seconds)

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, SHC_DATA_DIR=str(DATA), PORT=PORT, PYTHONUNBUFFERED="1",
               PYTHONIOENCODING="utf-8")
    env.setdefault("SHC_SECURE_COOKIES", "1")  # the public address is https

    token_file = DATA / "tunnel_token.txt"
    tunnel_token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN") or (
        token_file.read_text().strip() if token_file.exists() else "")
    cloudflared = find_cloudflared()

    website = Keeper("website", [PY, "-m", "server.app"], env=env, retry_seconds=3)
    website.start()
    if not wait_for_website():
        say("The website didn't start. Look for the error above.")
    password_file = DATA / "password.txt"
    if password_file.exists() and not os.environ.get("SHC_PASSWORD"):
        say(f"Website password: {password_file.read_text().strip()}")

    # The bridge exits if no Arduino is plugged in; it is retried every 30 s.
    bridge_args = [PY, str(ROOT / "bridge" / "serial_bridge.py"), "--server", f"http://127.0.0.1:{PORT}"]
    key = os.environ.get("SHC_DEVICE_TOKEN") or (
        (DATA / "device_token.txt").read_text().strip() if (DATA / "device_token.txt").exists() else "")
    if key:
        bridge_args += ["--key", key]
    bridge = Keeper("arduino", bridge_args, env=env, retry_seconds=30)
    bridge.start()

    def tunnel_line(line: str):
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            URL_FILE.write_text(url)
            print("\n" + "=" * 64, flush=True)
            print(f"  Your website is live at:  {url}", flush=True)
            print("  Open it on your phone from anywhere. Keep this window open.", flush=True)
            print("=" * 64 + "\n", flush=True)
        elif "Registered tunnel connection" in line and tunnel_token:
            say("Tunnel connected - your website is live at your own domain.")
        elif "ERR" in line or "error" in line.lower():
            print(f"  tunnel: {line}", flush=True)

    if tunnel_token:
        args = [cloudflared, "tunnel", "--no-autoupdate", "run", "--token", tunnel_token]
        if URL_FILE.exists():
            URL_FILE.unlink()
    else:
        args = [cloudflared, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"]
    say("Opening the Cloudflare Tunnel...")
    tunnel = Keeper("tunnel", args, env=env, on_line=tunnel_line, retry_seconds=10)
    tunnel.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        say("Stopping...")
        for k in (tunnel, bridge, website):
            k.stop()
        if URL_FILE.exists():
            URL_FILE.unlink()


if __name__ == "__main__":
    main()
