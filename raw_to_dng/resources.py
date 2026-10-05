"""Lower converter priority and regulate average CPU use without changing pixels."""
from __future__ import annotations

import sys
import time


PRIORITY_LABELS = {"normal": "通常", "low": "低め", "idle": "最低"}


class ProcessControl:
    def __init__(self, pid: int, priority: str, cpu_limit: int, *, api=None, cpus: int | None = None):
        self._paused = []
        self.enabled = priority != "normal" or cpu_limit < 100
        if not self.enabled:
            return
        if api is None:
            try:
                import psutil as api
            except ImportError as exc:
                raise RuntimeError("CPU・優先度設定にはpsutilが必要です。python -m pip install . を実行してください。") from exc
        self.api, self.priority = api, priority
        try:
            self.root = api.Process(pid)
        except api.NoSuchProcess:
            self.enabled = False
            return
        self.rate = (cpus or api.cpu_count() or 1) * cpu_limit / 100
        self.limited = cpu_limit < 100
        self._processes, self._totals = {}, {}
        self._balance = 0.0
        self._last_cpu = self._snapshot()[1]
        self._last_wall = time.monotonic()

    def _priority(self, process) -> None:
        if self.priority == "normal":
            return
        current = process.nice()
        if sys.platform == "win32":
            value = self.api.BELOW_NORMAL_PRIORITY_CLASS if self.priority == "low" else self.api.IDLE_PRIORITY_CLASS
        else:
            value = max(current, 10 if self.priority == "low" else 19)
        if current != value:
            process.nice(value)

    def _snapshot(self):
        try:
            discovered = [self.root, *self.root.children(recursive=True)]
        except self.api.NoSuchProcess:
            discovered = []
        for process in discovered:
            try:
                key = (process.pid, process.create_time())
                if key not in self._processes:
                    self._processes[key] = process
            except self.api.NoSuchProcess:
                pass
        active = []
        for key, process in list(self._processes.items()):
            try:
                if not process.is_running():
                    del self._processes[key]
                    continue
                # A converter can reset its priority during startup or create
                # children later. Keep the user's selection throughout the run.
                self._priority(process)
                usage = process.cpu_times()
                self._totals[key] = max(self._totals.get(key, 0), usage.user + usage.system)
                active.append(process)
            except self.api.NoSuchProcess:
                del self._processes[key]
        return active, sum(self._totals.values())

    def update(self, cancel, check_running) -> None:
        if not self.enabled:
            return
        active, cpu = self._snapshot()
        if not self.limited:
            return
        now = time.monotonic()
        self._balance = min(self.rate * 0.1, self._balance + (now - self._last_wall) * self.rate - max(0, cpu - self._last_cpu))
        self._last_cpu, self._last_wall = cpu, now
        pause = max(0, -self._balance / self.rate)
        if not active or pause < 0.01:
            return
        check_running()
        try:
            for process in active:
                try:
                    process.suspend()
                    self._paused.append(process)
                except self.api.NoSuchProcess:
                    pass
            until = time.monotonic() + pause
            while time.monotonic() < until:
                cancel.wait(min(0.1, max(0, until - time.monotonic())))
                check_running()
        finally:
            self.close()

    def close(self) -> None:
        failed, error = [], None
        for process in reversed(self._paused):
            try:
                process.resume()
            except self.api.NoSuchProcess:
                pass
            except Exception as exc:
                failed.append(process)
                error = exc
        self._paused = failed
        if error is not None:
            raise RuntimeError("変換プロセスの再開に失敗しました。") from error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
