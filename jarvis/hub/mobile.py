"""Phone-friendly dashboard over HTTPS, authenticated by a device-bound key (no passwords).

Pairing: the browser makes a non-extractable ECDSA P-256 key (WebCrypto) and sends the public half with the
one-time token. You compare the 6-digit code on the hub and approve. Login: the hub sends a random challenge,
the browser signs it with the private key it can never export, and gets a short session cookie.
"""
import base64
import hmac
import secrets
import time
import uuid
from pathlib import Path

from aiohttp import web
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from ..security import pki, policy
from ..security.ratelimit import FailureLimiter
from .pairing import sas_code

WEB = Path(__file__).parent / "mobile_web"
DEV = web.RequestKey("device", dict)
SESSION_TTL = 3600
CHALLENGE_TTL = 60
PAIR_WAIT = 90
LOGIN_CONTEXT = b"jarvis-mobile-login\x00"
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def login_message(nonce: str, device_id: str) -> bytes:
    return LOGIN_CONTEXT + nonce.encode() + b"\x00" + device_id.encode()


def verify_login(pubkey_der: bytes, nonce: str, device_id: str, sig_p1363: bytes) -> bool:
    try:
        if len(sig_p1363) != 64:
            return False
        pub = serialization.load_der_public_key(pubkey_der)
        der_sig = encode_dss_signature(int.from_bytes(sig_p1363[:32], "big"), int.from_bytes(sig_p1363[32:], "big"))
        pub.verify(der_sig, login_message(nonce, device_id), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def parse_pubkey(b64: str) -> bytes:
    der = base64.b64decode(b64, validate=True)
    pub = serialization.load_der_public_key(der)
    if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(pub.curve, ec.SECP256R1):
        raise ValueError("key must be ECDSA P-256")
    return der


class Mobile:
    def __init__(self, hub):
        self.hub = hub
        self.challenges: dict[str, float] = {}
        self.sessions: dict[str, tuple[str, float]] = {}
        self.limiter = FailureLimiter()

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._guard], client_max_size=16 * 1024)
        r = app.router
        r.add_get("/m", self._page)
        r.add_get("/m/", self._page)
        r.add_get("/m/app.js", self._static)
        r.add_get("/m/app.css", self._static)
        r.add_get("/ca.pem", self._ca)
        r.add_get("/favicon.ico", self._nothing)
        r.add_post("/m/pair", self._pair)
        r.add_get("/m/challenge", self._challenge)
        r.add_post("/m/login", self._login)
        r.add_get("/m/api/devices", self._devices)
        r.add_post("/m/api/send", self._send)
        r.add_post("/m/api/logout", self._logout)
        return app

    # ---- guard -------------------------------------------------------------
    @web.middleware
    async def _guard(self, request, handler):
        if request.method == "POST":
            if request.headers.get("X-Jarvis") != "1":
                raise web.HTTPForbidden()
            origin = request.headers.get("Origin")
            if origin and origin != f"https://{request.headers.get('Host', '')}":
                raise web.HTTPForbidden()   # cross-site request
        if request.path.startswith("/m/api/"):
            dev = self._session_device(request)
            if dev is None:
                raise web.HTTPUnauthorized()
            request[DEV] = dev
        resp = await handler(request)
        resp.headers.update({
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": CSP, "Strict-Transport-Security": "max-age=31536000"})
        return resp

    def _session_device(self, request):
        sid = request.cookies.get("jarvis_m", "")
        now = time.time()
        for k in [k for k, (_, exp) in self.sessions.items() if exp < now]:
            del self.sessions[k]
        entry = next((v for k, v in self.sessions.items() if hmac.compare_digest(sid, k)), None) if sid else None
        dev = self.hub.registry.get(entry[0]) if entry else None
        return dev if dev and not dev["revoked"] and dev.get("kind") == "web" else None

    # ---- static --------------------------------------------------------------
    async def _page(self, request):
        return web.Response(body=(WEB / "index.html").read_bytes(), content_type="text/html")

    async def _static(self, request):
        name = request.path.rsplit("/", 1)[1]
        ctype = "application/javascript" if name.endswith(".js") else "text/css"
        return web.Response(body=(WEB / name).read_bytes(), content_type=ctype)

    async def _nothing(self, request):
        return web.Response(status=204)

    async def _ca(self, request):
        return web.Response(body=pki.cert_pem(self.hub.ca_cert), content_type="application/x-pem-file",
                            headers={"Content-Disposition": 'attachment; filename="jarvis-hub-ca.pem"'})

    # ---- pairing -----------------------------------------------------------
    async def _pair(self, request):
        hub, ip = self.hub, request.remote or "?"
        if self.limiter.blocked(ip):
            hub.audit.write("mobile_pair_blocked", ip=ip)
            return web.json_response({"error": "too many attempts"}, status=429)
        try:
            body = await request.json()
            token, der = body["token"], parse_pubkey(body["pubkey"])
        except Exception:
            self.limiter.fail(ip)
            return web.json_response({"error": "bad request"}, status=400)
        invite = hub.pairing.consume(token)
        if invite is None:
            self.limiter.fail(ip)
            hub.audit.write("mobile_pair_failed", ip=ip, why="invalid or expired token")
            return web.json_response({"error": "invalid token"}, status=403)
        from .server import clean
        pend = hub.pairing.add_pending(name=clean(body.get("name", "phone")), platform="browser", device_class="phone",
                                       role=invite.role, sas=sas_code(token, der), ip=ip, csr_pem=der)
        if pend is None:
            return web.json_response({"error": "too many pending requests"}, status=429)
        hub.audit.write("pair_requested", id=pend.id, name=pend.name, ip=ip, role=pend.role, sas=pend.sas, via="browser")
        import asyncio
        try:
            await asyncio.wait_for(pend.event.wait(), hub.pair_wait)
        except asyncio.TimeoutError:
            hub.pairing.drop(pend.id)
            return web.json_response({"error": "not approved in time"}, status=408)
        hub.pairing.drop(pend.id)
        if not pend.approved:
            hub.audit.write("pair_rejected", id=pend.id)
            return web.json_response({"error": "rejected"}, status=403)
        device_id = uuid.uuid4().hex
        preset = policy.ROLES[pend.role]
        hub.registry.add_web(device_id, pend.name, pend.role, base64.b64encode(der).decode(),
                             preset["can_send"], preset["accepts"])
        hub.audit.write("pair_approved", id=pend.id, device=device_id, name=pend.name, role=pend.role, via="browser")
        return web.json_response({"device_id": device_id})

    # ---- login ---------------------------------------------------------------
    async def _challenge(self, request):
        now = time.time()
        for k in [k for k, t in self.challenges.items() if t < now]:
            del self.challenges[k]
        if len(self.challenges) > 200:
            raise web.HTTPTooManyRequests()
        nonce = secrets.token_urlsafe(24)
        self.challenges[nonce] = now + CHALLENGE_TTL
        return web.json_response({"nonce": nonce})

    async def _login(self, request):
        hub, ip = self.hub, request.remote or "?"
        if self.limiter.blocked(ip):
            return web.json_response({"error": "too many attempts"}, status=429)
        try:
            b = await request.json()
            nonce, device_id, sig = b["nonce"], b["device_id"], base64.b64decode(b["sig"], validate=True)
            assert isinstance(nonce, str) and isinstance(device_id, str)
        except Exception:
            self.limiter.fail(ip)
            return web.json_response({"error": "bad request"}, status=400)
        expiry = self.challenges.pop(nonce, 0)              # single use
        dev = hub.registry.get(device_id)
        ok = (expiry > time.time() and dev is not None and dev.get("kind") == "web" and not dev["revoked"]
              and verify_login(base64.b64decode(dev["pubkey"]), nonce, device_id, sig))
        if not ok:
            self.limiter.fail(ip)
            hub.audit.write("mobile_login_failed", ip=ip, device=device_id[:32])
            return web.json_response({"error": "login failed"}, status=403)
        sid = secrets.token_urlsafe(32)
        self.sessions[sid] = (device_id, time.time() + SESSION_TTL)
        hub.audit.write("mobile_login", device=device_id, ip=ip)
        resp = web.json_response({"ok": True, "name": dev["name"], "role": dev["role"], "can_send": dev["can_send"]})
        resp.set_cookie("jarvis_m", sid, max_age=SESSION_TTL, httponly=True, secure=True, samesite="Strict", path="/m")
        return resp

    async def _logout(self, request):
        self.sessions.pop(request.cookies.get("jarvis_m", ""), None)
        return web.json_response({"ok": True})

    # ---- API -------------------------------------------------------------------
    async def _devices(self, request):
        me = request[DEV]
        return web.json_response({
            "me": {"name": me["name"], "role": me["role"], "can_send": me["can_send"]},
            "devices": [{"id": d["id"], "name": d["name"], "class": d["class"], "online": d["id"] in self.hub.agents}
                        for d in self.hub.registry.all()
                        if not d["revoked"] and d.get("kind") != "web" and d["id"] != me["id"]]})

    async def _send(self, request):
        try:
            b = await request.json()
            target, cmd, args = b["target"], b["cmd"], b.get("args", {})
            assert isinstance(target, str) and isinstance(cmd, str) and isinstance(args, dict)
        except Exception:
            return web.json_response({"ok": False, "error": "malformed"}, status=400)
        return web.json_response(await self.hub._route(request[DEV], target, cmd, args))
