"""Read-only evidence retrieval over the current NTFS scan."""

from __future__ import annotations

import heapq
import ntpath
import os
import re

from ..models import FileRecord, ScanResult
from .evidence import format_bytes


def assess_path(path: str, system_drive: str | None = None) -> dict[str, str]:
    """Classify cleanup risk conservatively; never claim a path is safe to delete."""
    normalized = ntpath.normcase(ntpath.normpath(path.removeprefix("\\\\?\\")))
    drive, tail = ntpath.splitdrive(normalized)
    system = ntpath.normcase((system_drive or os.environ.get("SystemDrive") or "C:").rstrip("\\/"))
    parts = [part for part in tail.split("\\") if part]
    if not drive or not parts:
        return {"risk": "protected", "guidance": "Do not delete or move a drive root."}
    first = parts[0]
    if first in {"program files", "program files (x86)", "programdata", "system volume information", "recovery"}:
        action = "Uninstall an unwanted application through Windows Settings." if first.startswith("program files") else "Use Windows Storage cleanup or the relevant application settings."
        return {"risk": "protected", "guidance": f"Do not directly delete or move this system/application location. {action}"}
    if drive == system:
        if first in {"windows", "boot"}:
            return {"risk": "protected", "guidance": "Do not directly delete or move this system location. Use Windows Storage cleanup instead."}
        if first in {"pagefile.sys", "swapfile.sys", "hiberfil.sys"}:
            return {"risk": "protected", "guidance": "Windows manages this file; do not delete it directly."}
        if first == "users" and "appdata" in parts:
            return {"risk": "protected", "guidance": "Application data can contain settings and state. Do not delete the folder directly."}
    if "downloads" in parts or "documents" in parts or "desktop" in parts:
        return {"risk": "review", "guidance": "Personal files require your review and a backup check before removal."}
    return {"risk": "unknown", "guidance": "The scan alone cannot establish that deleting or moving this path is safe."}


