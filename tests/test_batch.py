"""Scheduling tests use event gates rather than machine-dependent timings."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from raw_to_dng.cli import _settings, make_parser
from raw_to_dng.core import Job, Mode, Result, Settings, Source, _delete_source, _source_identity, publish, run_batch, speed_settings
from dataclasses import asdict, replace


class GatedConverter:
    executable = Path('fake-adobe')
    version = '16.0'

    def __init__(self, count):
        self.entered = [threading.Event() for _ in range(count)]
        self.release = [threading.Event() for _ in range(count)]
        self.finished = [threading.Event() for _ in range(count)]
        self.lock = threading.Lock()
        self.active = self.maximum = 0
        self.budgets = []

    def convert(self, job, cancel, on_retry, *, cpu_budget, reserved, defer_source_delete):
        index = int(job.source.path.stem)
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.budgets.append(cpu_budget)
        self.entered[index].set()
        try:
            on_retry({'attempt': 2, 'message': 'test retry'})
            while not self.release[index].wait(0.01):
                if cancel.is_set():
                    return Result(str(job.source.path), str(job.destination), 'cancelled')
            return Result(str(job.source.path), str(job.destination), 'ok')
        finally:
            with self.lock:
                self.active -= 1


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def jobs(self, count, workers):
        return [Job(Source(self.directory / f'{i}.NEF'), self.directory / f'{i}.dng',
                    Settings(parallel_jobs=workers)) for i in range(count)]

    def start(self, jobs, converter, cancel):
        output, events, failures = [], [], []
        def callback(kind, data):
            events.append((kind, data, threading.get_ident()))
            if kind == 'result':
                converter.finished[data['index']].set()
        def worker():
            try:
                output.append(run_batch(converter, jobs, self.directory, cancel, callback))
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(self.stop, converter, cancel, thread)
        return thread, output, events, failures

    def stop(self, converter, cancel, thread):
        cancel.set()
        for release in converter.release:
            release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive(), 'batch did not stop')

    def test_bounded_parallelism_refills_a_slot_and_returns_input_order(self):
        jobs = self.jobs(4, 2)
        converter, cancel = GatedConverter(4), threading.Event()
        thread, output, events, failures = self.start(jobs, converter, cancel)
        self.assertTrue(converter.entered[0].wait(5))
        self.assertTrue(converter.entered[1].wait(5))
        self.assertFalse(converter.entered[2].is_set())
        converter.release[1].set()
        self.assertTrue(converter.entered[2].wait(5))
        converter.release[2].set()
        self.assertTrue(converter.entered[3].wait(5))
        converter.release[3].set()
        self.assertTrue(converter.finished[3].wait(5))
        converter.release[0].set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        results, report = output[0]
        self.assertEqual(converter.maximum, 2)
        self.assertTrue(all(budget is converter.budgets[0] for budget in converter.budgets))
        self.assertEqual([r.source for r in results], [str(job.source.path) for job in jobs])
        self.assertEqual([data['index'] for kind, data, _ in events if kind == 'result'], [1, 2, 3, 0])
        self.assertEqual(len({tid for _, _, tid in events}), 1)
        for index in range(4):
            kinds = [kind for kind, data, _ in events if data.get('index') == index]
            self.assertEqual(kinds, ['start', 'retry', 'result'])
        records = [json.loads(line) for line in report.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[0]['parallel_jobs'], 2)
        self.assertEqual(records[-1]['pending'], 0)

    def test_cancel_stops_all_active_files_without_starting_waiting_files(self):
        jobs = self.jobs(5, 2)
        converter, cancel = GatedConverter(5), threading.Event()
        thread, output, events, failures = self.start(jobs, converter, cancel)
        self.assertTrue(converter.entered[0].wait(5))
        self.assertTrue(converter.entered[1].wait(5))
        cancel.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual([r.status for r in output[0][0]], ['cancelled', 'cancelled'])
        self.assertTrue(all(not entered.is_set() for entered in converter.entered[2:]))
        summary = events[-1][1]
        self.assertEqual((summary['completed'], summary['pending'], summary['cancelled']), (2, 3, True))

    def test_worker_exception_is_reported_and_following_files_continue(self):
        jobs = self.jobs(3, 2)
        def convert(job, *_args, **_kwargs):
            if job is jobs[0]:
                raise RuntimeError('unexpected failure')
            return Result(str(job.source.path), str(job.destination), 'ok')
        converter = SimpleNamespace(executable=Path('fake-adobe'), version='16', convert=convert)
        results, report = run_batch(converter, jobs, self.directory)
        self.assertEqual([r.status for r in results], ['error', 'ok', 'ok'])
        self.assertIn('unexpected failure', results[0].message)
        self.assertEqual(json.loads(report.read_text(encoding='utf-8').splitlines()[-1])['errors'], 1)

    def test_defaults_remain_sequential_and_an_empty_batch_finishes(self):
        jobs = self.jobs(2, 1)
        converter = GatedConverter(2)
        for release in converter.release:
            release.set()
        results, _report = run_batch(converter, jobs, self.directory)
        self.assertEqual(len(results), 2)
        self.assertEqual(converter.maximum, 1)
        results, report = run_batch(converter, [], self.directory)
        self.assertEqual(results, [])
        self.assertEqual(json.loads(report.read_text(encoding='utf-8').splitlines()[-1])['pending'], 0)

    def test_renamed_output_never_uses_another_jobs_reserved_destination(self):
        target = self.directory / 'photo.dng'
        target.write_bytes(b'previous')
        reserved = self.directory / 'photo_001.dng'
        stage = self.directory / 'stage.dng'
        stage.write_bytes(b'new photo')
        result = publish(stage, target, 'rename', reserved=frozenset({str(reserved).casefold()}))
        self.assertEqual(result, self.directory / 'photo_002.dng')
        self.assertFalse(reserved.exists())
        self.assertEqual(target.read_bytes(), b'previous')

    def test_cli_applies_parallel_setting_to_conversion_and_quality_comparison(self):
        for command in ('convert', 'compare'):
            args = make_parser().parse_args([command, 'RAW', '--output', 'DNG', '-j', '4'])
            self.assertEqual(_settings(args).parallel_jobs, 4)
        args.jobs = 0
        with self.assertRaises(ValueError):
            _settings(args)

    def test_speed_preset_preserves_quality_resolution_and_output_options(self):
        settings = Settings(distance=0.2, effort=9, long_edge=6000, preview=0,
                            priority='low', cpu_limit=90, parallel_jobs=4, collision='skip', delete_source=True)
        result = speed_settings(settings)
        self.assertEqual(asdict(result), {**asdict(settings), 'effort':7, 'priority':'normal', 'cpu_limit':100, 'parallel_jobs':2})
        self.assertEqual((result.distance, result.long_edge, result.delete_source), (0.2, 6000, True))
        flags = result.adobe_flags()
        self.assertEqual(flags[flags.index('-jxl_effort')+1], '7')
        self.assertEqual(flags[flags.index('-jxl_distance')+1], '0.2')
        with self.assertRaises(ValueError):
            speed_settings(replace(settings, mode=Mode.LOSSY_JPEG))

    def test_cli_speed_preset_records_the_settings_actually_used(self):
        args = make_parser().parse_args(['convert','RAW','--output','DNG','--speed','--distance','0.2','--long-edge','6000'])
        settings = _settings(args)
        self.assertEqual((settings.effort, settings.priority, settings.cpu_limit, settings.parallel_jobs), (7, 'normal', 100, 2))
        self.assertEqual((settings.distance, settings.long_edge), (0.2, 6000))

    def test_batch_per_output_time_uses_wall_time_not_sum_of_parallel_job_times(self):
        jobs = self.jobs(2, 2)
        # Each simulated file reports 6 minutes but the batch wall time is 7
        # minutes. The average output interval must be 210, not 360 seconds.
        clock = SimpleNamespace(value=100.0)
        def convert(job, *_args, **_kwargs):
            return Result(str(job.source.path), str(job.destination), 'ok', elapsed_seconds=360)
        def event(kind, data):
            if kind == 'done':
                return
            if kind == 'result':
                clock.value = 520.0
        converter = SimpleNamespace(executable=Path('fake-adobe'), version='16', convert=convert)
        from unittest.mock import patch
        with patch('raw_to_dng.core.time.monotonic', side_effect=lambda: clock.value):
            _results, report = run_batch(converter, jobs, self.directory, on_event=event)
        summary = json.loads(report.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual((summary['elapsed_seconds'], summary['seconds_per_success']), (420, 210))

    def test_source_deletion_checks_file_identity_and_all_saved_outputs(self):
        # Run file-system deletion guards on Windows as well as POSIX; the
        # subprocess Adobe fixture in test_app uses a POSIX-only executable.
        for mutation in ('none', 'source', 'output'):
            with self.subTest(mutation=mutation):
                source = self.directory / f'{mutation}.TIFF'
                source.write_bytes(b'original raw')
                destinations = [self.directory / f'{mutation}_{i}.dng' for i in range(2)]
                results = []
                for destination in destinations:
                    destination.write_bytes(b'verified output')
                    results.append(Result(str(source), str(destination), 'ok', output_bytes=15,
                                          dng={'validated': True}, _source_identity=_source_identity(source.stat())))
                if mutation == 'source':
                    source.write_bytes(b'new raw')
                elif mutation == 'output':
                    destinations[1].unlink()
                _delete_source(results)
                self.assertEqual(source.exists(), mutation != 'none')
                self.assertEqual([r.source_delete_status for r in results],
                                 ['deleted' if mutation == 'none' else 'error'] * 2)


if __name__ == '__main__':
    unittest.main()
