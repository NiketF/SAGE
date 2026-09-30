import unittest

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.models import FileRecord, VolumeInfo
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes


def make_result():
    records = {
        5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
        10: FileRecord(10, 5, "Users", FILE_ATTRIBUTE_DIRECTORY),
        11: FileRecord(11, 10, "profile.dat", 0, logical_size=10, allocated_size=16),
        12: FileRecord(12, 999, "orphan.txt", 0, logical_size=3, allocated_size=8),
    }
    return build_scan_result(VolumeInfo("C:", "System", "NTFS"), records)


SEQUENCED_ROOT_FRN = (7 << 48) | 5


def make_sequenced_records(include_root: bool) -> dict[int, FileRecord]:
    records = {
        10: FileRecord(10, SEQUENCED_ROOT_FRN, "Users", FILE_ATTRIBUTE_DIRECTORY),
        11: FileRecord(11, 10, "profile.dat", 0),
    }
    if include_root:
        records[SEQUENCED_ROOT_FRN] = FileRecord(
            SEQUENCED_ROOT_FRN, 5, ".", FILE_ATTRIBUTE_DIRECTORY
        )
    return records


class HierarchyTests(unittest.TestCase):
    def test_finds_sequence_tagged_root_record(self):
        result = build_scan_result(
            VolumeInfo("E:", "Data", "NTFS"), make_sequenced_records(True)
        )
        self.assertEqual(result.root_frn, SEQUENCED_ROOT_FRN)
        self.assertEqual(result.path_for(11), "E:\\Users\\profile.dat")
        self.assertEqual(result.root_stats, (2, 1, 1))

    def test_synthesizes_root_from_children_when_it_is_not_enumerated(self):
        result = build_scan_result(
            VolumeInfo("E:", "Data", "NTFS"), make_sequenced_records(False)
        )
        self.assertEqual(result.root_frn, SEQUENCED_ROOT_FRN)
        self.assertEqual(result.records[SEQUENCED_ROOT_FRN].name, ".")
        self.assertEqual(result.path_for(11), "E:\\Users\\profile.dat")

    def test_builds_recursive_counts_and_reattaches_orphans(self):
        result = make_result()
        self.assertEqual(result.records[12].parent_frn, 5)
        self.assertEqual(result.stats[10], (1, 1, 0))
        self.assertEqual(result.stats[5], (3, 2, 1))
        self.assertEqual(result.path_for(11), "C:\\Users\\profile.dat")

    def test_aggregates_sizes_bottom_up(self):
        result = make_result()
        totals = calculate_directory_sizes(result)
        self.assertEqual(totals[10], (10, 16))
        self.assertEqual(totals[5], (13, 24))
        self.assertTrue(result.sizes_complete)

    def test_path_builder_stops_on_cycle(self):
        result = make_result()
        result.records[10].parent_frn = 10
        self.assertIsNone(result.path_for(11))


if __name__ == "__main__":
    unittest.main()
