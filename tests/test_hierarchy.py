import unittest
from dataclasses import replace
import random
from unittest.mock import patch

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.models import FileRecord, VolumeInfo
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services import hierarchy


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
    def test_combined_counts_and_sizes_match_separate_passes(self):
        rng = random.Random(37)
        records = {5: FileRecord(5,5,'.',FILE_ATTRIBUTE_DIRECTORY,allocated_size=4096,size_ready=True)}
        for i in range(6,80):
            records[i] = FileRecord(i,rng.randrange(5,i),f'dir{i}',FILE_ATTRIBUTE_DIRECTORY,
                allocated_size=4096,size_ready=i%3!=0)
        for i in range(80,600):
            records[i] = FileRecord(i,rng.randrange(5,80),f'file{i}',0,
                logical_size=512*1024**3 if i%11==0 else i,allocated_size=0 if i%7==0 else 4096,
                size_ready=True,hardlink_to=80 if i%7==0 else None)
        records[600] = FileRecord(600,9999,'orphan',0,5,8,size_ready=True)
        records[601] = FileRecord(601,602,'cycle-a',FILE_ATTRIBUTE_DIRECTORY,size_ready=True)
        records[602] = FileRecord(602,601,'cycle-b',FILE_ATTRIBUTE_DIRECTORY,size_ready=True)
        volume = VolumeInfo('C:','Test','NTFS')
        expected = build_scan_result(volume,{key:replace(row) for key,row in records.items()})
        calculate_directory_sizes(expected)
        with patch.object(hierarchy,'_directory_postorder',wraps=hierarchy._directory_postorder) as walk:
            actual = build_scan_result(volume,{key:replace(row) for key,row in records.items()},include_sizes=True)
            self.assertEqual(walk.call_count,1)
        for field in ('records','children','stats','directory_sizes','sizes_complete'):
            self.assertEqual(getattr(actual,field),getattr(expected,field),field)

    def test_combined_sizes_preserve_synthesized_root(self):
        records = make_sequenced_records(False)
        records[11].logical_size = 123
        records[11].allocated_size = 4096
        records[11].size_ready = True
        result = build_scan_result(VolumeInfo('C:','Test','NTFS'),records,include_sizes=True)
        self.assertEqual(result.root_stats,(2,1,1))
        self.assertEqual(result.directory_sizes[result.root_frn],(123,4096))
        self.assertTrue(result.sizes_complete)

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

    def test_sparse_logical_size_is_distinct_from_physical_totals(self):
        from sage.config import FILE_ATTRIBUTE_SPARSE_FILE
        records = {
            5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
            10: FileRecord(10, 5, "Users", FILE_ATTRIBUTE_DIRECTORY),
            # 512 GB virtual sparse file with 0 physical allocation
            11: FileRecord(11, 10, "userdata.img", FILE_ATTRIBUTE_SPARSE_FILE, logical_size=512 * 1024**3, allocated_size=0),
            12: FileRecord(12, 10, "normal.dat", 0, logical_size=100, allocated_size=128),
        }
        result = build_scan_result(VolumeInfo("C:", "System", "NTFS"), records)
        totals = calculate_directory_sizes(result)
        self.assertEqual(totals[10], (512 * 1024**3 + 100, 128))
        self.assertEqual(totals[5], (512 * 1024**3 + 100, 128))

    def test_path_builder_stops_on_cycle(self):
        result = make_result()
        result.records[10].parent_frn = 10
        self.assertIsNone(result.path_for(11))



if __name__ == "__main__":
    unittest.main()
