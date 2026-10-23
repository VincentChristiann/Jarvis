import asyncio
import base64

import pytest

from jarvis.commands import backend as be
from jarvis.commands.builtin import HANDLERS, Context
from jarvis.device import localconfig as lc
from jarvis.device.agent import Agent, Client
from jarvis.device.identity import Identity
from jarvis.security import policy

from .conftest import pair_device


class FakeBackend:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def rec(*a):
            self.calls.append((name, *a))
        return rec


def ctx(tmp_path, **cfg):
    return Context(config={"share_dir": str(tmp_path / "share"), **cfg}, backend=FakeBackend())


# ---- validation -------------------------------------------------------------
def test_every_catalog_command_has_a_handler():
    assert set(policy.CATALOG) - {"hub.devices"} == set(HANDLERS)


@pytest.mark.parametrize("cmd,args", [
    ("volume.set", {"percent": 101}), ("volume.set", {"percent": "50"}), ("volume.set", {"percent": True}),
    ("volume.step", {"delta": 99}), ("input.move", {"dx": 5000, "dy": 0}), ("input.move", {"dx": 1}),
    ("input.click", {"button": "evil"}), ("input.click", {"count": 3}), ("input.scroll", {"amount": 500}),
    ("input.type", {"text": ""}), ("input.type", {"text": "a" * 201}), ("input.type", {"text": "rm\n"}),
    ("input.key", {"key": "ctrl+alt+;"}), ("input.key", {"key": "meta+a"}), ("input.key", {"key": "ctrl+ctrl+a"}),
])
def test_bad_arguments_rejected(tmp_path, cmd, args):
    c = ctx(tmp_path)
    with pytest.raises(ValueError):
        HANDLERS[cmd](args, c)
    assert c.backend.calls == []


def test_good_arguments_reach_backend(tmp_path):
    c = ctx(tmp_path)
    HANDLERS["volume.set"]({"percent": 40}, c)
    HANDLERS["input.move"]({"dx": -5, "dy": 9}, c)
    HANDLERS["input.click"]({}, c)
    HANDLERS["input.key"]({"key": "ctrl+shift+t"}, c)
    HANDLERS["media.next"]({}, c)
    assert c.backend.calls == [("volume_set", 40), ("mouse_move", -5, 9), ("click", "left", 1),
                               ("key", "ctrl+shift+t"), ("media", "next")]


def test_key_combo_parser():
    assert be.parse_combo("Ctrl+Shift+T") == (["ctrl", "shift"], "t")
    assert be.parse_combo("f12") == ([], "f12")
    for bad in ("f13", "ctrl+", "", "a+b", "ctrl+enter+x"):
        with pytest.raises(ValueError):
            be.parse_combo(bad)


# ---- Linux backend builds argument lists, no shell ---------------------------
def test_linux_backend_commands():
    calls = []
    lb = be.LinuxBackend(run=lambda argv, **k: calls.append(argv), which=lambda n: "/usr/bin/" + n)
    lb.mouse_move(3, -4)
    lb.scroll(-2)
    lb.type_text("hi; rm -rf /")
    lb.key("ctrl+c")
    lb.volume_step(-5)
    assert calls[0] == ["xdotool", "mousemove_relative", "--", "3", "-4"]
    assert calls[1] == ["xdotool", "click", "--repeat", "2", "5"]
    assert calls[2] == ["xdotool", "type", "--delay", "8", "--", "hi; rm -rf /"]  # one argv item, never a shell string
    assert calls[3] == ["xdotool", "key", "--", "ctrl+c"]
    assert calls[4][:3] == ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@"] and calls[4][3] == "5%-"


def test_backend_reports_missing_tool():
    lb = be.LinuxBackend(run=lambda *a, **k: None, which=lambda n: None)
    with pytest.raises(be.BackendError, match="xdotool"):
        lb.mouse_move(1, 1)


