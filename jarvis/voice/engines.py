"""Speech recognition engines. Audio is 16 kHz, 16-bit, mono PCM. Everything runs on this machine.

pocketsphinx (default): bundled English model, recognises only the command grammar, so it is small, fast and
    predictable. Speech that is not a command is rejected by two independent checks (see `_accept`).
whisper (optional): faster-whisper for free-form speech and arbitrary device/app names. Needs a one-time model
    download (`jarvis voice install --engine whisper`); not part of the default install.
"""
import difflib
import hashlib
import os
import threading
from dataclasses import dataclass

from . import intents

SAMPLE_RATE = 16000


class EngineUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Vocab:
    device_names: tuple = ()
    app_names: tuple = ()


@dataclass
class Result:
    text: str
    accepted: bool
    confidence: float = 0.0
    reason: str = ""


def _strip_noise_words(s: str) -> str:
    return " ".join(w for w in s.split() if w not in ("jarvis", "hey", "ok", "okay", "please"))


class PocketSphinxEngine:
    name = "pocketsphinx"

    # Accept when the grammar decoder is confident, OR the free-form decoder independently heard the same words.
    # Calibrated on synthetic voices (see docs/SECURITY.md): real microphones will differ, so both are configurable.
    def __init__(self, min_confidence=0.70, min_agreement=0.50):
        try:
            from pocketsphinx import Decoder, get_model_path
        except ImportError as e:
            raise EngineUnavailable("pocketsphinx is not installed (pip install 'jarvis[voice]')") from e
        self._Decoder = Decoder
        mp = os.path.join(get_model_path(), "en-us")
        self._common = dict(hmm=os.path.join(mp, "en-us"), dict=os.path.join(mp, "cmudict-en-us.dict"), loglevel="FATAL")
        self._free = Decoder(**self._common, lm=os.path.join(mp, "en-us.lm.bin"))
        self._grammars: dict[str, object] = {}
        self._lock = threading.Lock()
        self.min_confidence, self.min_agreement = min_confidence, min_agreement

    def _grammar_decoder(self, vocab: Vocab):
        known = lambda w: self._free.lookup_word(w) is not None
        text = intents.jsgf(vocab.device_names, vocab.app_names, known)
        key = hashlib.sha1(text.encode()).hexdigest()
        dec = self._grammars.get(key)
        if dec is None:
            dec = self._Decoder(**self._common)
            dec.add_jsgf_string("cmd", text)
            dec.activate_search("cmd")
            if len(self._grammars) >= 8:
                self._grammars.pop(next(iter(self._grammars)))
            self._grammars[key] = dec
        return dec

    @staticmethod
    def _decode(dec, pcm: bytes):
        dec.start_utt()
        dec.process_raw(pcm, False, True)
        dec.end_utt()
        return dec.hyp()

    def transcribe(self, pcm: bytes, vocab: Vocab = Vocab()) -> Result:
        with self._lock:
            g = self._decode(self._grammar_decoder(vocab), pcm)
            if g is None or not g.hypstr.strip():
                return Result("", False, 0.0, "nothing recognised")
            f = self._decode(self._free, pcm)
        heard = g.hypstr.strip()
        free = f.hypstr.strip() if f else ""
        agreement = difflib.SequenceMatcher(None, _strip_noise_words(heard), _strip_noise_words(free)).ratio()
        ok = g.best_score >= self.min_confidence or agreement >= self.min_agreement
        return Result(heard if ok else "", ok, round(float(g.best_score), 3), "" if ok else "not a command")


class WhisperEngine:
    name = "whisper"

    def __init__(self, model="base.en", model_dir=None, local_only=True):
        try:
            import numpy
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise EngineUnavailable("faster-whisper is not installed (pip install faster-whisper)") from e
        self._np = numpy
        try:
            self._model = WhisperModel(model, device="auto", compute_type="int8",
                                       download_root=model_dir, local_files_only=local_only)
        except Exception as e:
            raise EngineUnavailable(f"whisper model '{model}' is not downloaded; run `jarvis voice install --engine whisper`") from e
        self._lock = threading.Lock()

    def transcribe(self, pcm: bytes, vocab: Vocab = Vocab()) -> Result:
        audio = self._np.frombuffer(pcm, dtype=self._np.int16).astype(self._np.float32) / 32768.0
        with self._lock:
            segments, _ = self._model.transcribe(
                audio, language="en", beam_size=1, vad_filter=True, condition_on_previous_text=False,
                initial_prompt=intents.prompt_hint(vocab.device_names, vocab.app_names))
            text = " ".join(s.text.strip() for s in segments).strip()
        return Result(text, bool(text), 1.0 if text else 0.0, "" if text else "nothing recognised")


def download_whisper(model="base.en", model_dir=None):
    """One-time download (needs internet)."""
    try:
        from faster_whisper.utils import download_model
    except ImportError as e:
        raise EngineUnavailable("pip install faster-whisper first") from e
    return download_model(model, cache_dir=model_dir)


def load_engine(preference="auto", **kw):
    """auto: Whisper if installed and downloaded, else PocketSphinx. 'off' returns None."""
    if preference == "off":
        return None
    errors = []
    order = {"auto": ("whisper", "pocketsphinx"), "whisper": ("whisper",), "pocketsphinx": ("pocketsphinx",)}[preference]
    for name in order:
        try:
            return WhisperEngine(**kw.get("whisper", {})) if name == "whisper" else PocketSphinxEngine(**kw.get("pocketsphinx", {}))
        except EngineUnavailable as e:
            errors.append(str(e))
    if preference == "auto":
        return None
    raise EngineUnavailable("; ".join(errors))
