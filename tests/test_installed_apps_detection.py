import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from sage.models import FileRecord, VolumeInfo
from sage.services.hierarchy import build_scan_result, calculate_directory_sizes
from sage.services.conversation import DriveAssistant
from sage.services.local_ai import OllamaClient
from sage.services.local_database import DriveIndex
from sage.services.cleanup import CleanupSuggestion


def create_scan(files_and_dirs: dict[str, int], drive="D:"):
    records = {5: FileRecord(5, 5, ".", 16)}
    paths = {"": 5}
    for name, size in files_and_dirs.items():
        parts = name.rstrip("/").split("/")
        for n, part in enumerate(parts):
            path = "/".join(parts[:n + 1])
            if path in paths:
                continue
            identity = len(paths) + 5
            is_folder = n < len(parts) - 1 or name.endswith("/")
            paths[path] = identity
            records[identity] = FileRecord(
                identity,
                paths["/".join(parts[:n])],
                part,
                16 if is_folder else 0,
                size,
                size,
                size_ready=not is_folder,
                modified=100,
            )
    result = build_scan_result(VolumeInfo(drive, "GamesVolume", "NTFS"), records)
    calculate_directory_sizes(result)
    return result


class InstalledAppsDetectionTests(unittest.TestCase):
    def test_ghost_apps_deleted_from_disk_are_never_suggested(self):
        # Scan only has Kingdom Come and Need for Speed on D:\Games
        scan_result = create_scan({
            "Games/Kingdom Come - Deliverance/game.exe": 50 * 1024**3,
            "Games/Need for Speed Heat/nfs.exe": 30 * 1024**3,
            "Games/photo.jpg": 100 * 1024,
        }, drive="D:")

        mock_registry_apps = [
            # Ghost apps: was deleted from disk long ago
            {
                "name": "God of War",
                "publisher": "PlayStation PC",
                "location": "D:\\Games\\God of War",
                "uninstall_string": "D:\\Games\\God of War\\unins000.exe",
                "estimated_bytes": 64 * 1024**3,
            },
            {
                "name": "Spider-Man: Miles Morales",
                "publisher": "PlayStation PC",
                "location": "D:\\Games\\Marvel's Spider-Man - Miles Morales",
                "uninstall_string": "D:\\Games\\Marvel's Spider-Man - Miles Morales\\unins000.exe",
                "estimated_bytes": 50 * 1024**3,
            },
            # Real apps: exists on disk and in scan
            {
                "name": "Kingdom Come: Deliverance",
                "publisher": "Warhorse Studios",
                "location": "D:\\Games\\Kingdom Come - Deliverance",
                "uninstall_string": "D:\\Games\\Kingdom Come - Deliverance\\unins000.exe",
                "estimated_bytes": 45 * 1024**3,
            },
            {
                "name": "Need for Speed Heat",
                "publisher": "Electronic Arts",
                "location": "D:\\Games\\Need for Speed Heat",
                "uninstall_string": "D:\\Games\\Need for Speed Heat\\unins000.exe",
                "estimated_bytes": 28 * 1024**3,
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            index = DriveIndex(str(Path(tmp) / "index.sqlite3"))
            client = OllamaClient(transport=lambda _: {"message": {"content": "Here is an explanation of Games."}})
            assistant = DriveAssistant(scan_result, client, index)

            # Mock os.path.exists to only return True for existing folders
            def mock_exists(path):
                norm = str(path).casefold()
                return "god of war" not in norm and "spider-man" not in norm

            with patch("sage.services.conversation.installed_apps", return_value=mock_registry_apps), \
                 patch("sage.services.conversation.os.path.exists", side_effect=mock_exists):
                suggestions = assistant._detect_installed_apps(("D:\\Games",))

            names = [s.title for s in suggestions]
            self.assertIn("Kingdom Come: Deliverance", names)
            self.assertIn("Need for Speed Heat", names)
            self.assertNotIn("God of War", names)
            self.assertNotIn("Spider-Man: Miles Morales", names)

            # Verify that real scanned size from MFT is used (50 GB, not the 45 GB estimate from registry)
            kcd = next(s for s in suggestions if s.title == "Kingdom Come: Deliverance")
            self.assertEqual(kcd.allocated, 50 * 1024**3)
            self.assertTrue(kcd.action.startswith("uninstall:"))
