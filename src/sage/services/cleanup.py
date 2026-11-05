"""Find specific, regenerable space hogs in a finished scan.

Only paths matching these rules are offered for deletion. Everything else stays
a review item, so a model reply can never turn an arbitrary path into a delete
button.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import ScanResult
from .evidence import format_bytes
import ntpath

MIN_SUGGESTION_BYTES = 20 * 1024 * 1024
MAX_SUGGESTIONS = 12
_USER = r"users\\[^\\]+\\"


@dataclass(frozen=True, slots=True)
class CleanupSuggestion:
    path: str
    title: str
    reason: str
    allocated: int
    contents_only: bool = True
    action: str = "delete"


@dataclass(frozen=True, slots=True)
class _Rule:
    names: frozenset[str]
    pattern: re.Pattern[str]
    title: str
    reason: str
    contents_only: bool = True
    action: str = "delete"
    directory: bool = True
    suffixes: tuple[str, ...] = ()
    sibling: str = ""


def _rule(names: str, pattern: str, title: str, reason: str, **options) -> _Rule:
    return _Rule(frozenset(names.split("|")), re.compile(pattern), title, reason, **options)


# Paths are matched relative to the drive root, lowercase, without a leading backslash.
_RULES = (
    _rule("$recycle.bin", r"^\$recycle\.bin$", "Recycle Bin",
          "Items in the Recycle Bin. Emptying it removes their recovery option; measured allocation is not a guaranteed space saving.", action="empty_recycle_bin"),
    _rule("windows.old|$windows.~bt|$windows.~ws", r"^(windows\.old|\$windows\.~bt|\$windows\.~ws)$",
          "Previous Windows installation / upgrade files",
          "Left over from a Windows upgrade. After deleting, you can no longer roll back that upgrade.",
          contents_only=False),
    _rule("download", r"^windows\\softwaredistribution\\download$", "Windows Update download cache",
          "This matches the conventional Windows Update download location. Installation and update activity were not checked; review Windows Temporary files cleanup first."),
    _rule("deliveryoptimization", r"^windows\\softwaredistribution\\deliveryoptimization$",
          "Delivery Optimization cache", "This matches a Delivery Optimization cache location. Review eligible contents through Windows Storage cleanup."),
    _rule("cache", r"^windows\\serviceprofiles\\networkservice\\appdata\\local\\microsoft\\windows\\deliveryoptimization\\cache$",
          "Delivery Optimization cache", "This matches a Delivery Optimization cache location. Review eligible contents through Windows Storage cleanup."),
    _rule("temp", r"^windows\\temp$", "Windows temporary files",
          "Leftovers from installers and system tasks. Files still in use are skipped."),
    _rule("temp", _USER + r"appdata\\local\\temp$", "Your temporary files",
          "Leftovers from apps and installers. Close apps first; files still in use are skipped."),
    _rule("minidump|livekernelreports", r"^windows\\(minidump|livekernelreports)$", "Crash dump files",
          "Diagnostic dumps from past crashes. Only useful if you are debugging a crash."),
    _rule("reportarchive|reportqueue", r"^programdata\\microsoft\\windows\\wer\\(reportarchive|reportqueue)$",
          "Windows error reports", "Archived crash reports. Windows does not need them to run."),
    _rule("crashdumps", _USER + r"appdata\\local\\crashdumps$", "App crash dumps",
          "Dumps written when apps crashed. Only useful for debugging."),
    _rule("memory.dmp", r"^windows\\memory\.dmp$", "System crash dump",
          "Full memory dump from a past blue screen. Only useful for debugging.", directory=False),
    _rule("cache|code cache|gpucache|cachestorage",
          _USER + r"appdata\\local\\.+\\user data\\[^\\]+\\(cache|code cache|gpucache|service worker\\cachestorage)$",
          "Browser cache", "This matches a browser cache location. Prefer browser cleanup; clearing it can require downloads and affect offline content. Close the browser first."),
    _rule("cache2", _USER + r"appdata\\local\\mozilla\\firefox\\profiles\\[^\\]+\\cache2$", "Firefox cache",
          "This matches a Firefox cache location. Review it through Firefox cleanup and close Firefox before removing files."),
    _rule("cache|code cache|gpucache|cacheddata|cachestorage",
          _USER + r"appdata\\roaming\\[^\\]+\\(cache|code cache|gpucache|cacheddata|service worker\\cachestorage)$",
          "App cache (Discord, Teams, VS Code, Slack, …)", "This matches a conventional app cache location. Confirm the owning app and that its contents can be regenerated; close the app before cleanup."),
    _rule("inetcache", _USER + r"appdata\\local\\microsoft\\windows\\inetcache$", "Internet cache",
          "Cached web content used by Windows components. Rebuilt automatically."),
    _rule("dxcache|glcache|dxccache|d3dscache",
          _USER + r"appdata\\local\\(nvidia\\(dxcache|glcache)|amd\\(dxcache|glcache|dxccache)|d3dscache)$",
          "GPU shader cache", "Compiled shaders. Games rebuild them; the first launch afterward may stutter."),
    _rule("cache", _USER + r"appdata\\local\\pip\\cache$", "pip download cache",
          "This matches a conventional pip cache location, not proof that packages are installed or remain downloadable. Review with pip cache tools."),
    _rule("npm-cache", _USER + r"appdata\\(local|roaming)\\npm-cache$", "npm cache",
          "This matches an npm cache location. Confirm package sources remain available and review with npm cache tools."),
    _rule("cache", _USER + r"appdata\\local\\yarn\\cache$", "Yarn cache",
          "This matches a Yarn cache location. Confirm package sources remain available before clearing the cache."),
    _rule("v3-cache", _USER + r"appdata\\local\\nuget\\v3-cache$", "NuGet HTTP cache",
          "Downloaded NuGet metadata and packages. Rebuilt on the next restore."),
    _rule("caches", _USER + r"\.gradle\\caches$", "Gradle cache",
          "This matches a Gradle cache location. Later builds may need downloads or locally generated dependencies; check before removal."),
    _rule("go-build", _USER + r"appdata\\local\\go-build$", "Go build cache",
          "Compiled Go build output. Rebuilt on the next build."),
    # Installed apps (Electron resources, extensions, global npm) ship node_modules too and cannot
    # be restored with npm install, so only a top-level node_modules beside package.json qualifies.
    _rule("node_modules",
          r"^(?!(?:.*\\)?(?:program files[^\\]*|programdata|windows|appdata|resources|\.[^\\]+)\\)"
          r"(?!.*node_modules\\)(?:.*\\)?node_modules$",
          "node_modules (project dependencies)",
          "A node_modules folder beside package.json. Preserve source and local changes; verify dependencies and any private packages remain available before removal.",
          contents_only=False, sibling="package.json"),
    _rule("", _USER + r"downloads\\[^\\]+$", "Executable or disk image in Downloads",
          "An executable, installer-format file or disk image in Downloads. Its purpose and another available copy were not verified; keep it if it is your only copy.",
          contents_only=False, directory=False, suffixes=(".exe", ".msi", ".msix", ".iso")),
)


def _has_sibling(result: ScanResult, parent_frn: int, name: str) -> bool:
    records = result.records
    return any(
        child in records and records[child].name.casefold() == name
        for child in result.children.get(parent_frn, ())
    )


def find_cleanup_suggestions(
    result: ScanResult, limit: int = MAX_SUGGESTIONS, minimum: int = MIN_SUGGESTION_BYTES,
    *, scope: str | None = None
) -> list[CleanupSuggestion]:
    """Return the largest rule-matched items, without nesting one inside another."""
    if not result.sizes_complete:
        return []
    prefix = len(result.volume.drive) + 1
    found: list[CleanupSuggestion] = []
    # Only reject impossible names here; the original ordered rules decide eligibility.
    candidate_names = {name for rule in _RULES for name in rule.names}
    candidate_suffixes = tuple(suffix for rule in _RULES for suffix in rule.suffixes)
    for record in result.records.values():
        if record.frn == result.root_frn:
            continue
        name = record.name.casefold()
        if name not in candidate_names and not name.endswith(candidate_suffixes):
            continue
        relative: str | None = None
        for rule in _RULES:
            if rule.directory != record.is_directory:
                continue
            if name not in rule.names and not (rule.suffixes and name.endswith(rule.suffixes)):
                continue
            allocated = (
                result.directory_sizes.get(record.frn, (0, 0))[1]
                if record.is_directory else record.allocated_size
            )
            if allocated < minimum:
                break
            if relative is None:
                path = result.path_for(record.frn)
                if path is None:
                    break
                if scope:
                    key = ntpath.normcase(ntpath.normpath(path))
                    scope_key = ntpath.normcase(ntpath.normpath(scope)).rstrip("\\")
                    if key != scope_key and not key.startswith(scope_key + "\\"):
                        break
                relative = path[prefix:].casefold()
            if rule.pattern.search(relative):
                if rule.sibling and not _has_sibling(result, record.parent_frn, rule.sibling):
                    break
                found.append(CleanupSuggestion(
                    path, rule.title, rule.reason, allocated, rule.contents_only, rule.action
                ))
                break
    found.sort(key=lambda suggestion: suggestion.allocated, reverse=True)
    chosen: list[CleanupSuggestion] = []
    for suggestion in found:
        key = suggestion.path.casefold() + "\\"
        if any(key.startswith(other) or other.startswith(key) for other in (c.path.casefold() + "\\" for c in chosen)):
            continue
        chosen.append(suggestion)
        if len(chosen) >= limit:
            break
    return chosen


def cleanup_report(result: ScanResult, suggestions: list[CleanupSuggestion]) -> str:
    drive = result.volume.drive
    if not result.sizes_complete:
        return "Sizes are still being measured. I'll list exactly what you can delete once they finish."
    if not suggestions:
        return (
            f"No matching cleanup candidates over {format_bytes(MIN_SUGGESTION_BYTES)} "
            f"were listed for {drive}. This does not establish that nothing else can be removed. "
            "Ask about a folder to review its contents and application removal options."
        )
    total = sum(suggestion.allocated for suggestion in suggestions)
    return (
        f"**Review these {len(suggestions)} cleanup candidates on {drive}** "
        f"({format_bytes(total)} of attributed allocation), largest first. "
        "These match known location or file-type rules; their current use, recoverability "
        "and replacement copies were not verified. Actual space recovered may differ. "
        "Review each condition before deletion. Deletion is **permanent** "
        "(it skips the Recycle Bin), and files in use are skipped."
    )
