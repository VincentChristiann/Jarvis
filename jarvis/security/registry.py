"""Registry of paired devices."""
import time
from pathlib import Path

from ..paths import read_json, write_json


class Registry:
    def __init__(self, path: Path):
        self.path = path
        self._d: dict[str, dict] = read_json(path, {})

    def _save(self):
        write_json(self.path, self._d)

    def add(self, device_id, name, platform, device_class, role, cert_fp, expires_at, can_send, accepts):
        self._d[device_id] = {
            "id": device_id, "name": name, "platform": platform, "class": device_class,
            "role": role, "cert_fp": cert_fp, "paired_at": time.time(), "expires_at": expires_at,
            "revoked": False, "can_send": list(can_send), "accepts": list(accepts),
        }
        self._save()

    def get(self, device_id):
        return self._d.get(device_id)

    def all(self):
        return list(self._d.values())

    def revoke(self, device_id) -> bool:
        d = self._d.get(device_id)
        if not d:
            return False
        d["revoked"] = True
        self._save()
        return True
