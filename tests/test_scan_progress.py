import unittest
import queue
import threading
from types import SimpleNamespace
from unittest.mock import patch

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.models import FileRecord, VolumeInfo
from sage.services.scanner import scan_ntfs
from sage.ui.application import _ScanProgress, SageApplication


class ScanProgressTests(unittest.TestCase):
    def test_count_callback_preserves_original_scanner_signature(self):
        counts = []

        def fake_enumerate(_drive, progress, cancelled):
            self.assertIsNone(cancelled)
            progress(1)
            return {5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY)}, 0

        with patch(
            "sage.services.scanner.get_volume_info",
            return_value=VolumeInfo("E:", "Data", "NTFS"),
        ), patch("sage.services.scanner.enumerate_mft", side_effect=fake_enumerate):
            result = scan_ntfs(
                "E:",
                progress=counts.append,
            )

        self.assertEqual(counts, [1])
        self.assertEqual(result.root_frn, 5)

    def test_ui_callback_accepts_old_counts_and_new_details(self):
        events = queue.SimpleQueue()
        reporter = _ScanProgress(events, 7)
        reporter(10)
        reporter(11, 0.4)
        reporter.on_details(10, 0.5)
        reporter.on_stage("Building MFT hierarchy")
        self.assertEqual(events.get_nowait(), ("mft_progress", 7, 10, None))
        self.assertEqual(events.get_nowait(), ("mft_progress", 7, 11, 0.4))
        self.assertEqual(events.get_nowait(), ("mft_progress", 7, 10, 0.5))
        self.assertEqual(events.get_nowait(), ("mft_stage", 7, "Building MFT hierarchy"))

    def test_scan_worker_uses_supported_scanner_arguments(self):
        events = queue.SimpleQueue()
        app = SimpleNamespace(_events=events)
        result = object()

        with patch("sage.ui.application.scan_ntfs", autospec=True, return_value=result) as scanner:
            SageApplication._scan_worker(app, 7, "E:", threading.Event())

        self.assertEqual(events.get_nowait(), ("scan_done", 7, result))
        scanner.assert_called_once()
        self.assertEqual(scanner.call_args.args, ("E:",))
        self.assertEqual(set(scanner.call_args.kwargs), {"progress", "cancelled"})


if __name__ == "__main__":
    unittest.main()
