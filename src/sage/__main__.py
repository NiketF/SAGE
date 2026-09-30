"""SAGE application entry point."""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from .platform.windows.admin import is_admin, relaunch_as_admin

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STARTUP_LOG = PROJECT_ROOT / "sage-startup.log"


def _log(message: str) -> None:
    try:
        with STARTUP_LOG.open("a", encoding="utf-8") as stream:
            timestamp = datetime.now().isoformat(timespec="seconds")
            stream.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


def _show_fatal_error(message: str) -> None:
    _log(message)
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "SAGE startup error",
            0x10,
        )
    except Exception:
        pass


def main() -> int:
    try:
        _log(
            f"start pid={os.getpid()} cwd={os.getcwd()!r} "
            f"executable={sys.executable!r} argv={sys.argv!r} admin={is_admin()}"
        )
        if os.name != "nt":
            raise RuntimeError("SAGE is a Windows-only NTFS application.")
        if not is_admin():
            _log("requesting UAC elevation")
            if relaunch_as_admin():
                _log("elevated child launched; ending unelevated parent")
                return 0
            raise RuntimeError("Administrator access is required to read NTFS MFT data.")

        _log("elevated process importing UI")
        from .ui.application import SageApplication

        app = SageApplication()
        _log("UI created; entering main loop")
        app.mainloop()
        _log("UI main loop ended")
        return 0
    except Exception:
        details = traceback.format_exc()
        _show_fatal_error(details)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
