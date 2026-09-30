import struct
import unittest

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.platform.windows.mft import parse_usn_records


def make_v2_record(frn: int, parent: int, name: str, attributes: int) -> bytes:
    encoded = name.encode("utf-16-le")
    record_length = 60 + len(encoded)
    record = bytearray(record_length)
    struct.pack_into("<IHH", record, 0, record_length, 2, 0)
    struct.pack_into("<QQ", record, 8, frn, parent)
    struct.pack_into("<I", record, 52, attributes)
    struct.pack_into("<HH", record, 56, len(encoded), 60)
    record[60:] = encoded
    return bytes(record)


class MftParserTests(unittest.TestCase):
    def test_parses_usn_v2_record(self):
        payload = struct.pack("<Q", 99) + make_v2_record(
            42, 5, "Documents", FILE_ATTRIBUTE_DIRECTORY
        )
        records = list(parse_usn_records(payload))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].frn, 42)
        self.assertEqual(records[0].parent_frn, 5)
        self.assertEqual(records[0].name, "Documents")
        self.assertTrue(records[0].is_directory)

    def test_ignores_truncated_record(self):
        payload = struct.pack("<Q", 99) + make_v2_record(42, 5, "x", 0)[:-1]
        self.assertEqual(list(parse_usn_records(payload)), [])


if __name__ == "__main__":
    unittest.main()
