import pytest

from jarvis.security import policy
from jarvis.voice import intents
from jarvis.voice.intents import fold_numbers, normalize, parse

DEVS = [{"id": "d1", "name": "workstation"}, {"id": "d2", "name": "my-laptop"}, {"id": "d3", "name": "Living Room PC"}]
APPS = {"d1": ["firefox", "text-editor"], "d2": ["calculator"]}


def cmd(text, devices=DEVS, apps=APPS):
    i = parse(text, devices, apps)
    return i.kind, i.cmd, i.args, i.device_id


@pytest.mark.parametrize("text,expected", [
    ("play", ("command", "media.play_pause", {}, None)),
    ("Pause the music.", ("command", "media.play_pause", {}, None)),
    ("jarvis next track", ("command", "media.next", {}, None)),
    ("Hey Jarvis, skip this song!", ("command", "media.next", {}, None)),
    ("previous song", ("command", "media.previous", {}, None)),
    ("volume up", ("command", "volume.step", {"delta": 10}, None)),
    ("turn it down", ("command", "volume.step", {"delta": -10}, None)),
    ("louder please", ("command", "volume.step", {"delta": 10}, None)),
    ("could you lower the volume", ("command", "volume.step", {"delta": -10}, None)),
    ("mute", ("command", "volume.mute", {}, None)),
    ("lock the computer", ("command", "system.lock", {}, None)),
    ("ping", ("command", "ping", {}, None)),
])
def test_basic_commands(text, expected):
    assert cmd(text) == expected


@pytest.mark.parametrize("text,percent", [
    ("set volume to 40", 40), ("set the volume to forty percent", 40), ("volume seventy", 70),
    ("volume to forty five", 45), ("set volume to one hundred", 100), ("volume zero", 0),
    ("set volume to 5%", 5), ("volume at twenty", 20), ("volume ninety nine", 99),
])
def test_set_volume(text, percent):
    assert cmd(text)[:3] == ("command", "volume.set", {"percent": percent})


@pytest.mark.parametrize("text", ["set volume to 101", "volume 500", "volume to one thousand"])
def test_volume_out_of_range_is_refused_not_clamped(text):
    i = parse(text, DEVS, APPS)
    assert i.kind in ("invalid", "unknown") and not i.cmd


def test_number_folding():
    assert fold_numbers("set volume to twenty one".split()) == ["set", "volume", "to", "21"]
    assert fold_numbers("one hundred".split()) == ["100"]
    assert fold_numbers("fifteen".split()) == ["15"]
    assert fold_numbers("twenty twenty".split()) == ["20", "20"]          # not 40
    assert fold_numbers("ten five".split()) == ["10", "5"]
    assert normalize("Jarvis, NEXT track!!") == "jarvis next track"
    assert normalize("5%") == "5 percent"


def test_device_targeting():
    assert cmd("next track on the workstation")[3] == "d1"
    assert cmd("volume up on my laptop")[3] == "d2"
    assert cmd("mute on living room pc")[3] == "d3"
    assert cmd("jarvis pause for the workstation please")[3] == "d1"
    i = parse("lock the computer on workstation", DEVS, APPS)
    assert (i.cmd, i.device_name) == ("system.lock", "workstation")
    assert cmd("next track on the toaster")[0] == "unknown"                  # unknown device: do nothing


def test_apps_only_when_the_device_owner_configured_them():
    i = parse("open firefox", DEVS, APPS)
    assert (i.kind, i.cmd, i.args, i.device_id) == ("command", "app.open", {"name": "firefox"}, "d1")
    i = parse("launch text editor on the workstation", DEVS, APPS)
    assert i.args == {"name": "text-editor"} and i.device_id == "d1"
    assert parse("open calculator", DEVS, APPS).device_id == "d2"           # owner of that app is the target
    assert parse("open calculator on the workstation", DEVS, APPS).kind == "invalid"   # not on that device
    assert parse("open chrome", DEVS, APPS).kind == "invalid"
    assert parse("open bash", DEVS, APPS).kind == "invalid"
    assert parse("open firefox", DEVS, {}).kind == "invalid"


@pytest.mark.parametrize("text", [
    "", "   ", "the weather is nice today", "block the road", "play the guitar", "I want to unlock my phone",
    "lock", "volume", "set the volume", "turn it up to eleven", "next track and then lock the computer",
    "delete everything", "run rm -rf slash", "type hello world", "click", "open", "jarvis",
    "pausea", "mutex", "skipping stones",
])
def test_everything_else_is_unknown_and_runs_nothing(text):
    i = parse(text, DEVS, APPS)
    assert i.kind in ("unknown", "invalid") and not i.cmd


def test_wake_word_detection():
    assert parse("jarvis play", DEVS, APPS).wake
    assert parse("okay jarvis play", DEVS, APPS).wake
    assert not parse("play", DEVS, APPS).wake
    assert not parse("jarvisplay", DEVS, APPS).wake
    assert parse("jarvis", DEVS, APPS).kind == "unknown" and parse("jarvis", DEVS, APPS).wake


def test_help_and_devices():
    assert parse("what can you do", DEVS, APPS).kind == "help"
    assert parse("jarvis list devices", DEVS, APPS).kind == "devices"


def test_voice_can_only_produce_catalogued_non_input_commands():
    """Whatever the phrase table says, voice must never reach input.*, files.* or unknown commands."""
    for spec in intents.SPECS:
        assert spec.cmd in policy.CATALOG
        assert policy.family(spec.cmd) not in ("input", "files")


def test_every_phrase_in_the_table_parses_to_its_own_command():
    for spec in intents.SPECS:
        for ph in spec.phrases:
            i = parse(ph, DEVS, APPS)
            assert i.kind in ("command", "devices"), ph
            assert i.cmd == spec.cmd or spec.cmd == "hub.devices", ph
            assert i.args == dict(spec.args), ph


def test_grammar_covers_the_phrase_table():
    g = intents.jsgf(["workstation", "my-laptop"], ["firefox", "text-editor"])
    for spec in intents.SPECS:
        for ph in spec.phrases:
            assert ph in g
    assert "workstation" in g and "my laptop" in g and "text editor" in g
    assert "jarvis" in g and "<num>" in g


def test_grammar_drops_words_the_recognizer_cannot_pronounce():
    g = intents.jsgf(["zxqv-station"], ["frobnicator"], known_word=lambda w: w not in ("zxqv", "frobnicator"))
    assert "zxqv" not in g and "frobnicator" not in g and "next track" in g
