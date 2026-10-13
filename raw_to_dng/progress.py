"""Batch counts and an approximate ETA, independent of Tk and the converter."""
from __future__ import annotations

from dataclasses import dataclass, field
import heapq
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
    _active: dict[int, float] = field(default_factory=dict)
    _parallel_jobs: int = 1

    @property
    def completed(self) -> int:
        return len(self._finished)

    @property
    def remaining(self) -> int:
        return self.total - len(self._finished)

    def plan(self, work: Iterable[bool], parallel_jobs: int = 1) -> None:
        if type(parallel_jobs) is not int or parallel_jobs < 1:
            raise ValueError("並列実行数は1以上です。")
        flags = tuple(work)
        self.total, self.planned, self.stopped = len(flags), True, False
        self._pending_work = {i for i, needs_conversion in enumerate(flags) if needs_conversion}
        self._finished.clear()
        self._seconds, self._samples, self._parallel_jobs = 0.0, 0, parallel_jobs
        self._active.clear()

    def start(self, index: int, now: float) -> None:
        if 0 <= index < self.total and index not in self._finished:
            self._active.setdefault(index, now)

    def result(self, index: int, status: str, elapsed: float, attempted: bool) -> None:
        if not 0 <= index < self.total or index in self._finished:
            return
        self._finished.add(index)
        self._pending_work.discard(index)
        self._active.pop(index, None)
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
        # Estimate when all worker slots become free, including the final
        # partial wave. Out-of-order completion must not reset other timers.
        active = [max(1.0, average - max(0, now - started))
                  for index, started in self._active.items() if index in self._pending_work]
        slots = active + [0.0] * max(0, min(self._parallel_jobs, len(self._pending_work)) - len(active))
        heapq.heapify(slots)
        for _ in range(len(self._pending_work) - len(active)):
            heapq.heapreplace(slots, slots[0] + average)
        return max(1.0, max(slots))

    def summary(self, now: float) -> str:
        if not self.planned:
            return "全数 — / 残件数 — / 残り時間 —"
        eta = "中止" if self.stopped else format_remaining(self.estimate(now))
        return f"全数 {self.total}件 / 残件数 {self.remaining}件 / 残り時間 {eta}"
