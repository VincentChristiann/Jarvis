"""Command handlers a device can run. No shell is ever used; every argument is validated.

Each handler is `handler(args, ctx)`. `ctx` carries the owner's local config and the platform backend.
"""
import base64
import binascii
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .backend import clean_text, get_backend, parse_combo

MAX_FILE = 4 * 1024 * 1024
MAX_LIST = 500


@dataclass
class Context:
    config: dict
    backend: object = None
    _backend_factory: object = field(default=get_backend, repr=False)

    @property
    def be(self):
        if self.backend is None:
            self.backend = self._backend_factory()
        return self.backend

    @property
    def share_dir(self) -> Path:
        return Path(self.config.get("share_dir") or Path.home() / "Jarvis-Share")


# ---- argument helpers -------------------------------------------------------
def _int(args, name, lo, hi, default=None):
    v = args.get(name, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{name} must be an integer {lo}..{hi}")
    return v


def _choice(args, name, options, default=None):
    v = args.get(name, default)
    if v not in options:
        raise ValueError(f"{name} must be one of {sorted(options)}")
    return v


# ---- basics -----------------------------------------------------------------
def ping(args, ctx):
    return {"pong": time.time()}


def system_info(args, ctx):
    return {"hostname": platform.node(), "os": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "python": platform.python_version()}


def notify(args, ctx):
    title, message = args.get("title", "Jarvis"), args.get("message", "")
    if not isinstance(title, str) or not isinstance(message, str) or len(title) > 100 or len(message) > 500:
        raise ValueError("title/message must be short strings")
    delivered = False
    if sys.platform.startswith("linux") and shutil.which("notify-send"):
        delivered = subprocess.run(["notify-send", "--", title, message], timeout=5).returncode == 0
    return {"delivered": delivered}


def system_lock(args, ctx):
    if sys.platform.startswith("linux"):
        cmd = ["loginctl", "lock-session"]
    elif sys.platform == "win32":
        cmd = ["rundll32.exe", "user32.dll,LockWorkStation"]
    else:
        raise RuntimeError("lock not supported on this platform")
    subprocess.run(cmd, timeout=10, check=True)
    return {"locked": True}


# ---- media and volume -------------------------------------------------------
def media_play_pause(args, ctx):
    ctx.be.media("play-pause")
    return {}


def media_next(args, ctx):
    ctx.be.media("next")
    return {}


def media_previous(args, ctx):
    ctx.be.media("previous")
    return {}


def volume_set(args, ctx):
    ctx.be.volume_set(_int(args, "percent", 0, 100))
    return {}


def volume_step(args, ctx):
    ctx.be.volume_step(_int(args, "delta", -20, 20))
    return {}


def volume_mute(args, ctx):
    ctx.be.volume_mute()
    return {}


# ---- apps (only names the device owner configured) ---------------------------
def app_open(args, ctx):
    name = args.get("name")
    argv = (ctx.config.get("apps") or {}).get(name) if isinstance(name, str) else None
    if not argv or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError("unknown app; the device owner must add it with `jarvis device app add`")
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return {"started": name}


# ---- files (confined to the share folder) -----------------------------------
def _safe_path(ctx, rel, must_exist=True) -> Path:
    if not isinstance(rel, str) or len(rel) > 300 or "\x00" in rel:
        raise ValueError("bad path")
    p = Path(rel)
    if p.is_absolute() or p.drive or ".." in p.parts:
        raise ValueError("path must be relative and stay inside the share folder")
    root = ctx.share_dir.resolve()
    full = (root / p).resolve()
    if full != root and root not in full.parents:
        raise ValueError("path escapes the share folder")  # also catches symlinks pointing outside
    if must_exist and not full.exists():
        raise FileNotFoundError("no such file")
    return full


def files_list(args, ctx):
    ctx.share_dir.mkdir(parents=True, exist_ok=True)
    d = _safe_path(ctx, args.get("path", ""))
    if not d.is_dir():
        raise ValueError("not a folder")
    out = []
    for e in sorted(d.iterdir(), key=lambda x: x.name.lower())[:MAX_LIST]:
        st = e.stat()
        out.append({"name": e.name, "dir": e.is_dir(), "size": 0 if e.is_dir() else st.st_size})
    return {"entries": out}


def files_get(args, ctx):
    f = _safe_path(ctx, args.get("path"))
    if not f.is_file():
        raise ValueError("not a file")
    if f.stat().st_size > MAX_FILE:
        raise ValueError("file too large (4 MB limit)")
    return {"name": f.name, "size": f.stat().st_size, "b64": base64.b64encode(f.read_bytes()).decode()}


def files_put(args, ctx):
    ctx.share_dir.mkdir(parents=True, exist_ok=True)
    f = _safe_path(ctx, args.get("path"), must_exist=False)
    b64 = args.get("b64")
    if not isinstance(b64, str) or len(b64) > MAX_FILE * 4 // 3 + 8:
        raise ValueError("file too large (4 MB limit)")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid file data")
    if len(data) > MAX_FILE:
        raise ValueError("file too large (4 MB limit)")
    if f.exists() and not args.get("overwrite"):
        raise ValueError("file exists; pass overwrite=true to replace it")
    if f.is_dir():
        raise ValueError("path is a folder")
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, f)
    return {"saved": f.name, "size": len(data)}


# ---- remote input (needs an approved control window on the hub) --------------
def input_move(args, ctx):
    ctx.be.mouse_move(_int(args, "dx", -2000, 2000), _int(args, "dy", -2000, 2000))
    return {}


def input_click(args, ctx):
    ctx.be.click(_choice(args, "button", {"left", "right", "middle"}, "left"), _int(args, "count", 1, 2, 1))
    return {}


def input_scroll(args, ctx):
    ctx.be.scroll(_int(args, "amount", -20, 20))
    return {}


def input_type(args, ctx):
    ctx.be.type_text(clean_text(args.get("text")))
    return {}


def input_key(args, ctx):
    combo = args.get("key")
    parse_combo(combo)
    ctx.be.key(combo)
    return {}


HANDLERS = {
    "ping": ping, "system.info": system_info, "notify": notify, "system.lock": system_lock,
    "media.play_pause": media_play_pause, "media.next": media_next, "media.previous": media_previous,
    "volume.set": volume_set, "volume.step": volume_step, "volume.mute": volume_mute,
    "app.open": app_open, "files.list": files_list, "files.get": files_get, "files.put": files_put,
    "input.move": input_move, "input.click": input_click, "input.scroll": input_scroll,
    "input.type": input_type, "input.key": input_key,
}

# Enabled on a freshly paired device. Everything else (apps, files, remote input, lock) is opt-in by the owner.
DEFAULT_LOCAL_ACCEPT = ["ping", "system.info", "notify", "media.*", "volume.*"]
