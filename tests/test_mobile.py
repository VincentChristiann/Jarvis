import asyncio
import base64
import json
import ssl

import aiohttp
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from jarvis.device.agent import Agent
from jarvis.hub.mobile import login_message
from jarvis.hub.pairing import sas_code
from jarvis.security import pki

from .conftest import pair_device


def ssl_ctx(hub):
    return ssl.create_default_context(cadata=pki.cert_pem(hub.ca_cert).decode())


def url(hub, path):
    return f"https://127.0.0.1:{hub.ports['mobile']}{path}"


def new_browser_key():
    key = ec.generate_private_key(ec.SECP256R1())
    spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key, spki


def sign_p1363(key, msg):
    r, s = decode_dss_signature(key.sign(msg, ec.ECDSA(hashes.SHA256())))
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")        # what WebCrypto produces


H = {"X-Jarvis": "1"}


def session(hub):
    return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx(hub)),
                                 cookie_jar=aiohttp.CookieJar(unsafe=True))


async def pair_browser(hub, s, role="phone", approve=True, key=None):
    key, spki = key or new_browser_key()
    token = hub.pairing.create_invite(role)

    async def approver():
        for _ in range(100):
            if hub.pairing.pending:
                p = next(iter(hub.pairing.pending.values()))
                assert p.sas == sas_code(token, spki)               # the phone computes the same code
                hub.pairing.resolve(p.id, approve)
                return
            await asyncio.sleep(0.02)
    t = asyncio.create_task(approver())
    r = await s.post(url(hub, "/m/pair"), json={"token": token, "pubkey": base64.b64encode(spki).decode(), "name": "my phone"}, headers=H)
    await t
    return key, r.status, await r.json()


async def do_login(hub, s, key, device_id, sig_key=None):
    nonce = (await (await s.get(url(hub, "/m/challenge"))).json())["nonce"]
    sig = sign_p1363(sig_key or key, login_message(nonce, device_id))
    r = await s.post(url(hub, "/m/login"), json={"nonce": nonce, "device_id": device_id, "sig": base64.b64encode(sig).decode()}, headers=H)
    return r, nonce, sig


async def test_pair_login_and_use(hub, tmp_path):
    node = await pair_device(hub, tmp_path, "pc", "node", "desktop")
    agent = asyncio.create_task(Agent(node).run())
    try:
        for _ in range(100):
            if node.device_id in hub.agents:
                break
            await asyncio.sleep(0.05)
        async with session(hub) as s:
            key, status, body = await pair_browser(hub, s)
            assert status == 200
            dev = hub.registry.get(body["device_id"])
            assert dev["kind"] == "web" and dev["role"] == "phone" and dev["cert_fp"] == ""
            r, *_ = await do_login(hub, s, key, body["device_id"])
            assert r.status == 200
            c = r.headers["Set-Cookie"]
            assert "Secure" in c and "HttpOnly" in c and "SameSite=Strict" in c
            lst = await (await s.get(url(hub, "/m/api/devices"))).json()
            assert [d["name"] for d in lst["devices"]] == ["pc"]              # only real devices, never itself
            ok = await (await s.post(url(hub, "/m/api/send"), json={"target": node.device_id, "cmd": "ping"}, headers=H)).json()
            assert ok["ok"]
            bad = await (await s.post(url(hub, "/m/api/send"), json={"target": node.device_id, "cmd": "system.lock"}, headers=H)).json()
            assert not bad["ok"] and "denied" in bad["error"]                 # phones cannot lock
    finally:
        agent.cancel()


async def test_login_is_bound_to_the_device_key(hub):
    async with session(hub) as s:
        key, _, body = await pair_browser(hub, s)
        attacker = ec.generate_private_key(ec.SECP256R1())
        r, *_ = await do_login(hub, s, key, body["device_id"], sig_key=attacker)   # signed with someone else's key
        assert r.status == 403
        r, nonce, sig = await do_login(hub, s, key, body["device_id"])
        assert r.status == 200
        again = await s.post(url(hub, "/m/login"), json={"nonce": nonce, "device_id": body["device_id"],
                                                         "sig": base64.b64encode(sig).decode()}, headers=H)
        assert again.status == 403                                              # a captured login cannot be replayed


