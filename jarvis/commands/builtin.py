"""Command handlers a device can run. No shell is ever used; arguments are validated."""
import platform
import shutil
import subprocess
import sys
import time


def ping(args):
    return {"pong": time.time()}


def system_info(args):
    return {"hostname": platform.node(), "os": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "python": platform.python_version()}


def notify(args):
    title, message = args.get("title", "Jarvis"), args.get("message", "")
    if not isinstance(title, str) or not isinstance(message, str) or len(title) > 100 or len(message) > 500:
        raise ValueError("title/message must be short strings")
    delivered = False
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        delivered = subprocess.run(["notify-send", "--", title, message], timeout=5).returncode == 0
    return {"delivered": delivered}


def system_lock(args):
    if sys.platform.startswith("linux"):
        cmd = ["loginctl", "lock-session"]
    elif sys.platform == "win32":
        cmd = ["rundll32.exe", "user32.dll,LockWorkStation"]
    else:
        raise RuntimeError("lock not supported on this platform")
    subprocess.run(cmd, timeout=10, check=True)
    return {"locked": True}


HANDLERS = {"ping": ping, "system.info": system_info, "notify": notify, "system.lock": system_lock}

# Enabled with no extra setup. Everything else (e.g. system.lock) must be turned on by the device owner.
DEFAULT_LOCAL_ACCEPT = ["ping", "system.info", "notify"]
