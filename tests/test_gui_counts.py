"""Exercise queued GUI results without requiring a display server."""
import queue
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from raw_to_dng.gui import Application
from raw_to_dng.progress import BatchProgress


class GuiCountsTests(unittest.TestCase):
    def test_live_counts_handle_parallel_results_retries_and_source_delete_events(self):
        app = Application.__new__(Application)
        interpreter = tk.Tcl()
        # Keep the mock scheduler from retaining a bound callback and its Tcl interpreter.
        app.root = SimpleNamespace(after=lambda *_: None)
        app.events = queue.Queue()
        app.batch_progress = BatchProgress()
        app.batch_progress.plan([True] * 4, parallel_jobs=2)
        app.result_counts = tk.StringVar(interpreter)
        app.status = tk.StringVar(interpreter)
        app.progress_summary = tk.StringVar(interpreter)
        app.progress = MagicMock()
        app.result_tree = MagicMock()
        app.result_details = {}
        app.busy = True
        app.events.put(("retry", {"index": 0, "source": "one.NEF", "message": "再試行"}))
        app._poll()
        self.assertEqual(app.result_counts.get(), "成功 0件 / エラー 0件")
        for index, status in ((1, "ok"), (0, "error"), (2, "skipped"), (3, "cancelled")):
            data = {"index": index, "status": status, "source": f"{index}.NEF", "message": "結果"}
            app.events.put(("result", data))
            app._poll()
        app.events.put(("source_delete", {"index": 1, "status": "ok", "source": "1.NEF",
                                         "message": "結果", "source_delete_message": "削除済み"}))
        app._poll()
        self.assertEqual(app.result_counts.get(), "成功 1件 / エラー 1件")
        self.assertEqual(app.batch_progress.completed, 4)


if __name__ == "__main__":
    unittest.main()
