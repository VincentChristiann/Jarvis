"""Real speech through the real recogniser. Audio is synthesised offline with ffmpeg's flite voices."""
import shutil
import subprocess
import sys
import types

import pytest

pytest.importorskip("pocketsphinx")

from jarvis.voice import intents
from jarvis.voice.engines import PocketSphinxEngine, Vocab, WhisperEngine, EngineUnavailable, load_engine

VOICES = ["slt", "awb"]
POSITIVE = [
    ("jarvis next track", "media.next"), ("jarvis previous track", "media.previous"), ("jarvis pause", "media.play_pause"),
    ("jarvis volume up", "volume.step"), ("jarvis volume down", "volume.step"), ("jarvis mute", "volume.mute"),
    ("jarvis lock the computer", "system.lock"), ("jarvis set volume to forty percent", "volume.set"),
    ("jarvis open firefox", "app.open"), ("next track", "media.next"), ("pause the music", "media.play_pause"),
]
NEGATIVE = [
    "the weather is quite nice today and I went for a long walk", "please send me the quarterly report before the meeting tomorrow",
    "I think we should order pizza for dinner tonight", "what did you say about the new apartment",
    "can you hand me that blue notebook from the table", "the train to london leaves at nine thirty",
    "we need to talk about your attitude",
]
DEVS = [{"id": "d1", "name": "workstation"}]
APPS = {"d1": ["firefox"]}
VOCAB = Vocab(("workstation",), ("firefox",))


def synth(text, voice):
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"flite=text='{text}':voice={voice}",
                        "-ar", "16000", "-ac", "1", "-f", "s16le", "-"], capture_output=True, timeout=30)
    if p.returncode != 0 or not p.stdout:
        pytest.skip("ffmpeg without flite: cannot synthesise test speech")
    return p.stdout


@pytest.fixture(scope="module")
def engine():
    return PocketSphinxEngine()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_commands_are_recognised_and_understood(engine):
    ok = total = 0
    for text, cmd in POSITIVE:
        for v in VOICES:
            r = engine.transcribe(synth(text, v), VOCAB)
            total += 1
            i = intents.parse(r.text, DEVS, APPS) if r.accepted else None
            ok += bool(i and i.cmd == cmd)
    assert ok / total >= 0.9, f"only {ok}/{total} commands understood"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_speech_that_is_not_a_command_is_rejected(engine):
    accepted = []
    for text in NEGATIVE:
        for v in VOICES:
            r = engine.transcribe(synth(text, v), VOCAB)
            if r.accepted and intents.parse(r.text, DEVS, APPS).kind == "command":
                accepted.append((v, text, r.text))
    assert len(accepted) <= 1, accepted             # measured 0 of 50 on a wider set; allow one for voice/library drift


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_device_name_in_the_spoken_command(engine):
    r = engine.transcribe(synth("jarvis next track on the workstation", "slt"), VOCAB)
    i = intents.parse(r.text, DEVS, APPS)
    assert (i.cmd, i.device_id) == ("media.next", "d1")


def test_silence_and_noise_are_rejected(engine):
    import random
    assert not engine.transcribe(b"\x00\x00" * 16000, VOCAB).accepted
    rnd = random.Random(1)
    noise = b"".join(int(rnd.gauss(0, 800)).to_bytes(2, "little", signed=True) for _ in range(16000))
    r = engine.transcribe(noise, VOCAB)
    assert not (r.accepted and intents.parse(r.text, DEVS, APPS).kind == "command")


def test_grammar_cache_is_bounded(engine):
    for i in range(12):
        engine.transcribe(b"\x00\x00" * 8000, Vocab((f"device{i}",), ()))
    assert len(engine._grammars) <= 8


# ---- optional Whisper engine: wiring only (the model is not available offline here) -----------------------
def test_whisper_engine_wiring_with_a_stub_library(monkeypatch):
    seen = {}

    class Seg:
        text = " Jarvis next track. "

    class FakeModel:
        def __init__(self, name, **kw):
            seen["init"] = (name, kw)

        def transcribe(self, audio, **kw):
            seen["audio"], seen["kw"] = audio, kw
            return [Seg()], None

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    eng = WhisperEngine(model="base.en")
    r = eng.transcribe((1000).to_bytes(2, "little", signed=True) * 160, Vocab(("workstation",), ("firefox",)))
    assert r.accepted and r.text == "Jarvis next track."
    assert seen["init"][1]["local_files_only"] is True                       # never downloads behind your back
    assert seen["audio"].dtype.name == "float32" and abs(seen["audio"][0] - 1000 / 32768) < 1e-6
    assert "workstation" in seen["kw"]["initial_prompt"] and seen["kw"]["vad_filter"] is True


def test_whisper_missing_model_falls_back(monkeypatch):
    mod = types.ModuleType("faster_whisper")

    def boom(*a, **k):
        raise OSError("not cached")
    mod.WhisperModel = boom
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    with pytest.raises(EngineUnavailable):
        WhisperEngine()
    assert load_engine("auto").name == "pocketsphinx"                        # auto falls back, does not crash
    assert load_engine("off") is None
    with pytest.raises(EngineUnavailable):
        load_engine("whisper")
