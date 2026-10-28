"""Preserve graph totals, link paths and incomplete-size behaviour during optimization."""
from dataclasses import replace
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from sage.models import FileRecord, ScanResult, VolumeInfo
from sage.services import hierarchy, scanner
from sage.platform.windows.mft import _complete_mft_records


class PortableScanTests(unittest.TestCase):
    def test_workload_diagnostics_identify_optimized_aggregation(self):
        from test_scan_timing import ScanTimingTests
        with tempfile.TemporaryDirectory() as folder, \
             patch.dict(os.environ, {'LOCALAPPDATA': folder, 'SAGE_SCAN_TIMINGS': '1'}):
            result, _ = ScanTimingTests().replay()
            report = json.loads((Path(folder)/'SAGE'/'scan-timing-last.json').read_text())
        self.assertEqual(report['aggregation_algorithm'], 'single_child_pass')
        self.assertEqual(report['mft_valid_bytes'], 7 * 1024)
        self.assertEqual(report['mft_record_bytes'], 1024)
        self.assertEqual(report['mft_bytes_read'], 8 * 1024)
        self.assertEqual(report['mft_read_calls'], 2)
        self.assertEqual(report['mft_data_runs'], 1)
        self.assertEqual(report['base_records'], len(result.records))
        self.assertEqual(report['extension_groups'], 0)
        self.assertNotIn('test.bin', json.dumps(report))

    def test_single_walk_matches_postorder_on_cycles_duplicates_and_missing_children(self):
        rng = random.Random(591)
        for case in range(30):
            records = {n: FileRecord(n, 5, str(n), 16 if n < 15 else 0,
                logical_size=(1 << 75) + n, allocated_size=n * 4096,
                size_ready=n % 3 != 0) for n in range(5, 35)}
            children = {n: [rng.randrange(5, 38) for _ in range(8)] for n in range(5, 15)}
            result = ScanResult(VolumeInfo('C:', '', 'NTFS'), records, children, 5)
            expected = {}
            for frn in hierarchy._directory_postorder(result):
                files = folders = 0
                for child_frn in children.get(frn, ()):
                    child = records.get(child_frn)
                    if child is None:
                        continue
                    if child.is_directory:
                        _, nested_files, nested_folders = expected.get(child_frn, (0, 0, 0))
                        files += nested_files
                        folders += nested_folders + 1
                    else:
                        files += 1
                expected[frn] = (files + folders, files, folders)
            expected_sizes = hierarchy.calculate_directory_sizes(result)
            result.directory_sizes = {}
            self.assertEqual(hierarchy.calculate_stats(result), expected, case)
            self.assertEqual(result.directory_sizes, {})
            self.assertEqual(hierarchy.calculate_stats(result, include_sizes=True), expected, case)
            self.assertEqual(result.directory_sizes, expected_sizes, case)

    def test_deep_tree_remains_iterative(self):
        records = {5: FileRecord(5, 5, '.', 16, size_ready=True)}
        for n in range(6, 3006):
            records[n] = FileRecord(n, n - 1, str(n), 16, size_ready=True)
        records[3006] = FileRecord(3006, 3005, 'leaf', 0, 123, 4096, size_ready=True)
        result = hierarchy.build_scan_result(VolumeInfo('C:', '', 'NTFS'), records, include_sizes=True)
        self.assertEqual(result.root_stats, (3001, 1, 3000))
        self.assertEqual(result.directory_sizes[5], (123, 4096))

    def test_readiness_preserves_size_fallback_and_counts(self):
        volume = VolumeInfo('C:', '', 'NTFS')
        for ready in (True, False):
            records = {5: FileRecord(5, 5, '.', 16),
                6: FileRecord(6, 5, 'file', 0, 123, 4096, size_ready=ready)}
            with patch.object(scanner, 'get_volume_info', return_value=volume), \
                 patch.object(scanner, 'enumerate_mft', return_value=(records, 7)):
                result = scanner.scan_ntfs('C:')
            self.assertEqual(result.sizes_complete, ready)
            self.assertEqual(result.sized_files, 1 if ready else 0)
            self.assertEqual(result.skipped_records, 7)
            self.assertIs(result.records, records)

    def test_alias_deduplication_preserves_primary_order_and_allocation(self):
        for names in ([], [[5, 'first'], [5, 'first'], [6, 'second']],
                      [(6, 'other'), (5, 'primary'), (6, 'other')]):
            original = FileRecord(9, 5, 'primary', 0, 200, 4096,
                                  size_ready=True, modified=123, link_count=4)
            records = {9: replace(original)}
            details = {9: {'names': names}}
            self.assertEqual(_complete_mft_records(records, details, {}), 0)
            expected = list(dict.fromkeys(map(tuple, names)))
            if expected and (5, 'primary') not in expected:
                primary = expected[0]
            else:
                primary = (5, 'primary')
            self.assertEqual((records[9].parent_frn, records[9].name), primary)
            self.assertEqual([(r.parent_frn, r.name) for r in records.values()],
                             [primary] + [pair for pair in expected if pair != primary])
            self.assertEqual(sum(r.allocated_size for r in records.values()), 4096)
            self.assertTrue(all(r.logical_size == 200 and r.link_count == 4 for r in records.values()))


if __name__ == '__main__':
    unittest.main()
