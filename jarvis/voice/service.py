"""The hub's voice service: audio in, a command (or an answer) out.

Rules this module enforces, in order:
  1. The microphone must be on: the hub's mode (operator) or this phone's own voice session. Otherwise audio is refused.
  2. Audio lives in memory only. It is never written to disk and never logged. The audit log records *that* a voice
     command ran and what it was, never what was said.
  3. In hands-free listening an utterance is ignored unless it starts with the wake word "Jarvis".
  4. Speech is turned into a command only by the strict parser (jarvis.voice.intents). Unknown speech does nothing.
  5. Voice goes through the same authorization as any other sender, and high-risk commands (lock) are never run
     silently: the operator must confirm them on the hub.
"""
import asyncio
import collections
import time
from concurrent.futures import ThreadPoolExecutor

from ..security import policy
from . import intents
from .engines import SAMPLE_RATE, EngineUnavailable, Vocab, load_engine

MAX_SECONDS = 15
MIN_SECONDS = 0.25
MAX_BYTES = MAX_SECONDS * SAMPLE_RATE * 2
MIN_BYTES = int(MIN_SECONDS * SAMPLE_RATE) * 2
RATE_LIMIT, RATE_WINDOW = 12, 60.0
DECODE_TIMEOUT = 20.0
MIN_RMS = 60                      # 16-bit units; below this is silence


class VoiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def rms(pcm: bytes) -> float:
    import array
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0
    return (sum(x * x for x in a) / len(a)) ** 0.5


def humanize(error: str, device: str = "") -> str:
    e = (error or "").lower()
    where = f" on {device}" if device else ""
    if "offline" in e:
        return f"{device or 'That device'} is offline."
    if "not enabled" in e:
        return f"{device or 'That device'} hasn't enabled that."
    if "not confirmed" in e:
        return "That wasn't confirmed."
    if e.startswith("denied"):
        return "That isn't allowed from here."
    if "timed out" in e:
        return f"{device or 'The device'} didn't answer."
    if "install one of" in e or "install" in e:
        return f"A tool for that is missing{where}."
    return f"That didn't work{where}."


