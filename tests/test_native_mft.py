"""Parity checks between the Rust `sage_native` scanner and the Python reference parser."""
import random
import struct
import unittest

from sage.platform.windows.mft import parse_mft_image, records_from_native
from test_mft_parser import (make_mft_record, pack_file_name, pack_nonresident_data,
                             pack_resident_attr, pack_resident_data, pack_sparse_nonresident_data)
from test_scan_assessment import named_data

try:
    import sage_native
except ImportError:
    sage_native = None

RECORD_SIZE = 1024
SEQ = 1 << 48


def frn(index: int, sequence: int = 1) -> int:
    return (sequence << 48) | index


def with_fixup(record: bytearray, usn: bytes = b"\x2a\x00") -> bytes:
    """Protect a record with an Update Sequence Array, as NTFS stores it on disk."""
    record = bytearray(record)
    struct.pack_into("<HH", record, 4, 0x30, 3)
    record[0x30:0x32] = usn
    for i in (1, 2):
        end = i * 512
        record[0x30 + i * 2 : 0x32 + i * 2] = record[end - 2 : end]
        record[end - 2 : end] = usn
    return bytes(record)


def standard_info(modified_unix: int, attributes: int = 0) -> bytes:
    filetime = 116444736000000000 + modified_unix * 10_000_000
    return pack_resident_attr(0x10, struct.pack("<4QI", 0, filetime, 0, 0, attributes) + bytes(0x24))


def build_image() -> bytes:
    root = frn(5)
    records = [bytes(RECORD_SIZE) for _ in range(40)]

    def put(index, *attrs, flags=1, sequence=1, base=0, links=1, fixup=True):
        record = make_mft_record(*attrs, flags=flags, sequence=sequence, base=base)
        struct.pack_into("<H", record, 0x12, links)
        records[index] = with_fixup(record) if fixup else bytes(record)

    put(5, standard_info(1_600_000_000, 0x6), pack_file_name(".", root, ns=3, directory=True), flags=3)
    put(6, pack_file_name("Users", root, directory=True), flags=3)
    put(7, pack_file_name("Program Files", root), pack_file_name("PROGRA~1", root, ns=2), flags=3)
    put(8, standard_info(1_700_000_000, 0x20), pack_file_name("notes.txt", frn(6)), pack_resident_data(12))
    put(9, pack_file_name("big.bin", frn(6)), pack_nonresident_data(10_000_000, 10_002_432))
    put(10, pack_file_name("sparse.img", frn(6)), pack_sparse_nonresident_data(1 << 39, 65536))
    # Hard link: two Win32 names plus a DOS alias.
    put(11, pack_file_name("shared.dll", frn(6)), pack_file_name("SHARED~1.DLL", frn(6), ns=2),
        pack_file_name("linked.dll", frn(7)), pack_nonresident_data(100, 4096), links=2)
    # Base record with $ATTRIBUTE_LIST and an extension record holding another name and stream.
    put(12, pack_file_name("large.bin", frn(6)), pack_resident_attr(0x20, b""), pack_nonresident_data(100, 4096))
    put(13, pack_file_name("large-link.bin", frn(7)), named_data("sidecar", 25, 8192), base=frn(12))
    # Extension whose base record is missing.
    put(14, named_data("orphan", 1, 4096), base=frn(30, 9))
    # Deleted record, record failing fixup, record without a name.
    put(15, pack_file_name("deleted.txt", root), flags=0)
    put(16, pack_file_name("torn.txt", root), fixup=False)
    put(17, pack_resident_data(5))
    # Junction keeps the reparse attribute; a cloud placeholder does not.
    put(18, standard_info(1_650_000_000, 0x410), pack_file_name("link", root),
        pack_resident_attr(0xC0, struct.pack("<I", 0xA0000003)), flags=3)
    put(19, standard_info(1_650_000_000, 0x400), pack_file_name("cloud.docx", frn(6)),
        pack_resident_attr(0xC0, struct.pack("<I", 0x9000001A)))
    put(20, pack_file_name("WofFile.exe", frn(7)), pack_sparse_nonresident_data(5000, 0),
        named_data("WofCompressedData", 100, 4096))
    put(21, pack_file_name("Dir$I30", root, directory=True), named_data("$I30", 8192, 8192, kind=0xA0), flags=3)
    put(22, pack_file_name("seq.txt", root), pack_resident_data(3), sequence=7)
    put(23, pack_file_name("ünïcode 文件.txt", frn(6)), pack_resident_data(9))
    return b"".join(records)


def native_records(image: bytes):
    return records_from_native(sage_native.parse_mft_image(image, RECORD_SIZE))


@unittest.skipIf(sage_native is None, "sage_native is not built; see native/README section in README.md")
class NativeParityTests(unittest.TestCase):
    def assert_parity(self, image: bytes) -> None:
        expected_records, expected_skipped = parse_mft_image(image, RECORD_SIZE)
        actual_records, actual_skipped = native_records(image)
        self.assertEqual(actual_skipped, expected_skipped)
        self.assertEqual(list(actual_records.items()), list(expected_records.items()))

    def test_matches_python_on_synthetic_volume(self):
        image = build_image()
        self.assert_parity(image)
        records, skipped = native_records(image)
        self.assertEqual(skipped, 3)  # torn fixup, unnamed record, orphan extension
        self.assertEqual(records[frn(8)].name, "notes.txt")
        self.assertEqual(records[frn(8)].modified, 1_700_000_000)
        self.assertEqual((records[frn(9)].logical_size, records[frn(9)].allocated_size), (10_000_000, 10_002_432))
        self.assertEqual(records[frn(12)].allocated_size, 4096 + 8192)
        self.assertEqual(records[frn(22, 7)].name, "seq.txt")
        self.assertEqual(records[frn(23)].name, "ünïcode 文件.txt")
        aliases = [r for r in records.values() if r.hardlink_to is not None]
        self.assertEqual({(a.name, a.hardlink_to) for a in aliases},
                         {("linked.dll", frn(11)), ("large-link.bin", frn(12))})
        self.assertTrue(all(a.allocated_size == 0 for a in aliases))

    def test_matches_python_on_corrupted_records(self):
        base = build_image()
        rng = random.Random(1234)
        compared = 0
        for _ in range(3000):
            image = bytearray(base)
            for _ in range(rng.randint(1, 6)):
                index = rng.randrange(5, 24) * RECORD_SIZE
                image[index + rng.randrange(0, 0x200)] = rng.randrange(256)
            try:
                expected = parse_mft_image(bytes(image), RECORD_SIZE)
            except (struct.error, IndexError, ValueError):
                continue  # The Python reader rejects this layout outright; Rust must still not crash.
            finally:
                native_records(bytes(image))
            actual = native_records(bytes(image))
            self.assertEqual(actual[1], expected[1])
            self.assertEqual(list(actual[0].items()), list(expected[0].items()))
            compared += 1
        self.assertGreater(compared, 2000)

    def test_rejects_unusable_record_size(self):
        with self.assertRaises(ValueError):
            sage_native.parse_mft_image(b"", 16)


if __name__ == "__main__":
    unittest.main()
