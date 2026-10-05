from __future__ import annotations

import unittest

from raw_to_dng.progress import BatchProgress, format_remaining


class ProgressTests(unittest.TestCase):
    def test_total_and_remaining_include_skips_and_errors(self):
        progress = BatchProgress()
        progress.plan([True, False, True])
        self.assertEqual((progress.total, progress.remaining), (3, 3))
        progress.result(0, 'error', 10, True)
        self.assertEqual(progress.remaining, 2)
        progress.result(1, 'skipped', 0, False)
        self.assertEqual(progress.remaining, 1)
        progress.result(2, 'ok', 20, True)
        self.assertEqual(progress.remaining, 0)
        self.assertEqual(progress.estimate(100), 0)

    def test_eta_waits_for_a_conversion_but_all_skips_need_no_conversion_time(self):
        progress = BatchProgress()
        progress.plan([True, True])
        self.assertIsNone(progress.estimate(0))
        progress.plan([False, False])
        self.assertEqual(progress.estimate(0), 0)

    def test_pending_skips_and_skip_durations_do_not_inflate_eta(self):
        progress = BatchProgress()
        progress.plan([True, False, True, True])
        progress.result(0, 'ok', 10, True)
        self.assertEqual(progress.estimate(100), 20)
        progress.result(1, 'skipped', 500, False)
        self.assertEqual(progress.estimate(100), 20)

    def test_active_conversion_counts_down_and_never_shows_zero_before_completion(self):
        progress = BatchProgress()
        progress.plan([True, True])
        progress.result(0, 'ok', 10, True)
        progress.start(1, 100)
        self.assertEqual(progress.estimate(104), 6)
        self.assertEqual(progress.estimate(150), 1)
        self.assertEqual(progress.remaining, 1)

    def test_retry_elapsed_time_is_one_sample_and_one_completed_file(self):
        progress = BatchProgress()
        progress.plan([True, True, True])
        progress.result(0, 'ok', 10, True)
        # The converter reports the combined elapsed time for all attempts.
        progress.result(1, 'ok', 30, True)
        self.assertEqual(progress.remaining, 1)
        self.assertEqual(progress.estimate(100), 20)

    def test_cancel_and_new_batch_stop_and_reset_estimates(self):
        progress = BatchProgress()
        progress.plan([True, True])
        progress.result(0, 'ok', 10, True)
        progress.stopped = True
        self.assertIsNone(progress.estimate(100))
        self.assertIn('残件数 1件 / 残り時間 中止', progress.summary(100))
        progress.plan([True, True, True])
        self.assertEqual(progress.remaining, 3)
        self.assertIsNone(progress.estimate(100))
        self.assertFalse(progress.stopped)

    def test_duplicate_results_and_unstarted_errors_do_not_corrupt_estimates(self):
        progress = BatchProgress()
        progress.plan([True, True, True])
        progress.result(0, 'error', 100, False)
        progress.result(0, 'ok', 200, True)
        progress.result(99, 'ok', 200, True)
        self.assertEqual(progress.remaining, 2)
        self.assertIsNone(progress.estimate(0))
        progress.result(1, 'ok', 10, True)
        self.assertEqual(progress.estimate(0), 10)

    def test_eta_format_handles_seconds_minutes_hours_and_unknown(self):
        for seconds, expected in ((None, '計算中'), (0, '0秒'), (0.1, '約1秒'),
                                  (59.1, '約1分00秒'), (125, '約2分05秒'), (3661, '約1時間1分')):
            with self.subTest(seconds=seconds):
                self.assertEqual(format_remaining(seconds), expected)


if __name__ == '__main__':
    unittest.main()
