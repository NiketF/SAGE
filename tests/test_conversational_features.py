import tempfile
import unittest
from unittest.mock import patch

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.models import FileRecord, VolumeInfo
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services.local_ai import ConversationAssistant, OllamaClient
from sage.services.local_database import DriveIndex
from sage.services.tool_registry import ToolRegistry


def sample_scan():
    records = {
        5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
        10: FileRecord(10, 5, "Program Files (x86)", FILE_ATTRIBUTE_DIRECTORY),
        11: FileRecord(11, 10, "example.exe", 0, logical_size=20, allocated_size=24, size_ready=True),
        20: FileRecord(20, 5, "Users", FILE_ATTRIBUTE_DIRECTORY),
        21: FileRecord(21, 20, "Ada", FILE_ATTRIBUTE_DIRECTORY),
        22: FileRecord(22, 21, "Downloads", FILE_ATTRIBUTE_DIRECTORY),
        23: FileRecord(23, 22, "archive.zip", 0, logical_size=100, allocated_size=104, size_ready=True),
    }
    result = build_scan_result(VolumeInfo("C:", "System", "NTFS"), records)
    calculate_directory_sizes(result)
    result.sized_files = 2
    return result


class LocalIndexTests(unittest.TestCase):
    def test_index_persists_metadata_and_supports_name_retrieval(self):
        result = sample_scan()
        with tempfile.TemporaryDirectory() as tmp:
            index = DriveIndex(db_path=f"{tmp}/index.sqlite3")
            index.update_scan(result)
            names = index.search_names("archive", limit=5)
            self.assertTrue(any(item["path"].endswith("archive.zip") for item in names))
            context = index.retrieve_context("archive", limit=5)
            self.assertTrue(context["citations"])
            self.assertIn("source", context["citations"][0])


class ToolRegistryTests(unittest.TestCase):
    def test_tool_validation_rejects_unknown_or_invalid_arguments(self):
        registry = ToolRegistry(None, None)
        with self.assertRaisesRegex(ValueError, "Unknown tool"):
            registry.call("delete_path", {"path": "C:\\temp"})
        with self.assertRaisesRegex(ValueError, "Unexpected arguments"):
            registry.call("inspect_path", {"path": "C:\\temp", "force": True})


class ConversationIntegrationTests(unittest.TestCase):
    def test_modify_question_uses_explain_before_action_read_only_reply(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: {"message": {"content": "unused"}}))
        reply = assistant.answer("What happens if I modify C:\\Users\\Ada\\Downloads\\archive.zip?")
        self.assertIn("archive.zip", reply)
        self.assertIn("No action was performed", reply)
        self.assertIn("read-only", reply)

    def test_c_drive_uses_separate_stricter_policy(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: {"message": {"content": "unused"}}))
        assistant.answer("Can I delete C:\\Users\\Ada\\Downloads\\archive.zip?")
        policy = assistant.last_context.get("action_policy", {})
        self.assertEqual(policy.get("tier"), "c_strict_read_only")
        self.assertTrue(policy.get("read_only"))

    def test_context_includes_citations(self):
        assistant = ConversationAssistant(sample_scan(), OllamaClient(transport=lambda _: {"message": {"content": "unused"}}))
        assistant.answer("Explain C:\\Users\\Ada\\Downloads\\archive.zip")
        refs = assistant.last_context.get("references", [])
        self.assertIsInstance(refs, list)
        self.assertTrue(all("path" in ref for ref in refs))

    def test_model_deadline_has_deterministic_fallback(self):
        assistant = ConversationAssistant(
            sample_scan(),
            OllamaClient(transport=lambda _: {"message": {"content": "This is a model response."}}),
        )
        with patch("sage.services.local_ai.MODEL_TARGET_SECONDS", 0):
            reply = assistant.answer("Give me an overview of files")
        self.assertIn("exceeded the 0-second target", reply)
        self.assertIn("Basic scan answer", reply)


if __name__ == "__main__":
    unittest.main()
