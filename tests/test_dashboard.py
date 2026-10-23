import asyncio

import aiohttp

from jarvis.device.agent import Agent

from .conftest import pair_device


def base(hub):
    return f"http://127.0.0.1:{hub.ports['admin']}"


def bearer(hub):
    return {"Authorization": f"Bearer {hub.admin_token}"}


async def login(hub, s):
    r = await s.post(base(hub) + "/admin/login-code", headers=bearer(hub))
    code = (await r.json())["code"]
    r = await s.get(base(hub) + f"/login?code={code}", allow_redirects=False)
    return code, r


async def test_dashboard_needs_login(hub):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        assert (await s.get(base(hub) + "/")).status == 401
        assert (await s.get(base(hub) + "/dashboard.js")).status == 401
        assert (await s.get(base(hub) + "/login?code=guess")).status == 403


async def test_login_code_single_use_and_session_works(hub):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        code, r = await login(hub, s)
        assert r.status == 302
        assert "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Strict" in r.headers["Set-Cookie"]
        page = await s.get(base(hub) + "/")
        assert page.status == 200 and "Jarvis" in await page.text()
        csp = page.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert (await s.get(base(hub) + f"/login?code={code}", allow_redirects=False)).status == 403  # burned


async def test_login_code_cannot_be_minted_with_session(hub):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        await login(hub, s)
        r = await s.post(base(hub) + "/admin/login-code", headers={"X-Jarvis": "1"})
        assert r.status == 403


async def test_session_post_requires_csrf_header(hub):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        await login(hub, s)
        assert (await s.post(base(hub) + "/admin/revoke", json={"id": "x"})).status == 403
        assert (await s.post(base(hub) + "/admin/revoke", json={"id": "x"}, headers={"X-Jarvis": "1"})).status == 200


async def test_dns_rebinding_blocked(hub):
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
        r = await s.get(base(hub) + "/admin/devices", headers={**bearer(hub), "Host": "evil.example:8445"})
        assert r.status == 403


async def test_invite_includes_qr_and_dashboard_can_send(hub, tmp_path):
    node = await pair_device(hub, tmp_path, "pc1", "node", "desktop")
    agent = asyncio.create_task(Agent(node).run())
    try:
        for _ in range(100):
            if node.device_id in hub.agents:
                break
            await asyncio.sleep(0.05)
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as s:
            await login(hub, s)
            h = {"X-Jarvis": "1"}
            inv = await (await s.post(base(hub) + "/admin/invite", json={"role": "phone"}, headers=h)).json()
            assert inv["qr"].startswith("data:image/svg+xml;base64,") and inv["url"].startswith("jarvis://pair")
            r = await (await s.post(base(hub) + "/admin/send", json={"target": node.device_id, "cmd": "ping"}, headers=h)).json()
            assert r["ok"]
            r = await (await s.post(base(hub) + "/admin/send", json={"target": node.device_id, "cmd": "nonsense"}, headers=h)).json()
            assert not r["ok"]                                          # operator is still bound by the catalog
            aud = await (await s.get(base(hub) + "/admin/audit")).json()
            assert aud["ok"] and aud["entries"]
    finally:
        agent.cancel()
