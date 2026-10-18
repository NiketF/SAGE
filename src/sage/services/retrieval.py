"""Read-only evidence retrieval over the current NTFS scan."""

from __future__ import annotations

import heapq
import ntpath
import os
import re
import json
import time
import mimetypes
from collections import Counter, deque
from datetime import datetime, timezone

from ..models import FileRecord, ScanResult
from .evidence import format_bytes

_WORD = re.compile(r"[\w.\-]+")


def _name_key(name: str) -> str:
    """Match 'Program Files (x86)' to the words 'program files x86'."""
    return " ".join(_WORD.findall(name.casefold()))


def assess_path(path: str, system_drive: str | None = None) -> dict[str, str]:
    """Describe consequences, not a guarantee that a filename is disposable."""
    normalized = ntpath.normcase(ntpath.normpath(path.removeprefix("\\\\?\\")))
    drive, tail = ntpath.splitdrive(normalized)
    system = ntpath.normcase((system_drive or os.environ.get("SystemDrive") or "C:").rstrip("\\/"))
    parts = [part for part in tail.split("\\") if part]
    if not drive or not parts:
        return {"risk": "protected", "guidance": "Do not delete or move a drive root."}
    first = parts[0]
    if first in {"$mft", "$mftmirr", "$bitmap", "$logfile", "$boot", "$secure", "$volume", "$attrdef", "$upcase", "$extend", "$badclus"}:
        return {"risk": "protected", "guidance": "This is NTFS filesystem metadata used to maintain the drive. $MFT records its files and folders; other NTFS metadata supports allocation, recovery, security or filesystem structure. Do not manually delete or relocate it; doing so can damage the filesystem and make files inaccessible."}
    # ponytail: path rules identify likely roles, not installed dependencies.
    # Add verified application/known-folder evidence here when available.
    if ".git" in parts:
        return {"risk": "protected", "guidance": "This is repository metadata. Deleting it can lose local history, branches and uncommitted repository state; moving it alone can break the repository. Keep it with its project."}
    if first == "users" and len(parts) <= 2:
        return {"risk": "protected", "guidance": "This is a user-profile container. Do not delete or move the whole container; inspect its individual documents, downloads and application data instead."}
    if first == "documents and settings":
        return {"risk": "protected", "guidance": "This is a compatibility link to your user profiles, not your Documents folder. Do not delete or move it. Your documents are under Users."}
    if first in {"program files", "program files (x86)", "programdata", "system volume information", "recovery"}:
        action = "Use Windows Installed apps to uninstall an application you no longer use." if first.startswith("program files") else "Use Windows Storage cleanup or the relevant application settings."
        if len(parts) == 1 or first in {"system volume information", "recovery"}:
            return {"risk": "protected", "guidance": f"Do not directly delete or move this container. {action}"}
        return {"risk": "protected", "guidance": "This is an application location. Remove software with Installed apps or the owning application's cleanup flow, not by deleting this path directly."}
    if drive == system:
        if first in {"windows", "boot"}:
            if len(parts) == 1 or first == "boot" or parts in (["windows", "system32"], ["windows", "syswow64"], ["windows", "winsxs"]):
                return {"risk": "protected", "guidance": "This container supports Windows. Inspect individual contents rather than deleting or relocating the whole container."}
            return {"risk": "unverified", "guidance": "This is inside Windows. Establish the specific item's role and dependencies before recommending deletion or relocation; location alone is not proof of necessity."}
        if first in {"pagefile.sys", "swapfile.sys", "hiberfil.sys"}:
            return {"risk": "protected", "guidance": "Windows manages this file; do not delete it directly."}
        if first == "users" and "appdata" in parts:
            if len(parts) >= 5 and parts[2:5] == ["appdata", "local", "programs"]:
                return {"risk": "protected", "guidance": "This is an installed-application location under AppData. Use Installed apps or the app's own uninstaller instead of deleting this path directly."}
            if len(parts) >= 6 and parts[2:6] == ["appdata", "local", "pip", "cache"]:
                return {"risk": "managed_cleanup", "guidance": "This matches pip's conventional cache location, not installed packages. Verify the exact cache with `python -m pip cache dir`; if it matches, use `python -m pip cache purge`. Purging discards cached downloads/wheels and may require downloading or rebuilding them later. Do not move the folder manually."}
            if len(parts) >= 5 and parts[2:5] == ["appdata", "local", "temp"]:
                return {"risk": "managed_cleanup", "guidance": "This matches the user temporary-data location. Some files may still be in use. Use Windows Temporary files cleanup for eligible contents; do not delete or relocate the entire Temp folder."}
            return {"risk": "unverified", "guidance": "Application data can contain settings, caches and personal state. Assess this particular item's role instead of treating all application data alike."}
    if "downloads" in parts or "documents" in parts or "desktop" in parts:
        return {"risk": "review", "guidance": "Personal files require your review and a backup check before removal."}
    if ntpath.splitext(normalized)[1] == ".log":
        return {"risk": "unknown", "guidance": "The .log extension suggests a log, but does not establish its owner, retention needs or whether it is in use. Which application created it, and do you need its diagnostic history? Its contents have not been read."}
    return {"risk": "unknown", "guidance": "The scan alone cannot establish that deleting or moving this path is safe."}


