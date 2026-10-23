"""The device owner's own permissions. The hub cannot change these."""
from fnmatch import fnmatchcase

from ..security.policy import CATALOG


def _check_pattern(p: str):
    if p in ("*", "**", "") or not any(fnmatchcase(c, p) for c in CATALOG):
        raise ValueError(f"'{p}' matches no known command (use names like media.*, input.*, files.list)")


def allow(ident, patterns):
    for p in patterns:
        _check_pattern(p)
    acc = ident.config.setdefault("accept", [])
    acc.extend(p for p in patterns if p not in acc)
    ident.save_config()
    return acc


def disallow(ident, patterns):
    ident.config["accept"] = [p for p in ident.config.get("accept", []) if p not in patterns]
    ident.save_config()
    return ident.config["accept"]


def app_add(ident, name: str, argv: list[str]):
    if not name.replace("-", "").replace("_", "").isalnum() or not argv:
        raise ValueError("app name must be simple (letters, digits, - _) and a command is required")
    ident.config.setdefault("apps", {})[name] = list(argv)
    ident.save_config()


def app_remove(ident, name: str):
    ident.config.get("apps", {}).pop(name, None)
    ident.save_config()
