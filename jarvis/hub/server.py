"""The hub: pairing endpoint (TLS, pinned), control endpoint (mutual TLS), admin API (localhost)."""
import asyncio
import hmac
import json
import re
import secrets
import time
import uuid

import ifaddr
from aiohttp import WSMsgType, web
from cryptography import x509

from ..paths import home_dir, private_dir, write_private
from ..security import pki, policy
from ..security.audit import AuditLog
from ..security.ratelimit import FailureLimiter
from ..security.registry import Registry
from .pairing import PairingManager, sas_code

PAIR_WAIT = 90
CMD_TIMEOUT = 30
CONFIRM_TIMEOUT = 30
MAX_SKEW = 30
NONCE_TTL = 120
CLEAN = re.compile(r"[^\w .@\-()]")


def local_addresses() -> list[str]:
    out = []
    for ad in ifaddr.get_adapters():
        for ip in ad.ips:
            if isinstance(ip.ip, str) and not ip.ip.startswith("127."):
                out.append(ip.ip)
    return out


def clean(s, n=64) -> str:
    return CLEAN.sub("", str(s))[:n] or "unnamed"


def hub_dir(home) -> "Path":
    return home_dir(home) / "hub"


def init_hub(home=None):
    """Create the CA and admin token. Refuses to overwrite an existing hub."""
    d = private_dir(hub_dir(home))
    if (d / "ca.key").exists():
        raise SystemExit(f"hub already initialised at {d}")
    cert, key = pki.create_ca()
    write_private(d / "ca.key", pki.key_pem(key))
    write_private(d / "ca.pem", pki.cert_pem(cert))
    write_private(d / "admin.token", secrets.token_urlsafe(32).encode())
    return d


