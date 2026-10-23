"""Device identity on disk, and the pairing client."""
import asyncio
import datetime
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from cryptography.hazmat.primitives import serialization

from ..commands.builtin import DEFAULT_LOCAL_ACCEPT
from ..hub.pairing import sas_code
from ..paths import home_dir, private_dir, read_json, write_json, write_private
from ..security import pki


def device_dir(home=None) -> Path:
    return home_dir(home) / "device"


class PairingError(Exception):
    pass


def parse_invite(url: str) -> dict:
    u = urlparse(url)
    if u.scheme != "jarvis" or u.netloc != "pair":
        raise PairingError("not a jarvis pairing link")
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    try:
        return {"hosts": q["host"].split(","), "port": int(q["port"]), "ctl": int(q["ctl"]),
                "token": q["token"], "pin": bytes.fromhex(q["pin"])}
    except (KeyError, ValueError):
        raise PairingError("malformed pairing link")


def default_class() -> str:
    return "laptop"


async def pair(url: str, name: str, device_class: str = None, home=None, show_code=print, timeout=120) -> dict:
    inv = parse_invite(url)
    d = private_dir(device_dir(home))
    if (d / "cert.pem").exists():
        raise PairingError(f"already paired ({d}); remove it first to re-pair")
    key = pki.new_key()
    csr = pki.make_csr(key)
    # Pin the hub's pairing certificate from the link: a fake hotspot cannot read our token.
    ssl_pin = aiohttp.Fingerprint(inv["pin"])
    code = sas_code(inv["token"], pki.csr_der(csr))
    show_code(f"Confirmation code: {code}  (approve on the hub only if it shows the same code)")
    body = {"token": inv["token"], "csr": csr.public_bytes(serialization.Encoding.PEM).decode(),
            "name": name, "platform": sys.platform, "device_class": device_class or default_class()}
    last_err = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
        for host in inv["hosts"]:
            try:
                async with s.post(f"https://{host}:{inv['port']}/pair", json=body, ssl=ssl_pin) as r:
                    if r.status == 200:
                        resp = await r.json()
                        break
                    raise PairingError(f"hub refused pairing: {(await r.json()).get('error', r.status)}")
            except (aiohttp.ClientConnectorError, aiohttp.ServerFingerprintMismatch, asyncio.TimeoutError) as e:
                last_err = repr(e)
        else:
            raise PairingError(f"could not reach hub: {last_err}")
    write_private(d / "key.pem", pki.key_pem(key))
    write_private(d / "cert.pem", resp["cert"].encode())
    write_private(d / "ca.pem", resp["ca"].encode())
    write_json(d / "hub.json", {"hosts": inv["hosts"], "ctl": inv["ctl"], "device_id": resp["device_id"], "name": name})
    write_json(d / "config.json", {"accept": DEFAULT_LOCAL_ACCEPT})
    return resp


class Identity:
    def __init__(self, home=None):
        self.dir = device_dir(home)
        hub = read_json(self.dir / "hub.json")
        if hub is None:
            raise PairingError("this device is not paired; run `jarvis device pair <link>`")
        self.hub = hub
        self.device_id = hub["device_id"]
        self.config = read_json(self.dir / "config.json", {"accept": DEFAULT_LOCAL_ACCEPT})

    def ssl_context(self):
        return pki.client_context((self.dir / "ca.pem").read_bytes(), self.dir / "cert.pem", self.dir / "key.pem")

    def days_left(self) -> float:
        cert = pki.load_cert((self.dir / "cert.pem").read_bytes())
        return (cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 86400

    def renewal_csr(self) -> str:
        """CSR over the existing key, so installing the new certificate is a single atomic file swap."""
        key = pki.load_key((self.dir / "key.pem").read_bytes())
        return pki.make_csr(key).public_bytes(serialization.Encoding.PEM).decode()

    def install_cert(self, pem: str):
        cert = pki.load_cert(pem.encode())
        key = pki.load_key((self.dir / "key.pem").read_bytes())
        if cert.public_key().public_numbers() != key.public_key().public_numbers() or pki.cert_cn(cert) != self.device_id:
            raise PairingError("renewed certificate does not match this device")
        write_private(self.dir / "cert.pem", pem.encode())

    def reload_config(self):
        self.config = read_json(self.dir / "config.json", {"accept": DEFAULT_LOCAL_ACCEPT})

    def save_config(self):
        write_json(self.dir / "config.json", self.config)
