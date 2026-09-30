"""Windows Shell file operations used by the tree context menu."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

FO_MOVE = 0x0001
FO_COPY = 0x0002
FO_DELETE = 0x0003
FO_RENAME = 0x0004
FOF_ALLOWUNDO = 0x0040
FOF_WANTNUKEWARNING = 0x4000
COINIT_APARTMENTTHREADED = 0x2


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _multistring(paths: list[str]) -> ctypes.Array:
    if not paths:
        raise ValueError("Choose at least one file or folder.")
    if any(not os.path.isabs(path) for path in paths):
        raise ValueError("File operations require absolute paths.")
    return ctypes.create_unicode_buffer("\0".join(paths) + "\0\0")


def shell_file_operation(
    action: str,
    sources: list[str],
    destination: str | None = None,
    owner_hwnd: int = 0,
) -> bool:
    """Run a Shell copy/move/rename/recycle operation; return False if cancelled."""
    if os.name != "nt":
        raise RuntimeError("Windows Shell file operations require Windows.")
    actions = {"copy": FO_COPY, "move": FO_MOVE, "rename": FO_RENAME, "recycle": FO_DELETE}
    if action not in actions:
        raise ValueError(f"Unsupported file operation: {action}")
    if action in {"copy", "move", "rename"} and not destination:
        raise ValueError("A destination is required for this operation.")

    from_buffer = _multistring(sources)
    to_buffer = _multistring([destination]) if destination else None
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = owner_hwnd
    operation.wFunc = actions[action]
    operation.pFrom = ctypes.cast(from_buffer, wintypes.LPCWSTR)
    operation.pTo = ctypes.cast(to_buffer, wintypes.LPCWSTR) if to_buffer else None
    operation.fFlags = FOF_ALLOWUNDO | (FOF_WANTNUKEWARNING if action == "recycle" else 0)
    operation.lpszProgressTitle = "SAGE file operation"

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    initialized = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED) >= 0
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
        shell32.SHFileOperationW.restype = ctypes.c_int
        code = shell32.SHFileOperationW(ctypes.byref(operation))
        if code:
            raise OSError(code, f"Windows Shell operation failed (code {code}).")
        return not bool(operation.fAnyOperationsAborted)
    finally:
        if initialized:
            ole32.CoUninitialize()