# ---- file sandbox -----------------------------------------------------------
def test_files_roundtrip_and_sandbox(tmp_path):
    c = ctx(tmp_path)
    data = base64.b64encode(b"hello").decode()
    assert HANDLERS["files.put"]({"path": "notes/a.txt", "b64": data}, c)["size"] == 5
    assert HANDLERS["files.list"]({"path": "notes"}, c)["entries"] == [{"name": "a.txt", "dir": False, "size": 5}]
    assert base64.b64decode(HANDLERS["files.get"]({"path": "notes/a.txt"}, c)["b64"]) == b"hello"
    with pytest.raises(ValueError, match="exists"):
        HANDLERS["files.put"]({"path": "notes/a.txt", "b64": data}, c)
    HANDLERS["files.put"]({"path": "notes/a.txt", "b64": data, "overwrite": True}, c)


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../b", "..", "x\x00y"])
def test_path_traversal_blocked(tmp_path, path):
    c = ctx(tmp_path)
    for cmd in ("files.get", "files.list"):
        with pytest.raises((ValueError, FileNotFoundError)):
            HANDLERS[cmd]({"path": path}, c)
    with pytest.raises(ValueError):
        HANDLERS["files.put"]({"path": path, "b64": "aGk="}, c)


def test_symlink_escape_blocked(tmp_path):
    c = ctx(tmp_path)
    (tmp_path / "share").mkdir()
    (tmp_path / "outside.txt").write_text("secret")
    (tmp_path / "share" / "link").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(ValueError, match="escapes"):
        HANDLERS["files.get"]({"path": "link"}, c)
    with pytest.raises(ValueError):
        HANDLERS["files.put"]({"path": "link", "b64": "aGk=", "overwrite": True}, c)
    assert (tmp_path / "outside.txt").read_text() == "secret"


def test_file_size_and_encoding_limits(tmp_path):
    c = ctx(tmp_path)
    big = base64.b64encode(b"x" * (4 * 1024 * 1024 + 1)).decode()
    with pytest.raises(ValueError, match="too large"):
        HANDLERS["files.put"]({"path": "big", "b64": big}, c)
    with pytest.raises(ValueError, match="invalid"):
        HANDLERS["files.put"]({"path": "bad", "b64": "not base64!!"}, c)


