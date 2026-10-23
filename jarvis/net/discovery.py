"""LAN discovery with mDNS, so devices find the hub without internet or DNS."""
import asyncio
import socket

from zeroconf import ServiceInfo, ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

SERVICE = "_jarvis._tcp.local."


async def advertise(addresses, ctl_port, pair_port):
    zc = AsyncZeroconf()
    info = ServiceInfo(
        SERVICE, f"Jarvis Hub {socket.gethostname()}.{SERVICE}",
        addresses=[socket.inet_aton(a) for a in addresses if ":" not in a],
        port=ctl_port, properties={"pair": str(pair_port)}, server="jarvis.local.")
    await zc.async_register_service(info)
    return zc, info


async def discover(timeout=3.0):
    """Return [(address, ctl_port, pair_port)] for hubs on the local network."""
    found = []
    zc = AsyncZeroconf()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            asyncio.ensure_future(_resolve(zc, name, found))

    browser = AsyncServiceBrowser(zc.zeroconf, SERVICE, handlers=[on_change])
    await asyncio.sleep(timeout)
    await browser.async_cancel()
    await zc.async_close()
    return found


async def _resolve(zc, name, found):
    info = await zc.async_get_service_info(SERVICE, name)
    if info:
        for addr in info.parsed_addresses():
            found.append((addr, info.port, int(info.properties.get(b"pair", b"0"))))