class Hub:
    def __init__(self, home=None, host="0.0.0.0", pair_port=8443, ctl_port=8444, admin_port=8445,
                 advertise=(), cmd_timeout=CMD_TIMEOUT, pair_wait=PAIR_WAIT, confirm_timeout=CONFIRM_TIMEOUT):
        self.dir = hub_dir(home)
        self.host, self.advertise = host, list(advertise)
        self.req_ports = (pair_port, ctl_port, admin_port)
        self.ports: dict[str, int] = {}
        self.cmd_timeout, self.pair_wait, self.confirm_timeout = cmd_timeout, pair_wait, confirm_timeout
        self.pairing = PairingManager()
        self.limiter = FailureLimiter()
        self.agents: dict[str, web.WebSocketResponse] = {}
        self.clients: dict[str, set] = {}
        self.inflight: dict[str, tuple[asyncio.Future, str]] = {}
        self.confirmations: dict[str, dict] = {}
        self.nonces: dict[str, float] = {}
        self._runners = []

    # ---- lifecycle -------------------------------------------------------
    async def start(self):
        self.ca_cert = pki.load_cert((self.dir / "ca.pem").read_bytes())
        self.ca_key = pki.load_key((self.dir / "ca.key").read_bytes())
        self.admin_token = (self.dir / "admin.token").read_text().strip()
        self.registry = Registry(self.dir / "devices.json")
        self.audit = AuditLog(self.dir / "audit.log")
        self.addresses = self.advertise or local_addresses()

        # Fresh server key each start; the pairing pin is minted from it.
        skey = pki.new_key()
        self.server_cert = pki.issue_server_cert(self.ca_cert, self.ca_key, skey, self.addresses)
        self.pair_pin = pki.fingerprint(self.server_cert)
        pair_ctx = pki.server_context(self.server_cert, skey)
        ctl_ctx = pki.server_context(self.server_cert, skey, self.ca_cert, require_client=True)

        pair_app = web.Application(client_max_size=64 * 1024)
        pair_app.router.add_post("/pair", self._pair)
        ctl_app = web.Application(client_max_size=64 * 1024)
        ctl_app.router.add_get("/ws", self._ws)
        admin_app = web.Application(middlewares=[self._admin_auth], client_max_size=64 * 1024)
        r = admin_app.router
        r.add_post("/admin/invite", self._a_invite)
        r.add_get("/admin/pending", self._a_pending)
        r.add_post("/admin/resolve", self._a_resolve)
        r.add_get("/admin/devices", self._a_devices)
        r.add_post("/admin/revoke", self._a_revoke)
        r.add_get("/admin/confirmations", self._a_confirms)
        r.add_post("/admin/confirm", self._a_confirm)

        for name, app, port, ctx, host in (
            ("pair", pair_app, self.req_ports[0], pair_ctx, self.host),
            ("ctl", ctl_app, self.req_ports[1], ctl_ctx, self.host),
            ("admin", admin_app, self.req_ports[2], None, "127.0.0.1"),
        ):
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, port, ssl_context=ctx)
            await site.start()
            self.ports[name] = site._server.sockets[0].getsockname()[1]
            self._runners.append(runner)
        self.audit.write("hub_started", ports=self.ports)

    async def stop(self):
        for ws in list(self.agents.values()):
            await ws.close()
        for runner in self._runners:
            await runner.cleanup()
        self._runners.clear()

    # ---- pairing ---------------------------------------------------------
    async def _pair(self, request: web.Request):
        ip = request.remote or "?"
        if self.limiter.blocked(ip):
            self.audit.write("pair_blocked", ip=ip)
            return web.json_response({"error": "too many attempts"}, status=429)
        try:
            body = await request.json()
            token = body["token"]
        except Exception:
            self.limiter.fail(ip)
            return web.json_response({"error": "bad request"}, status=400)
        invite = self.pairing.consume(token)
        if invite is None:
            self.limiter.fail(ip)
            self.audit.write("pair_failed", ip=ip, why="invalid or expired token")
            return web.json_response({"error": "invalid token"}, status=403)
        try:
            csr_pem = str(body["csr"]).encode()
            csr = pki.validate_csr(csr_pem)
        except Exception:
            self.audit.write("pair_failed", ip=ip, why="bad csr")
            return web.json_response({"error": "bad csr"}, status=400)
        dclass = body.get("device_class")
        dclass = dclass if dclass in policy.DEVICE_CLASSES else "other"
        pend = self.pairing.add_pending(
            name=clean(body.get("name", "unnamed")), platform=clean(body.get("platform", "?"), 32),
            device_class=dclass, role=invite.role, sas=sas_code(token, pki.csr_der(csr)),
            ip=ip, csr_pem=csr_pem,
        )
        if pend is None:
            return web.json_response({"error": "too many pending requests"}, status=429)
        self.audit.write("pair_requested", id=pend.id, name=pend.name, ip=ip, role=pend.role, sas=pend.sas)
        try:
            await asyncio.wait_for(pend.event.wait(), self.pair_wait)
        except asyncio.TimeoutError:
            self.pairing.drop(pend.id)
            self.audit.write("pair_timeout", id=pend.id)
            return web.json_response({"error": "not approved in time"}, status=408)
        self.pairing.drop(pend.id)
        if not pend.approved:
            self.audit.write("pair_rejected", id=pend.id)
            return web.json_response({"error": "rejected"}, status=403)
        device_id = uuid.uuid4().hex
        cert = pki.sign_csr(self.ca_cert, self.ca_key, csr, device_id)
        preset = policy.ROLES[pend.role]
        self.registry.add(device_id, pend.name, pend.platform, pend.device_class, pend.role,
                          pki.fingerprint(cert), cert.not_valid_after_utc.timestamp(),
                          preset["can_send"], preset["accepts"])
        self.audit.write("pair_approved", id=pend.id, device=device_id, name=pend.name, role=pend.role)
        return web.json_response({
            "device_id": device_id, "cert": pki.cert_pem(cert).decode(),
            "ca": pki.cert_pem(self.ca_cert).decode(),
        })

    # ---- control channel -------------------------------------------------
    async def _ws(self, request: web.Request):
        der = request.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
        if not der:
            raise web.HTTPUnauthorized()
        cert = x509.load_der_x509_certificate(der)
        device_id = pki.cert_cn(cert)
        dev = self.registry.get(device_id)
        if not dev or dev["revoked"] or dev["cert_fp"] != pki.fingerprint(cert):
            self.audit.write("connect_denied", device=device_id, ip=request.remote)
            raise web.HTTPForbidden()
        kind = "agent" if request.query.get("kind") == "agent" else "client"
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
        await ws.prepare(request)
        if kind == "agent":
            old = self.agents.get(device_id)
            self.agents[device_id] = ws
            if old is not None:
                await old.close()
        else:
            self.clients.setdefault(device_id, set()).add(ws)
        self.audit.write("connected", device=device_id, kind=kind, ip=request.remote)
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    break
                try:
                    data = json.loads(msg.data)
                    kind_t = data["type"]
                except Exception:
                    break
                if kind_t == "cmd":
                    asyncio.create_task(self._handle_cmd(device_id, ws, data))
                elif kind_t == "result" and kind == "agent":
                    entry = self.inflight.get(data.get("id"))
                    if entry and entry[1] == device_id and not entry[0].done():
                        entry[0].set_result(data)
        finally:
            if kind == "agent" and self.agents.get(device_id) is ws:
                del self.agents[device_id]
            self.clients.get(device_id, set()).discard(ws)
            self.audit.write("disconnected", device=device_id, kind=kind)
        return ws

    def _fresh(self, sender: str, data: dict) -> bool:
        now = time.time()
        for n in [n for n, t in self.nonces.items() if t < now - NONCE_TTL]:
            del self.nonces[n]
        ts, nonce = data.get("ts"), data.get("nonce")
        if not isinstance(ts, (int, float)) or not isinstance(nonce, str) or len(nonce) > 64:
            return False
        if abs(now - ts) > MAX_SKEW:
            return False
        key = f"{sender}:{nonce}"
        if key in self.nonces:
            return False
        self.nonces[key] = now
        return True

    async def _handle_cmd(self, sender_id: str, ws, data: dict):
        rid = data.get("id")

        async def reply(ok, **kw):
            if not ws.closed:
                await ws.send_str(json.dumps({"type": "result", "id": rid, "ok": ok, **kw}))

        try:
            sender = self.registry.get(sender_id)
            cmd, target_id, args = data.get("cmd"), data.get("target"), data.get("args", {})
            if not (isinstance(cmd, str) and isinstance(target_id, str) and isinstance(args, dict)):
                return await reply(False, error="malformed command")
            if not self._fresh(sender_id, data):
                self.audit.write("cmd_denied", sender=sender_id, cmd=cmd, why="stale or replayed")
                return await reply(False, error="stale or replayed command")
            target = None if target_id == "hub" else self.registry.get(target_id)
            if target_id != "hub" and target is None:
                return await reply(False, error="unknown target")
            decision = policy.authorize(sender, target, cmd)
            if decision.verdict == "deny":
                self.audit.write("cmd_denied", sender=sender_id, target=target_id, cmd=cmd, why=decision.reason)
                return await reply(False, error=f"denied: {decision.reason}")
            if decision.verdict == "confirm":
                if not await self._confirm(sender, target_id, cmd):
                    self.audit.write("cmd_denied", sender=sender_id, target=target_id, cmd=cmd, why="not confirmed")
                    return await reply(False, error="denied: not confirmed by operator")
            self.audit.write("cmd_allowed", sender=sender_id, target=target_id, cmd=cmd)
            if target_id == "hub":
                return await reply(True, data=self._hub_command(cmd))
            agent = self.agents.get(target_id)
            if agent is None or agent.closed:
                return await reply(False, error="target offline")
            fid = uuid.uuid4().hex
            fut = asyncio.get_running_loop().create_future()
            self.inflight[fid] = (fut, target_id)
            try:
                await agent.send_str(json.dumps({"type": "exec", "id": fid, "from": sender_id, "cmd": cmd, "args": args}))
                result = await asyncio.wait_for(fut, self.cmd_timeout)
            except asyncio.TimeoutError:
                return await reply(False, error="target timed out")
            finally:
                self.inflight.pop(fid, None)
            await reply(bool(result.get("ok")), **{k: result[k] for k in ("data", "error") if k in result})
        except Exception:
            self.audit.write("cmd_error", sender=sender_id)
            await reply(False, error="internal error")

    def _hub_command(self, cmd):
        if cmd == "hub.devices":
            return [{"id": d["id"], "name": d["name"], "platform": d["platform"], "class": d["class"],
                     "online": d["id"] in self.agents} for d in self.registry.all() if not d["revoked"]]
        raise ValueError(cmd)

    async def _confirm(self, sender, target_id, cmd) -> bool:
        cid = uuid.uuid4().hex[:8]
        entry = {"id": cid, "sender": sender["name"], "target": target_id, "cmd": cmd,
                 "event": asyncio.Event(), "approved": None}
        self.confirmations[cid] = entry
        self.audit.write("confirm_requested", id=cid, sender=sender["id"], target=target_id, cmd=cmd)
        try:
            await asyncio.wait_for(entry["event"].wait(), self.confirm_timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self.confirmations.pop(cid, None)
        return bool(entry["approved"])

    # ---- admin API (127.0.0.1 only, bearer token) ------------------------
    @web.middleware
    async def _admin_auth(self, request, handler):
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), self.admin_token.encode()):
            raise web.HTTPUnauthorized()
        return await handler(request)

    async def _a_invite(self, request):
        body = await request.json()
        role = body.get("role", "phone")
        if role not in policy.ROLES:
            return web.json_response({"error": "unknown role"}, status=400)
        token = self.pairing.create_invite(role)
        hosts = body.get("hosts") or self.addresses
        url = (f"jarvis://pair?host={','.join(hosts)}&port={self.ports['pair']}&ctl={self.ports['ctl']}"
               f"&token={token}&pin={self.pair_pin}")
        self.audit.write("invite_created", role=role)
        return web.json_response({"url": url, "expires_in": 120, "role": role})

    async def _a_pending(self, request):
        return web.json_response([
            {"id": p.id, "name": p.name, "platform": p.platform, "class": p.device_class,
             "role": p.role, "sas": p.sas, "ip": p.ip} for p in self.pairing.pending.values()])

    async def _a_resolve(self, request):
        b = await request.json()
        ok = self.pairing.resolve(str(b.get("id")), bool(b.get("approve")))
        return web.json_response({"ok": ok})

    async def _a_devices(self, request):
        return web.json_response([{**d, "online": d["id"] in self.agents} for d in self.registry.all()])

    async def _a_revoke(self, request):
        b = await request.json()
        did = str(b.get("id"))
        ok = self.registry.revoke(did)
        if ok:
            self.audit.write("revoked", device=did)
            for ws in [self.agents.get(did), *self.clients.get(did, set())]:
                if ws is not None:
                    await ws.close()
        return web.json_response({"ok": ok})

    async def _a_confirms(self, request):
        return web.json_response([{k: v for k, v in c.items() if k not in ("event", "approved")}
                                  for c in self.confirmations.values()])

    async def _a_confirm(self, request):
        b = await request.json()
        c = self.confirmations.get(str(b.get("id")))
        if not c:
            return web.json_response({"ok": False})
        c["approved"] = bool(b.get("approve"))
        c["event"].set()
        self.audit.write("confirm_resolved", id=c["id"], approved=c["approved"])
        return web.json_response({"ok": True})