async def test_signature_for_other_device_or_nonce_rejected(hub):
    async with session(hub) as s:
        key, _, body = await pair_browser(hub, s)
        nonce = (await (await s.get(url(hub, "/m/challenge"))).json())["nonce"]
        sig = sign_p1363(key, login_message("some-other-nonce", body["device_id"]))
        r = await s.post(url(hub, "/m/login"), json={"nonce": nonce, "device_id": body["device_id"],
                                                     "sig": base64.b64encode(sig).decode()}, headers=H)
        assert r.status == 403


async def test_api_needs_session_and_csrf_header(hub):
    async with session(hub) as s:
        assert (await s.get(url(hub, "/m/api/devices"))).status == 401
        key, _, body = await pair_browser(hub, s)
        await do_login(hub, s, key, body["device_id"])
        assert (await s.post(url(hub, "/m/api/send"), json={"target": "hub", "cmd": "hub.devices"})).status == 403   # no header
        r = await s.post(url(hub, "/m/api/send"), json={"target": "hub", "cmd": "hub.devices"},
                         headers={**H, "Origin": "https://evil.example"})
        assert r.status == 403                                                  # cross-site origin


async def test_revoked_browser_loses_access_immediately(hub):
    async with session(hub) as s:
        key, _, body = await pair_browser(hub, s)
        await do_login(hub, s, key, body["device_id"])
        assert (await s.get(url(hub, "/m/api/devices"))).status == 200
        hub.registry.revoke(body["device_id"])
        assert (await s.get(url(hub, "/m/api/devices"))).status == 401          # live session cut off
        r, *_ = await do_login(hub, s, key, body["device_id"])
        assert r.status == 403


async def test_browser_pairing_rejected_and_bad_inputs(hub):
    async with session(hub) as s:
        _, status, _ = await pair_browser(hub, s, approve=False)
        assert status == 403
        _, spki = new_browser_key()
        r = await s.post(url(hub, "/m/pair"), json={"token": "nope", "pubkey": base64.b64encode(spki).decode()}, headers=H)
        assert r.status == 403
        token = hub.pairing.create_invite("phone")
        rsa_like = base64.b64encode(b"not a key").decode()
        r = await s.post(url(hub, "/m/pair"), json={"token": token, "pubkey": rsa_like}, headers=H)
        assert r.status == 400


async def test_web_device_cannot_use_mtls_channel_or_be_hub_admin(hub):
    async with session(hub) as s:
        key, _, body = await pair_browser(hub, s)
        assert hub.registry.get(body["device_id"])["cert_fp"] == ""
        # A browser device has no certificate, so the control port rejects it before any command.
        with pytest.raises((aiohttp.ClientError, OSError)):
            await s.ws_connect(f"wss://127.0.0.1:{hub.ports['ctl']}/ws?kind=client")


async def test_ca_download_and_headers(hub):
    async with session(hub) as s:
        r = await s.get(url(hub, "/ca.pem"))
        assert (await r.read()).startswith(b"-----BEGIN CERTIFICATE-----")
        page = await s.get(url(hub, "/m"))
        assert "default-src 'none'" in page.headers["Content-Security-Policy"] and "Strict-Transport-Security" in page.headers
        assert b"Jarvis" in await page.read()


async def test_login_failures_get_rate_limited(hub):
    async with session(hub) as s:
        key, _, body = await pair_browser(hub, s)
        attacker = ec.generate_private_key(ec.SECP256R1())
        for _ in range(5):
            r, *_ = await do_login(hub, s, key, body["device_id"], sig_key=attacker)
            assert r.status == 403
        r, *_ = await do_login(hub, s, key, body["device_id"])                  # even the right key is blocked now
        assert r.status == 429
