"""Who may send what to whom. Default is deny."""
from dataclasses import dataclass
from fnmatch import fnmatchcase

LOW, MEDIUM, HIGH, SESSION = "low", "medium", "high", "session"

# Every command the system knows. Anything absent is denied.
#   low/medium: allowed if roles and the target's own allow-list permit it
#   high:       the operator confirms every single use
#   session:    the operator approves once; that opens a short, revocable control window
CATALOG = {
    "ping": LOW,
    "system.info": LOW,
    "notify": LOW,
    "system.lock": HIGH,
    "hub.devices": LOW,
    "media.play_pause": LOW,
    "media.next": LOW,
    "media.previous": LOW,
    "volume.set": LOW,
    "volume.step": LOW,
    "volume.mute": LOW,
    "app.open": MEDIUM,
    "files.list": MEDIUM,
    "files.get": MEDIUM,
    "files.put": MEDIUM,
    "input.move": SESSION,
    "input.click": SESSION,
    "input.scroll": SESSION,
    "input.type": SESSION,
    "input.key": SESSION,
}

GRANT_TTL = 600  # seconds a control window stays open


def family(cmd: str) -> str:
    return cmd.split(".")[0]


_EVERYDAY = ["media.*", "volume.*", "app.*", "files.*", "input.*"]
ROLES = {
    # Full controller (your laptop/desktop): may send anything in the catalog.
    "controller": {"can_send": ["*"], "accepts": ["ping", "system.*", "notify", *_EVERYDAY]},
    # Phone: everyday remote control. Never locks, but may use the remote mouse/keyboard once you approve a window.
    "phone": {"can_send": ["ping", "system.info", "notify", "hub.devices", *_EVERYDAY], "accepts": ["ping", "notify"]},
    # Managed machine: only receives (and lists devices).
    "node": {"can_send": ["hub.devices"], "accepts": ["ping", "system.*", "notify", *_EVERYDAY]},
}

DEVICE_CLASSES = ("laptop", "desktop", "phone", "other")

# Sensors are opt-in per mode, and per device class.
MODE_SENSORS = {
    "idle": set(),
    "gesture": {"camera"},
    "hologram": {"camera"},
    "voice": {"microphone"},
    "gesture+voice": {"camera", "microphone"},
}
CLASS_SENSORS = {
    "laptop": {"camera", "microphone"},
    "desktop": {"camera", "microphone"},
    "phone": {"microphone"},  # phones: voice only, never the camera
    "other": set(),
}


def sensor_allowed(device_class: str, mode: str, sensor: str) -> bool:
    return sensor in MODE_SENSORS.get(mode, set()) and sensor in CLASS_SENSORS.get(device_class, set())


def _matches(patterns, cmd: str) -> bool:
    return any(fnmatchcase(cmd, p) for p in patterns)


@dataclass
class Decision:
    verdict: str  # allow | confirm | grant | deny
    reason: str = ""


def authorize(sender: dict, target: dict | None, cmd: str) -> Decision:
    """target is None for hub-local commands."""
    if sender.get("revoked"):
        return Decision("deny", "sender revoked")
    risk = CATALOG.get(cmd)
    if risk is None:
        return Decision("deny", "unknown command")
    if not _matches(sender.get("can_send", []), cmd):
        return Decision("deny", "sender not permitted to send this command")
    if target is not None:
        if target.get("revoked"):
            return Decision("deny", "target revoked")
        if not _matches(target.get("accepts", []), cmd):
            return Decision("deny", "target does not accept this command")
    if risk == HIGH:
        return Decision("confirm", "high-risk command")
    if risk == SESSION:
        return Decision("grant", "needs an approved control window")
    return Decision("allow")
