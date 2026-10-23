import asyncio
import math
import struct
import time

import aiohttp
import pytest

from jarvis.commands.builtin import Context
from jarvis.device.agent import Agent
from jarvis.voice.engines import Result
from jarvis.voice.service import MAX_BYTES, RATE_LIMIT

from .conftest import pair_device
from .test_commands import FakeBackend, wait_online
from .test_mobile import H, do_login, pair_browser, session, url

SECRET_WORDS = "pineapple confidential merger"


def tone(seconds=1.0, amp=3000):
    n = int(16000 * seconds)
    return b"".join(struct.pack("<h", int(amp * math.sin(i / 7))) for i in range(n))


class ScriptedEngine:
    name = "scripted"

    def __init__(self):
        self.queue, self.calls, self.vocab = [], 0, None

    def say(self, text, accepted=True):
        self.queue.append(Result(text, accepted))

    def transcribe(self, pcm, vocab):
        self.calls += 1
        self.vocab = vocab
        return self.queue.pop(0) if self.queue else Result("", False, 0, "nothing")


@pytest.fixture
def engine(hub):
    e = ScriptedEngine()
    hub.voice._engine, hub.voice._tried = e, True
    return e


def aurl(hub, path):
    return f"http://127.0.0.1:{hub.ports['admin']}{path}"


async def admin_session(hub):
    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    code = (await (await s.post(aurl(hub, "/admin/login-code"), headers={"Authorization": f"Bearer {hub.admin_token}"})).json())["code"]
    await s.get(aurl(hub, f"/login?code={code}"), allow_redirects=False)
    return s


async def speak(s, hub, ptt=True, target=None, audio=None, path="/admin/voice", base=aurl):
    headers = {**H, "Content-Type": "application/octet-stream", "X-Jarvis-PTT": "1" if ptt else "0"}
    if target:
        headers["X-Jarvis-Target"] = target
    r = await s.post(base(hub, path), data=audio if audio is not None else tone(), headers=headers)
    return r.status, await r.json()


async def online_node(hub, tmp_path, name="pc", apps=None, accept=("media.*", "volume.*", "app.*", "system.lock", "ping")):
    ident = await pair_device(hub, tmp_path, name, "node", "desktop", accept=list(accept))
    if apps:
        ident.config["apps"] = apps
        ident.save_config()
    fake = FakeBackend()
    task = asyncio.create_task(Agent(ident, Context(config=ident.config, backend=fake)).run())
    await wait_online(hub, ident)
    return ident, fake, task


# ---- the microphone gate -----------------------------------------------------------------
async def test_audio_is_refused_when_the_microphone_is_off(hub, engine):
    async with await admin_session(hub) as s:
        status, body = await speak(s, hub)
        assert status == 403 and "microphone is off" in body["error"]
        await s.post(aurl(hub, "/admin/mode"), json={"mode": "hologram"}, headers=H)       # camera only: still no microphone
        assert (await speak(s, hub))[0] == 403
    assert engine.calls == 0, "audio must not even be decoded while the microphone is off"


async def test_operator_voice_mode_runs_a_command(hub, engine, tmp_path):
    node, fake, task = await online_node(hub, tmp_path)
    try:
        async with await admin_session(hub) as s:
            await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
            engine.say("jarvis next track")
            status, body = await speak(s, hub, ptt=False)
            assert status == 200 and body["ok"] and body["reply"] == "Next track."
            assert fake.calls == [("media", "next")]
    finally:
        task.cancel()


async def test_voice_mode_refused_on_a_hub_without_a_microphone(tmp_path):
    from jarvis.hub.server import Hub, init_hub
    init_hub(tmp_path / "h")
    hub = Hub(tmp_path / "h", host="127.0.0.1", pair_port=0, ctl_port=0, admin_port=0, mobile_port=0,
              advertise=["127.0.0.1"], hub_class="other")
    await hub.start()
    try:
        async with await admin_session(hub) as s:
            r = await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
            assert r.status == 403
            assert (await speak(s, hub))[0] == 403
    finally:
        await hub.stop()


# ---- wake word, rejection, unknown speech ---------------------------------------------------------
async def test_hands_free_needs_the_wake_word_push_to_talk_does_not(hub, engine, tmp_path):
    node, fake, task = await online_node(hub, tmp_path)
    try:
        async with await admin_session(hub) as s:
            await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
            engine.say("next track")                                      # overheard, no wake word
            status, body = await speak(s, hub, ptt=False)
            assert body.get("ignored") and fake.calls == [] and body["reply"] == ""
            engine.say("next track")                                      # same words, but button held
            assert (await speak(s, hub, ptt=True))[1]["ok"] and fake.calls == [("media", "next")]
    finally:
        task.cancel()


