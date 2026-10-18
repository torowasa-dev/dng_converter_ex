"""Exercise real staging/validation/publication with a synthetic Adobe process.

Only subprocess execution is simulated; fixtures cannot prove image quality.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from raw_to_dng.cli import _settings, make_parser
from raw_to_dng.core import Converter, Mode, Settings, Source, plan_jobs, run_batch
from tests.fixtures import make_dng


class SizeLimitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input.NEF"
        self.source.write_bytes(b"original RAW")
        self.output = self.base / "output"
        self.converter = Converter.__new__(Converter)
        self.converter.executable = self.base / "Adobe.exe"
        self.converter.version = "16.0"
        self.commands = []
        self.sizes = [2000]
        self.failures = set()

        def popen(command, **kwargs):
            index = len(self.commands)
            self.commands.append(command)
            if index in self.failures:
                kwargs["stdout"].write(b"enc_ans.cc:222: JXL_DASSERT: n <= 255")
                code = 7
            else:
                stage = Path(command[command.index("-d") + 1]) / "converted.dng"
                lossy = "-lossy" in command
                make_dng(stage, width=100, height=100, linear=lossy,
                         compression=52546 if "-jxl" in command or "-losslessJXL" in command else 7,
                         embedded="-e" in command)
                size = self.sizes[min(index, len(self.sizes) - 1)]
                self.assertGreaterEqual(size, stage.stat().st_size)
                with stage.open("r+b") as stream:
                    stream.truncate(size)
                code = 0
            return SimpleNamespace(pid=123, returncode=code, poll=lambda: code)

        self.addCleanup(patch.stopall)
        patch("raw_to_dng.core.subprocess.Popen", side_effect=popen).start()
        patch("raw_to_dng.core.ProcessControl", return_value=MagicMock(enabled=True, throttled_seconds=0)).start()

    def job(self, **kwargs):
        return plan_jobs([Source(self.source)], self.output, Settings(**kwargs))[0]

    def distances(self):
        return [float(command[command.index("-jxl_distance") + 1]) for command in self.commands]

    def assert_clean(self):
        self.assertEqual(list(self.output.glob(".raw-to-dng-*")), [])

    def test_limit_disabled_preserves_single_conversion(self):
        job = self.job()
        result = self.converter.convert(job)
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(result.output_bytes, 2000)

    def test_exact_limit_is_accepted_and_one_byte_over_fails(self):
        for size, expected in ((1000, "ok"), (1001, "error")):
            with self.subTest(size=size):
                self.sizes = [size]
                job = self.job(max_file_mb=0.001)
                result = self.converter.convert(job)
                self.assertEqual(result.status, expected, result.message)
                self.assertEqual(result.attempts[0].output_bytes, size)
        self.assert_clean()

    def test_oversize_failure_preserves_source_and_existing_overwrite_target(self):
        job = self.job(max_file_mb=0.001, collision="overwrite", delete_source=True)
        self.output.mkdir()
        job.destination.write_bytes(b"previous DNG")
        result = self.converter.convert(job)
        self.assertEqual(result.status, "error")
        self.assertIn("最大ファイルサイズ超過", result.message)
        self.assertEqual(result.source_delete_status, "kept")
        self.assertEqual(self.source.read_bytes(), b"original RAW")
        self.assertEqual(job.destination.read_bytes(), b"previous DNG")
        self.assert_clean()

    def test_adaptation_reconverts_source_and_preserves_effort_and_resolution(self):
        self.sizes = [2000, 1500, 1000]
        job = self.job(max_file_mb=0.001, oversize_action="increase-distance", distance=0.2,
                       distance_step=0.1, effort=9, long_edge=100, preview=0)
        events = []
        result = self.converter.convert(job, on_retry=events.append)
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(self.distances(), [0.2, 0.3, 0.4])
        self.assertEqual(len(events), 2)
        self.assertEqual(result.requested_settings["distance"], 0.2)
        self.assertEqual(result.effective_settings["distance"], 0.4)
        self.assertEqual([a.output_bytes for a in result.attempts], [2000, 1500, 1000])
        for command in self.commands:
            self.assertEqual(command[-1], str(self.source))
            self.assertEqual(command[command.index("-jxl_effort") + 1], "9")
            self.assertEqual(command[command.index("-side") + 1], "100")
            self.assertIn("-p0", command)
        self.assertEqual(len({command[command.index("-d") + 1] for command in self.commands}), 3)
        self.assertEqual(job.destination.stat().st_size, 1000)
        self.assert_clean()

    def test_upper_bound_is_tried_once_even_if_not_a_whole_step(self):
        job = self.job(max_file_mb=0.001, oversize_action="increase-distance", distance=0.2,
                       distance_step=0.2, max_distance=0.5, delete_source=True)
        result = self.converter.convert(job)
        self.assertEqual(result.status, "error")
        self.assertEqual(self.distances(), [0.2, 0.4, 0.5])
        self.assertIn("distance上限", result.message)
        self.assertTrue(self.source.exists())
        self.assertFalse(job.destination.exists())
        self.assert_clean()

    def test_size_is_not_assumed_to_decrease_monotonically(self):
        self.sizes = [1500, 2000, 1000]
        result = self.converter.convert(self.job(max_file_mb=0.001, oversize_action="increase-distance"))
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(self.distances(), [0.1, 0.2, 0.3])

    def test_existing_skip_does_not_reconvert_or_delete_source(self):
        job = self.job(max_file_mb=0.001, collision="skip", delete_source=True)
        self.output.mkdir()
        job.destination.write_bytes(b"X" * 2000)
        result = self.converter.convert(job)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(self.commands, [])
        self.assertTrue(self.source.exists())

    def test_lossless_modes_can_fail_size_check_without_quality_change(self):
        job = self.job(mode=Mode.LOSSLESS_JPEG, max_file_mb=0.001)
        result = self.converter.convert(job)
        self.assertEqual(result.status, "error")
        self.assertEqual(len(self.commands), 1)
        self.assertNotIn("-jxl_distance", self.commands[0])
        self.assertFalse(job.destination.exists())

    def test_size_adaptation_and_histogram_recovery_share_history(self):
        self.failures = {0}
        self.sizes = [2000, 2000, 1000]
        job = self.job(max_file_mb=0.001, oversize_action="increase-distance", effort=9)
        result = self.converter.convert(job)
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual([a.settings["effort"] for a in result.attempts], [9, 8, 8])
        self.assertEqual(self.distances(), [0.1, 0.1, 0.2])
        self.assert_clean()

    def test_lossless_fallback_still_enforces_the_size_limit(self):
        self.failures = {0}
        job = self.job(max_file_mb=0.001, oversize_action="increase-distance", effort=7, jxl_fallback=True)
        result = self.converter.convert(job)
        self.assertEqual(result.status, "error")
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(result.attempts[-1].settings["mode"], "lossless-jpeg")
        self.assertFalse(job.destination.exists())

    def test_cancel_before_size_retry_discards_stage_and_preserves_source(self):
        cancel = threading.Event()
        result = self.converter.convert(self.job(max_file_mb=0.001, oversize_action="increase-distance",
                                                 delete_source=True), cancel, lambda _data: cancel.set())
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(len(self.commands), 1)
        self.assertTrue(self.source.exists())
        self.assert_clean()

    def test_timeout_is_shared_across_size_retries(self):
        job = self.job(max_file_mb=0.001, oversize_action="increase-distance", timeout_seconds=10)
        with patch("raw_to_dng.core.time.monotonic", return_value=1) as clock:
            result = self.converter.convert(job, on_retry=lambda _data: setattr(clock, "return_value", 12))
        self.assertEqual(result.status, "error")
        self.assertIn("タイムアウト", result.message)
        self.assertEqual(len(self.commands), 1)
        self.assert_clean()

    def test_batch_continues_after_oversize_and_reports_effective_quality(self):
        self.sizes = [2000, 1000]
        job = self.job(max_file_mb=0.001, delete_source=True)
        second = replace(job, destination=self.output / "second.dng")
        results, report = run_batch(self.converter, [job, second], self.output)
        self.assertEqual([r.status for r in results], ["error", "ok"])
        self.assertTrue(self.source.exists())
        records = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
        self.assertEqual((records[-1]["errors"], records[-1]["ok"]), (1, 1))
        self.assertEqual(records[1]["settings"]["max_file_bytes"], 1000)


class SizeSettingTests(unittest.TestCase):
    def test_invalid_values_and_incompatible_modes_are_rejected(self):
        values = [dict(max_file_mb=x) for x in (-1, float("nan"), float("inf"), True, "1", 0.0000001)]
        values += [dict(distance_step=x) for x in (0, -0.1, 0.0000001, float("nan"), float("inf"), True)]
        values += [dict(max_distance=x) for x in (-1, 7, float("nan"), float("inf"), True)]
        values += [dict(oversize_action="invalid"),
                   dict(max_file_mb=1, oversize_action="increase-distance", mode=Mode.LOSSLESS_JXL),
                   dict(max_file_mb=1, oversize_action="increase-distance", distance=1, max_distance=0.5)]
        for kwargs in values:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Settings(**kwargs).validate()

    def test_decimal_megabytes_are_floored_without_float_rounding_up(self):
        self.assertEqual(Settings(max_file_mb=0.000249).max_file_bytes, 249)
        self.assertEqual(Settings(max_file_mb=1.2345678).max_file_bytes, 1234567)
        self.assertIsNone(Settings().max_file_bytes)

    def test_cli_passes_size_options_to_both_commands(self):
        for command in ("convert", "compare"):
            args = make_parser().parse_args([command, "RAW", "--output", "DNG", "--max-file-mb", "12.5",
                                            "--oversize-action", "increase-distance", "--distance-step", "0.05",
                                            "--max-distance", "0.8"])
            settings = _settings(args)
            self.assertEqual((settings.max_file_bytes, settings.oversize_action, settings.distance_step,
                              settings.max_distance), (12500000, "increase-distance", 0.05, 0.8))


if __name__ == "__main__":
    unittest.main()