# ---- apps ---------------------------------------------------------------------
def test_app_open_only_configured_names(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr("jarvis.commands.builtin.subprocess.Popen", lambda argv, **k: started.append(argv))
    c = ctx(tmp_path, apps={"calc": ["gnome-calculator", "--x"]})
    HANDLERS["app.open"]({"name": "calc"}, c)
    assert started == [["gnome-calculator", "--x"]]
    for bad in ({"name": "bash"}, {"name": ["calc"]}, {}):
        with pytest.raises(ValueError):
            HANDLERS["app.open"](bad, c)
    assert len(started) == 1


# ---- owner allow-list helpers --------------------------------------------------
async def test_local_config_changes(hub, tmp_path):
    ident = await pair_device(hub, tmp_path, "pc", "node", "desktop")
    assert "input.*" not in ident.config["accept"] and "files.*" not in ident.config["accept"]
    lc.allow(ident, ["input.*", "files.list"])
    assert "input.*" in Identity(tmp_path / "pc").config["accept"]
    for bad in ("*", "nonsense.*", ""):
        with pytest.raises(ValueError):
            lc.allow(ident, [bad])
    lc.disallow(ident, ["input.*"])
    assert "input.*" not in Identity(tmp_path / "pc").config["accept"]
    with pytest.raises(ValueError):
        lc.app_add(ident, "bad name!", ["x"])


# ---- end to end through the hub: control windows -------------------------------
async def wait_online(hub, ident):
    for _ in range(100):
        if ident.device_id in hub.agents:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("agent never connected")


async def resolve_confirm(hub, approve=True, n=1):
    done = 0
    for _ in range(200):
        for cid, c in list(hub.confirmations.items()):
            c["approved"] = approve
            c["event"].set()
            done += 1
        if done >= n:
            return done
        await asyncio.sleep(0.02)
    return done


async def test_input_needs_one_approval_then_window_is_open(hub, tmp_path):
    phone = await pair_device(hub, tmp_path, "phone", "phone", "phone")
    node = await pair_device(hub, tmp_path, "pc", "node", "desktop", accept=["input.*", "media.*"])
    fake = FakeBackend()
    agent = asyncio.create_task(Agent(node, Context(config=node.config, backend=fake)).run())
    try:
        await wait_online(hub, node)
        async with Client(phone) as c:
            # 1) no approval -> the command is refused
            hub.confirm_timeout = 0.3
            r = await c.send(node.device_id, "input.move", {"dx": 1, "dy": 1})
            assert not r["ok"] and "control window" in r["error"] and fake.calls == []
            # 2) a burst of commands while the operator approves once -> a single prompt, all succeed
            hub.confirm_timeout = 5
            approver = asyncio.create_task(resolve_confirm(hub))
            rs = await asyncio.gather(*[c.send(node.device_id, "input.move", {"dx": i, "dy": 0}) for i in range(5)])
            assert all(r["ok"] for r in rs), rs
            assert await approver == 1
            # 3) the window is now open: no further prompt needed
            assert (await c.send(node.device_id, "input.click"))["ok"]
            assert len(hub.grants) == 1
            # 4) a different family (media) never needed a window
            assert (await c.send(node.device_id, "media.next"))["ok"]
            # 5) closing the window blocks input again
            key = next(iter(hub.grants))
            hub._drop_grants(key=key)
            hub.confirm_timeout = 0.3
            assert not (await c.send(node.device_id, "input.click"))["ok"]
    finally:
        agent.cancel()


async def test_revoking_sender_closes_its_windows(hub, tmp_path):
    phone = await pair_device(hub, tmp_path, "phone", "phone", "phone")
    node = await pair_device(hub, tmp_path, "pc", "node", "desktop", accept=["input.*"])
    agent = asyncio.create_task(Agent(node, Context(config=node.config, backend=FakeBackend())).run())
    try:
        await wait_online(hub, node)
        async with Client(phone) as c:
            approver = asyncio.create_task(resolve_confirm(hub))
            assert (await c.send(node.device_id, "input.move", {"dx": 1, "dy": 1}))["ok"]
            await approver
        assert hub.grants
        hub._drop_grants(device_id=phone.device_id)
        assert not hub.grants
    finally:
        agent.cancel()


async def test_device_owner_veto_beats_open_window(hub, tmp_path):
    ctrl = await pair_device(hub, tmp_path, "laptop", "controller", "laptop")
    node = await pair_device(hub, tmp_path, "pc", "node", "desktop", accept=["ping"])  # owner never enabled input
    fake = FakeBackend()
    agent = asyncio.create_task(Agent(node, Context(config=node.config, backend=fake)).run())
    try:
        await wait_online(hub, node)
        async with Client(ctrl) as c:
            approver = asyncio.create_task(resolve_confirm(hub))
            r = await c.send(node.device_id, "input.type", {"text": "hello"})
            await approver
            assert not r["ok"] and "not enabled" in r["error"] and fake.calls == []
    finally:
        agent.cancel()


async def test_allow_takes_effect_without_restart(hub, tmp_path):
    ctrl = await pair_device(hub, tmp_path, "laptop", "controller", "laptop")
    node = await pair_device(hub, tmp_path, "pc", "node", "desktop")
    agent = asyncio.create_task(Agent(node, Context(config=node.config, backend=FakeBackend())).run())
    try:
        await wait_online(hub, node)
        async with Client(ctrl) as c:
            assert not (await c.send(node.device_id, "files.list"))["ok"]
            lc.allow(Identity(tmp_path / "pc"), ["files.list"])
            node.config["share_dir"] = str(tmp_path / "s")
            assert (await c.send(node.device_id, "files.list"))["ok"]
    finally:
        agent.cancel()
