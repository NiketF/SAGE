"""Public scan orchestration for the fast MFT phase and size phase."""

from __future__ import annotations

from collections.abc import Callable

from ..models import ScanResult
from ..platform.windows.drives import get_volume_info
from ..platform.windows.mft import enumerate_mft
from ..platform.windows.sizes import collect_file_sizes
from .hierarchy import build_scan_result, calculate_directory_sizes


def scan_ntfs(
    drive: str,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ScanResult:
    """Complete only the fast MFT hierarchy phase."""
    volume = get_volume_info(drive)
    if volume.filesystem.upper() != "NTFS":
        raise ValueError(f"{volume.drive} is {volume.filesystem}, not NTFS.")
    records, skipped = enumerate_mft(
        volume.drive,
        progress=progress,
        cancelled=cancelled,
    )
    stage = getattr(progress, "on_stage", None)
    if stage:
        stage("Building MFT hierarchy")
    return build_scan_result(volume, records, skipped)


def analyze_sizes(
    result: ScanResult,
    progress: Callable[[int, int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Run the deliberately separate, cancellable slow phase."""
    collect_file_sizes(result, progress, cancelled)
    if cancelled and cancelled():
        raise InterruptedError("Size analysis cancelled.")
    calculate_directory_sizes(result)
