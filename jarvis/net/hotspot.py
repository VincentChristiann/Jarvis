"""Hub-owned Wi-Fi hotspot with a random SSID and password, plus the join QR payload.

Linux is automated through NetworkManager. Windows is NOT automated: the Mobile Hotspot API
needs an internet connection profile, so it is unreliable fully offline. On Windows, turn on
Mobile Hotspot in Settings and run the hub with `--advertise <hotspot address>`.
"""
import secrets
import shutil
import string
import subprocess
import sys

ALPHABET = string.ascii_letters + string.digits
HOTSPOT_ADDRESS = "10.42.0.1"  # NetworkManager's default shared-mode gateway


class HotspotUnavailable(Exception):
    pass


def new_credentials():
    ssid = "jarvis-" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    password = "".join(secrets.choice(ALPHABET) for _ in range(20))
    return ssid, password


def wifi_qr_payload(ssid: str, password: str) -> str:
    """Standard Wi-Fi QR format understood by Android and iOS cameras."""
    esc = lambda s: "".join("\\" + c if c in '\\;,:"' else c for c in s)
    return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"


def start(ssid: str, password: str, ifname: str = "wlan0", run=subprocess.run):
    if not sys.platform.startswith("linux"):
        raise HotspotUnavailable(__doc__)
    if shutil.which("nmcli") is None and run is subprocess.run:
        raise HotspotUnavailable("NetworkManager (nmcli) is required")
    # argument list, no shell: the password cannot inject commands
    run(["nmcli", "device", "wifi", "hotspot", "ifname", ifname, "con-name", "jarvis-hotspot",
         "ssid", ssid, "password", password], check=True, timeout=30)
    return HOTSPOT_ADDRESS


def stop(run=subprocess.run):
    run(["nmcli", "connection", "down", "jarvis-hotspot"], check=False, timeout=15)
