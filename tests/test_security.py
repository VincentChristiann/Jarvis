import asyncio
import json
import ssl
import time

import aiohttp
import pytest

from jarvis.device.agent import Agent, Client
from jarvis.device.identity import PairingError, pair
from jarvis.hub.pairing import PairingManager, sas_code
from jarvis.security import pki, policy
from jarvis.security.audit import AuditLog
from jarvis.security.ratelimit import FailureLimiter

from .conftest import approve_next, link, pair_device


# ---- pure units -----------------------------------------------------------
def test_sensor_modes_and_classes():
    assert not policy.sensor_allowed("laptop", "idle", "camera")        # off by default
    assert policy.sensor_allowed("laptop", "gesture", "camera")
    assert not policy.sensor_allowed("laptop", "voice", "camera")       # wrong mode
    assert not policy.sensor_allowed("phone", "gesture", "camera")      # phones never use camera
    assert policy.sensor_allowed("phone", "voice", "microphone")


def test_policy_default_deny():
    phone = {"can_send": ["ping"], "revoked": False}
    node = {"accepts": ["ping"], "revoked": False}
    assert policy.authorize(phone, node, "ping").verdict == "allow"
    assert policy.authorize(phone, node, "notify").verdict == "deny"        # not in can_send
    assert policy.authorize(phone, node, "rm -rf").verdict == "deny"        # unknown command
    assert policy.authorize({**phone, "revoked": True}, node, "ping").verdict == "deny"
    ctrl = {"can_send": ["*"], "revoked": False}
    node2 = {"accepts": ["system.lock"], "revoked": False}
    assert policy.authorize(ctrl, node2, "system.lock").verdict == "confirm"  # high risk
    assert policy.authorize(ctrl, node, "system.lock").verdict == "deny"      # target refuses


def test_invite_single_use_and_expiry():
    now = [1000.0]
    pm = PairingManager(clock=lambda: now[0])
    t = pm.create_invite("phone")
    assert pm.consume(t) is not None
    assert pm.consume(t) is None                                             # replay
    t2 = pm.create_invite("phone")
    now[0] += 121
    assert pm.consume(t2) is None                                            # expired
    assert pm.consume("x" * 500) is None and pm.consume(None) is None


def test_rate_limiter():
    now = [0.0]
    rl = FailureLimiter(3, 100, clock=lambda: now[0])
    for _ in range(3):
        rl.fail("a")
    assert rl.blocked("a") and not rl.blocked("b")
    now[0] += 101
    assert not rl.blocked("a")


def test_audit_chain_detects_tampering(tmp_path):
    p = tmp_path / "audit.log"
    log = AuditLog(p)
    for i in range(3):
        log.write("e", n=i)
    assert AuditLog.verify(p) == (True, None)
    lines = p.read_text().splitlines()
    lines[1] = lines[1].replace('"n": 1', '"n": 9').replace('"n":1', '"n":9')
    p.write_text("\n".join(lines) + "\n")
    assert AuditLog.verify(p)[0] is False
    with pytest.raises(RuntimeError):
        AuditLog(p)                                                          # refuses to append to a broken log


def test_audit_deleted_line_detected(tmp_path):
    p = tmp_path / "a.log"
    log = AuditLog(p)
    for i in range(3):
        log.write("e", n=i)
    lines = p.read_text().splitlines()
    p.write_text(lines[0] + "\n" + lines[2] + "\n")
    assert AuditLog.verify(p)[0] is False


def test_sas_depends_on_token_and_key():
    k1, k2 = pki.new_key(), pki.new_key()
    a = sas_code("tok", pki.csr_der(pki.make_csr(k1)))
    assert a != sas_code("tok", pki.csr_der(pki.make_csr(k2)))   # a swapped key changes the code
    assert a != sas_code("tok2", pki.csr_der(pki.make_csr(k1)))