async def test_rejected_and_unknown_speech_do_nothing_and_leave_no_transcript(hub, engine, tmp_path):
    node, fake, task = await online_node(hub, tmp_path)
    try:
        async with await admin_session(hub) as s:
            await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
            engine.say("", accepted=False)
            assert (await speak(s, hub, ptt=True))[1]["ignored"]
            engine.say(f"jarvis {SECRET_WORDS}")
            body = (await speak(s, hub, ptt=False))[1]
            assert not body["ok"] and "can't do that" in body["reply"]
            engine.say("jarvis play")
            assert (await speak(s, hub, ptt=False))[1]["ok"]
        assert fake.calls == [("media", "play-pause")]
        log = (hub.dir / "audit.log").read_text()
        assert "voice_unknown" in log and "voice_command" in log
        for word in SECRET_WORDS.split() + ["next track", "jarvis"]:
            assert word not in log, f"spoken words leaked into the audit log: {word!r}"
    finally:
        task.cancel()


async def test_audio_is_never_written_to_disk(hub, engine, tmp_path):
    node, fake, task = await online_node(hub, tmp_path)
    try:
        before = {p: p.stat().st_size for p in tmp_path.rglob("*") if p.is_file() and p.name != "audit.log"}
        async with await admin_session(hub) as s:
            await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
            engine.say("jarvis ping")
            await speak(s, hub, ptt=False, audio=tone(2))
        after = {p: p.stat().st_size for p in tmp_path.rglob("*") if p.is_file() and p.name != "audit.log"}
        assert set(after) - set(before) <= {p for p in after if p.name == "gestures.json"}
        assert not [p for p in after if p.suffix in (".wav", ".raw", ".pcm", ".mp3", ".ogg")]
    finally:
        task.cancel()


# ---- limits ---------------------------------------------------------------------------------------------
async def test_audio_limits_and_silence(hub, engine):
    async with await admin_session(hub) as s:
        await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
        assert (await speak(s, hub, audio=b"\x01\x00" * 100))[0] == 400            # too short
        assert (await speak(s, hub, audio=tone(0.5) + b"x"))[0] == 400              # odd length
        assert (await speak(s, hub, audio=b"\x00" * (MAX_BYTES + 2)))[0] == 413     # too long (refused before buffering it all)
        status, body = await speak(s, hub, audio=b"\x00\x00" * 16000)               # silence
        assert status == 200 and body["ignored"]
        assert engine.calls == 0


async def test_rate_limit(hub, engine):
    async with await admin_session(hub) as s:
        await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
        codes = [(await speak(s, hub))[0] for _ in range(RATE_LIMIT + 2)]
        assert codes[:RATE_LIMIT] == [200] * RATE_LIMIT and codes[RATE_LIMIT:] == [429, 429]


async def test_admin_voice_requires_login_and_header(hub, engine):
    async with aiohttp.ClientSession() as anon:
        r = await anon.post(aurl(hub, "/admin/voice"), data=tone(), headers={**H, "Content-Type": "application/octet-stream"})
        assert r.status == 401
    async with await admin_session(hub) as s:
        r = await s.post(aurl(hub, "/admin/voice"), data=tone())                      # no CSRF header
        assert r.status == 403


# ---- choosing the device ------------------------------------------------------------------------------------
async def test_device_targeting_by_name_hint_and_ambiguity(hub, engine, tmp_path):
    a, fa, ta = await online_node(hub, tmp_path, "workstation")
    b, fb, tb = await online_node(hub, tmp_path, "laptop")
    try:
        async with await admin_session(hub) as s:
            await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
            engine.say("jarvis volume up")                                              # two devices, nothing chosen
            body = (await speak(s, hub, ptt=False))[1]
            assert not body["ok"] and "Which device" in body["reply"] and not fa.calls and not fb.calls
            engine.say("jarvis volume up on the laptop")
            assert (await speak(s, hub, ptt=False))[1]["ok"] and fb.calls == [("volume_step", 10)] and not fa.calls
            engine.say("jarvis mute")                                                   # UI hint selects the device
            assert (await speak(s, hub, ptt=False, target=a.device_id))[1]["ok"] and fa.calls == [("volume_mute",)]
            assert {"workstation", "laptop"} <= set(engine.vocab.device_names)           # recogniser is told the names
    finally:
        ta.cancel(); tb.cancel()


