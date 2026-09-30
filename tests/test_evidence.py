import unittest

from sage.models import FileRecord, VolumeInfo
from sage.services.evidence import EvidenceAssistant, format_bytes
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.config import FILE_ATTRIBUTE_DIRECTORY


def evidence_result():
    records = {
        5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
        10: FileRecord(10, 5, "Data", FILE_ATTRIBUTE_DIRECTORY),
        11: FileRecord(11, 10, "large.bin", 0, 2048, 4096),
    }
    result = build_scan_result(VolumeInfo("E:", "Archive", "NTFS"), records)
    calculate_directory_sizes(result)
    return result


class EvidenceTests(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(1024**3), "1.0 GB")

    def test_assistant_uses_current_evidence(self):
        assistant = EvidenceAssistant(evidence_result())
        answer = assistant.answer("What are the largest folders?")
        self.assertIn("Data", answer)
        self.assertIn("4.0 KB allocated", answer)
        self.assertIn("Evidence source", answer)

    def test_assistant_does_not_guess_before_size_phase(self):
        result = evidence_result()
        result.sizes_complete = False
        answer = EvidenceAssistant(result).answer("How much space is used?")
        self.assertIn("will not estimate", answer)


if __name__ == "__main__":
    unittest.main()
