from __future__ import annotations

import subprocess
import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import psutil

from raw_to_dng.resources import ProcessControl


class FakeProcess:
    def __init__(self, pid, children=()):
        self.pid, self.descendants = pid, list(children)
        self.cpu, self.priority, self.paused, self.fail_suspend = 0.0, 0, False, False
        self.resumed = 0

    def create_time(self): return 1.0
    def is_running(self): return True
    def children(self, recursive): return self.descendants
    def cpu_times(self): return SimpleNamespace(user=self.cpu, system=0.0)
    def nice(self, value=None):
        if value is not None: self.priority = value
        return self.priority
    def suspend(self):
        if self.fail_suspend: raise PermissionError('simulated denial')
        self.paused = True
    def resume(self):
        self.paused = False
        self.resumed += 1


class Gone(Exception): pass


class FakeClock:
    value = 0.0
    def __call__(self): return self.value


class FakeEvent:
    def __init__(self, clock, stop=False):
        self.clock, self.stop, self.cancelled = clock, stop, False
    def wait(self, delay):
        self.clock.value += delay
        self.cancelled = self.stop


class ResourceTests(unittest.TestCase):
    def control(self, root, clock, priority='low', limit=25, cpus=1):
        api = SimpleNamespace(Process=lambda pid: root, cpu_count=lambda: 4, NoSuchProcess=Gone,
                              BELOW_NORMAL_PRIORITY_CLASS=0x4000, IDLE_PRIORITY_CLASS=0x40)
        with patch('raw_to_dng.resources.time.monotonic', clock):
            return ProcessControl(root.pid, priority, limit, api=api, cpus=cpus)

    def test_defaults_do_not_modify_or_monitor_processes(self):
        with ProcessControl(0, 'normal', 100) as control:
            control.update(None, lambda: self.fail('Default must not throttle'))
            self.assertFalse(control.enabled)

    def test_priority_is_applied_to_converter_and_children(self):
        child, clock = FakeProcess(2), FakeClock()
        root = FakeProcess(1, [child])
        with patch('raw_to_dng.resources.sys.platform', 'linux'):
            self.control(root, clock, priority='idle')
        self.assertEqual((root.priority, child.priority), (19, 19))
        with patch('raw_to_dng.resources.sys.platform', 'win32'):
            self.control(root, clock, priority='low')
        self.assertEqual((root.priority, child.priority), (0x4000, 0x4000))

    def test_limit_is_normalized_to_logical_cpu_count(self):
        root, clock = FakeProcess(1), FakeClock()
        control = self.control(root, clock, cpus=4)
        self.assertEqual(control.rate, 1)
        root.cpu, clock.value = 0.3, 0.1
        with patch('raw_to_dng.resources.time.monotonic', clock):
            control.update(FakeEvent(clock), lambda: None)
        self.assertGreaterEqual(clock.value, 0.29)
        self.assertFalse(root.paused)

    def test_cancel_during_throttle_always_resumes_all_processes(self):
        child, clock = FakeProcess(2), FakeClock()
        root = FakeProcess(1, [child])
        control = self.control(root, clock)
        root.cpu, clock.value = 1, 0.1
        event = FakeEvent(clock, stop=True)
        def check():
            if event.cancelled: raise InterruptedError('cancelled')
        with patch('raw_to_dng.resources.time.monotonic', clock), self.assertRaises(InterruptedError):
            control.update(event, check)
        self.assertFalse(root.paused or child.paused)
        self.assertEqual((root.resumed, child.resumed), (1, 1))

    def test_partial_suspend_failure_resumes_already_paused_processes(self):
        child, clock = FakeProcess(2), FakeClock()
        root = FakeProcess(1, [child])
        control = self.control(root, clock)
        root.cpu, clock.value, child.fail_suspend = 1, 0.1, True
        with patch('raw_to_dng.resources.time.monotonic', clock), self.assertRaises(PermissionError):
            control.update(FakeEvent(clock), lambda: None)
        self.assertFalse(root.paused)
        self.assertEqual(root.resumed, 1)

    def test_exited_children_do_not_make_accumulated_cpu_time_decrease(self):
        child, clock = FakeProcess(2), FakeClock()
        root = FakeProcess(1, [child])
        child.cpu = 2
        control = self.control(root, clock)
        child.is_running = lambda: False
        root.descendants = []
        self.assertEqual(control._snapshot()[1], 2)

    def test_real_child_priority_and_cpu_usage_are_regulated(self):
        if sys.platform.startswith('linux') and int(os.readlink('/proc/self')) != os.getpid():
            self.skipTest('Virtual process IDs differ from procfs; native process control is tested in CI.')
        parent_priority = psutil.Process().nice()
        process = subprocess.Popen([sys.executable, '-c', 'while True: pass'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = time.monotonic()
        try:
            monitored = psutil.Process(process.pid)
            with ProcessControl(process.pid, 'low', 25, cpus=1) as control:
                def check():
                    if time.monotonic() - started > 5: raise TimeoutError('test deadline')
                while time.monotonic() - started < 0.8:
                    time.sleep(0.1)
                    control.update(threading.Event(), check)
                usage = monitored.cpu_times()
                self.assertLess((usage.user + usage.system) / (time.monotonic() - started), 0.7)
                expected = psutil.BELOW_NORMAL_PRIORITY_CLASS if sys.platform == 'win32' else max(parent_priority, 10)
                self.assertEqual(monitored.nice(), expected)
            self.assertEqual(psutil.Process().nice(), parent_priority)
        finally:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


if __name__ == '__main__': unittest.main()