async def test_apps_reported_by_the_device_are_understood(hub, engine, tmp_path):
    node, fake, task = await online_node(hub, tmp_path, apps={"firefox": ["true"]})
    try:
        for _ in range(100):
            if hub.device_apps.get(node.device_id):
                break
            await asyncio.sleep(0.05)
        assert hub.device_apps[node.device_id] == ["firefox"]
        async with await admin_session(hub) as s:
            await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
            engine.say("jarvis open firefox")
            body = (await speak(s, hub, ptt=False))[1]
            assert body["ok"] and body["reply"].startswith("Opening firefox")
            assert "firefox" in engine.vocab.app_names
            engine.say("jarvis open chrome")
            assert not (await speak(s, hub, ptt=False))[1]["ok"]
    finally:
        task.cancel()


async def test_agent_cannot_register_bogus_app_names(hub, tmp_path):
    node = await pair_device(hub, tmp_path, "pc", "node", "desktop")
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f"wss://127.0.0.1:{hub.ports['ctl']}/ws?kind=agent", ssl=node.ssl_context())
        await ws.send_str('{"type":"hello","apps":["ok-app","bad name; rm -rf /"]}')
        await asyncio.sleep(0.2)
        assert node.device_id not in hub.device_apps
        await ws.send_str('{"type":"hello","apps":["ok-app"]}')
        await asyncio.sleep(0.2)
        assert hub.device_apps[node.device_id] == ["ok-app"]
        await ws.close()


# ---- high risk is never silent ------------------------------------------------------------------------------------
async def test_lock_by_voice_waits_for_confirmation(hub, engine, tmp_path):
    node, fake, task = await online_node(hub, tmp_path)
    try:
        async with await admin_session(hub) as s:
            await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)
            engine.say("jarvis lock the computer")
            t0 = time.monotonic()
            status, body = await speak(s, hub, ptt=False)
            assert body["pending"] and "confirmation" in body["reply"] and time.monotonic() - t0 < 2   # does not hang on the prompt
            for _ in range(100):
                if hub.confirmations:
                    break
                await asyncio.sleep(0.02)
            (c,) = hub.confirmations.values()
            assert c["cmd"] == "system.lock"
            c["approved"] = False                                                                       # the operator says no
            c["event"].set()
            await asyncio.sleep(0.3)
        log = (hub.dir / "audit.log").read_text()
        assert "not confirmed" in log
        assert '"cmd": "system.lock", "event": "cmd_allowed"' not in log
    finally:
        task.cancel()


# ---- phones --------------------------------------------------------------------------------------------------------------------
async def test_phone_voice_session_gating_and_use(hub, engine, tmp_path):
    node, fake, task = await online_node(hub, tmp_path)
    try:
        async with session(hub) as s:
            key, _, body = await pair_browser(hub, s)
            await do_login(hub, s, key, body["device_id"])
            audio_h = {**H, "Content-Type": "application/octet-stream", "X-Jarvis-PTT": "1"}
            r = await s.post(url(hub, "/m/api/voice"), data=tone(), headers=audio_h)
            assert r.status == 403 and engine.calls == 0                                   # no session yet
            r = await s.post(url(hub, "/m/api/voice/session"), json={"on": True}, headers=H)
            assert (await r.json())["on"] is True
            engine.say("pause")
            r = await s.post(url(hub, "/m/api/voice"), data=tone(), headers=audio_h)
            assert r.status == 200 and (await r.json())["ok"] and fake.calls == [("media", "play-pause")]
            await s.post(url(hub, "/m/api/voice/session"), json={"on": False}, headers=H)
            assert (await s.post(url(hub, "/m/api/voice"), data=tone(), headers=audio_h)).status == 403
            events = (hub.dir / "audit.log").read_text()
            assert "voice_session_on" in events and "voice_session_off" in events
    finally:
        task.cancel()


async def test_phone_voice_obeys_phone_permissions(hub, engine, tmp_path):
    node, fake, task = await online_node(hub, tmp_path)
    try:
        async with session(hub) as s:
            key, _, body = await pair_browser(hub, s)
            await do_login(hub, s, key, body["device_id"])
            await s.post(url(hub, "/m/api/voice/session"), json={"on": True}, headers=H)
            h = {**H, "Content-Type": "application/octet-stream", "X-Jarvis-PTT": "1"}
            engine.say("lock the computer")                  # the phone role may not lock at all
            b = await (await s.post(url(hub, "/m/api/voice"), data=tone(), headers=h)).json()
            assert not b["ok"] and "isn't allowed" in b["reply"] and not b.get("pending")   # told the truth, immediately
            await asyncio.sleep(0.2)
            assert not hub.confirmations and fake.calls == []                                # and nobody was bothered
    finally:
        task.cancel()


