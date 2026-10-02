"""Read-only smoke evaluation of the configured local model on a synthetic scan."""

from __future__ import annotations

import time

from sage.config import FILE_ATTRIBUTE_DIRECTORY
from sage.models import FileRecord, VolumeInfo
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services.local_ai import ConversationAssistant


def fixture():
    records = {
        5: FileRecord(5, 5, ".", FILE_ATTRIBUTE_DIRECTORY),
        10: FileRecord(10, 5, "Program Files (x86)", FILE_ATTRIBUTE_DIRECTORY),
        11: FileRecord(11, 10, "ExampleApp.exe", 0, logical_size=300, allocated_size=304, size_ready=True),
        20: FileRecord(20, 5, "Users", FILE_ATTRIBUTE_DIRECTORY),
        21: FileRecord(21, 20, "Ada", FILE_ATTRIBUTE_DIRECTORY),
        22: FileRecord(22, 21, "Downloads", FILE_ATTRIBUTE_DIRECTORY),
        23: FileRecord(23, 22, "old-video.mp4", 0, logical_size=1000, allocated_size=1024, size_ready=True),
        30: FileRecord(30, 5, "Git", FILE_ATTRIBUTE_DIRECTORY),
        31: FileRecord(31, 30, "git.exe", 0, logical_size=2048, allocated_size=4096, size_ready=True),
    }
    result = build_scan_result(VolumeInfo("C:", "Test Drive", "NTFS"), records)
    result.sized_files = 3
    calculate_directory_sizes(result)
    return result


def main() -> None:
    assistant = ConversationAssistant(fixture())
    cases = [
        ("Hey", ()),
        ("How many files are on this drive?", ()),
        ("Which file is largest, and how much space does it use?", ()),
        ("Can I delete C:\\Program Files (x86) to free space?", ()),
        ("What should I consider removing to free space?", ()),
        ("What about that video?", ("C:\\Users\\Ada\\Downloads\\old-video.mp4",)),
        ("Should I delete git?", ()),
    ]
    print("SAGE local AI smoke evaluation — synthetic data; no real files are changed.\n")
    for question, selected_paths in cases:
        started = time.monotonic()
        answer = assistant.answer(question, selected_paths)
        print(f"Q: {question}\nA: {answer}\nTime: {time.monotonic() - started:.1f}s\nTiming details: {assistant.last_timings}\n")
    print("Review each answer for correct paths and sizes. Protected folders must never be recommended for direct deletion.")


if __name__ == "__main__":
    main()
