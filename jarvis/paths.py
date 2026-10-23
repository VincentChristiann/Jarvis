"""Filesystem helpers that keep secrets private to the owning user."""
import json
import os
from pathlib import Path


def home_dir(override: str | None = None) -> Path:
    return Path(override or os.environ.get("JARVIS_HOME") or Path.home() / ".jarvis")


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass  # Windows: inherits the user profile ACL
    return path


def write_private(path: Path, data: bytes) -> None:
    """Atomically write a file readable only by the current user."""
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path: Path, obj) -> None:
    write_private(path, json.dumps(obj, indent=2, sort_keys=True).encode())


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
