from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from raw_to_dng.cli import main
from raw_to_dng.core import Converter, Mode, Settings, Source, plan_jobs, publish, run_batch, scan_inputs
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
                         Settings(name_template="../{stem}"), Settings(name_template="{stem.__class__}"),
                         Settings(name_template="{index:999999999d}")):
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    settings.validate()


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
        self.assertEqual([item.path for item in sources], [source])
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
        code = '''import math, sys, time
from pathlib import Path
sys.path.insert(0, PROJECT)
from tests.fixtures import make_dng
args = sys.argv[1:]
source = Path(args[-1])
dest = Path(args[args.index('-d')+1]) / args[args.index('-o')+1]
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


if __name__ == "__main__":
    unittest.main()
