"""Live health counters of every source, shown on the 🩺 status page."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# A real-time source that received nothing for this long is flagged as stale.
STALE_AFTER_S = 180


@dataclass
class SourceStats:
    name: str
    mode: str = ""
    connected: bool = False
    events: int = 0
    last_event: float | None = None
    detections: int = 0
    errors: int = 0
    last_error: str | None = None
    last_error_at: float | None = None
    started_at: float = field(default_factory=time.time)

    def event(self, n: int = 1) -> None:
        self.events += n
        self.last_event = time.time()

    def error(self, message: str) -> None:
        self.errors += 1
        self.last_error = message[:200]
        self.last_error_at = time.time()

    def state(self, now: float | None = None) -> str:
        """'ok' | 'stale' | 'down'"""
        now = now or time.time()
        if not self.connected:
            return "down"
        reference = self.last_event or self.started_at
        return "ok" if now - reference < STALE_AFTER_S else "stale"