class VoiceService:
    def __init__(self, hub, engine="auto", wake_required=True):
        self.hub = hub
        self._pref, self._engine, self._tried = engine, None, False
        self.wake_required = wake_required
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice")
        self.sessions: dict[str, float] = {}                      # phone device id -> expiry
        self._rate: dict[str, collections.deque] = {}

    # ---- engine ---------------------------------------------------------------
    async def engine(self):
        if not self._tried:
            self._tried = True
            try:
                self._engine = await asyncio.get_running_loop().run_in_executor(self._pool, load_engine, self._pref)
            except EngineUnavailable as e:
                self.error = str(e)
        return self._engine

    async def status(self) -> dict:
        eng = await self.engine()
        return {"available": eng is not None, "engine": getattr(eng, "name", None), "wake_required": self.wake_required,
                "max_seconds": MAX_SECONDS}

    # ---- microphone gating ---------------------------------------------------------
    def operator_source(self) -> dict:
        h = self.hub
        mode = h.current_mode()
        mic = policy.MODE_SENSORS.get(mode, set())
        allowed = "microphone" in mic and policy.sensor_allowed(h.hub_class, mode, "microphone")
        sender = {"id": "operator", "name": "operator", "can_send": ["*"], "revoked": False}
        return {"id": "operator", "name": "the hub", "mic": allowed, "sender": sender}

    def device_source(self, dev: dict) -> dict:
        on = self.sessions.get(dev["id"], 0) > time.time()
        allowed = on and policy.sensor_allowed(dev.get("class", "other"), "voice", "microphone")
        return {"id": dev["id"], "name": dev["name"], "mic": allowed, "sender": dev}

    def start_session(self, dev: dict, on: bool, renew: bool = False) -> dict:
        if on and renew and self.sessions.get(dev["id"], 0) <= time.time():
            raise VoiceError("the microphone session is not active", 409)      # renewals never revive a closed session
        if on:
            if not policy.sensor_allowed(dev.get("class", "other"), "voice", "microphone"):
                self.hub.audit.write("voice_refused", device=dev["id"])
                raise VoiceError("this device may not use the microphone", 403)
            fresh = self.sessions.get(dev["id"], 0) <= time.time()
            self.sessions[dev["id"]] = time.time() + policy.MODE_TTL
            if fresh:
                self.hub.audit.write("voice_session_on", device=dev["id"])
        elif self.sessions.pop(dev["id"], None):
            self.hub.audit.write("voice_session_off", device=dev["id"])
        left = max(0, int(self.sessions.get(dev["id"], 0) - time.time()))
        return {"on": left > 0, "expires_in": left}

    def end_sessions_for(self, device_id: str):
        self.sessions.pop(device_id, None)

    def end_all_sessions(self):
        for did in list(self.sessions):
            self.sessions.pop(did, None)
            self.hub.audit.write("voice_session_off", device=did, why="hub")

    # ---- the main entry point ---------------------------------------------------------
    def _check_rate(self, source_id):
        q = self._rate.setdefault(source_id, collections.deque())
        now = time.monotonic()
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise VoiceError("too many voice requests; wait a moment", 429)
        q.append(now)

    def _devices(self):
        return [{"id": d["id"], "name": d["name"]} for d in self.hub.registry.all()
                if not d["revoked"] and d.get("kind") != "web"]

    async def handle(self, source: dict, pcm: bytes, ptt: bool, target_hint: str | None = None) -> dict:
        h = self.hub
        if not source["mic"]:
            h.audit.write("voice_refused", source=source["id"], why="microphone is off")
            raise VoiceError("the microphone is off; start listening first", 403)
        if len(pcm) < MIN_BYTES or len(pcm) % 2:
            raise VoiceError("audio too short")
        if len(pcm) > MAX_BYTES:
            raise VoiceError(f"audio longer than {MAX_SECONDS} seconds", 413)
        self._check_rate(source["id"])
        engine = await self.engine()
        if engine is None:
            raise VoiceError("no speech engine is installed (pip install 'jarvis[voice]')", 503)
        if rms(pcm) < MIN_RMS:
            return {"ok": False, "ignored": True, "reply": ""}

        devices = self._devices()
        apps = dict(h.device_apps)
        vocab = Vocab(tuple(d["name"] for d in devices), tuple(sorted({a for v in apps.values() for a in v})))
        try:
            result = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(self._pool, engine.transcribe, pcm, vocab), DECODE_TIMEOUT)
        except asyncio.TimeoutError:
            raise VoiceError("speech recognition timed out", 504)
        finally:
            del pcm                                             # audio never leaves memory, and is dropped now
        if not result.accepted:
            h.audit.write("voice_rejected", source=source["id"])
            return {"ok": False, "ignored": True, "reply": "" if not ptt else "I didn't catch a command. Try again, starting with Jarvis."}

        intent = intents.parse(result.text, devices, apps)
        if self.wake_required and not ptt and not intent.wake:
            return {"ok": False, "ignored": True, "reply": ""}   # background speech: say nothing, keep nothing
        heard = result.text

        if intent.kind == "help":
            return {"ok": True, "heard": heard, "reply": intents.HELP_TEXT}
        if intent.kind in ("unknown", "invalid"):
            h.audit.write("voice_unknown", source=source["id"])
            return {"ok": False, "heard": heard, "reply": intent.reason if intent.kind == "invalid" and intent.reason
                    else "Sorry, I can't do that. Say help to hear what I can do."}
        if intent.kind == "devices":
            r = await h._route(source["sender"], "hub", "hub.devices", {})
            if not r["ok"]:
                return {"ok": False, "heard": heard, "reply": humanize(r.get("error"))}
            on = [d["name"] for d in r["data"] if d["online"]]
            off = [d["name"] for d in r["data"] if not d["online"]]
            say = ("Online: " + ", ".join(on) + "." if on else "Nothing is online.") + (" Offline: " + ", ".join(off) + "." if off else "")
            return {"ok": True, "heard": heard, "reply": say}

        # ---- a command: pick the device, then run it as this speaker ----
        target = self._pick_target(intent, target_hint, devices)
        if isinstance(target, str):
            return {"ok": False, "heard": heard, "reply": target}
        name = target["name"] if len(devices) > 1 else ""
        decision = policy.authorize(source["sender"], h.registry.get(target["id"]), intent.cmd)
        if decision.verdict == "confirm":                      # only when a confirmation will really be asked for
            asyncio.create_task(self._run(source, target, intent))
            return {"ok": True, "heard": heard, "pending": True,
                    "reply": "I've asked for confirmation on the hub."}
        r = await self._run(source, target, intent)
        if r["ok"]:
            return {"ok": True, "heard": heard, "reply": intent.say + (f" ({name})" if name else "")}
        return {"ok": False, "heard": heard, "reply": humanize(r.get("error"), target["name"])}

    def _pick_target(self, intent, hint, devices):
        by_id = {d["id"]: d for d in devices}
        if intent.device_id in by_id:
            return by_id[intent.device_id]
        if hint in by_id:
            return by_id[hint]
        online = [d for d in devices if d["id"] in self.hub.agents]
        if len(online) == 1:
            return online[0]
        if not online:
            return "No device is online."
        return "Which device? Say, for example, " + f"'on {online[0]['name']}'."

    async def _run(self, source, target, intent) -> dict:
        h = self.hub
        r = await h._route(source["sender"], target["id"], intent.cmd, intent.args)
        h.audit.write("voice_command", source=source["id"], cmd=intent.cmd, target=target["id"], ok=r["ok"])
        return r
