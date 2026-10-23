"""Device runtime: the agent (receives commands) and the client (sends commands)."""
import asyncio
import json
import logging
import secrets
import time
import uuid
from fnmatch import fnmatchcase

import aiohttp

from ..commands.builtin import HANDLERS, Context
from ..commands.backend import BackendError
from .identity import Identity

RENEW_DAYS = 29  # just inside the hub's 30-day renewal window
log = logging.getLogger("jarvis.device")


async def _connect(session, ident: Identity, kind: str, hosts=None):
    last = None
    for host in hosts or ident.hub["hosts"]:
        try:
            return await session.ws_connect(f"wss://{host}:{ident.hub['ctl']}/ws?kind={kind}",
                                            ssl=ident.ssl_context(), heartbeat=20, max_msg_size=8 * 1024 * 1024)
        except (aiohttp.ClientError, OSError) as e:
            last = e
    raise ConnectionError(f"hub unreachable: {last!r}")


class Agent:
    def __init__(self, ident: Identity, ctx: Context | None = None):
        self.ident = ident
        self.ctx = ctx or Context(config=ident.config)

    def local_allowed(self, cmd: str) -> bool:
        """The owner's own allow-list. The hub cannot override this."""
        return any(fnmatchcase(cmd, p) for p in self.ident.config.get("accept", []))

    async def execute(self, cmd: str, args: dict) -> dict:
        self.ident.reload_config()          # `jarvis device allow ...` takes effect immediately
        self.ctx.config = self.ident.config
        if not self.local_allowed(cmd):
            return {"ok": False, "error": "not enabled on this device"}
        handler = HANDLERS.get(cmd)
        if handler is None:
            return {"ok": False, "error": "unsupported command"}
        try:
            data = await asyncio.get_running_loop().run_in_executor(None, handler, args, self.ctx)
            return {"ok": True, "data": data}
        except (ValueError, FileNotFoundError, BackendError) as e:
            return {"ok": False, "error": str(e)[:200]}      # our own validation messages: safe to show
        except Exception as e:
            log.warning("command %s failed: %r", cmd, e)
            return {"ok": False, "error": "command failed"}

    async def run(self, stop: asyncio.Event | None = None):
        delay = 1
        async with aiohttp.ClientSession() as session:
            while not (stop and stop.is_set()):
                try:
                    ws = await _connect(session, self.ident, "agent")
                    delay = 1
                    log.info("connected to hub")
                    if self.ident.days_left() < RENEW_DAYS:
                        await ws.send_str(json.dumps({"type": "renew", "csr": self.ident.renewal_csr()}))
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        data = json.loads(msg.data)
                        if data.get("type") == "renewed":
                            if data.get("ok"):
                                self.ident.install_cert(data["cert"])
                                log.info("certificate renewed")
                            else:
                                log.warning("certificate renewal refused by hub")
                        elif data.get("type") == "exec":
                            res = await self.execute(data["cmd"], data.get("args", {}))
                            await ws.send_str(json.dumps({"type": "result", "id": data["id"], **res}))
                except (ConnectionError, aiohttp.ClientError, OSError) as e:
                    log.info("disconnected (%s); retrying in %ss", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)


class Client:
    """Send commands to the hub (and through it to other devices) as this device. Many may be in flight."""

    def __init__(self, ident: Identity):
        self.ident = ident
        self._waiting: dict[str, asyncio.Future] = {}

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await _connect(self._session, self.ident, "client")
        except BaseException:
            await self._session.close()
            raise
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc):
        self._reader.cancel()
        await self._ws.close()
        await self._session.close()

    async def _read(self):
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                data = json.loads(msg.data)
                fut = self._waiting.pop(data.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(data)
        finally:
            for fut in self._waiting.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("connection closed"))
            self._waiting.clear()

    async def send(self, target: str, cmd: str, args: dict | None = None, timeout=60) -> dict:
        rid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self._waiting[rid] = fut
        await self._ws.send_str(json.dumps({
            "type": "cmd", "id": rid, "target": target, "cmd": cmd, "args": args or {},
            "ts": time.time(), "nonce": secrets.token_hex(16)}))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiting.pop(rid, None)