async def test_revoking_a_phone_ends_its_voice_session(hub, engine):
    async with session(hub) as s:
        key, _, body = await pair_browser(hub, s)
        await do_login(hub, s, key, body["device_id"])
        await s.post(url(hub, "/m/api/voice/session"), json={"on": True}, headers=H)
        assert hub.voice.sessions
        async with await admin_session(hub) as op:
            await op.post(aurl(hub, "/admin/revoke"), json={"id": body["device_id"]}, headers=H)
        assert not hub.voice.sessions
        r = await s.post(url(hub, "/m/api/voice"), data=tone(), headers={**H, "Content-Type": "application/octet-stream"})
        assert r.status == 401


async def test_a_device_class_without_a_microphone_cannot_start_a_session(hub):
    dev = {"id": "x", "name": "kiosk", "class": "other", "revoked": False, "can_send": [], "accepts": []}
    from jarvis.voice.service import VoiceError
    with pytest.raises(VoiceError) as e:
        hub.voice.start_session(dev, True)
    assert e.value.status == 403


async def test_voice_status_reports_engine(hub, engine):
    async with await admin_session(hub) as s:
        st = await (await s.get(aurl(hub, "/admin/voice/status"))).json()
        assert st["available"] and st["engine"] == "scripted" and st["wake_required"] is True


# ---- renewals can never undo the hub's kill switch ------------------------------------------------------------
async def test_a_renewal_never_revives_a_mode_the_hub_turned_off(hub, engine):
    async with await admin_session(hub) as s:
        assert (await (await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice"}, headers=H)).json())["mode"] == "voice"
        ok = await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice", "renew": True}, headers=H)
        assert ok.status == 200
        await s.post(aurl(hub, "/admin/mode"), json={"mode": "idle"}, headers=H)             # operator hits the kill switch
        late = await s.post(aurl(hub, "/admin/mode"), json={"mode": "voice", "renew": True}, headers=H)
        assert late.status == 409
        assert (await (await s.get(aurl(hub, "/admin/mode"))).json())["mode"] == "idle"       # still off
        assert (await speak(s, hub))[0] == 403                                               # and the microphone really is closed
        wrong = await s.post(aurl(hub, "/admin/mode"), json={"mode": "hologram", "renew": True}, headers=H)
        assert wrong.status == 409                                                           # a renewal cannot change the mode either


async def test_kill_switch_also_closes_phone_microphones(hub, engine):
    async with session(hub) as s:
        key, _, body = await pair_browser(hub, s)
        await do_login(hub, s, key, body["device_id"])
        await s.post(url(hub, "/m/api/voice/session"), json={"on": True}, headers=H)
        assert hub.voice.sessions
        async with await admin_session(hub) as op:
            await op.post(aurl(hub, "/admin/mode"), json={"mode": "idle"}, headers=H)
        assert not hub.voice.sessions
        r = await s.post(url(hub, "/m/api/voice/session"), json={"on": True, "renew": True}, headers=H)
        assert r.status == 409                                                               # the phone's own renewal cannot reopen it
        h = {**H, "Content-Type": "application/octet-stream"}
        assert (await s.post(url(hub, "/m/api/voice"), data=tone(), headers=h)).status == 403
        r = await s.post(url(hub, "/m/api/voice/session"), json={"on": True}, headers=H)     # but the user can start a new one
        assert (await r.json())["on"] is True


# ---- static files -----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/voice/..%2fserver.py", "/voice/%2e%2e%2fserver.py", "/voice/../server.py", "/voice/nope.js"])
async def test_voice_static_paths_cannot_escape(hub, path):
    async with await admin_session(hub) as s:
        r = await s.get(aurl(hub, path))
        assert r.status in (404, 400) and b"admin_token" not in await r.read()
    async with session(hub) as s:
        r = await s.get(url(hub, path))
        assert r.status in (404, 400) and b"admin_token" not in await r.read()


async def test_voice_scripts_are_served_but_only_after_login_on_the_dashboard(hub):
    async with aiohttp.ClientSession() as anon:
        assert (await anon.get(aurl(hub, "/voice/voice.js"))).status == 401
    async with await admin_session(hub) as s:
        r = await s.get(aurl(hub, "/voice/worklet.js"))
        assert r.status == 200 and "registerProcessor" in await r.text()
        assert "microphone=(self)" in r.headers["Permissions-Policy"]
    async with session(hub) as s:                           # phones fetch it before signing in: it is public code, no secrets
        r = await s.get(url(hub, "/voice/voice.js"))
        assert r.status == 200 and "microphone=(self)" in r.headers["Permissions-Policy"] and "camera=()" in r.headers["Permissions-Policy"]
