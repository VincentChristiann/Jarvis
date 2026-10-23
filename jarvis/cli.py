"""jarvis command line: hub administration and device control."""
import argparse
import asyncio
import json
import logging
import sys

import aiohttp
import qrcode

from .device.identity import Identity, PairingError, pair
from .hub.server import Hub, hub_dir, init_hub
from .paths import home_dir
from .security.audit import AuditLog
from .voice.engines import EngineUnavailable


def _show_qr(text: str):
    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    qr.print_ascii(invert=True)


async def _admin(args, method, path, body=None):
    d = hub_dir(args.home)
    token = (d / "admin.token").read_text().strip()
    async with aiohttp.ClientSession(headers={"Authorization": f"Bearer {token}"}) as s:
        async with s.request(method, f"http://127.0.0.1:{args.admin_port}{path}", json=body) as r:
            r.raise_for_status()
            return await r.json()


def _table(rows, cols):
    for r in rows:
        print("  ".join(f"{k}={r[k]}" for k in cols))
    if not rows:
        print("(none)")


async def hub_serve(args):
    hub = Hub(args.home, host=args.host, pair_port=args.pair_port, ctl_port=args.ctl_port,
              admin_port=args.admin_port, advertise=args.advertise, hub_class=args.hub_class, voice_engine=args.voice_engine)
    await hub.start()
    print(f"hub running  pair:{hub.ports['pair']} control:{hub.ports['ctl']} admin(127.0.0.1):{hub.ports['admin']}")
    print(f"addresses: {', '.join(hub.addresses)}")
    try:
        from .net.discovery import advertise
        await advertise(hub.addresses, hub.ports["ctl"], hub.ports["pair"])
        print("mDNS advertising as jarvis.local")
    except Exception as e:  # discovery is a convenience, never a requirement
        print(f"mDNS unavailable ({e}); devices must use the pairing link's address")
    await asyncio.Event().wait()


async def hub_cmd(args):
    c = args.hub_cmd
    if c == "pair":
        r = await _admin(args, "POST", "/admin/invite", {"role": args.role, "hosts": args.host or None,
                                                         "kind": "web" if args.web else "app"})
        _show_qr(r["url"])
        if args.web:
            print(f"Phone browser: first install the hub certificate from {r['ca_url']}\nand check its SHA-256 fingerprint is {r['ca_fp']}")
        print(f"\nRole: {r['role']}  expires in {r['expires_in']}s\nLink: {r['url']}\n")
        print("Then run `jarvis hub pending` and `jarvis hub approve <id>` after checking the code matches.")
    elif c == "dashboard":
        r = await _admin(args, "POST", "/admin/login-code")
        url = f"http://127.0.0.1:{args.admin_port}/login?code={r['code']}"
        print(f"Open this link in a browser on THIS machine (valid {r['expires_in']}s, one use):\n{url}")
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    elif c == "camera-off":
        print(await _admin(args, "POST", "/admin/mode", {"mode": "idle"}))
    elif c == "pending":
        _table(await _admin(args, "GET", "/admin/pending"), ["id", "name", "platform", "class", "role", "sas", "ip"])
    elif c in ("approve", "reject"):
        print(await _admin(args, "POST", "/admin/resolve", {"id": args.id, "approve": c == "approve"}))
    elif c == "devices":
        _table(await _admin(args, "GET", "/admin/devices"), ["id", "name", "class", "role", "online", "revoked"])
    elif c == "revoke":
        print(await _admin(args, "POST", "/admin/revoke", {"id": args.id}))
    elif c == "confirms":
        _table(await _admin(args, "GET", "/admin/confirmations"), ["id", "sender", "target", "cmd"])
    elif c in ("allow", "deny"):
        print(await _admin(args, "POST", "/admin/confirm", {"id": args.id, "approve": c == "allow"}))
    elif c == "audit":
        path = hub_dir(args.home) / "audit.log"
        ok, bad = AuditLog.verify(path) if path.exists() else (True, None)
        print("chain OK" if ok else f"TAMPERED at line {bad}")
        for r in AuditLog.tail(path, args.n):
            print(json.dumps(r))


async def device_cmd(args):
    c = args.dev_cmd
    if c == "pair":
        r = await pair(args.link, args.name, args.cls, args.home)
        print(f"Paired. device id {r['device_id']}")
    elif c == "run":
        from .device.agent import Agent
        print("agent running; ctrl-c to stop")
        await Agent(Identity(args.home)).run()
    elif c in ("allow", "disallow", "config", "app"):
        from .device import localconfig as lc
        ident = Identity(args.home)
        if c == "allow":
            print("accepting:", lc.allow(ident, args.patterns))
        elif c == "disallow":
            print("accepting:", lc.disallow(ident, args.patterns))
        elif c == "app":
            if args.app_cmd == "add":
                lc.app_add(ident, args.name, args.argv)
            else:
                lc.app_remove(ident, args.name)
            print("apps:", ident.config.get("apps", {}))
        else:
            print(json.dumps(ident.config, indent=2))
    elif c == "send":
        from .device.agent import Client
        extra = dict(kv.split("=", 1) for kv in args.arg)
        async with Client(Identity(args.home)) as cl:
            print(json.dumps(await cl.send(args.target, args.command, extra), indent=2))


