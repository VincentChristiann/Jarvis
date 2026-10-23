"""The hub: pairing endpoint (TLS, pinned), control endpoint (mutual TLS), admin API (localhost)."""
import asyncio
import base64
import hmac
import io
import json
import re
import secrets
import time
import uuid

import ifaddr
import qrcode
import qrcode.image.svg
from aiohttp import WSMsgType, web
from cryptography import x509

from pathlib import Path

from ..paths import home_dir, private_dir, write_private
from ..security import pki, policy
from ..security.audit import AuditLog
from ..security.ratelimit import FailureLimiter
from ..security.registry import Registry
from .mobile import Mobile
from .pairing import PairingManager, sas_code

PAIR_WAIT = 90
CMD_TIMEOUT = 30
CONFIRM_TIMEOUT = 30
VIA = web.RequestKey("via", str)
CTL_MAX_MSG = 8 * 1024 * 1024  # fits a 4 MB file after base64
WEB = Path(__file__).parent / "web"
SESSION_TTL = 8 * 3600
LOGIN_CODE_TTL = 60
RENEW_WINDOW = 30 * 86400
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
    def __init__(self, home=None, host="0.0.0.0", pair_port=8443, ctl_port=8444, admin_port=8445, mobile_port=8446,
                 advertise=(), cmd_timeout=CMD_TIMEOUT, pair_wait=PAIR_WAIT, confirm_timeout=CONFIRM_TIMEOUT):
        self.dir = hub_dir(home)
        self.host, self.advertise = host, list(advertise)
        self.req_ports = (pair_port, ctl_port, admin_port, mobile_port)
        self.ports: dict[str, int] = {}
        self.cmd_timeout, self.pair_wait, self.confirm_timeout = cmd_timeout, pair_wait, confirm_timeout
        self.pairing = PairingManager()
        self.limiter = FailureLimiter()
        self.agents: dict[str, web.WebSocketResponse] = {}
        self.clients: dict[str, set] = {}
        self.inflight: dict[str, tuple[asyncio.Future, str]] = {}
        self.confirmations: dict[str, dict] = {}
        self.nonces: dict[str, float] = {}
        self.sessions: dict[str, float] = {}
        self.grants: dict[tuple, float] = {}
        self._grant_wait: dict[tuple, asyncio.Future] = {}
        self.login_codes: dict[str, float] = {}
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
        r.add_get("/", self._web_index)
        r.add_get("/dashboard.js", self._web_static)
        r.add_get("/dashboard.css", self._web_static)
        r.add_get("/login", self._login)
        r.add_post("/admin/login-code", self._a_login_code)
        r.add_get("/admin/audit", self._a_audit)
        r.add_post("/admin/send", self._a_send)
        r.add_get("/admin/grants", self._a_grants)
        r.add_post("/admin/grant-close", self._a_grant_close)
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
            ("mobile", Mobile(self).app(), self.req_ports[3], pair_ctx, self.host),
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
        fp = pki.fingerprint(cert)
        if not dev or dev["revoked"] or fp not in (dev["cert_fp"], dev.get("prev_fp")):
            self.audit.write("connect_denied", device=device_id, ip=request.remote)
            raise web.HTTPForbidden()
        if fp == dev["cert_fp"]:
            self.registry.confirm_cert(device_id)  # the renewed cert works, so the old one can go
        kind = "agent" if request.query.get("kind") == "agent" else "client"
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=CTL_MAX_MSG)
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
                elif kind_t == "renew":
                    await self._renew(device_id, ws, data)
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

    async def _renew(self, device_id: str, ws, data: dict):
        """Issue a fresh certificate (same identity, same key) to a device that is close to expiry."""
        dev = self.registry.get(device_id)
        try:
            if dev["expires_at"] - time.time() > RENEW_WINDOW:
                raise ValueError("not due for renewal")
            csr = pki.validate_csr(str(data["csr"]).encode())
            cert = pki.sign_csr(self.ca_cert, self.ca_key, csr, device_id)
        except Exception as e:
            self.audit.write("renew_denied", device=device_id, why=str(e)[:80])
            return await ws.send_str(json.dumps({"type": "renewed", "ok": False, "error": "renewal refused"}))
        self.registry.rotate_cert(device_id, pki.fingerprint(cert), cert.not_valid_after_utc.timestamp())
        self.audit.write("renewed", device=device_id)
        await ws.send_str(json.dumps({"type": "renewed", "ok": True, "cert": pki.cert_pem(cert).decode()}))

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
        try:
            cmd, target_id, args = data.get("cmd"), data.get("target"), data.get("args", {})
            if not (isinstance(cmd, str) and isinstance(target_id, str) and isinstance(args, dict)):
                res = {"ok": False, "error": "malformed command"}
            elif not self._fresh(sender_id, data):
                self.audit.write("cmd_denied", sender=sender_id, cmd=cmd, why="stale or replayed")
                res = {"ok": False, "error": "stale or replayed command"}
            else:
                res = await self._route(self.registry.get(sender_id), target_id, cmd, args)
        except Exception:
            self.audit.write("cmd_error", sender=sender_id)
            res = {"ok": False, "error": "internal error"}
        if not ws.closed:
            await ws.send_str(json.dumps({"type": "result", "id": rid, **res}))

    async def _route(self, sender: dict, target_id: str, cmd: str, args: dict, operator=False) -> dict:
        """Authorize and deliver one command. `operator` = the hub owner acting locally (skips confirm)."""
        sid = sender["id"]
        target = None if target_id == "hub" else self.registry.get(target_id)
        if target_id != "hub" and target is None:
            return {"ok": False, "error": "unknown target"}
        decision = policy.authorize(sender, target, cmd)
        if decision.verdict == "deny":
            self.audit.write("cmd_denied", sender=sid, target=target_id, cmd=cmd, why=decision.reason)
            return {"ok": False, "error": f"denied: {decision.reason}"}
        if decision.verdict == "confirm" and not operator:
            if not await self._confirm(sender, target_id, cmd):
                self.audit.write("cmd_denied", sender=sid, target=target_id, cmd=cmd, why="not confirmed")
                return {"ok": False, "error": "denied: not confirmed by operator"}
        if decision.verdict == "grant" and not operator:
            if not await self._ensure_grant(sender, target_id, cmd):
                self.audit.write("cmd_denied", sender=sid, target=target_id, cmd=cmd, why="control window not approved")
                return {"ok": False, "error": "denied: control window not approved by operator"}
        self.audit.write("cmd_allowed", sender=sid, target=target_id, cmd=cmd)
        if target_id == "hub":
            return {"ok": True, "data": self._hub_command(cmd)}
        agent = self.agents.get(target_id)
        if agent is None or agent.closed:
            return {"ok": False, "error": "target offline"}
        fid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.inflight[fid] = (fut, target_id)
        try:
            await agent.send_str(json.dumps({"type": "exec", "id": fid, "from": sid, "cmd": cmd, "args": args}))
            result = await asyncio.wait_for(fut, self.cmd_timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "target timed out"}
        finally:
            self.inflight.pop(fid, None)
        return {"ok": bool(result.get("ok")), **{k: result[k] for k in ("data", "error") if k in result}}

    def _hub_command(self, cmd):
        if cmd == "hub.devices":
            return [{"id": d["id"], "name": d["name"], "platform": d["platform"], "class": d["class"],
                     "online": d["id"] in self.agents} for d in self.registry.all() if not d["revoked"]]
        raise ValueError(cmd)

    async def _ensure_grant(self, sender, target_id, cmd) -> bool:
        """One operator approval opens a time-boxed control window for sender -> target -> command family."""
        key = (sender["id"], target_id, policy.family(cmd))
        if self.grants.get(key, 0) > time.time():
            return True
        waiting = self._grant_wait.get(key)
        if waiting is not None:          # a burst of cursor moves must not stack up prompts
            return await asyncio.shield(waiting)
        fut = asyncio.get_running_loop().create_future()
        self._grant_wait[key] = fut
        try:
            ok = await self._confirm(sender, target_id, f"{key[2]}.* control for {policy.GRANT_TTL // 60} min")
            if ok:
                self.grants[key] = time.time() + policy.GRANT_TTL
                self.audit.write("grant_opened", sender=sender["id"], target=target_id, family=key[2])
            fut.set_result(ok)
            return ok
        finally:
            self._grant_wait.pop(key, None)

    def _drop_grants(self, device_id=None, key=None):
        for k in list(self.grants):
            if k == key or (device_id and device_id in k[:2]):
                del self.grants[k]
                self.audit.write("grant_closed", sender=k[0], target=k[1], family=k[2])

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
        # DNS-rebinding defence: a hostile web page must not be able to talk to this port by name.
        if request.headers.get("Host", "").rsplit(":", 1)[0] not in ("127.0.0.1", "localhost"):
            raise web.HTTPForbidden()
        if request.path == "/login":
            return await handler(request)
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if supplied and hmac.compare_digest(supplied.encode(), self.admin_token.encode()):
            request[VIA] = "bearer"
        elif self._session_ok(request.cookies.get("jarvis_session", "")):
            request[VIA] = "session"
            if request.method != "GET" and request.headers.get("X-Jarvis") != "1":
                raise web.HTTPForbidden()  # a cross-site form cannot set custom headers
        else:
            raise web.HTTPUnauthorized()
        resp = await handler(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        return resp

    def _session_ok(self, sid: str) -> bool:
        now = time.time()
        for k in [k for k, t in self.sessions.items() if t < now]:
            del self.sessions[k]
        return bool(sid) and any(hmac.compare_digest(sid, k) for k in self.sessions)

    async def _login(self, request):
        code = request.query.get("code", "")
        now = time.time()
        valid = [k for k, t in self.login_codes.items() if t > now and hmac.compare_digest(code, k)]
        self.login_codes.clear()  # any attempt burns outstanding codes
        if not code or not valid:
            self.audit.write("dashboard_login_failed")
            raise web.HTTPForbidden(text="invalid or expired login link")
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = now + SESSION_TTL
        self.audit.write("dashboard_login")
        resp = web.HTTPFound("/")
        resp.set_cookie("jarvis_session", sid, max_age=SESSION_TTL, httponly=True, samesite="Strict", path="/")
        raise resp

    async def _a_login_code(self, request):
        if request[VIA] != "bearer":
            raise web.HTTPForbidden()
        code = secrets.token_urlsafe(24)
        self.login_codes[code] = time.time() + LOGIN_CODE_TTL
        return web.json_response({"code": code, "expires_in": LOGIN_CODE_TTL})

    async def _web_index(self, request):
        return web.Response(body=(WEB / "index.html").read_bytes(), content_type="text/html")

    async def _web_static(self, request):
        name = request.path.lstrip("/")
        ctype = "application/javascript" if name.endswith(".js") else "text/css"
        return web.Response(body=(WEB / name).read_bytes(), content_type=ctype)

    async def _a_audit(self, request):
        path = self.dir / "audit.log"
        ok, bad = AuditLog.verify(path)
        return web.json_response({"ok": ok, "bad": bad, "entries": AuditLog.tail(path, 40)})

    async def _a_grants(self, request):
        now = time.time()
        name = lambda i: (self.registry.get(i) or {}).get("name", i)
        return web.json_response([
            {"id": ":".join(k), "sender": name(k[0]), "target": name(k[1]), "family": k[2], "expires_in": int(t - now)}
            for k, t in self.grants.items() if t > now])

    async def _a_grant_close(self, request):
        b = await request.json()
        for k in list(self.grants):
            if ":".join(k) == b.get("id"):
                self._drop_grants(key=k)
                return web.json_response({"ok": True})
        return web.json_response({"ok": False})

    async def _a_send(self, request):
        """The hub owner runs a command from the dashboard. Device-side allow-lists still apply."""
        b = await request.json()
        if not (isinstance(b.get("cmd"), str) and isinstance(b.get("target"), str) and isinstance(b.get("args", {}), dict)):
            return web.json_response({"ok": False, "error": "malformed"}, status=400)
        operator = {"id": "operator", "name": "operator", "can_send": ["*"], "revoked": False}
        return web.json_response(await self._route(operator, b["target"], b["cmd"], b.get("args", {}), operator=True))

    async def _a_invite(self, request):
        body = await request.json()
        role = body.get("role", "phone")
        if role not in policy.ROLES:
            return web.json_response({"error": "unknown role"}, status=400)
        token = self.pairing.create_invite(role)
        hosts = body.get("hosts") or self.addresses
        if body.get("kind") == "web":
            url = f"https://{hosts[0]}:{self.ports['mobile']}/m#pair={token}"
        else:
            url = (f"jarvis://pair?host={','.join(hosts)}&port={self.ports['pair']}&ctl={self.ports['ctl']}"
                   f"&token={token}&pin={self.pair_pin}")
        self.audit.write("invite_created", role=role, kind=body.get("kind", "app"))
        svg = io.BytesIO()
        qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=2).save(svg)
        qr = "data:image/svg+xml;base64," + base64.b64encode(svg.getvalue()).decode()
        return web.json_response({"url": url, "expires_in": 120, "role": role, "qr": qr,
                                  "ca_fp": pki.fingerprint(self.ca_cert), "ca_url": f"https://{hosts[0]}:{self.ports['mobile']}/ca.pem"})

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
            self._drop_grants(device_id=did)
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
