"""Checks for scan accounting."""
import struct
import unittest
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

from sage.models import FileRecord, VolumeInfo
from sage.platform.windows.mft import parse_mft_file_record, _complete_mft_records, enumerate_mft
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services.retrieval import ScanEvidence
from test_mft_parser import (make_mft_record, pack_file_name, pack_nonresident_data,
                             pack_sparse_nonresident_data, pack_resident_attr)


def named_data(name, logical, allocated, kind=0x80, lowest_vcn=0):
    raw = bytearray(pack_nonresident_data(logical, allocated))
    encoded = name.encode('utf-16-le')
    raw.extend(encoded)
    raw.extend(bytes((-len(raw)) % 8))
    struct.pack_into('<II', raw, 0, kind, len(raw))
    raw[9] = len(name)
    struct.pack_into('<H', raw, 0x0A, 0x40)
    struct.pack_into('<q', raw, 0x10, lowest_vcn)
    return bytes(raw)


class ScanAssessmentTests(unittest.TestCase):
    def test_native_reader_pipeline_joins_extensions_and_counts_link_paths(self):
        root, folder, file_id = ((1 << 48) | n for n in (5, 6, 7))
        chunks = [bytes(1024) for _ in range(9)]
        chunks[1] = make_mft_record(pack_file_name('deleted.txt', root), flags=0)
        chunks[5] = make_mft_record(pack_file_name('.', root), flags=3)
        chunks[6] = make_mft_record(pack_file_name('Dir', root), flags=3)
        chunks[7] = make_mft_record(pack_file_name('one.bin', root), pack_resident_attr(0x20, b''),
                                    pack_nonresident_data(100, 4096))
        chunks[8] = make_mft_record(pack_file_name('two.bin', folder),
                                    named_data('extra', 25, 8192), base=file_id)
        data = b''.join(chunks)
        geometry = SimpleNamespace(BytesPerSector=512, BytesPerCluster=1024,
            BytesPerFileRecordSegment=1024, MftValidDataLength=len(data), MftStartLcn=1)
        kernel = Mock()
        kernel.CreateFileW.return_value = 1
        with patch('sage.platform.windows.mft.scan_backend', return_value='python'), \
             patch('sage.platform.windows.mft._kernel32', return_value=kernel), \
             patch('sage.platform.windows.mft._volume_data', return_value=geometry), \
             patch('sage.platform.windows.mft._mft_data_runs', return_value=[(1, 9)]), \
             patch('sage.platform.windows.mft.apply_mft_fixup', return_value=True), \
             patch('sage.platform.windows.mft._read_at', side_effect=[bytes(1024), data]):
            records, skipped = enumerate_mft('C:')
        result = build_scan_result(VolumeInfo('C:', '', 'NTFS'), records, skipped)
        calculate_directory_sizes(result)
        self.assertEqual(skipped, 0)
        self.assertEqual(result.root_stats, (3, 2, 1))
        self.assertEqual(result.directory_sizes[root], (200, 12288))
        self.assertEqual(ScanEvidence(result).inspect(r'C:\Dir\two.bin')['logical_bytes'], 100)
        kernel.CloseHandle.assert_called_once_with(1)

    def test_named_streams_and_directory_indexes_contribute_only_allocation(self):
        parsed = parse_mft_file_record(make_mft_record(pack_file_name('compressed.bin', 5),
            pack_sparse_nonresident_data(1000, 0), named_data('WofCompressedData', 100, 4096)))
        self.assertEqual(parsed[5:7], (1000, 4096))
        directory = parse_mft_file_record(make_mft_record(pack_file_name('Dir', 5),
            named_data('$I30', 8192, 8192, kind=0xA0), flags=3))
        records = {5: FileRecord(5, 5, '.', 16, size_ready=True),
                   6: FileRecord(6, 5, 'Dir', 16, allocated_size=directory[6], size_ready=True),
                   7: FileRecord(7, 6, 'compressed.bin', 0, *parsed[5:7], size_ready=True)}
        result = build_scan_result(VolumeInfo('C:', '', 'NTFS'), records)
        calculate_directory_sizes(result)
        self.assertEqual(result.directory_sizes[5], (1000, 12288))

    def test_hardlinks_keep_all_paths_and_one_physical_allocation(self):
        details = {}
        parsed = parse_mft_file_record(make_mft_record(pack_file_name('shared.dll', 6),
            pack_file_name('SHARED~1.DLL', 6, ns=2), pack_file_name('linked.dll', 7),
            pack_nonresident_data(100, 4096)), details)
        self.assertEqual(len(details['names']), 2)
        records = {5: FileRecord(5, 5, '.', 16), 6: FileRecord(6, 5, 'A', 16),
                   7: FileRecord(7, 5, 'B', 16),
                   8: FileRecord(8, 6, parsed[3], 0, *parsed[5:7], size_ready=True)}
        self.assertEqual(_complete_mft_records(records, {8: details}, {}), 0)
        result = build_scan_result(VolumeInfo('C:', '', 'NTFS'), records)
        calculate_directory_sizes(result)
        self.assertEqual(result.root_stats, (4, 2, 2))
        self.assertEqual(result.directory_sizes[5], (200, 4096))
        self.assertEqual(ScanEvidence(result).inspect(r'C:\B\linked.dll')['logical_bytes'], 100)

    def test_extension_streams_merge_without_double_counting_continuations(self):
        base_details = {}
        parse_mft_file_record(make_mft_record(pack_file_name('large.bin', 5),
            pack_resident_attr(0x20, b''), pack_nonresident_data(100, 4096)), base_details)
        extra = {}
        parse_mft_file_record(make_mft_record(named_data('', 999, 999, lowest_vcn=2),
            named_data('sidecar', 25, 8192), base=8), extra)
        records = {8: FileRecord(8, 5, 'large.bin', 0, 100, 4096, size_ready=True)}
        missing = (2 << 48) | 8
        self.assertEqual(_complete_mft_records(records, {8: base_details}, {8: extra, missing: extra}), 1)
        self.assertEqual((records[8].logical_size, records[8].allocated_size), (100, 12288))


if __name__ == '__main__':
    unittest.main()