def build_parser():
    p = argparse.ArgumentParser(prog="jarvis")
    p.add_argument("--home", help="data directory (default ~/.jarvis or $JARVIS_HOME)")
    p.add_argument("--admin-port", type=int, default=8445)
    sub = p.add_subparsers(dest="group", required=True)

    hub = sub.add_parser("hub").add_subparsers(dest="hub_cmd", required=True)
    hub.add_parser("init")
    s = hub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--pair-port", type=int, default=8443)
    s.add_argument("--ctl-port", type=int, default=8444)
    s.add_argument("--hub-class", choices=["laptop", "desktop", "other"], default="laptop",
                   help="what this machine is; only laptops/desktops may use the camera")
    s.add_argument("--voice-engine", choices=["auto", "pocketsphinx", "whisper", "off"], default="auto",
                   help="speech recognition: auto uses Whisper if installed, else the bundled PocketSphinx")
    s.add_argument("--advertise", nargs="*", default=[], help="addresses to put in certificates and links (hotspot/overlay)")
    s = hub.add_parser("pair")
    s.add_argument("--role", default="phone", choices=["phone", "controller", "node"])
    s.add_argument("--host", nargs="*", help="override the addresses shown in the link")
    s.add_argument("--web", action="store_true", help="pair a phone browser instead of a Jarvis app")
    for name in ("pending", "devices", "confirms", "dashboard", "camera-off"):
        hub.add_parser(name)
    for name in ("approve", "reject", "revoke", "allow", "deny"):
        hub.add_parser(name).add_argument("id")
    s = hub.add_parser("audit")
    s.add_argument("-n", type=int, default=20)

    vis = sub.add_parser("vision", help="gesture tracking assets").add_subparsers(dest="vis_cmd", required=True)
    vis.add_parser("install", help="download and verify the hand-tracking files once (needs internet)")

    vo = sub.add_parser("voice", help="speech recognition").add_subparsers(dest="voice_cmd", required=True)
    s = vo.add_parser("install", help="optional: download the Whisper model (needs internet and `pip install faster-whisper`)")
    s.add_argument("--engine", choices=["whisper"], default="whisper")
    s.add_argument("--model", default="base.en")
    vo.add_parser("check", help="show which speech engine the hub would use")

    svc = sub.add_parser("service", help="start Jarvis automatically").add_subparsers(dest="svc_cmd", required=True)
    for name in ("install", "uninstall"):
        s = svc.add_parser(name)
        s.add_argument("role", choices=["hub", "device"])
        s.add_argument("--system", action="store_true", help="Linux: system-wide unit (needs root and --user)")
        s.add_argument("--user", help="Linux system unit: account to run as")
        if name == "install":
            s.add_argument("--dry-run", action="store_true")

    dev = sub.add_parser("device").add_subparsers(dest="dev_cmd", required=True)
    s = dev.add_parser("pair")
    s.add_argument("link")
    s.add_argument("--name", required=True)
    s.add_argument("--class", dest="cls", choices=["laptop", "desktop", "phone", "other"], default="laptop")
    dev.add_parser("run")
    dev.add_parser("config", help="show what this device accepts")
    for name in ("allow", "disallow"):
        dev.add_parser(name, help=f"{name} command families on this device, e.g. input.* files.*").add_argument("patterns", nargs="+")
    app = dev.add_parser("app", help="apps the hub may launch on this device").add_subparsers(dest="app_cmd", required=True)
    s = app.add_parser("add")
    s.add_argument("name")
    s.add_argument("argv", nargs="+", help="program and arguments")
    app.add_parser("remove").add_argument("name")
    s = dev.add_parser("send")
    s.add_argument("target", help="device id or 'hub'")
    s.add_argument("command")
    s.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        if args.group == "hub" and args.hub_cmd == "init":
            print(f"hub initialised at {init_hub(args.home)}")
        elif args.group == "hub" and args.hub_cmd == "serve":
            asyncio.run(hub_serve(args))
        elif args.group == "hub":
            asyncio.run(hub_cmd(args))
        elif args.group == "voice":
            from .voice import engines
            if args.voice_cmd == "install":
                print("downloading", args.model, "…")
                print("installed at", engines.download_whisper(args.model))
            else:
                eng = engines.load_engine("auto")
                print(f"speech engine: {eng.name}" if eng else "no speech engine available: pip install 'jarvis[voice]'")
        elif args.group == "vision":
            from . import vision_assets
            vision_assets.main()
        elif args.group == "service":
            from . import service
            if args.svc_cmd == "install":
                print(service.install(args.role, str(home_dir(args.home)), args.system, args.user, args.dry_run))
            else:
                print(service.uninstall(args.role, args.system))
        else:
            asyncio.run(device_cmd(args))
    except (PairingError, ConnectionError, aiohttp.ClientError, FileNotFoundError, RuntimeError, ValueError, PermissionError, ImportError, EngineUnavailable) as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