def test_device_cert_cannot_act_as_hub(tmp_path):
    ca, cakey = pki.create_ca()
    dkey = pki.new_key()
    dcert = pki.sign_csr(ca, cakey, pki.make_csr(dkey), "dev1")
    ctx_server = pki.server_context(dcert, dkey)
    cf, kf = tmp_path / "c.pem", tmp_path / "k.pem"
    ckey = pki.new_key()
    ccert = pki.sign_csr(ca, cakey, pki.make_csr(ckey), "dev2")
    cf.write_bytes(pki.cert_pem(ccert))
    kf.write_bytes(pki.key_pem(ckey))
    ctx_client = pki.client_context(pki.cert_pem(ca), cf, kf)

    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0, ssl=ctx_server)
        port = server.sockets[0].getsockname()[1]
        with pytest.raises((ssl.SSLError, OSError)):
            await asyncio.open_connection("127.0.0.1", port, ssl=ctx_client)
        server.close()
    asyncio.run(go())


# ---- end to end -----------------------------------------------------------
async def test_pair_and_ping(hub, tmp_path):
    phone = await pair_device(hub, tmp_path, "phone1", "phone", "phone")
    node = await pair_device(hub, tmp_path, "pc1", "node", "desktop")
    agent = asyncio.create_task(Agent(node).run())
    try:
        for _ in range(100):
            if node.device_id in hub.agents:
                break
            await asyncio.sleep(0.05)
        async with Client(phone) as c:
            r = await c.send(node.device_id, "ping")
            assert r["ok"] and "pong" in r["data"]
            devs = (await c.send("hub", "hub.devices"))["data"]
            assert {d["name"] for d in devs} == {"phone1", "pc1"}
    finally:
        agent.cancel()


async def test_wrong_pin_rejects_hub(hub, tmp_path):
    """A fake hub (wrong certificate fingerprint) never receives the token."""
    url, _ = link(hub)
    bad = url.replace(hub.pair_pin, "00" * 32)
    with pytest.raises(PairingError):
        await pair(bad, "x", "laptop", tmp_path / "x", show_code=lambda s: None)
    assert not hub.pairing.pending


async def test_token_single_use_and_unapproved_rejected(hub, tmp_path):
    url, _ = link(hub)
    t = asyncio.create_task(approve_next(hub, approve=False))
    with pytest.raises(PairingError, match="rejected"):
        await pair(url, "a", "laptop", tmp_path / "a", show_code=lambda s: None)
    await t
    with pytest.raises(PairingError, match="invalid token"):                 # token burned
        await pair(url, "a", "laptop", tmp_path / "a2", show_code=lambda s: None)


async def test_bad_tokens_get_blocked(hub, tmp_path):
    url, _ = link(hub)
    bad = url.split("token=")[0] + "token=nope&pin=" + hub.pair_pin
    for _ in range(5):
        with pytest.raises(PairingError):
            await pair(bad, "z", "laptop", tmp_path / "z", show_code=lambda s: None)
    with pytest.raises(PairingError, match="too many"):
        await pair(bad, "z", "laptop", tmp_path / "z", show_code=lambda s: None)


async def test_unpaired_client_cannot_connect(hub, tmp_path):
    ca = pki.cert_pem(hub.ca_cert)
    rogue_ca, rogue_key = pki.create_ca("rogue")                             # attacker's own CA
    key = pki.new_key()
    cert = pki.sign_csr(rogue_ca, rogue_key, pki.make_csr(key), "evil")
    (tmp_path / "c.pem").write_bytes(pki.cert_pem(cert))
    (tmp_path / "k.pem").write_bytes(pki.key_pem(key))
    ctx = pki.client_context(ca, tmp_path / "c.pem", tmp_path / "k.pem")
    async with aiohttp.ClientSession() as s:
        with pytest.raises((aiohttp.ClientError, OSError)):
            await s.ws_connect(f"wss://127.0.0.1:{hub.ports['ctl']}/ws", ssl=ctx)


async def test_phone_cannot_lock_and_policy_denied_audited(hub, tmp_path):
    phone = await pair_device(hub, tmp_path, "phone1", "phone", "phone")
    node = await pair_device(hub, tmp_path, "pc1", "node", "desktop")
    async with Client(phone) as c:
        r = await c.send(node.device_id, "system.lock")
        assert not r["ok"] and r["error"].startswith("denied")
        r = await c.send(node.device_id, "format.disk")
        assert not r["ok"]
    assert any(e["event"] == "cmd_denied" for e in AuditLog.tail(hub.dir / "audit.log", 50))
    assert AuditLog.verify(hub.dir / "audit.log")[0]