class ScanEvidence:
    """Bounded retrieval so a local model never receives the entire drive tree."""

    def __init__(self, result: ScanResult) -> None:
        self.result = result
        self._folders: dict[str, list[int]] | None = None
        self._largest_cache: dict[tuple, tuple[int, ...]] = {}
        self._name_cache: dict[tuple, tuple[int, ...]] = {}
        self._summary: dict | None = None
        self._role_cache: dict[str, tuple[float, dict]] = {}
        self._applications: tuple[float, list[dict]] | None = None

    def _installed_applications(self) -> list[dict]:
        """Cache local install records; an absent record never proves an app unused."""
        now = time.monotonic()
        if self._applications and now - self._applications[0] < 60:
            return self._applications[1]
        apps = []
        try:
            import winreg
            deadline = now + 0.04
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | view) as root:
                            for index in range(winreg.QueryInfoKey(root)[0]):
                                if time.monotonic() >= deadline:
                                    break
                                try:
                                    with winreg.OpenKey(root, winreg.EnumKey(root, index)) as key:
                                        name = winreg.QueryValueEx(key, "DisplayName")[0]
                                        location = winreg.QueryValueEx(key, "InstallLocation")[0]
                                        if isinstance(name, str) and isinstance(location, str) and ntpath.isabs(location) and ntpath.dirname(ntpath.normpath(location)) != ntpath.normpath(location):
                                            norm_loc = ntpath.normpath(location)
                                            if os.path.exists(norm_loc):
                                                apps.append({"name": name[:100], "location": norm_loc})
                                except OSError:
                                    continue
                    except OSError:
                        continue
        except ImportError:
            pass
        # ponytail: a 40 ms inventory budget can yield partial records. Missing
        # matches stay unknown; use on-demand app-specific lookup if needed later.
        self._applications = (now, apps)
        return apps

    def _read_excerpt(self, record: FileRecord, limit: int = 2048) -> dict:
        """Read only a small selected local file; never execute it or follow links."""
        path = self.result.path_for(record.frn)
        from .local_database import DriveIndex
        if not path or not DriveIndex._safe_for_content_index(path,ntpath.splitext(path)[1].casefold(),bool(record.attributes & (0x400|0x1000|0x400000))):
            return {"status":"not_read","reason":"Content policy excludes this item."}
        if not path or not record.size_ready or record.size_error or record.logical_size > limit:
            return {"status": "not_read", "reason": "Only small measured files are eligible."}
        current = record.frn
        seen = set()
        while True:
            if current in seen or current not in self.result.records:
                return {"status": "not_read", "reason": "Unresolved ancestry."}
            seen.add(current)
            ancestor = self.result.records[current]
            if ancestor.attributes & (0x400 | 0x1000 | 0x400000):
                return {"status": "not_read", "reason": "Reparse point."}
            if current == self.result.root_frn:
                break
            current = ancestor.parent_frn
        try:
            current_path = path
            while ntpath.dirname(current_path) != current_path:
                info = os.lstat(current_path)
                if getattr(info, "st_file_attributes", 0) & 0x400:
                    return {"status": "not_read", "reason": "Current path contains a reparse point."}
                current_path = ntpath.dirname(current_path)
            with open(path, "rb") as stream:
                if os.name == "nt":
                    import ctypes
                    import msvcrt
                    resolved = ctypes.create_unicode_buffer(32768)
                    function = ctypes.windll.kernel32.GetFinalPathNameByHandleW
                    function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
                    length = function(msvcrt.get_osfhandle(stream.fileno()), resolved, len(resolved), 0)
                    final_path = resolved.value.removeprefix("\\\\?\\")
                    if not length or length >= len(resolved) or ntpath.normcase(final_path) != ntpath.normcase(ntpath.normpath(path)):
                        return {"status": "not_read", "reason": "Opened path did not match the scanned target."}
                data = stream.read(limit + 1)
            if len(data) > limit:
                return {"status": "not_read", "reason": "File exceeds the excerpt limit."}
            if b"\x00" in data:
                return {"status": "binary", "bytes_read": len(data)}
            text = data.decode("utf-8-sig")
            if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
                return {"status": "binary", "bytes_read": len(data)}
            return {"status": "text", "bytes_read": len(data), "text": text}
        except (OSError, UnicodeError, KeyError):
            return {"status": "unavailable", "reason": "Contents could not be inspected."}

    def tree_summary(self, path: str, *, max_records=20000, seconds=0.1) -> dict:
        """Summarise retained MFT evidence without opening files or following links."""
        entry = self.inspect(path)
        if 'error' in entry:
            return entry
        result = self.result
        key = (entry['frn'], result.sizes_complete, result.sized_files)
        cache = getattr(self, '_tree_cache', {})
        default_limits = max_records == 20000 and seconds == 0.1
        if default_limits and key in cache:
            return cache[key]
        root = result.records[entry['frn']]
        pending = deque([root.frn])
        seen = set()
        extensions, flags = Counter(), Counter()
        examples, folders = {}, []
        files = directories = unknown_sizes = gaps = links = 0
        truncated = False
        deadline = time.monotonic() + seconds
        # ponytail: bounded metadata work per item; large trees explicitly stay partial.
        # A background aggregate can replace this walk if measured scans need it.
        while pending and len(seen) < max_records and time.monotonic() < deadline:
            frn = pending.popleft()
            if frn in seen:
                gaps += 1
                continue
            seen.add(frn)
            record = result.records.get(frn)
            if record is None:
                gaps += 1
                continue
            for mask, label in ((1,'read_only_attribute'),(2,'hidden'),(4,'system'),
                                (0x200,'sparse'),(0x400,'reparse_point'),(0x800,'compressed'),
                                (0x1000,'offline'),(0x4000,'encrypted')):
                if record.attributes & mask:
                    flags[label] += 1
            links += int(bool(record.attributes & 0x400))
            if record.is_directory:
                if frn != root.frn:
                    directories += 1
                    if len(folders) < 6:
                        folders.append(frn)
                if not record.attributes & 0x400:
                    children = result.children.get(frn, ())
                    room = max(0, max_records - len(seen) - len(pending))
                    truncated |= len(children) > room
                    pending.extend(children[:room])
            else:
                files += 1
                extension = ntpath.splitext(record.name)[1].casefold() or '(no extension)'
                extensions[extension] += 1
                examples.setdefault(extension, frn)
                unknown_sizes += int(not record.size_ready or bool(record.size_error))
        partial = bool(pending or gaps or links or truncated)
        types = []
        for extension, count in extensions.most_common(6):
            sample = result.records[examples[extension]]
            types.append({'extension':extension,'files_observed':count,
                          'example_path':result.path_for(sample.frn),
                          'example_space':format_bytes(sample.allocated_size) if sample.size_ready and not sample.size_error else 'pending'})
        summary = {'path':entry['path'], 'source':'Retained MFT scan metadata; file contents were not read by this inspection.',
                   'partial':partial,'records_checked':len(seen),'files_observed':files,
                   'subfolders_observed':directories,'file_types':types,
                   'subfolder_examples':[result.path_for(frn) for frn in folders],
                   'attributes_observed':dict(flags),'files_with_unknown_size':unknown_sizes,
                   'unfollowed_links':links,'missing_or_repeated_records':gaps,
                   'coverage':'Bounded tree inspection is incomplete.' if partial else 'All reachable retained records in this item were inspected.',
                   'limits':'No active-use, backup, runtime dependency, access-permission or content verification.'}
        if root.is_directory:
            counts = result.stats.get(root.frn)
            if counts is not None:
                summary['scanned_totals'] = {'files':counts[1],'subfolders':counts[2]}
        else:
            try:
                summary['modified_date'] = datetime.fromtimestamp(root.modified,timezone.utc).date().isoformat() if root.modified else 'unknown'
            except (ValueError, OverflowError, OSError):
                summary['modified_date'] = 'unknown'
        if default_limits:
            if len(cache) >= 32:
                cache.clear()
            cache[key] = summary
            self._tree_cache = cache
        return summary

    def role_evidence(self, path: str) -> dict:
        """Bounded evidence about this item, not a declaration that it is safe."""
        key = ntpath.normcase(path)
        now = time.monotonic()
        if key in self._role_cache and now - self._role_cache[key][0] < 30:
            return self._role_cache[key][1]
        entry = self.inspect(path)
        if "error" in entry:
            return entry
        record = self.result.records[entry["frn"]]
        if record.attributes & 0x400:
            return {"path": entry["path"], "role": "Unresolved", "contents_read": False,
                    "coverage": "Reparse point contents were not inspected.",
                    "dependencies_verified": False, "owner_verified": False}
        children = self.result.children.get(record.frn, ())
        names = {self.result.records[frn].name.casefold(): frn for frn in children[:128]}
        observed = [self.result.records[frn].name for frn in children[:12]]
        evidence = {"path": entry["path"], "observed_children": observed,
                    "coverage": f"Sampled {min(len(children), 128)} of {len(children)} direct children.",
                    "role": "Unresolved", "basis": "Scan metadata", "contents_read": False,
                    "dependencies_verified": False, "owner_verified": False}
        matches = [app for app in self._installed_applications() if self.within(path, app["location"])]
        evidence["installation_matches"] = sorted(matches, key=lambda app: len(app["location"]), reverse=True)[:2]
        evidence["inventory_coverage"] = "Partial local installation records; no match does not establish an orphan."
        if record.is_directory:
            if "steam.exe" in names and ("steamapps" in names or "userdata" in names):
                evidence["role"] = "Steam client installation"
                evidence["has_user_data"] = "userdata" in names
                evidence["basis"] = "Observed steam.exe and Steam data directories"
                evidence["action"] = "Do not delete the whole installation to clean individual data. Games or the installation can be relocated through Steam's supported procedure."
                if "steamapps" in names:
                    nested = self.result.children.get(names["steamapps"], ())
                    evidence["steam_data"] = [self.result.records[frn].name for frn in nested[:16]]
                    common = next((frn for frn in nested if self.result.records[frn].name.casefold() == "common"), None)
                    if common is not None:
                        games = self.result.children.get(common, ())
                        evidence["game_folders"] = [self.result.records[frn].name for frn in games[:8]]
                        evidence["game_folder_count"] = len(games)
            elif matches:
                evidence["role"] = f"Inside the registered installation of {evidence['installation_matches'][0]['name']}"
            elif ".git" in names:
                evidence["role"] = "Project containing Git repository metadata"
            for filename in ("package.json", "pyproject.toml", "appmanifest.acf"):
                if filename not in names:
                    continue
                excerpt = self._read_excerpt(self.result.records[names[filename]])
                evidence["manifest"] = {"filename": filename, **excerpt}
                evidence["contents_read"] = excerpt.get("status") in {"text", "binary"}
                if filename == "package.json" and excerpt.get("status") == "text":
                    try:
                        data = json.loads(excerpt["text"])
                        if isinstance(data, dict):
                            evidence["package"] = {field: str(data[field])[:120] for field in ("name", "description") if field in data}
                    except ValueError:
                        pass
                if "text" in evidence["manifest"]:
                    evidence["manifest"]["text"] = evidence["manifest"]["text"][:512]
                break
        elif record.logical_size <= 2048 and not path.casefold().split("\\")[-1].startswith("$"):
            evidence["excerpt"] = self._read_excerpt(record)
            evidence["contents_read"] = evidence["excerpt"].get("status") in {"text", "binary"}
            if "text" in evidence["excerpt"]:
                evidence["excerpt"]["text"] = evidence["excerpt"]["text"][:512]
        evidence["file_type"] = mimetypes.guess_type(path)[0] if not record.is_directory else None
        if len(self._role_cache) >= 32:
            self._role_cache.clear()
        self._role_cache[key] = (now, evidence)
        return evidence

    _IGNORED = frozenset({
        "a", "an", "and", "are", "be", "both", "can", "could", "deleted", "delete", "do", "does", "drive", "file", "files", "folder",
        "folders", "for", "from", "have", "how", "i", "in", "is", "it", "keep", "me", "move", "my", "of", "on",
        "or", "please", "remove", "should", "that", "the", "this", "to", "what", "which", "would", "you",
        "about", "big", "details", "give", "information", "large", "largest", "biggest", "list", "much",
        "overview", "show", "size", "space", "storage", "tell", "use", "used", "uses", "using", "taking",
        "take", "worth", "hey", "hello", "hi", "these", "those", "them", "its", "itself", "safe", "free", "clear",
        "clean", "cleanup", "junk", "inside", "not", "avoid", "never", "shouldn", "t", "explain", "really", "so", "needed", "necessary", "sensitive", "private", "contains", "contain", "why", "where", "there", "here", "c", "d",
        "am", "was", "were", "being", "good", "guy", "looking", "we", "they", "who",
        "need", "needs", "want", "wants", "around", "approx", "approximately", "some", "any", "many", "more", "less",
        "things", "thing", "stuff", "item", "items", "anything", "something", "everything", "nothing",
        "up", "may", "might", "shall", "will", "get", "got",
        "gb", "mb", "kb", "tb", "gig", "gigs", "gigabyte", "gigabytes", "megabyte", "megabytes", "byte", "bytes",
        "safety", "harm", "break", "broken", "work", "fine", "ok", "okay", "sure", "all",
    })

    def _folder_index(self) -> dict[str, list[int]]:
        if self._folders is None:
            self._folders = {}
            for record in self.result.records.values():
                if record.is_directory and record.frn != self.result.root_frn:
                    self._folders.setdefault(_name_key(record.name), []).append(record.frn)
        return self._folders

    def _compact_folder_index(self) -> dict[str, list[int]]:
        """'program data' and 'ProgramData' share one key."""
        cached = getattr(self, "_compact_folders", None)
        if cached is None:
            cached = {}
            for key, frns in self._folder_index().items():
                cached.setdefault(key.replace(" ", ""), []).extend(frns)
            self._compact_folders = cached
        return cached

    def loose_folders(self, name: str, scope: str | None = None, limit: int = 6) -> list[dict]:
        """Folders whose name matches once spaces and punctuation are ignored."""
        compact = "".join(_WORD.findall(name.casefold()))
        if len(compact) < 2 or compact.isdigit():
            return []
        found = []
        for frn in self._compact_folder_index().get(compact, ()):
            path = self.result.path_for(frn)
            if path and frn not in found and (scope is None or self.within(path, scope)):
                p_lower = path.casefold()
                if any(x in p_lower for x in ("\\.git\\", "\\$recycle.bin\\", "\\system volume information\\", "\\.svn\\", "\\.hg\\")):
                    continue
                if len(compact) == 2 and path.count("\\") > 2:
                    continue
                found.append(frn)
        found.sort(key=lambda frn: (self.result.path_for(frn) or "").count("\\"))
        return [self._entry(self.result.records[frn]) for frn in found[:limit]]

    def named_folders(self, question: str, limit: int = 6) -> list[int]:
        """Folders named exactly in the question; longest names win over their sub-phrases."""
        words = _WORD.findall(question.casefold())
        index = self._folder_index()
        found: list[int] = []
        allow_internal = bool(re.search(r"\b(?:git|\.git|recycle|recycle\.bin)\b", question, re.I))
        quoted = [value for groups in re.findall(r'"([^"\n]+)"|`([^`\n]+)`|(?<!\w)\x27([^\x27\n]+)\x27', question) for value in groups if value]
        for name in quoted:
            for frn in index.get(_name_key(name), ()):
                if frn not in found and len(found) < limit:
                    found.append(frn)
        used: set[int] = set()
        for width in range(min(5, len(words)), 0, -1):
            for start in range(len(words) - width + 1):
                span = range(start, start + width)
                if len(found) >= limit:
                    return found
                if used.intersection(span):
                    continue
                phrase_words = words[start:start + width]
                if all(w in self._IGNORED or w.isdigit() for w in phrase_words):
                    continue
                phrase = " ".join(phrase_words).rstrip(".")
                if phrase.isdigit():
                    continue
                if width == 1 and (phrase in self._IGNORED or len(phrase) <= 2 and phrase not in {"hp", "os", "ui", "db", "ai"}):
                    continue
                matches = [frn for frn in index.get(phrase, ()) if frn not in found]
                valid_matches = []
                for frn in matches:
                    p = self.result.path_for(frn)
                    if p:
                        p_lower = p.casefold()
                        if not allow_internal:
                            if any(x in p_lower for x in ("\\.git\\", "\\$recycle.bin\\", "\\system volume information\\", "\\.svn\\", "\\.hg\\")):
                                continue
                        valid_matches.append(frn)
                if valid_matches:
                    used.update(span)
                    found.extend(valid_matches[: limit - len(found)])
        return found

    def mentioned(self, question: str, limit: int = 6) -> dict:
        """Resolve names such as Git before asking the model to interpret them."""
        words = _WORD.findall(question.casefold())
        quoted = [value.casefold() for groups in re.findall(r'"([^"\n]+)"|`([^`\n]+)`|(?<!\w)\x27([^\x27\n]+)\x27', question) for value in groups if value]
        found = self.named_folders(question, limit)
        exact_match = bool(found)
        ignored = self._IGNORED
        short_ok = {"hp", "git"}
        terms = list(dict.fromkeys(quoted + [
            word.rstrip(".") for word in words
            if word not in ignored and (len(word.rstrip(".")) >= 3 or word.rstrip(".") in short_ok)
        ]))[:4]
        if not found and terms:
            exact: list[int] = []
            partial: list[int] = []
            for record in self.result.records.values():
                name = record.name.casefold()
                if name in terms:
                    exact.append(record.frn)
                elif len(partial) < limit and any(term in name for term in terms):
                    partial.append(record.frn)
            found = exact[:limit] if exact else partial[:limit]
            exact_match = bool(exact)
        match_type = "exact" if exact_match else ("partial" if found else "none")
        return {"items": [self._entry(self.result.records[frn]) for frn in found], "searched_terms": terms,
                "exact": exact_match, "match_type": match_type}

    def _allocated(self, record: FileRecord) -> int:
        if record.is_directory:
            return self.result.directory_sizes.get(record.frn, (0, 0))[1]
        return record.allocated_size

    def largest_children(self, frn: int, limit: int = 8) -> list[str]:
        """'path - size' lines for a folder's biggest direct children."""
        result = self.result
        children = [result.records[c] for c in result.children.get(frn, ()) if c in result.records]
        biggest = heapq.nlargest(limit, children, key=self._allocated)
        return [
            f"{result.path_for(child.frn)} ({'folder' if child.is_directory else 'file'}) - {format_bytes(self._allocated(child))}"
            for child in biggest
        ]

    def drive_summary(self) -> dict:
        """Compact whole-drive picture sent with every model request."""
        if self._summary is None:
            result = self.result
            items, files, folders = result.root_stats
            logical, allocated = result.directory_sizes.get(result.root_frn, (0, 0))
            summary = {
                "drive": result.volume.display_name,
                "used": format_bytes(allocated) if result.sizes_complete else "pending",
                "files": files,
                "folders": folders,
                "top_level_by_size": self.largest_children(result.root_frn, 12),
            }
            if result.sizes_complete:
                summary["largest_files"] = [
                    f"{entry['path']} - {entry['allocated_display']}"
                    for entry in self.largest("files", 10)["items"]
                ]
                deep = heapq.nlargest(
                    40,
                    (r for r in result.records.values() if r.is_directory and r.frn != result.root_frn),
                    key=self._allocated,
                )
                leaves = [r for r in deep if not any(
                    other is not r and self._is_ancestor(r.frn, other.frn) for other in deep
                )]
                summary["biggest_leaf_folders"] = [
                    f"{result.path_for(r.frn)} - {format_bytes(self._allocated(r))}" for r in leaves[:10]
                ]
            self._summary = summary
        return self._summary

    def _is_ancestor(self, ancestor: int, frn: int) -> bool:
        records = self.result.records
        current = records[frn].parent_frn
        for _ in range(64):
            if current == ancestor:
                return True
            if current == self.result.root_frn or current not in records:
                return False
            current = records[current].parent_frn
        return False

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
            "frn": record.frn,
            "path": path,
            "kind": "folder" if record.is_directory else "file",
            "logical_bytes": logical if ready else None,
            "allocated_bytes": allocated if ready else None,
            "logical_display": format_bytes(logical) if ready else "pending",
            "allocated_display": format_bytes(allocated) if ready else "pending",
            "size_error": record.size_error,
            "cleanup_risk": assess_path(path or ""),
            "role_basis": "Path/attribute heuristic",
            "owner_verified": False,
            "dependencies_verified": False,
            "is_reparse_point": bool(record.attributes & 0x400),
            "child_names": [result.records[frn].name for frn in result.children.get(record.frn, ())[:6]] if record.is_directory else [],
            "child_count": len(result.children.get(record.frn, ())) if record.is_directory else 0,
        }

    def largest(self, kind: str = "folders", limit: int = 8, *, scope: str | None = None, protected_only: bool = False) -> dict:
        result = self.result
        limit = max(1, min(int(limit), 20))
        scope = scope or result.volume.drive + "\\"
        parent = self.inspect(scope)
        if "error" in parent:
            return parent
        if not result.sizes_complete:
            return {"status": "sizes_pending", "items": []}
        def candidates():
            pending = list(result.children.get(parent["frn"], ())) if not parent.get("is_reparse_point") else []
            seen = {parent["frn"]}
            while pending:
                frn = pending.pop()
                if frn in seen:
                    continue
                seen.add(frn)
                record = result.records[frn]
                # Do not rank or traverse junctions as though they were ordinary folders.
                if record.attributes & 0x400:
                    continue
                matches = kind == "items" or record.is_directory == (kind != "files")
                if matches and (record.is_directory or (record.size_ready and not record.size_error)):
                    if not protected_only or assess_path(result.path_for(frn) or "")["risk"] == "protected":
                        yield record
                if record.is_directory:
                    pending.extend(result.children.get(frn, ()))
        # ponytail: first scoped rank walks its subtree; keep 32 ranks per completed scan.
        # If real scans make this too slow, precompute ranks during scan completion.
        cache_key = (kind, limit, ntpath.normcase(scope), protected_only)
        if cache_key not in self._largest_cache:
            if len(self._largest_cache) >= 32:
                self._largest_cache.clear()
            self._largest_cache[cache_key] = tuple(record.frn for record in heapq.nlargest(
                limit, candidates(), key=self._allocated,
            ))
        return {"status": "complete", "scope": scope, "ranking": "allocated_bytes",
                "items": [self._entry(result.records[frn]) for frn in self._largest_cache[cache_key]]}

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
        if requested == ntpath.normcase(result.volume.drive):
            requested += "\\"
        if not requested.startswith(ntpath.normcase(result.volume.drive + "\\")):
            return {"error": "Path is outside the current scanned drive."}
        if requested == ntpath.normcase(result.volume.drive + "\\"):
            logical, allocated = result.directory_sizes.get(result.root_frn, (0, 0))
            return {"frn": result.root_frn, "path": result.volume.drive + "\\", "kind": "drive", "logical_bytes": logical if result.sizes_complete else None, "allocated_bytes": allocated if result.sizes_complete else None, "logical_display": format_bytes(logical) if result.sizes_complete else "pending", "allocated_display": format_bytes(allocated) if result.sizes_complete else "pending", "cleanup_risk": assess_path(path)}
        current = result.root_frn
        for part in requested[len(result.volume.drive) + 1:].split("\\"):
            current = next(
                (frn for frn in result.children.get(current, ())
                 if frn in result.records and ntpath.normcase(result.records[frn].name) == part),
                None,
            )
            if current is None:
                break
        if current is not None:
            return self._entry(result.records[current])
        name = ntpath.basename(requested)
        for record in result.records.values():
            if record.name.casefold() == name.casefold():
                actual = result.path_for(record.frn)
                if actual and ntpath.normcase(actual) == requested:
                    return self._entry(record)
        return {"error": "Path was not found in the current scan.", "path": path}

    def scoped_items(self, path: str, limit: int = 8) -> dict:
        """Direct children only: do not mistake a tiny nested component for a candidate."""
        parent = self.inspect(path)
        if "error" in parent:
            return parent
        records = [self.result.records[frn] for frn in self.result.children.get(parent["frn"], ())]
        biggest = heapq.nlargest(limit, records, key=lambda record: self.result.directory_sizes.get(record.frn, (0, 0))[1] if record.is_directory else record.allocated_size)
        return {"items": [self._entry(record) for record in biggest], "total_children": len(records)}

    def cleanup_candidates(self, path: str, limit: int = 6) -> dict:
        """Rank conditional candidates in the requested subtree, not the whole drive."""
        parent = self.inspect(path)
        if "error" in parent:
            return parent
        if not self.result.sizes_complete:
            return {"status": "sizes_pending", "items": []}
        def candidates():
            pending = [(parent["frn"], parent["path"])]
            while pending:
                frn, current_path = pending.pop()
                record = self.result.records[frn]
                risk = assess_path(current_path)["risk"]
                if record.attributes & 0x400 or (record.attributes & 0x4 and not record.is_directory):
                    continue
                if frn != parent["frn"] and risk in {"managed_cleanup", "review"}:
                    if not record.is_directory or risk == "managed_cleanup":
                        entry = self._entry(record)
                        if entry["allocated_bytes"]:
                            yield entry
                        continue
                if record.is_directory:
                    parts = ntpath.normcase(current_path).split("\\")
                    if ".git" in parts or (len(parts) == 2 and parts[1] in {"windows", "program files", "program files (x86)", "programdata", "recovery", "system volume information"}):
                        continue
                    if "appdata" in parts and "programs" in parts:
                        continue
                    if "appdata" in parts and risk in {"protected", "unverified"}:
                        index = parts.index("appdata")
                        suffix = parts[index + 1:]
                        if suffix and suffix not in (["local"], ["local", "pip"]):
                            continue
                    pending.extend((child, ntpath.join(current_path, self.result.records[child].name)) for child in self.result.children.get(frn, ()))
        ranked = heapq.nlargest(limit, candidates(), key=lambda item: item["allocated_bytes"])
        return {"status": "complete", "items": ranked, "scope": parent["path"], "size_errors": self.result.size_errors}

    @staticmethod
    def within(path: str, scope: str) -> bool:
        path = ntpath.normcase(ntpath.normpath(path))
        scope = ntpath.normcase(ntpath.normpath(scope)).rstrip("\\")
        return path == scope or path.startswith(scope + "\\")

    def resolve_name(self, name: str, scope: str | None = None, limit: int = 7) -> list[dict]:
        """Exact names only; a partial match is a clarification option, not a target."""
        key = (name.casefold(), ntpath.normcase(scope or ""), limit)
        if key not in self._name_cache:
            matches = []
            for record in self.result.records.values():
                if record.name.casefold() != key[0] or record.frn == self.result.root_frn:
                    continue
                path = self.result.path_for(record.frn)
                if path and (scope is None or self.within(path, scope)):
                    matches.append(record.frn)
                    if len(matches) >= limit:
                        break
            # ponytail: exact-name lookup is one metadata pass on a cache miss.
            # Cache 32 queries rather than building a second index for every file.
            if len(self._name_cache) >= 32:
                self._name_cache.clear()
            self._name_cache[key] = tuple(matches)
        return [self._entry(self.result.records[frn]) for frn in self._name_cache[key]]

    def folder_summary(self, path: str) -> dict:
        parent = self.inspect(path)
        if "error" in parent:
            return parent
        if parent.get("is_reparse_point"):
            return {"path": path, "coverage": "Reparse point; linked contents were not inspected."}
        children = [self.result.records[frn] for frn in self.result.children.get(parent["frn"], ())]
        types: dict[str, dict] = {}
        files = [record for record in children if not record.is_directory]
        for record in files:
            extension = ntpath.splitext(record.name)[1].casefold() or "(no extension)"
            group = types.setdefault(extension, {"extension": extension, "files": 0, "measured_files": 0, "allocated_bytes": 0})
            group["files"] += 1
            if record.size_ready and not record.size_error:
                group["measured_files"] += 1
                group["allocated_bytes"] += record.allocated_size
        groups = heapq.nlargest(4, types.values(), key=lambda item: item["allocated_bytes"])
        for group in groups:
            group["allocated_display"] = format_bytes(group.pop("allocated_bytes")) if self.result.sizes_complete else "pending"
        return {
            "path": path, "direct_files": len(files), "direct_folders": len(children) - len(files),
            "subtree_counts": dict(zip(("items", "files", "folders"), self.result.stats.get(parent["frn"], (0, 0, 0)))),
            "largest_direct_children": [self._entry(child) for child in heapq.nlargest(6, children, key=self._allocated)],
            "direct_file_types": groups,
            "coverage": "Counts cover indexed direct children; largest-child and file-type examples are partial. No contents were read.",
        }
