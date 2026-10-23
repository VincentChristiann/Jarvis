"""Who may send what to whom. Default is deny."""
from dataclasses import dataclass
from fnmatch import fnmatchcase

LOW, MEDIUM, HIGH = "low", "medium", "high"

# Every command the system knows. Anything absent is denied.
CATALOG = {
    "ping": LOW,
    "system.info": LOW,
    "notify": LOW,
    "system.lock": HIGH,
    "hub.devices": LOW,
}

ROLES = {
    # Full controller (your laptop/desktop): may send anything in the catalog, high risk still needs confirmation.
    "controller": {"can_send": ["*"], "accepts": ["ping", "system.info", "notify", "system.lock"]},
    # Phone: can issue everyday commands and receive notifications and pings, never locks or high risk.
    "phone": {"can_send": ["ping", "system.info", "notify", "hub.devices"], "accepts": ["ping", "notify"]},
    # Managed machine: only receives.
    "node": {"can_send": ["hub.devices"], "accepts": ["ping", "system.info", "notify", "system.lock"]},
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
    verdict: str  # allow | confirm | deny
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
    return Decision("allow")