async def test_device_local_allowlist_vetoes_hub(hub, tmp_path):
    ctrl = await pair_device(hub, tmp_path, "laptop1", "controller", "laptop")
    node = await pair_device(hub, tmp_path, "pc1", "node", "desktop", accept=["ping"])  # owner only enabled ping
    agent = asyncio.create_task(Agent(node).run())
    try:
        for _ in range(100):
            if node.device_id in hub.agents:
                break
            await asyncio.sleep(0.05)
        async with Client(ctrl) as c:
            r = await c.send(node.device_id, "system.info")                  # hub allows, device refuses
            assert not r["ok"] and "not enabled" in r["error"]
            assert (await c.send(node.device_id, "ping"))["ok"]
    finally:
        agent.cancel()


async def test_high_risk_needs_operator_confirmation(hub, tmp_path):
    ctrl = await pair_device(hub, tmp_path, "laptop1", "controller", "laptop")
    node = await pair_device(hub, tmp_path, "pc1", "node", "desktop", accept=["system.lock"])
    agent = asyncio.create_task(Agent(node).run())
    try:
        for _ in range(100):
            if node.device_id in hub.agents:
                break
            await asyncio.sleep(0.05)
        async with Client(ctrl) as c:
            r = await c.send(node.device_id, "system.lock")                   # nobody confirms -> times out -> denied
            assert not r["ok"] and "not confirmed" in r["error"]
    finally:
        agent.cancel()


async def test_replayed_command_rejected(hub, tmp_path):
    phone = await pair_device(hub, tmp_path, "phone1", "phone", "phone")
    async with aiohttp.ClientSession() as s:
        ws = await s.ws_connect(f"wss://127.0.0.1:{hub.ports['ctl']}/ws?kind=client", ssl=phone.ssl_context())
        msg = {"type": "cmd", "id": "1", "target": "hub", "cmd": "hub.devices", "args": {},
               "ts": time.time(), "nonce": "abc"}
        await ws.send_str(json.dumps(msg))
        assert json.loads((await ws.receive()).data)["ok"]
        await ws.send_str(json.dumps({**msg, "id": "2"}))                    # same nonce
        assert "replayed" in json.loads((await ws.receive()).data)["error"]
        await ws.send_str(json.dumps({**msg, "id": "3", "nonce": "new", "ts": time.time() - 3600}))
        assert not json.loads((await ws.receive()).data)["ok"]               # stale


async def test_revoke_cuts_off_and_blocks_reconnect(hub, tmp_path):
    phone = await pair_device(hub, tmp_path, "phone1", "phone", "phone")
    async with Client(phone) as c:
        assert (await c.send("hub", "hub.devices"))["ok"]
        hub.registry.revoke(phone.device_id)
        r = await c.send("hub", "hub.devices")
        assert not r["ok"]
    with pytest.raises(ConnectionError):
        async with Client(phone):
            pass


async def test_admin_api_requires_token(hub):
    async with aiohttp.ClientSession() as s:
        r = await s.get(f"http://127.0.0.1:{hub.ports['admin']}/admin/devices")
        assert r.status == 401
        r = await s.get(f"http://127.0.0.1:{hub.ports['admin']}/admin/devices",
                        headers={"Authorization": "Bearer wrong"})
        assert r.status == 401
        r = await s.get(f"http://127.0.0.1:{hub.ports['admin']}/admin/devices",
                        headers={"Authorization": f"Bearer {hub.admin_token}"})
        assert r.status == 200


def test_secret_files_are_owner_only(tmp_path):
    import os
    import stat
    from jarvis.hub.server import hub_dir, init_hub
    init_hub(tmp_path)
    AuditLog(hub_dir(tmp_path) / "audit.log").write("x")
    for f in hub_dir(tmp_path).iterdir():
        if os.name == "posix":
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name
