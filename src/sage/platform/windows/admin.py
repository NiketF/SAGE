"""Administrator detection and UAC relaunch."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path
from ctypes import wintypes


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.IsUserAnAdmin.argtypes = []
        shell32.IsUserAnAdmin.restype = wintypes.BOOL
        return bool(shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin() -> bool:
    """Request UAC elevation and return whether a process was launched."""
    if os.name != "nt":
        raise RuntimeError("SAGE requires Windows.")

    if getattr(sys, "frozen", False):
        executable = sys.executable
        parameters = subprocess.list2cmdline(sys.argv[1:])
    else:
        executable = sys.executable
        entrypoint = Path(sys.argv[0]).resolve()
        if entrypoint.suffix.lower() == ".py" and entrypoint.name != "__main__.py":
            arguments = [str(entrypoint), *sys.argv[1:]]
        else:
            arguments = ["-m", "sage", *sys.argv[1:]]
        parameters = subprocess.list2cmdline(arguments)

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE
    project_directory = str(Path(__file__).resolve().parents[4])
    result = shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        parameters,
        project_directory,
        1,
    )
    result_value = result if isinstance(result, int) else int(result or 0)
    return result_value > 32
