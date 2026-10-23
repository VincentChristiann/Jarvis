"""Turn spoken words into Jarvis commands. Offline, strict, and deterministic.

The same phrase table drives the parser and the recognizer's grammar (`jsgf`), so they cannot drift apart.
Anything that does not match a known phrase exactly is "unknown": nothing is guessed, nothing is run.
"""
import re
from dataclasses import dataclass, field

WAKE_WORDS = ("hey jarvis", "ok jarvis", "okay jarvis", "jarvis")
POLITE_PREFIX = ("please", "can you", "could you", "would you", "will you")
POLITE_SUFFIX = ("please",)


@dataclass(frozen=True)
class Spec:
    cmd: str
    args: tuple = ()          # tuple of (key, value) so the spec is hashable
    phrases: tuple = ()
    say: str = ""             # what to say back on success


SPECS = (
    Spec("media.play_pause", (), ("play", "pause", "resume", "play pause", "play the music", "pause the music",
                                   "stop the music", "resume the music", "play music", "pause music"), "Okay."),
    Spec("media.next", (), ("next", "next track", "next song", "skip", "skip track", "skip song", "skip this song",
                             "skip this track"), "Next track."),
    Spec("media.previous", (), ("previous", "previous track", "previous song", "last track", "last song",
                                 "go back a track"), "Previous track."),
    Spec("volume.step", (("delta", 10),), ("volume up", "turn it up", "louder", "turn the volume up", "raise the volume",
                                            "increase the volume", "turn up the volume"), "Volume up."),
    Spec("volume.step", (("delta", -10),), ("volume down", "turn it down", "quieter", "turn the volume down",
                                             "lower the volume", "decrease the volume", "turn down the volume"), "Volume down."),
    Spec("volume.mute", (), ("mute", "unmute", "mute the sound", "mute the volume", "toggle mute"), "Okay."),
    Spec("system.lock", (), ("lock the computer", "lock the screen", "lock it", "lock this computer", "lock my computer",
                              "lock the pc", "lock the laptop"), "Locking."),
    Spec("ping", (), ("ping",), "Pong."),
    Spec("hub.devices", (), ("list devices", "show devices", "what devices are online", "which devices are online",
                              "what devices do i have", "list my devices"), ""),
)
HELP_PHRASES = ("help", "what can you do", "what can i say", "what commands are there")
OPEN_VERBS = ("open", "launch", "start")
VOLUME_SET = re.compile(r"^(?:set )?(?:the )?volume(?: to| at)? (\d{1,4})(?: percent)?$")

# ---- numbers ---------------------------------------------------------------------------
_ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}


def fold_numbers(tokens: list[str]) -> list[str]:
    """['set','volume','to','forty','five'] -> ['set','volume','to','45']; 'one hundred' -> '100'."""
    out, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in _TENS:
            val = _TENS[t]
            if i + 1 < len(tokens) and tokens[i + 1] in _ONES and 1 <= _ONES[tokens[i + 1]] <= 9:
                val += _ONES[tokens[i + 1]]; i += 1
            out.append(str(val))
        elif t in _ONES:
            if t == "one" and i + 1 < len(tokens) and tokens[i + 1] == "hundred":
                out.append("100"); i += 1
            else:
                out.append(str(_ONES[t]))
        else:
            out.append(t)
        i += 1
    return out


def normalize(text: str) -> str:
    text = text.lower().replace("%", " percent ")
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text).split())


def _name_key(name: str) -> str:
    return normalize(name.replace("-", " ").replace("_", " "))


# ---- parsing ------------------------------------------------------------------------------
@dataclass
class Intent:
    kind: str                                  # command | devices | help | unknown | invalid
    cmd: str = ""
    args: dict = field(default_factory=dict)
    say: str = ""
    device_id: str | None = None               # spoken target, if any
    device_name: str = ""
    wake: bool = False
    reason: str = ""


def _strip_edges(s: str, prefixes, suffixes) -> str:
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            if s.startswith(p + " "):
                s, changed = s[len(p) + 1:], True
        for p in suffixes:
            if s.endswith(" " + p):
                s, changed = s[: -len(p) - 1], True
    return s


