import time


class FailureLimiter:
    """Block a key (IP) after too many failures inside a window."""

    def __init__(self, max_failures: int = 5, window: float = 600.0, clock=time.monotonic):
        self.max, self.window, self.clock = max_failures, window, clock
        self._fails: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = self.clock() - self.window
        recent = [t for t in self._fails.get(key, []) if t > cutoff]
        if recent:
            self._fails[key] = recent
        else:
            self._fails.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max

    def fail(self, key: str) -> None:
        self._recent(key)
        self._fails.setdefault(key, []).append(self.clock())