class ScanEvidence:
    """Bounded retrieval so a local model never receives the entire drive tree."""

    def __init__(self, result: ScanResult) -> None:
        self.result = result
        self._folders: dict[str, list[int]] | None = None
        self._largest_cache: dict[tuple[str, int], tuple[int, ...]] = {}

    def _folder_index(self) -> dict[str, list[int]]:
        if self._folders is None:
            self._folders = {}
            for record in self.result.records.values():
                if record.is_directory and record.frn != self.result.root_frn:
                    self._folders.setdefault(record.name.casefold(), []).append(record.frn)
        return self._folders

    def mentioned(self, question: str, limit: int = 6) -> dict:
        """Resolve names such as Git before asking the model to interpret them."""
        words = re.findall(r"[\w.\-]+", question.casefold())
        ignored = {"a", "an", "and", "are", "can", "could", "delete", "do", "does", "drive", "file", "files", "folder", "folders", "for", "from", "have", "how", "i", "in", "is", "it", "keep", "me", "move", "my", "of", "on", "or", "please", "remove", "should", "that", "the", "this", "to", "what", "which", "would", "you"}
        ignored.update({"about", "big", "details", "give", "information", "large", "largest", "biggest", "list", "much", "overview", "show", "size", "space", "storage", "tell", "use", "used", "uses", "using", "taking", "take", "worth", "hey", "hello", "hi", "these", "those", "them", "its"})
        index = self._folder_index()
        found: list[int] = []
        quoted = [value.casefold() for groups in re.findall(r'"([^"\n]+)"|`([^`\n]+)`|(?<!\w)\x27([^\x27\n]+)\x27', question) for value in groups if value]
        for name in quoted:
            for frn in index.get(name, ()):
                if frn not in found and len(found) < limit:
                    found.append(frn)
        for width in range(min(5, len(words)), 0, -1):
            if len(found) >= limit:
                break
            for start in range(len(words) - width + 1):
                phrase = " ".join(words[start:start + width]).rstrip(".")
                if width == 1 and phrase in ignored:
                    continue
                for frn in index.get(phrase, ()):
                    if frn not in found:
                        found.append(frn)
                        if len(found) >= limit:
                            break
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        terms = list(dict.fromkeys(quoted + [word.rstrip(".") for word in words if word not in ignored and len(word.rstrip(".")) >= 2]))[:4]
        if not found and terms:
            exact: list[int] = []
            partial: list[int] = []
            for record in self.result.records.values():
                name = record.name.casefold()
                if name in terms:
                    exact.append(record.frn)
                elif len(partial) < limit and any(term in name for term in terms):
                    partial.append(record.frn)
            found = (exact + partial)[:limit]
        return {"items": [self._entry(self.result.records[frn]) for frn in found], "searched_terms": terms}

    def overview(self) -> dict:
        result = self.result
        items, files, folders = result.root_stats
        logical, allocated = result.directory_sizes.get(result.root_frn, (0, 0))
        return {
            "drive": result.volume.drive,
            "filesystem": result.volume.filesystem,
            "records": len(result.records),
            "items": items,
            "files": files,
            "folders": folders,
            "sizes_complete": result.sizes_complete,
            "sized_files": result.sized_files,
            "logical_bytes": logical if result.sizes_complete else None,
            "allocated_bytes": allocated if result.sizes_complete else None,
            "size_errors": result.size_errors,
        }

    def _entry(self, record: FileRecord) -> dict:
        result = self.result
        path = result.path_for(record.frn)
        logical, allocated = (
            result.directory_sizes.get(record.frn, (0, 0))
            if record.is_directory else (record.logical_size, record.allocated_size)
        )
        ready = result.sizes_complete if record.is_directory else record.size_ready and not record.size_error
        return {
            "path": path,
            "kind": "folder" if record.is_directory else "file",
            "logical_bytes": logical if ready else None,
            "allocated_bytes": allocated if ready else None,
            "logical_display": format_bytes(logical) if ready else "pending",
            "allocated_display": format_bytes(allocated) if ready else "pending",
            "size_error": record.size_error,
            "cleanup_risk": assess_path(path or ""),
            "child_names": [result.records[frn].name for frn in result.children.get(record.frn, ())[:6]] if record.is_directory else [],
            "child_count": len(result.children.get(record.frn, ())) if record.is_directory else 0,
        }

    def largest(self, kind: str = "folders", limit: int = 8) -> dict:
        result = self.result
        limit = max(1, min(int(limit), 20))
        if not result.sizes_complete:
            return {"status": "sizes_pending", "items": []}
        want_folders = kind != "files"
        candidates = (
            record for record in result.records.values()
            if record.frn != result.root_frn and record.is_directory == want_folders
        )
        cache_key = ("folders" if want_folders else "files", limit)
        if cache_key not in self._largest_cache:
            self._largest_cache[cache_key] = tuple(record.frn for record in heapq.nlargest(
                limit, candidates,
                key=lambda record: result.directory_sizes.get(record.frn, (0, 0))[1]
                if want_folders else record.allocated_size,
            ))
        biggest = [result.records[frn] for frn in self._largest_cache[cache_key]]
        return {"status": "complete", "items": [self._entry(record) for record in biggest]}

    def search(self, term: str, limit: int = 10) -> dict:
        term = term.strip().casefold()[:120]
        limit = max(1, min(int(limit), 20))
        if len(term) < 2:
            return {"error": "Provide at least two characters to search."}
        matches = []
        for record in self.result.records.values():
            if term in record.name.casefold():
                matches.append(record)
                if len(matches) >= limit:
                    break
        return {"term": term, "items": [self._entry(record) for record in matches]}

    def inspect(self, path: str) -> dict:
        result = self.result
        requested = ntpath.normcase(ntpath.normpath(path.removeprefix("\\\\?\\")))
        if not requested.startswith(ntpath.normcase(result.volume.drive + "\\")):
            return {"error": "Path is outside the current scanned drive."}
        if requested == ntpath.normcase(result.volume.drive + "\\"):
            logical, allocated = result.directory_sizes.get(result.root_frn, (0, 0))
            return {"path": path, "kind": "drive", "logical_bytes": logical if result.sizes_complete else None, "allocated_bytes": allocated if result.sizes_complete else None, "logical_display": format_bytes(logical) if result.sizes_complete else "pending", "allocated_display": format_bytes(allocated) if result.sizes_complete else "pending", "cleanup_risk": assess_path(path)}
        name = ntpath.basename(requested)
        for record in result.records.values():
            if record.name.casefold() != name.casefold():
                continue
            actual = result.path_for(record.frn)
            if actual and ntpath.normcase(actual) == requested:
                return self._entry(record)
        return {"error": "Path was not found in the current scan.", "path": path}
