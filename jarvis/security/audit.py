"""Append-only, hash-chained audit log. Editing or deleting any line breaks the chain."""
import hashlib
import json
import os
import time
from pathlib import Path

GENESIS = "0" * 64


def _digest(prev: str, record: dict) -> str:
    body = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._last = GENESIS
        if path.exists():
            ok, _ = self.verify(path)
            if not ok:
                raise RuntimeError(f"audit log {path} failed verification; refusing to append")
            lines = path.read_text().splitlines()
            if lines:
                self._last = json.loads(lines[-1])["h"]

    def write(self, event: str, **fields) -> None:
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        h = _digest(self._last, record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps({"h": h, "r": record}, sort_keys=True) + "\n")
        self._last = h

    @staticmethod
    def verify(path: Path):
        """Return (ok, first_bad_line_number or None)."""
        prev = GENESIS
        for n, line in enumerate(path.read_text().splitlines(), 1):
            try:
                entry = json.loads(line)
                if _digest(prev, entry["r"]) != entry["h"]:
                    return False, n
                prev = entry["h"]
            except (ValueError, KeyError):
                return False, n
        return True, None

    @staticmethod
    def tail(path: Path, n: int = 20):
        if not path.exists():
            return []
        return [json.loads(l)["r"] for l in path.read_text().splitlines()[-n:]]
