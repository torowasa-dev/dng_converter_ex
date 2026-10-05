from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from raw_to_dng.cli import main
from raw_to_dng.core import Converter, Mode, Settings, Source, is_jxl_histogram_assert, plan_jobs, publish, run_batch, scan_inputs
from raw_to_dng.dng import DngError, inspect_dng
from tests.fixtures import make_dng


class SettingsTests(unittest.TestCase):
    def test_mp_to_adobe_pixels_and_quality_are_independent(self):
        settings = Settings(megapixels=24, distance=0.5)
        flags = settings.adobe_flags()
        self.assertEqual(flags[flags.index("-count") + 1], "24000000")
        self.assertEqual(flags[flags.index("-jxl_distance") + 1], "0.5")
        self.assertEqual(flags[flags.index("-jxl_effort") + 1], "7")
        self.assertIn("-lossy", flags)
        self.assertEqual(Settings(megapixels=12.5).pixel_limit, 12500000)

    def test_impossible_options_are_rejected(self):
        for settings in (Settings(mode=Mode.LOSSLESS_JPEG, megapixels=24),
                         Settings(megapixels=24, long_edge=6000),
                         Settings(distance=float("nan")), Settings(distance=7),
                         Settings(timeout_seconds=0), Settings(effort=0),
                         Settings(priority='unknown'), Settings(cpu_limit=0), Settings(cpu_limit=101), Settings(cpu_limit=True),
                         Settings(name_template="../{stem}"), Settings(name_template="{stem.__class__}"),
                         Settings(name_template="{index:999999999d}")):
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    settings.validate()

    def test_fallback_cannot_discard_a_resolution_request(self):
        for settings in (Settings(megapixels=24, jxl_fallback=True),
                         Settings(long_edge=6000, jxl_fallback=True)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate()

    def test_histogram_match_excludes_other_assertions(self):
        self.assertTrue(is_jxl_histogram_assert(r'..\..\libjxl\lib\jxl\enc_ans.cc:222: JXL_DASSERT: n <= 255'))
        self.assertTrue(is_jxl_histogram_assert('enc_ans.cc:228: JXL_DASSERT: n\t<=  255'))
        for output in ('GPU disabled', 'other.cc:222: JXL_DASSERT: n <= 255',
                       'enc_ans.cc:222: JXL_DASSERT: n <= 256',
                       'enc_ans.cc:222: JXL_DASSERT: n <= 2550',
                       'enc_ans.cc:222: JXL_DASSERT: other_n <= 255',
                       'enc_ans.cc:222: other error\nelsewhere: JXL_DASSERT: n <= 255'):
            with self.subTest(output=output):
                self.assertFalse(is_jxl_histogram_assert(output))


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_endian_bigtiff_and_raw_child_ifd(self):
        for endian in ("<", ">"):
            for big in (False, True):
                for child in (False, True):
                    with self.subTest(endian=endian, big=big, child=child):
                        path = make_dng(self.base / "synthetic.dng", endian=endian, big=big, raw_ifd_child=child)
                        info = inspect_dng(path)
                        self.assertEqual(info.width * info.height, 24000000)
                        self.assertEqual(info.compression, 52546)
                        self.assertTrue(info.wb_metadata_ready)

    def test_missing_wb_does_not_masquerade_as_editable_color_raw(self):
        info = inspect_dng(make_dng(self.base / "missing.dng", wb=False))
        self.assertFalse(info.wb_metadata_ready)
        self.assertTrue(info.warnings)

    def test_out_of_bounds_raw_and_rendered_rgb_are_rejected(self):
        for kwargs in ({"tile_outside": True}, {"photo": 2}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(DngError):
                    inspect_dng(make_dng(self.base / "bad.dng", **kwargs))

    def test_truncated_and_cyclic_ifd_are_rejected(self):
        path = make_dng(self.base / "truncated.dng")
        path.write_bytes(path.read_bytes()[:30])
        with self.assertRaises(DngError):
            inspect_dng(path)
        path = make_dng(self.base / "cycle.dng")
        data = bytearray(path.read_bytes())
        count = struct.unpack_from("<H", data, 8)[0]
        struct.pack_into("<I", data, 8 + 2 + count * 12, 8)
        path.write_bytes(data)
        with self.assertRaises(DngError):
            inspect_dng(path)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_collisions_between_raw_extensions_cannot_overwrite_each_other(self):
        paths = [self.base / "same.NEF", self.base / "same.CR3"]
        for path in paths:
            path.write_bytes(b"original")
        jobs = plan_jobs([Source(path) for path in paths], self.base / "out", Settings(collision="overwrite"))
        self.assertEqual(len({job.destination for job in jobs}), 2)

    def test_dng_input_cannot_be_overwritten_even_when_overwrite_selected(self):
        source = self.base / "same.dng"
        source.write_bytes(b"original")
        with self.assertRaises(ValueError):
            plan_jobs([Source(source)], self.base, Settings(collision="overwrite"))
        self.assertEqual(source.read_bytes(), b"original")

    def test_existing_output_is_preserved_in_rename_and_skip(self):
        target = self.base / "image.dng"
        target.write_bytes(b"keep")
        stage = self.base / "stage.dng"
        stage.write_bytes(b"new")
        renamed = publish(stage, target, "rename")
        self.assertEqual(renamed.name, "image_001.dng")
        self.assertEqual(target.read_bytes(), b"keep")
        stage.write_bytes(b"skipped")
        self.assertIsNone(publish(stage, target, "skip"))
        self.assertEqual(target.read_bytes(), b"keep")

    def test_scan_avoids_previous_outputs_and_handles_unicode(self):
        source = self.base / "写真" / "撮影.NEF"
        source.parent.mkdir()
        source.write_bytes(b"raw")
        output = self.base / "出力"
        output.mkdir()
        (output / "old.dng").write_bytes(b"previous")
        sources = scan_inputs([self.base], include_dng=True, exclude_directory=output)
        self.assertEqual([item.path for item in sources], [source.resolve()])
        jobs = plan_jobs(sources, output, Settings())
        self.assertEqual(jobs[0].destination.parent.name, "写真")


@unittest.skipIf(os.name == "nt", "The simulated executable uses a POSIX shebang; production Adobe Windows execution is not tested here.")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.output = self.base / "出力"
        self.engine = self.base / "Adobe 模擬 engine"
        project = Path(__file__).resolve().parent.parent
        code = '''import json, math, os, sys, time
from pathlib import Path
sys.path.insert(0, PROJECT)
from tests.fixtures import make_dng
args = sys.argv[1:]
source = Path(args[-1])
dest = Path(args[args.index('-d')+1]) / args[args.index('-o')+1]
with source.with_suffix('.attempts.jsonl').open('a', encoding='utf-8') as history:
    history.write(json.dumps({'args': args, 'stage': str(dest), 'pid': os.getpid()}) + chr(10))
effort = int(args[args.index('-jxl_effort')+1]) if '-jxl_effort' in args else None
jxl = '-jxl' in args or '-losslessJXL' in args
assertion = 'enc_ans.cc:222: JXL_DASSERT: n <= 255'
if source.stem.startswith('gpuerror'):
    print('GPU disabled', file=sys.stderr)
    sys.exit(7)
if source.stem.startswith('otherassert'):
    print(assertion.replace('255', '256'), file=sys.stderr)
    sys.exit(7)
if source.stem.startswith('warningassert'):
    print(assertion, file=sys.stderr)
if jxl and source.stem.startswith('retryhang') and effort == 7:
    time.sleep(8)
should_assert = jxl and (source.stem.startswith('alwaysassert') or
    source.stem.startswith(('effort8success', 'unrelatedafterassert')) and effort is not None and effort > 8 or
    source.stem.startswith(('highassert', 'retryhang', 'mutatingassert', 'missingafterassert')) and effort is not None and effort > 7)
if should_assert:
    dest.write_bytes(b'partial output from failed Adobe run')
    if source.stem.startswith('mutatingassert'):
        source.write_bytes(b'input changed during conversion')
    print(assertion, file=sys.stderr)
    sys.exit(7)
if source.stem.startswith('missingafterassert'):
    sys.exit(0)
if source.stem.startswith('unrelatedafterassert'):
    print('simulated unsupported RAW', file=sys.stderr)
    sys.exit(7)
if source.stem.startswith('hang'):
    time.sleep(8)
if source.stem.startswith('fail'):
    print('simulated unsupported RAW', file=sys.stderr)
    sys.exit(7)
if source.stem.startswith('missing'):
    sys.exit(0)
if source.stem.startswith('corrupt'):
    dest.write_bytes(b'not TIFF')
    sys.exit(0)
jxl = '-jxl' in args or '-losslessJXL' in args
lossy = '-lossy' in args
compression = 52546 if jxl else (34892 if lossy else (1 if '-u' in args else 7))
if source.stem.startswith('ignorecompression'):
    compression = 7
w, h = 8000, 5250
if '-count' in args and not source.stem.startswith('ignoreresize'):
    n = int(args[args.index('-count')+1])
    scale = min(1, math.sqrt(n/(w*h)))
    w, h = max(1, int(w*scale)), max(1, int(h*scale))
if '-side' in args:
    scale = min(1, int(args[args.index('-side')+1])/max(w,h))
    w, h = max(1, int(w*scale)), max(1, int(h*scale))
make_dng(dest, width=w, height=h, compression=compression, linear=lossy or '-l' in args,
         wb=not source.stem.startswith('nowb'), embedded='-e' in args)
'''.replace("PROJECT", repr(str(project)))
        self.engine.write_text("#!" + sys.executable + "\n" + code, encoding="utf-8")
        self.engine.chmod(0o755)
        self.converter = Converter(self.engine)

    def tearDown(self):
        self.temporary.cleanup()

    def jobs(self, names, settings=None):
        sources = []
        for name in names:
            path = self.base / name
            path.write_bytes(b"raw source unchanged")
            sources.append(Source(path))
        return plan_jobs(sources, self.output, settings or Settings())

    def history(self, job):
        return [json.loads(line) for line in job.source.path.with_suffix('.attempts.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_existing_output_skips_conversion_and_batch_continues(self):
        jobs = self.jobs(['already.NEF', 'new.NEF'], Settings(collision='skip'))
        self.output.mkdir()
        existing = jobs[0].destination
        existing.write_bytes(b'existing output unchanged')
        stat = existing.stat()
        results, report = run_batch(self.converter, jobs, self.output)
        self.assertEqual([r.status for r in results], ['skipped', 'ok'])
        self.assertEqual(results[0].attempts, [])
        self.assertFalse(jobs[0].source.path.with_suffix('.attempts.jsonl').exists())
        self.assertEqual(existing.read_bytes(), b'existing output unchanged')
        self.assertEqual(existing.stat().st_mtime_ns, stat.st_mtime_ns)
        self.assertFalse(existing.with_name('already_001.dng').exists())
        self.assertEqual(len(self.history(jobs[1])), 1)
        records = [json.loads(line) for line in report.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(records[1]['requested_settings']['collision'], 'skip')
        self.assertEqual(records[-1]['skipped'], 1)
        self.assertEqual(records[-1]['ok'], 1)

    def test_existing_output_is_converted_when_skip_is_disabled(self):
        for collision in ('rename', 'overwrite'):
            with self.subTest(collision=collision):
                job = self.jobs([f'{collision}.NEF'], Settings(collision=collision))[0]
                self.output.mkdir(exist_ok=True)
                job.destination.write_bytes(b'previous output')
                result = self.converter.convert(job)
                self.assertEqual(result.status, 'ok', result.message)
                self.assertEqual(len(self.history(job)), 1)
                self.assertTrue(Path(result.destination).is_file())
                if collision == 'rename':
                    self.assertEqual(job.destination.read_bytes(), b'previous output')
                    self.assertNotEqual(Path(result.destination), job.destination)
                else:
                    self.assertEqual(Path(result.destination), job.destination)
                    self.assertNotEqual(job.destination.read_bytes(), b'previous output')

    def test_assert_retry_preserves_distance_resolution_and_metadata(self):
        cases = [(effort, resolution) for effort in (8, 9)
                 for resolution in ({'megapixels': 24}, {'long_edge': 5000})]
        for effort, resolution in cases:
            with self.subTest(effort=effort, resolution=resolution):
                settings = Settings(distance=0.2, effort=effort, embed_original=True, **resolution)
                job = self.jobs([f'highassert{effort}' + next(iter(resolution)) + '.NEF'], settings)[0]
                source = job.source.path.read_bytes()
                result = self.converter.convert(job)
                expected_efforts = list(range(effort, 6, -1))
                count = len(expected_efforts)
                self.assertEqual(result.status, 'ok', result.message)
                self.assertEqual([a.settings['effort'] for a in result.attempts], expected_efforts)
                self.assertEqual([a.exit_code for a in result.attempts], [7] * (count - 1) + [0])
                self.assertEqual(result.requested_settings['effort'], effort)
                self.assertEqual(result.effective_settings['effort'], 7)
                self.assertIn('effort ' + '→'.join(map(str, expected_efforts)), result.message)
                for attempt in result.attempts[:-1]:
                    self.assertIn('JXL_DASSERT', attempt.log)
                history = self.history(job)
                self.assertEqual(len({h['stage'] for h in history}), count)
                self.assertEqual(len({h['pid'] for h in history}), count)
                for h in history:
                    args = h['args']
                    self.assertEqual(args[args.index('-jxl_distance') + 1], '0.2')
                    self.assertIn('-e', args)
                    self.assertEqual(Path(args[-1]), job.source.path)
                self.assertTrue(result.dng['wb_metadata_ready'])
                self.assertTrue(result.dng['has_original_raw'])
                self.assertEqual(result.effective_settings['adobe_pixel_limit'], settings.pixel_limit)
                self.assertEqual(job.source.path.read_bytes(), source)
                self.assertEqual(list(self.output.glob('.raw-to-dng-*')), [])

    def test_retry_stops_at_effort8_success_before_effort7_or_fallback(self):
        job = self.jobs(['effort8success.NEF'], Settings(effort=9, distance=0.2, jxl_fallback=True))[0]
        retries = []
        result = self.converter.convert(job, on_retry=retries.append)
        self.assertEqual(result.status, 'ok', result.message)
        self.assertEqual([a.settings['effort'] for a in result.attempts], [9, 8])
        self.assertEqual(len(self.history(job)), 2)
        self.assertEqual(result.effective_settings['mode'], 'lossy-jxl')
        self.assertEqual(result.effective_settings['effort'], 8)
        self.assertEqual(result.effective_settings['distance'], 0.2)
        self.assertIn('effort 9→8', result.message)
        self.assertEqual([r['settings']['effort'] for r in retries], [8])

    def test_resource_settings_are_applied_to_each_retry_without_changing_quality(self):
        settings = Settings(effort=9, distance=0.2, megapixels=24, priority='low', cpu_limit=50)
        job = self.jobs(['highassertresources.NEF'], settings)[0]
        with patch('raw_to_dng.core.ProcessControl') as control:
            control.return_value.__enter__.return_value.enabled = True
            result = self.converter.convert(job)
        self.assertEqual(result.status, 'ok', result.message)
        self.assertEqual(control.call_count, 3)
        for call in control.call_args_list:
            self.assertEqual(call.args[1:], ('low', 50))
        for attempt in result.attempts:
            self.assertEqual((attempt.settings['priority'], attempt.settings['cpu_limit']), ('low', 50))
            self.assertEqual((attempt.settings['distance'], attempt.settings['megapixels']), (0.2, 24))

    def test_cli_resource_options_are_recorded_without_adobe_flags(self):
        from io import StringIO
        source = self.jobs(['dryresources.NEF'])[0].source.path
        out = StringIO()
        with patch('sys.stdout', out):
            code = main(['convert', str(source), '--output', str(self.output), '--dry-run',
                         '--priority', 'idle', '--cpu-limit', '25'])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())[0]
        self.assertEqual((data['settings']['priority'], data['settings']['cpu_limit']), ('idle', 25))
        self.assertNotIn('--cpu-limit', data['adobe_flags'])

    def test_retry_is_opt_out_and_does_not_repeat_effort7(self):
        for effort, enabled in ((9, False), (7, True), (5, True)):
            with self.subTest(effort=effort, enabled=enabled):
                job = self.jobs([f'alwaysassert{effort}{enabled}.NEF'], Settings(effort=effort, jxl_retry=enabled))[0]
                result = self.converter.convert(job)
                self.assertEqual(result.status, 'error')
                self.assertEqual(len(self.history(job)), 1)
                self.assertFalse(job.destination.exists())

    def test_matching_assert_stops_at_effort7(self):
        job = self.jobs(['alwaysassert.NEF'], Settings(effort=9))[0]
        result = self.converter.convert(job)
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(self.history(job)), 3)
        self.assertEqual([a.settings['effort'] for a in result.attempts], [9, 8, 7])
        self.assertIsNone(result.effective_settings)
        self.assertFalse(job.destination.exists())
        self.assertEqual(list(self.output.glob('.raw-to-dng-*')), [])

    def test_unrelated_errors_and_successful_logs_do_not_retry(self):
        for name, status in (('gpuerror.NEF', 'error'), ('otherassert.NEF', 'error'),
                             ('warningassert.NEF', 'ok'), ('fail.NEF', 'error')):
            with self.subTest(name=name):
                job = self.jobs([name], Settings(effort=9, jxl_fallback=True))[0]
                result = self.converter.convert(job)
                self.assertEqual(result.status, status, result.message)
                self.assertEqual(len(self.history(job)), 1)

    def test_lossless_jpeg_fallback_is_opt_in_and_reported(self):
        job = self.jobs(['alwaysassertfallback.NEF'], Settings(effort=9, jxl_fallback=True))[0]
        source = job.source.path.read_bytes()
        result = self.converter.convert(job)
        self.assertEqual(result.status, 'ok', result.message)
        self.assertEqual([a.settings['mode'] for a in result.attempts], ['lossy-jxl'] * 3 + ['lossless-jpeg'])
        self.assertEqual([a.settings['adobe_jxl_effort'] for a in result.attempts], [9, 8, 7, None])
        self.assertEqual(result.dng['compression'], 7)
        self.assertEqual(result.dng['width'] * result.dng['height'], 42000000)
        self.assertEqual(result.effective_settings['mode'], 'lossless-jpeg')
        self.assertIsNone(result.effective_settings['adobe_jxl_effort'])
        self.assertIn('ロスレスJPEGで成功', result.message)
        self.assertNotIn('-lossy', result.command)
        self.assertNotIn('-jxl', result.command)
        self.assertEqual(job.source.path.read_bytes(), source)

    def test_lossless_jxl_does_not_retry_an_ignored_gui_effort(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                job = self.jobs([f'alwaysassertlossless{fallback}.NEF'], Settings(mode=Mode.LOSSLESS_JXL, effort=9, jxl_fallback=fallback))[0]
                result = self.converter.convert(job)
                self.assertEqual(result.status, 'ok' if fallback else 'error', result.message)
                self.assertEqual(len(self.history(job)), 2 if fallback else 1)
                self.assertNotIn('-jxl_effort', result.attempts[0].command)

    def test_partial_output_cannot_masquerade_as_retry_success(self):
        job = self.jobs(['missingafterassert.NEF'], Settings(effort=9))[0]
        result = self.converter.convert(job)
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(self.history(job)), 3)
        self.assertIn('DNGを生成しませんでした', result.message)
        self.assertFalse(job.destination.exists())

    def test_fallback_is_not_used_after_an_unrelated_retry_failure(self):
        job = self.jobs(['unrelatedafterassert.NEF'], Settings(effort=9, jxl_fallback=True))[0]
        result = self.converter.convert(job)
        self.assertEqual(result.status, 'error')
        self.assertEqual(len(self.history(job)), 2)
        self.assertEqual([a.settings['effort'] for a in result.attempts], [9, 8])
        self.assertIn('unsupported RAW', result.message)
        self.assertFalse(job.destination.exists())

    def test_changed_source_is_rejected_before_retry_process(self):
        job = self.jobs(['mutatingassert.NEF'], Settings(effort=9))[0]
        result = self.converter.convert(job)
        self.assertEqual(result.status, 'error')
        self.assertIn('入力ファイルが変更', result.message)
        self.assertEqual(len(self.history(job)), 1)
        self.assertFalse(job.destination.exists())

    def test_timeout_budget_is_shared_by_retries(self):
        job = self.jobs(['alwaysasserttimeout.NEF'], Settings(effort=9, jxl_fallback=True, timeout_seconds=0.5))[0]
        clock = [100.0]
        def advance_after_failure(_data):
            clock[0] += 0.3
        # Advance a controlled clock between failed processes so slow startup
        # cannot consume the test budget before the retry is actually exercised.
        with patch('raw_to_dng.core.time.monotonic', side_effect=lambda: clock[0]):
            result = self.converter.convert(job, on_retry=advance_after_failure)
        self.assertEqual(result.status, 'error')
        self.assertIn('タイムアウト', result.message)
        self.assertEqual(len(self.history(job)), 2)
        self.assertAlmostEqual(result.elapsed_seconds, 0.6)
        self.assertFalse(job.destination.exists())
        self.assertEqual(list(self.output.glob('.raw-to-dng-*')), [])

    def test_cancel_during_retry_terminates_process_and_preserves_pending_jobs(self):
        jobs = self.jobs(['retryhang.NEF', 'pending.NEF'], Settings(effort=9))
        cancel = threading.Event()
        def stop_hanging_retry():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    if len(self.history(jobs[0])) >= 3:
                        break
                except (OSError, ValueError):
                    pass
                if cancel.wait(0.02):
                    return
            cancel.set()
        stopper = threading.Thread(target=stop_hanging_retry, daemon=True)
        stopper.start()
        try:
            results, report = run_batch(self.converter, jobs, self.output, cancel)
        finally:
            cancel.set()
            stopper.join(timeout=2)
        self.assertEqual([r.status for r in results], ['cancelled'])
        self.assertEqual(len(self.history(jobs[0])), 3)
        self.assertFalse(any(job.destination.exists() for job in jobs))
        self.assertEqual(json.loads(report.read_text(encoding='utf-8').splitlines()[-1])['pending'], 1)
        self.assertEqual(list(self.output.glob('.raw-to-dng-*')), [])

    def test_report_and_events_include_requested_and_actual_settings(self):
        jobs = self.jobs(['highassertreport.NEF', 'goodafter.NEF'], Settings(effort=9, megapixels=24))
        events = []
        results, report = run_batch(self.converter, jobs, self.output, on_event=lambda kind, data: events.append((kind, data)))
        self.assertEqual([r.status for r in results], ['ok', 'ok'])
        retries = [data for kind, data in events if kind == 'retry']
        self.assertEqual([r['index'] for r in retries], [0, 0])
        self.assertEqual([r['settings']['effort'] for r in retries], [8, 7])
        self.assertEqual([r['attempt'] for r in retries], [2, 3])
        self.assertEqual([r['message'] for r in retries], ['effort 9→8で再試行', 'effort 8→7で再試行'])
        records = [json.loads(line) for line in report.read_text(encoding='utf-8').splitlines()]
        first = records[1]
        self.assertEqual(first['settings']['effort'], 9)
        self.assertEqual(first['requested_settings']['effort'], 9)
        self.assertEqual(first['effective_settings']['effort'], 7)
        self.assertEqual(len(first['attempts']), 3)
        self.assertEqual(records[-1]['ok'], 2)

    def test_42mp_to_24mp_pipeline_and_raw_metadata(self):
        jobs = self.jobs(["撮影 42MP.NEF"], Settings(megapixels=24, embed_original=True))
        original = jobs[0].source.path.read_bytes()
        result = self.converter.convert(jobs[0])
        self.assertEqual(result.status, "ok", result.message)
        self.assertLessEqual(result.dng["megapixels"], 24)
        self.assertTrue(result.dng["wb_metadata_ready"])
        self.assertTrue(result.dng["has_original_raw"])
        self.assertEqual(jobs[0].source.path.read_bytes(), original)
        self.assertEqual(list(self.output.glob(".raw-to-dng-*")), [])

    def test_ignored_flags_or_missing_wb_cannot_produce_false_success(self):
        for name in ("ignorecompression.NEF", "ignoreresize.NEF", "nowb.NEF", "missing.NEF", "corrupt.NEF"):
            with self.subTest(name=name):
                job = self.jobs([name], Settings(megapixels=24))[0]
                result = self.converter.convert(job)
                self.assertEqual(result.status, "error", result.message)
                self.assertFalse(job.destination.exists())

    def test_conversion_error_does_not_stop_following_files(self):
        jobs = self.jobs(["fail.NEF", "good.NEF"])
        events = []
        results, report = run_batch(self.converter, jobs, self.output, on_event=lambda kind, data: events.append(kind))
        self.assertEqual([r.status for r in results], ["error", "ok"])
        records = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[-1]["ok"], 1)
        self.assertEqual(records[-1]["errors"], 1)
        self.assertEqual(events[-1], "done")

    def test_timeout_terminates_process_and_discards_partial_output(self):
        job = self.jobs(["hang.NEF"], Settings(timeout_seconds=0.3))[0]
        result = self.converter.convert(job)
        self.assertEqual(result.status, "error")
        self.assertIn("タイムアウト", result.message)
        self.assertLess(result.elapsed_seconds, 4)
        self.assertFalse(job.destination.exists())
        self.assertEqual(list(self.output.glob(".raw-to-dng-*")), [])

    def test_cancel_stops_current_job_and_preserves_pending_jobs(self):
        jobs = self.jobs(["hang.NEF", "pending.NEF"])
        cancel = threading.Event()
        timer = threading.Timer(0.25, cancel.set)
        timer.start()
        try:
            results, report = run_batch(self.converter, jobs, self.output, cancel)
        finally:
            timer.cancel()
        self.assertEqual([r.status for r in results], ["cancelled"])
        self.assertFalse(any(job.destination.exists() for job in jobs))
        self.assertEqual(json.loads(report.read_text(encoding="utf-8").splitlines()[-1])["pending"], 1)

    def test_shell_characters_in_input_names_are_not_executed(self):
        name = "$(touch SHOULD_NOT_EXIST).NEF"
        job = self.jobs([name])[0]
        result = self.converter.convert(job)
        self.assertEqual(result.status, "ok", result.message)
        self.assertFalse((self.base / "SHOULD_NOT_EXIST").exists())

    def test_cli_dry_run_works_without_adobe(self):
        source = self.jobs(["dry.NEF"])[0].source.path
        from io import StringIO
        out = StringIO()
        with patch("sys.stdout", out):
            code = main(["convert", str(source), "--output", str(self.output), "--mp", "24", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())[0]["settings"]["adobe_pixel_limit"], 24000000)
        self.assertFalse(self.output.exists())

    def test_cli_recovery_options_are_preserved_and_resize_conflict_is_rejected(self):
        from io import StringIO
        source = self.jobs(['dryrecovery.NEF'])[0].source.path
        args = ['convert', str(source), '--output', str(self.output), '--effort', '9',
                '--no-jxl-retry', '--jxl-fallback', '--dry-run']
        out = StringIO()
        with patch('sys.stdout', out):
            self.assertEqual(main(args), 0)
        settings = json.loads(out.getvalue())[0]['settings']
        self.assertFalse(settings['jxl_retry'])
        self.assertTrue(settings['jxl_fallback'])
        with patch('sys.stderr', StringIO()):
            self.assertEqual(main(args + ['--mp', '24']), 2)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
