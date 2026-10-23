import time

import aiohttp
import pytest

from jarvis.hub.server import Hub, init_hub
from jarvis.security import policy
from jarvis.security.audit import AuditLog

H = {"X-Jarvis": "1"}


def base(hub):
    return f"http://127.0.0.1:{hub.ports['admin']}"


async def login(hub):
    s = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
    code = (await (await s.post(base(hub) + "/admin/login-code", headers={"Authorization": f"Bearer {hub.admin_token}"})).json())["code"]
    await s.get(base(hub) + f"/login?code={code}", allow_redirects=False)
    return s


def events(hub):
    return [e["event"] for e in AuditLog.tail(hub.dir / "audit.log", 100)]


# ---- camera / microphone modes ------------------------------------------------
async def test_sensors_are_off_by_default_and_modes_are_audited(hub):
    s = await login(hub)
    async with s:
        assert (await (await s.get(base(hub) + "/admin/mode")).json())["mode"] == "idle"
        m = await (await s.post(base(hub) + "/admin/mode", json={"mode": "hologram"}, headers=H)).json()
        assert m["mode"] == "hologram" and 0 < m["expires_in"] <= policy.MODE_TTL
        await s.post(base(hub) + "/admin/mode", json={"mode": "hologram"}, headers=H)       # renewal
        await s.post(base(hub) + "/admin/mode", json={"mode": "idle"}, headers=H)
    changes = [e for e in AuditLog.tail(hub.dir / "audit.log", 100) if e["event"] == "mode_changed"]
    assert [c["mode"] for c in changes] == ["hologram", "idle"]                             # renewal is not logged


async def test_mode_expires_by_itself(hub):
    s = await login(hub)
    async with s:
        await s.post(base(hub) + "/admin/mode", json={"mode": "gesture"}, headers=H)
        hub.mode_until = time.time() - 1                                                    # the page stopped renewing
        assert (await (await s.get(base(hub) + "/admin/mode")).json())["mode"] == "idle"
    ev = [e for e in AuditLog.tail(hub.dir / "audit.log", 100) if e["event"] == "mode_changed"][-1]
    assert ev["mode"] == "idle" and ev["why"] == "expired"


async def test_unknown_mode_rejected(hub):
    async with await login(hub) as s:
        r = await s.post(base(hub) + "/admin/mode", json={"mode": "spy"}, headers=H)
        assert r.status == 400


async def test_camera_refused_on_a_device_class_without_one(tmp_path):
    init_hub(tmp_path / "h")
    hub = Hub(tmp_path / "h", host="127.0.0.1", pair_port=0, ctl_port=0, admin_port=0, mobile_port=0,
              advertise=["127.0.0.1"], hub_class="other")
    await hub.start()
    try:
        async with await login(hub) as s:
            r = await s.post(base(hub) + "/admin/mode", json={"mode": "hologram"}, headers=H)
            assert r.status == 403 and "may not use the camera" in (await r.json())["error"]
            assert (await (await s.get(base(hub) + "/admin/mode")).json())["mode"] == "idle"
            assert (await s.post(base(hub) + "/admin/mode", json={"mode": "idle"}, headers=H)).status == 200
        assert "mode_refused" in events(hub)
    finally:
        await hub.stop()


# ---- gesture bindings ------------------------------------------------------------
async def test_gesture_map_defaults_and_save(hub):
    async with await login(hub) as s:
        r = await (await s.get(base(hub) + "/admin/gesture-map")).json()
        assert r["map"]["swipe_right"]["cmd"] == "media.next"
        assert "system.lock" not in r["choices"] and "input.move" not in r["choices"]
        new = {**r["map"], "swipe_right": {"cmd": "media.previous", "args": {}}, "hold_fist": None}
        assert (await s.post(base(hub) + "/admin/gesture-map", json={"map": new}, headers=H)).status == 200
        again = await (await s.get(base(hub) + "/admin/gesture-map")).json()
        assert again["map"]["swipe_right"]["cmd"] == "media.previous" and again["map"]["hold_fist"] is None


@pytest.mark.parametrize("bad", [
    {"swipe_up": {"cmd": "system.lock"}},                  # high risk: a stray swipe must never lock the machine
    {"swipe_up": {"cmd": "input.type", "args": {"text": "x"}}},   # remote input needs a control window
    {"swipe_up": {"cmd": "files.put"}},
    {"swipe_up": {"cmd": "nonsense"}},
    {"not_a_gesture": {"cmd": "ping"}},
    {"swipe_up": {"cmd": "ping", "args": {"x": "y" * 400}}},
    {"swipe_up": "ping"},
])
async def test_dangerous_gesture_bindings_refused(hub, bad):
    async with await login(hub) as s:
        r = await s.post(base(hub) + "/admin/gesture-map", json={"map": bad}, headers=H)
        assert r.status == 400


async def test_corrupt_saved_map_falls_back_to_defaults(hub):
    (hub.dir / "gestures.json").write_text('{"swipe_up": {"cmd": "system.lock"}}')          # tampered on disk
    assert hub.gesture_map()["swipe_up"]["cmd"] == "volume.step"


# ---- serving ----------------------------------------------------------------------
async def test_hologram_needs_login_and_is_hardened(hub):
    async with aiohttp.ClientSession() as anon:
        assert (await anon.get(base(hub) + "/hologram")).status == 401
        assert (await anon.get(base(hub) + "/hologram/gestures.js")).status == 401
        assert (await anon.get(base(hub) + "/vendor/models/hand_landmarker.task")).status == 401
    async with await login(hub) as s:
        page = await s.get(base(hub) + "/hologram")
        assert page.status == 200
        assert "wasm-unsafe-eval" in page.headers["Content-Security-Policy"]
        assert "camera=(self)" in page.headers["Permissions-Policy"]
        assert "'unsafe-inline'" not in page.headers["Content-Security-Policy"]
        dash = await s.get(base(hub) + "/")
        assert "camera=()" in dash.headers["Permissions-Policy"]                             # only the hologram page may ask
        assert "wasm-unsafe-eval" not in dash.headers["Content-Security-Policy"]
        js = await s.get(base(hub) + "/hologram/gestures.js")
        assert js.status == 200 and js.headers["Content-Type"].startswith("application/javascript")


@pytest.mark.parametrize("path", ["/vendor/../../../../etc/passwd", "/vendor/%2e%2e/%2e%2e/hub.py",
                                  "/hologram/..%2f..%2fserver.py", "/vendor/../../../server.py"])
async def test_static_paths_cannot_escape(hub, path):
    async with await login(hub) as s:
        r = await s.get(base(hub) + path)
        assert r.status in (404, 400)
        assert b"admin_token" not in await r.read()


async def test_vision_status_endpoint(hub):
    async with await login(hub) as s:
        r = await (await s.get(base(hub) + "/admin/vision")).json()
        assert isinstance(r["installed"], bool)