def parse(text: str, devices=(), apps=None) -> Intent:
    """devices: [{"id","name"}]; apps: {device_id: [names]} (names the owner configured on each device)."""
    s = normalize(text)
    wake = False
    for w in WAKE_WORDS:                       # longest first
        if s == w or s.startswith(w + " "):
            wake, s = True, s[len(w):].strip()
            break
    s = _strip_edges(s, POLITE_PREFIX, POLITE_SUFFIX)
    s = " ".join(fold_numbers(s.split()))
    if not s:
        return Intent("unknown", wake=wake, reason="empty")

    # trailing "on <device>"
    dev = None
    for d in sorted(devices, key=lambda d: -len(_name_key(d["name"]))):
        k = _name_key(d["name"])
        m = re.search(rf"\b(?:on|for|to) (?:the |my )?{re.escape(k)}$", s) if k else None
        if m:
            dev, s = d, s[: m.start()].strip()
            break

    def done(i: Intent) -> Intent:
        i.wake = wake
        if dev:
            i.device_id, i.device_name = dev["id"], dev["name"]
        return i

    if s in HELP_PHRASES:
        return done(Intent("help"))
    m = VOLUME_SET.match(s)
    if m:
        n = int(m.group(1))
        if n > 100:
            return done(Intent("invalid", reason="Volume must be between 0 and 100."))
        return done(Intent("command", "volume.set", {"percent": n}, f"Volume {n} percent."))
    for spec in SPECS:
        if s in spec.phrases:
            if spec.cmd == "hub.devices":
                return done(Intent("devices"))
            return done(Intent("command", spec.cmd, dict(spec.args), spec.say))
    m = re.match(rf"^(?:{'|'.join(OPEN_VERBS)}) (?:the )?([a-z0-9 ]+)$", s)
    if m:
        spoken = m.group(1).strip()
        known = {}
        for did, names in (apps or {}).items():
            if dev and did != dev["id"]:
                continue
            for n in names:
                known.setdefault(_name_key(n), (n, did))
        hit = known.get(spoken)
        if hit:
            i = Intent("command", "app.open", {"name": hit[0]}, f"Opening {spoken}.")
            if not dev:                         # the app's owner is the target
                d = next((d for d in devices if d["id"] == hit[1]), None)
                if d:
                    dev = d
            return done(i)
        return done(Intent("invalid", reason=f"I don't know an app called {spoken}."))
    return done(Intent("unknown", reason="no matching command"))


# ---- vocabulary for the recognizer ----------------------------------------------------------------
HELP_TEXT = ("You can say: play, pause, next track, previous track, volume up, volume down, mute, "
             "set volume to fifty, lock the computer, open an app, or list devices. "
             "Add 'on' and a device name to pick a device.")


def jsgf(device_names=(), app_names=(), known_word=lambda w: True) -> str:
    """Build the recognizer grammar from the same phrase table. Words the recognizer cannot pronounce are dropped."""
    def ok(phrase: str) -> bool:
        return bool(phrase.split()) and all(known_word(w) for w in phrase.split())

    cmds = [p for spec in SPECS for p in spec.phrases] + list(HELP_PHRASES)
    alts = " | ".join(p for p in dict.fromkeys(cmds) if ok(p))
    verbs = "|".join(OPEN_VERBS)
    apps = [normalize(a.replace("-", " ").replace("_", " ")) for a in app_names]
    apps = [a for a in dict.fromkeys(apps) if ok(a)]
    names = [normalize(n.replace("-", " ").replace("_", " ")) for n in device_names]
    names = [n for n in dict.fromkeys(names) if ok(n)]
    lines = ["#JSGF V1.0;", "grammar jarvis;",
             "<ones> = one | two | three | four | five | six | seven | eight | nine;",
             "<teen> = ten | eleven | twelve | thirteen | fourteen | fifteen | sixteen | seventeen | eighteen | nineteen;",
             "<tens> = twenty | thirty | forty | fifty | sixty | seventy | eighty | ninety;",
             "<num> = zero | <ones> | <teen> | <tens> [<ones>] | one hundred;",
             "<setvol> = [set] [the] volume [to | at] <num> [percent];",
             f"<cmd> = {alts} | <setvol>" + (f" | ({verbs}) [the] ({' | '.join(apps)})" if apps else "") + ";"]
    target = f" [(on | for | to) [the | my] ({' | '.join(names)})]" if names else ""
    lines.append(f"public <utt> = [hey | ok | okay] [jarvis] [please | can you | could you] <cmd>{target} [please];")
    return "\n".join(lines)


def prompt_hint(device_names=(), app_names=()) -> str:
    """Context for free-form (Whisper) recognition so it prefers our words."""
    bits = ["Jarvis, play, pause, next track, previous track, volume up, volume down, mute, lock the computer, set volume to fifty percent."]
    if device_names:
        bits.append("Devices: " + ", ".join(device_names) + ".")
    if app_names:
        bits.append("Apps: " + ", ".join(app_names) + ".")
    return " ".join(bits)
