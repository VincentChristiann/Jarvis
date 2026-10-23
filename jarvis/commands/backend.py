"""Platform backends for media, volume and input. Commands are argument lists, never shell strings.

Linux: playerctl (media), wpctl/pactl/amixer (volume), xdotool (mouse/keyboard, X11 only).
Windows: the native SendInput / media-key API through ctypes.
"""
import re
import shutil
import subprocess
import sys


class BackendError(Exception):
    pass


MODIFIERS = ("ctrl", "alt", "shift", "super")
NAMED_KEYS = {
    "enter": "Return", "escape": "Escape", "tab": "Tab", "space": "space", "backspace": "BackSpace",
    "delete": "Delete", "left": "Left", "right": "Right", "up": "Up", "down": "Down",
    "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
}
_SIMPLE = re.compile(r"^[a-z0-9]$")
_FKEY = re.compile(r"^f([1-9]|1[0-2])$")


def parse_combo(combo: str) -> tuple[list[str], str]:
    """'ctrl+shift+t' -> (['ctrl','shift'], 't'). Strict allow-list; raises ValueError."""
    if not isinstance(combo, str) or len(combo) > 40:
        raise ValueError("bad key")
    parts = combo.lower().split("+")
    *mods, key = parts
    if len(set(mods)) != len(mods) or any(m not in MODIFIERS for m in mods):
        raise ValueError("unknown modifier")
    if not (_SIMPLE.match(key) or _FKEY.match(key) or key in NAMED_KEYS):
        raise ValueError("unknown key")
    return mods, key


def clean_text(text) -> str:
    if not isinstance(text, str) or not (0 < len(text) <= 200):
        raise ValueError("text must be 1-200 characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError("control characters not allowed; use input.key for Enter/Tab")
    return text


class LinuxBackend:
    def __init__(self, run=subprocess.run, which=shutil.which):
        self._run, self._which = run, which

    def _need(self, *names):
        for n in names:
            if self._which(n):
                return n
        raise BackendError(f"install one of: {', '.join(names)}")

    def _do(self, argv):
        self._run(argv, check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def media(self, action: str):  # play-pause | next | previous
        self._need("playerctl")
        self._do(["playerctl", action])

    def volume_set(self, pct: int):
        tool = self._need("wpctl", "pactl", "amixer")
        self._do({"wpctl": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{pct / 100:.2f}"],
                  "pactl": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"],
                  "amixer": ["amixer", "-q", "sset", "Master", f"{pct}%"]}[tool])

    def volume_step(self, delta: int):
        tool = self._need("wpctl", "pactl", "amixer")
        n = abs(delta)
        self._do({"wpctl": ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{n}%{'+' if delta > 0 else '-'}"],
                  "pactl": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{'+' if delta > 0 else '-'}{n}%"],
                  "amixer": ["amixer", "-q", "sset", "Master", f"{n}%{'+' if delta > 0 else '-'}"]}[tool])

    def volume_mute(self):
        tool = self._need("wpctl", "pactl", "amixer")
        self._do({"wpctl": ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"],
                  "pactl": ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
                  "amixer": ["amixer", "-q", "sset", "Master", "toggle"]}[tool])

    def mouse_move(self, dx: int, dy: int):
        self._need("xdotool")
        self._do(["xdotool", "mousemove_relative", "--", str(dx), str(dy)])

    def click(self, button: str, count: int):
        self._need("xdotool")
        self._do(["xdotool", "click", "--repeat", str(count), {"left": "1", "middle": "2", "right": "3"}[button]])

    def scroll(self, amount: int):  # positive = up
        self._need("xdotool")
        self._do(["xdotool", "click", "--repeat", str(abs(amount)), "4" if amount > 0 else "5"])

    def type_text(self, text: str):
        self._need("xdotool")
        self._do(["xdotool", "type", "--delay", "8", "--", text])

    def key(self, combo: str):
        mods, key = parse_combo(combo)
        self._need("xdotool")
        name = NAMED_KEYS.get(key, key)
        self._do(["xdotool", "key", "--", "+".join([*mods, name])])


class WindowsBackend:
    """SendInput based. Written against the Win32 API; not exercised by the automated tests."""

    VK = {"enter": 0x0D, "escape": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E,
          "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "home": 0x24, "end": 0x23,
          "pageup": 0x21, "pagedown": 0x22}
    MOD_VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "super": 0x5B}
    MEDIA = {"play-pause": 0xB3, "next": 0xB0, "previous": 0xB1}
    VOL_MUTE, VOL_DOWN, VOL_UP = 0xAD, 0xAE, 0xAF

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self.MOUSEINPUT, self.KEYBDINPUT, self.INPUT, self.U = MOUSEINPUT, KEYBDINPUT, INPUT, _U

    def _send(self, *inputs):
        arr = (self.INPUT * len(inputs))(*inputs)
        if self.ct.windll.user32.SendInput(len(inputs), arr, self.ct.sizeof(self.INPUT)) != len(inputs):
            raise BackendError("SendInput was blocked (is a secure desktop or elevated window active?)")

    def _mouse(self, flags, dx=0, dy=0, data=0):
        i = self.INPUT(type=0)
        i.u.mi = self.MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
        return i

    def _kbd(self, vk=0, flags=0, scan=0):
        i = self.INPUT(type=1)
        i.u.ki = self.KEYBDINPUT(vk, scan, flags, 0, 0)
        return i

    def _tap_vk(self, vk):
        self._send(self._kbd(vk), self._kbd(vk, 0x0002))

    def media(self, action):
        self._tap_vk(self.MEDIA[action])

    def volume_set(self, pct):
        # Windows has no absolute-volume key: go to zero, then step up (each step is 2%).
        for _ in range(50):
            self._tap_vk(self.VOL_DOWN)
        for _ in range(round(pct / 2)):
            self._tap_vk(self.VOL_UP)

    def volume_step(self, delta):
        for _ in range(max(1, abs(delta) // 2)):
            self._tap_vk(self.VOL_UP if delta > 0 else self.VOL_DOWN)

    def volume_mute(self):
        self._tap_vk(self.VOL_MUTE)

    def mouse_move(self, dx, dy):
        self._send(self._mouse(0x0001, dx, dy))

    def click(self, button, count):
        down, up = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}[button]
        for _ in range(count):
            self._send(self._mouse(down), self._mouse(up))

    def scroll(self, amount):
        self._send(self._mouse(0x0800, data=amount * 120))

    def type_text(self, text):
        events = []
        for ch in text:
            code = ord(ch)
            units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
            for u in units:
                events += [self._kbd(0, 0x0004, u), self._kbd(0, 0x0004 | 0x0002, u)]
        self._send(*events)

    def key(self, combo):
        mods, key = parse_combo(combo)
        vk = self.VK.get(key) or (0x70 + int(key[1:]) - 1 if _FKEY.match(key) else ord(key.upper()))
        downs = [self._kbd(self.MOD_VK[m]) for m in mods] + [self._kbd(vk)]
        ups = [self._kbd(vk, 0x0002)] + [self._kbd(self.MOD_VK[m], 0x0002) for m in reversed(mods)]
        self._send(*downs, *ups)


def get_backend():
    if sys.platform.startswith("linux"):
        return LinuxBackend()
    if sys.platform == "win32":
        return WindowsBackend()
    raise BackendError("this platform is not supported yet")
