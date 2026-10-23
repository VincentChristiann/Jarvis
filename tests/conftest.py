import asyncio

import pytest
import pytest_asyncio

from jarvis.device.identity import Identity, pair
from jarvis.hub.server import Hub, init_hub


@pytest_asyncio.fixture
async def hub(tmp_path):
    init_hub(tmp_path / "hub_home")
    h = Hub(tmp_path / "hub_home", host="127.0.0.1", pair_port=0, ctl_port=0, admin_port=0, mobile_port=0,
            advertise=["127.0.0.1"], cmd_timeout=3, pair_wait=3, confirm_timeout=2)
    await h.start()
    yield h
    await h.stop()


def link(hub, role="phone"):
    token = hub.pairing.create_invite(role)
    return (f"jarvis://pair?host=127.0.0.1&port={hub.ports['pair']}&ctl={hub.ports['ctl']}"
            f"&token={token}&pin={hub.pair_pin}"), token


async def approve_next(hub, approve=True):
    for _ in range(100):
        if hub.pairing.pending:
            pid = next(iter(hub.pairing.pending))
            hub.pairing.resolve(pid, approve)
            return
        await asyncio.sleep(0.02)


async def pair_device(hub, tmp_path, name, role="phone", cls="laptop", accept=None):
    home = tmp_path / name
    url, _ = link(hub, role)
    task = asyncio.create_task(approve_next(hub))
    await pair(url, name, cls, home, show_code=lambda s: None)
    await task
    ident = Identity(home)
    if accept is not None:
        from jarvis.paths import write_json
        write_json(ident.dir / "config.json", {"accept": accept})
        ident = Identity(home)
    return ident
