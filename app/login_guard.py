# -*- coding: utf-8 -*-
"""Login attempt limiting: a sliding window of failures per client address.

In-memory and per-process — the service runs as ONE process (the same
contract as the scheduler and the run queue). Behind a reverse proxy, run
uvicorn with --proxy-headers --forwarded-allow-ips=<proxy address> so
request.client is the real client and not the proxy; X-Forwarded-For is
never trusted directly (any client could forge it).
"""
import time


class LoginLimiter:
    """max_fails failures within window_sec block further attempts for that
    address until the oldest failure leaves the window. A successful login
    resets the address. The clock is injectable for tests."""

    def __init__(self, max_fails: int = 5, window_sec: float = 15 * 60,
                 clock=time.monotonic):
        self.max_fails = max_fails
        self.window_sec = window_sec
        self.clock = clock
        self._fails = {}

    def _recent(self, ip: str) -> list:
        now = self.clock()
        ts = [t for t in self._fails.get(ip, []) if now - t < self.window_sec]
        if ts:
            self._fails[ip] = ts
        else:
            self._fails.pop(ip, None)
        return ts

    def blocked(self, ip: str) -> bool:
        return len(self._recent(ip)) >= self.max_fails

    def retry_after(self, ip: str) -> int:
        """Seconds until the oldest failure leaves the window (0 if free)."""
        ts = self._recent(ip)
        if not ts:
            return 0
        return max(1, int(self.window_sec - (self.clock() - ts[0])) + 1)

    def record_failure(self, ip: str) -> None:
        ts = self._recent(ip)
        ts.append(self.clock())
        self._fails[ip] = ts

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)
