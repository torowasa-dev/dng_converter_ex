"""Batch counts and an approximate ETA, independent of Tk and the converter."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Iterable


def format_remaining(seconds: float | None) -> str:
    if seconds is None:
        return "計算中"
    seconds = math.ceil(max(0, seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, rest = divmod(rest, 60)
    if hours:
        return f"約{hours}時間{minutes}分"
    if minutes:
        return f"約{minutes}分{rest:02d}秒"
    return f"約{seconds}秒" if seconds else "0秒"


@dataclass
class BatchProgress:
    total: int = 0
    planned: bool = False
    stopped: bool = False
    _pending_work: set[int] = field(default_factory=set)
    _finished: set[int] = field(default_factory=set)
    _seconds: float = 0.0
    _samples: int = 0
    _active: int | None = None
    _started: float = 0.0

    @property
    def remaining(self) -> int:
        return self.total - len(self._finished)

    def plan(self, work: Iterable[bool]) -> None:
        flags = tuple(work)
        self.total, self.planned, self.stopped = len(flags), True, False
        self._pending_work = {i for i, needs_conversion in enumerate(flags) if needs_conversion}
        self._finished.clear()
        self._seconds, self._samples, self._active = 0.0, 0, None

    def start(self, index: int, now: float) -> None:
        self._active, self._started = index, now

    def result(self, index: int, status: str, elapsed: float, attempted: bool) -> None:
        if not 0 <= index < self.total or index in self._finished:
            return
        self._finished.add(index)
        self._pending_work.discard(index)
        self._active = None
        if attempted and status not in ("skipped", "cancelled") and elapsed > 0 and math.isfinite(elapsed):
            self._seconds += elapsed
            self._samples += 1

    def estimate(self, now: float) -> float | None:
        if not self.planned or self.stopped:
            return None
        if not self._pending_work:
            return 0.0
        if not self._samples:
            return None
        average = self._seconds / self._samples
        active_time = max(0, now - self._started) if self._active in self._pending_work else 0
        return max(1.0, average * len(self._pending_work) - active_time)

    def summary(self, now: float) -> str:
        if not self.planned:
            return "全数 — / 残件数 — / 残り時間 —"
        eta = "中止" if self.stopped else format_remaining(self.estimate(now))
        return f"全数 {self.total}件 / 残件数 {self.remaining}件 / 残り時間 {eta}"
