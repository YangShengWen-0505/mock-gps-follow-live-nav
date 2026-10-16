import math
import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window per-source failure counter with bounded memory.

    Shared by the login form and the API-key middleware so both throttles keep
    the same window, limit, and eviction behaviour.
    """

    def __init__(
        self,
        max_attempts: int = 5,
        window_seconds: int = 300,
        max_sources: int = 1024,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._max_sources = max_sources
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def record_failure(self, source: str) -> bool:
        """Record one failure; return True when the source is now throttled."""
        now = time.monotonic()
        with self._lock:
            for key in list(self._attempts):
                attempts = self._attempts[key]
                while attempts and now - attempts[0] > self._window_seconds:
                    attempts.popleft()
                if not attempts:
                    self._attempts.pop(key, None)
            # Evicting the least recently active source keeps a flood of unique
            # addresses from growing this map without bound.
            if source not in self._attempts and len(self._attempts) >= self._max_sources:
                oldest = min(self._attempts, key=lambda key: self._attempts[key][-1])
                self._attempts.pop(oldest, None)
            attempts = self._attempts.setdefault(source, deque())
            if len(attempts) >= self._max_attempts:
                return True
            attempts.append(now)
            return False

    def clear(self, source: str) -> None:
        with self._lock:
            self._attempts.pop(source, None)

    def retry_after(self, source: str) -> int:
        """Check the window before attempting another credential comparison."""
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts.get(source)
            if not attempts:
                return 0
            while attempts and now - attempts[0] >= self._window_seconds:
                attempts.popleft()
            if len(attempts) < self._max_attempts:
                return 0
            return max(1, math.ceil(self._window_seconds - (now - attempts[0])))

    def reset(self) -> None:
        with self._lock:
            self._attempts.clear()

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._attempts)
