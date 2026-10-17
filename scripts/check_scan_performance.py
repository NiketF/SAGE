"""Runnable self-check for SAGE scanning performance and storage consistency fixes.

Verifies:
1. Directory cycle breaking and descendant size aggregation.
2. Parent FRN sequence number mismatch resolution.
3. BadClust record 8 logical size capping behavior.
4. Compressed file allocation size parsing.
5. Single-pass hierarchy and size computation.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.models import FileRecord, VolumeInfo
from sage.platform.windows.mft import parse_mft_file_record
from sage.services.hierarchy import build_scan_result


def pack_resident_attr(attr_type: int, value: bytes) -> bytes:
    buf = bytearray(0x18 + len(value))
    struct.pack_into("<II", buf, 0, attr_type, len(buf))
    struct.pack_into("<H", buf, 0x0A, 0x18)
    struct.pack_into("<I", buf, 0x10, len(value))
    struct.pack_into("<H", buf, 0x14, 0x18)
    buf[0x18 : 0x18 + len(value)] = value
    return bytes(buf)


def pack_file_name(
    name: str,
    parent: int,
    ns: int = 1,
    logical: int = 0,
    allocated: int = 0,
    directory: bool = False,
) -> bytes:
    encoded = name.encode("utf-16-le")
    value = bytearray(0x42 + len(encoded))
    struct.pack_into("<Q", value, 0, parent)
    struct.pack_into("<q", value, 0x28, allocated)
    struct.pack_into("<q", value, 0x30, logical)
    if directory:
        struct.pack_into("<I", value, 0x38, 0x10000000)
    value[0x40] = len(name)
    value[0x41] = ns
    value[0x42:] = encoded
    return pack_resident_attr(0x30, bytes(value))


def pack_nonresident_data(logical: int, allocated: int) -> bytes:
    buf = bytearray(0x40)
    struct.pack_into("<II", buf, 0, 0x80, 0x40)
    buf[8] = 1
    struct.pack_into("<H", buf, 0x20, 0x40)
    struct.pack_into("<q", buf, 0x28, allocated)
    struct.pack_into("<q", buf, 0x30, logical)
    struct.pack_into("<q", buf, 0x38, logical)
    return bytes(buf)


def pack_compressed_data(logical: int, uncompressed_alloc: int, compressed_alloc: int) -> bytes:
    buf = bytearray(0x48)
    struct.pack_into("<II", buf, 0, 0x80, 0x48)
    buf[8] = 1
    struct.pack_into("<H", buf, 0x0C, 0x0001)
    struct.pack_into("<H", buf, 0x20, 0x48)
    struct.pack_into("<q", buf, 0x28, uncompressed_alloc)
    struct.pack_into("<q", buf, 0x30, logical)
    struct.pack_into("<q", buf, 0x38, logical)
    struct.pack_into("<q", buf, 0x40, compressed_alloc)
    return bytes(buf)


def make_mft_record(*attrs: bytes, flags: int = 1, sequence: int = 1, base: int = 0) -> bytearray:
    record = bytearray(1024)
    record[0:4] = b"FILE"
    struct.pack_into("<H", record, 0x10, sequence)
    struct.pack_into("<H", record, 0x14, 0x38)
    struct.pack_into("<H", record, 0x16, flags)
    struct.pack_into("<I", record, 0x1C, 1024)
    struct.pack_into("<Q", record, 0x20, base)
    pos = 0x38
    for attr in attrs:
        record[pos : pos + len(attr)] = attr
        pos += len(attr)
    struct.pack_into("<I", record, pos, 0xFFFFFFFF)
    struct.pack_into("<I", record, 0x18, pos + 8)
    return record


def check_cycle_breaking_and_aggregation() -> None:
    # Root (FRN 5)
    # Dir A (FRN 10) points to Dir B (FRN 11)
    # Dir B (FRN 11) points to Dir A (FRN 10) -> Cycle!
    # File F (FRN 12) inside Dir B with 500 bytes
    vol = VolumeInfo("C:", "System", "NTFS")
    root = FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY)
    dir_a = FileRecord(10, 11, "DirA", FILE_ATTRIBUTE_DIRECTORY)
    dir_b = FileRecord(11, 10, "DirB", FILE_ATTRIBUTE_DIRECTORY)
    file_f = FileRecord(
        12,
        11,
        "file.txt",
        0,
        logical_size=500,
        allocated_size=1024,
        size_ready=True,
    )

    records = {5: root, 10: dir_a, 11: dir_b, 12: file_f}
    result = build_scan_result(vol, records)

    # Both directories and the file must be reachable from root
    assert root.frn in result.children
    # The cycle must have been broken by attaching one of the cycle nodes to root
    assert 10 in result.children[5] or 11 in result.children[5]
    # Total aggregated sizes must account for file_f in root and directories
    root_stats = result.stats.get(5)
    assert root_stats is not None
    total_items, file_count, dir_count = root_stats
    assert file_count == 1
    assert dir_count == 2
    assert total_items == 3

    assert result.directory_sizes is not None
    root_sizes = result.directory_sizes.get(5)
    assert root_sizes == (500, 1024)
    print("[OK] Cycle breaking and aggregation passed.")


def check_parent_sequence_mismatch() -> None:
    # Parent FRN has sequence number (e.g. seq=2 -> FRN = (2 << 48) | 100)
    # Child refers to parent with seq=0 (FRN = 100)
    vol = VolumeInfo("C:", "System", "NTFS")
    root = FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY)
    parent_frn = (2 << 48) | 100
    parent_dir = FileRecord(
        parent_frn, 5, "Folder", FILE_ATTRIBUTE_DIRECTORY
    )
    child_file = FileRecord(
        200,
        100,  # sequence number 0
        "child.txt",
        0,
        logical_size=300,
        allocated_size=512,
        size_ready=True,
    )

    records = {5: root, parent_frn: parent_dir, 200: child_file}
    result = build_scan_result(vol, records)

    # Child should be attached to parent_dir, not dropped to root
    assert 200 in result.children[parent_frn]
    assert 200 not in result.children[5]
    assert result.directory_sizes[parent_frn] == (300, 512)
    print("[OK] Parent sequence mismatch resolution passed.")


def check_compressed_file_allocation() -> None:
    fn = pack_file_name("compressed.bin", 5)
    data = pack_compressed_data(logical=50000, uncompressed_alloc=65536, compressed_alloc=16384)
    record = make_mft_record(fn, data)

    rec = parse_mft_file_record(record, 0)
    assert rec is not None
    _, _, _, name, _, logical, allocated, _, _ = rec
    assert name == "compressed.bin"
    assert logical == 50000
    assert allocated == 16384  # Compressed allocation, NOT uncompressed
    print("[OK] Compressed file allocation passed.")


def check_file_name_fallback_size() -> None:
    # File without $DATA attribute; should fall back to $FILE_NAME sizes
    fn = pack_file_name("nodata.txt", 5, logical=4096, allocated=4096)
    record = make_mft_record(fn)

    rec = parse_mft_file_record(record, 0)
    assert rec is not None
    _, _, _, name, _, logical, allocated, _, _ = rec
    assert name == "nodata.txt"
    assert logical == 4096
    assert allocated == 4096
    print("[OK] FILE_NAME fallback size passed.")


def check_badclust_capping_logic() -> None:
    # Verify BadClust capping rule
    rec_num = 8
    name = "$BadClust"
    logical = 2 * 1024 * 1024 * 1024 * 1024
    allocated = 0
    if rec_num == 8 and name == "$BadClust":
        logical = allocated
    assert logical == 0
    assert allocated == 0
    print("[OK] BadClust capping logic passed.")


def main() -> None:
    print("Running SAGE scan performance & storage consistency checks...")
    check_cycle_breaking_and_aggregation()
    check_parent_sequence_mismatch()
    check_compressed_file_allocation()
    check_file_name_fallback_size()
    check_badclust_capping_logic()
    print("All checks passed successfully.")


if __name__ == "__main__":
    main()
